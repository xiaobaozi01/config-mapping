"""原文保留的 ANTLR 解析；TokenSource 仅产生结构标记，不建立语法树。"""

import re
from collections import deque

from antlr4 import CommonTokenStream, InputStream, Token
from antlr4.error.ErrorListener import ErrorListener
from antlr4.Token import CommonToken

from .generated.CiscoLexer import CiscoLexer
from .generated.CiscoParser import CiscoParser


class ConfigurationParseError(ValueError):
    """表示 Cisco 配置在 token 化或语法构建阶段无法可靠解析。

    解析错误阻止后续改写，避免在不完整结构上执行接口迁移。
    """

    pass


class _Errors(ErrorListener):
    """把 ANTLR 词法和语法错误转换为仓库统一解析异常。

    集中转换可让调用方只处理一种错误类型，同时保留原始行列位置。
    """

    def syntaxError(
        self, _recognizer, _offending_symbol, line, column, msg, _error
    ):
        """抛出带行列信息的 Cisco 配置解析异常。

        不返回错误 token，因为继续使用不完整解析结果可能产生错误配置。
        """
        raise ConfigurationParseError(
            f"Cisco 配置解析失败，行 {line} 列 {column + 1}: {msg}"
        )


class IndentationTokenSource:
    """在词法 token 流中补充 Cisco 缩进和 group 结构标记。

    配置层需要块边界而原始 lexer 只识别文本行，因此该适配器在不构建第二棵语法树
    的前提下生成 INDENT、DEDENT 和分隔 token。
    """

    def __init__(self, lexer):
        """绑定底层 lexer 并初始化缩进层级和待发 token 队列。

        虚拟 token 延迟发送，使当前真实 token 之前先补齐必要的层级变化。
        """
        self.lexer = lexer
        self.inputStream = lexer.inputStream
        self._factory = lexer._factory
        self.levels = [0]
        self.pending = deque()
        self.group = False
        self.group_base = None

    def getSourceName(self):
        """返回供 ANTLR 诊断使用的 token 源名称。

        稳定名称让解析错误能够识别来源，而不暴露临时对象表示。
        """
        return "Cisco configuration"

    def _virtual(self, kind, anchor):
        """基于锚点位置创建一个零长度结构 token。

        虚拟 token 只表达缩进变化，不应影响原始配置文本切片。
        """
        token = CommonToken(
            (self.lexer, self.inputStream),
            kind,
            start=anchor.start,
            stop=anchor.start - 1,
        )
        token.text, token.line, token.column = "", anchor.line, anchor.column
        return token

    def _align(self, indent, token):
        """生成从当前层级调整到目标缩进所需的虚拟 token。

        减少层级时连续发出 DEDENT，增加层级时发出一个 INDENT，使 parser 得到平衡边界。
        """
        while self.levels[-1] > indent:
            self.levels.pop()
            self.pending.append(self._virtual(CiscoLexer.DEDENT, token))
        if self.levels[-1] < indent:
            self.levels.append(indent)
            self.pending.append(self._virtual(CiscoLexer.INDENT, token))

    def _banner(self, token):
        """把多行 banner token 合并为包含完整正文的单一 token。

        banner 内容可包含任意命令文本，先按分隔符整体消费才能防止内部行被误解析。
        """
        match = re.match(
            r"\s*banner\s+\S+\s+(\^C|\S)(.*)$", token.text.rstrip("\r\n"), re.I
        )
        if match is None:
            raise ConfigurationParseError(
                f"Cisco 配置解析失败，行 {token.line} 列 "
                f"{token.column + 1}: banner 缺少结束分隔符"
            )
        delimiter, body = match.groups()
        if delimiter in body:
            return token
        while True:
            end = self.lexer.nextToken()
            if end.type == Token.EOF:
                raise ConfigurationParseError(
                    f"Cisco 配置解析失败，行 {token.line} 列 "
                    f"{token.column + 1}: banner 缺少结束分隔符 "
                    f"{delimiter}"
                )
            if delimiter in end.text:
                token.stop = end.stop
                token.text = self.inputStream.getText(token.start, end.stop)
                return token

    def nextToken(self):
        """返回下一个真实或虚拟 token，并维护 group 内的缩进基线。

        先发送待处理结构 token 再读取底层 lexer，可保持 parser 看到的层级顺序与源文本一致。
        """
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
                if (
                    self.group
                    and self.group_base is None
                    and token.type != CiscoLexer.SEPARATOR
                ):
                    self.group_base = indent
                effective = (
                    max(1, indent - (self.group_base or 0) + 1)
                    if self.group
                    else indent
                )
                self._align(effective, token)
                if token.type == CiscoLexer.GROUP:
                    self.group = True
                    self.group_base = None
                if token.type == CiscoLexer.BANNER:
                    token = self._banner(token)
        self.pending.append(token)
        return self.pending.popleft()


class CiscoSnapshot:
    """保存一次 Cisco 配置解析得到的不可变文本和 ANTLR 根上下文。

    快照隔离原始文本、修订号和解析结果，使事务失败时可以完整恢复上一修订。
    """

    def __init__(self, text: str, revision: int = 0):
        """解析配置文本并建立可遍历的 Cisco 根上下文。

        构造期间连接自定义错误监听器和缩进 token 源，确保失败立即暴露而不是返回残缺树。
        """
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
