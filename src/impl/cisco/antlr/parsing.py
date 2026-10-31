"""原文保留的 ANTLR 解析；TokenSource 仅产生结构标记，不建立语法树。"""

import re
from collections import deque
from antlr4 import CommonTokenStream, InputStream, Token
from antlr4.Token import CommonToken
from antlr4.error.ErrorListener import ErrorListener

from .generated.CiscoLexer import CiscoLexer
from .generated.CiscoParser import CiscoParser


class ConfigurationParseError(ValueError):
    pass


class _Errors(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        raise ConfigurationParseError(f"Cisco 配置解析失败，行 {line} 列 {column + 1}: {msg}")


class IndentationTokenSource:
    def __init__(self, lexer):
        self.lexer = lexer
        self.inputStream = lexer.inputStream
        self._factory = lexer._factory
        self.levels = [0]
        self.pending = deque()
        self.group = False
        self.group_base = None

    def getSourceName(self):
        return "Cisco configuration"

    def _virtual(self, kind, anchor):
        token = CommonToken((self.lexer, self.inputStream), kind, start=anchor.start, stop=anchor.start - 1)
        token.text, token.line, token.column = "", anchor.line, anchor.column
        return token

    def _align(self, indent, token):
        while self.levels[-1] > indent:
            self.levels.pop()
            self.pending.append(self._virtual(CiscoLexer.DEDENT, token))
        if self.levels[-1] < indent:
            self.levels.append(indent)
            self.pending.append(self._virtual(CiscoLexer.INDENT, token))

    def _banner(self, token):
        match = re.match(r"\s*banner\s+\S+\s+(\^C|\S)(.*)$", token.text.rstrip("\r\n"), re.I)
        if match is None:
            raise ConfigurationParseError(f"Cisco 配置解析失败，行 {token.line} 列 {token.column + 1}: banner 缺少结束分隔符")
        delimiter, body = match.groups()
        if delimiter in body:
            return token
        while True:
            end = self.lexer.nextToken()
            if end.type == Token.EOF:
                raise ConfigurationParseError(f"Cisco 配置解析失败，行 {token.line} 列 {token.column + 1}: banner 缺少结束分隔符 {delimiter}")
            if delimiter in end.text:
                token.stop = end.stop
                token.text = self.inputStream.getText(token.start, end.stop)
                return token

    def nextToken(self):
        if self.pending:
            return self.pending.popleft()
        token = self.lexer.nextToken()
        if token.type == Token.EOF:
            self._align(0, token)
        elif token.type not in {CiscoLexer.BLANK, CiscoLexer.COMMENT}:
            raw = token.text.expandtabs(8)
            indent = len(raw) - len(raw.lstrip())
            if token.type == CiscoLexer.GROUP and (indent > 0 or self.group):
                token.type = CiscoLexer.LINE
            if token.type == CiscoLexer.END_GROUP:
                self._align(0, token)
                self.group = False
            else:
                if self.group and self.group_base is None and token.type != CiscoLexer.SEPARATOR:
                    self.group_base = indent
                effective = max(1, indent - (self.group_base or 0) + 1) if self.group else indent
                self._align(effective, token)
                if token.type == CiscoLexer.GROUP:
                    self.group = True
                    self.group_base = None
                if token.type == CiscoLexer.BANNER:
                    token = self._banner(token)
        self.pending.append(token)
        return self.pending.popleft()


class CiscoSnapshot:
    def __init__(self, text: str, revision: int = 0):
        self.text, self.revision = text, revision
        lexer = CiscoLexer(InputStream(text))
        lexer.removeErrorListeners()
        lexer.addErrorListener(_Errors())
        self.tokens = CommonTokenStream(IndentationTokenSource(lexer))
        self.tokens.fill()
        parser = CiscoParser(self.tokens)
        parser.removeErrorListeners()
        parser.addErrorListener(_Errors())
        self.root = parser.configuration()
        self.root._snapshot = self
