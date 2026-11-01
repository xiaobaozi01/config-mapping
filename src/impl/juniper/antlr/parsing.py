"""Junos 原文保留的 ANTLR4 解析快照。"""

from __future__ import annotations

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from .generated.JunosLexer import JunosLexer
from .generated.JunosParser import JunosParser


class JunosConfigurationParseError(ValueError):
    """表示 Junos 配置无法可靠建立大括号结构。

    使用统一异常和行列信息可阻止调用方在不完整结构上继续迁移配置。
    """


class _Errors(ErrorListener):
    """把 ANTLR 词法和语法错误转换为仓库统一异常。

    集中转换错误能保留原始位置并避免解析器恢复后返回可能错误的残缺树。
    """

    def syntaxError(
        self, _recognizer, offending_symbol, line, column, msg, _error
    ):
        """抛出包含原始行列位置的解析异常。

        常见括号错误转换为稳定中文提示，其他语法错误保留 ANTLR 原始诊断。
        """
        del offending_symbol
        if msg.startswith("extraneous input '}'"):
            raise JunosConfigurationParseError(
                f"Junos 配置第 {line} 行出现多余右大括号"
            )
        if "<EOF>" in msg:
            raise JunosConfigurationParseError("Junos 配置大括号不平衡")
        raise JunosConfigurationParseError(
            f"Junos 配置解析失败，行 {line} 列 {column + 1}: {msg}"
        )


class JunosSnapshot:
    """保存一次 Junos 配置解析得到的不可变文本和根 Context。

    文本、token 流和根节点共享修订身份，使查询和事务验证始终基于同一份源码。
    """

    def __init__(self, text: str, revision: int = 0):
        """解析完整 Junos 文本并绑定同一修订的 token 流。

        构造阶段立即填满 token 流并执行完整语法，后续查询无需容忍增量解析状态。
        """
        self.text = text
        self.revision = revision
        lexer = JunosLexer(InputStream(text))
        lexer.removeErrorListeners()
        lexer.addErrorListener(_Errors())
        self.tokens = CommonTokenStream(lexer)
        self.tokens.fill()
        parser = JunosParser(self.tokens)
        parser.removeErrorListeners()
        parser.addErrorListener(_Errors())
        self.root = parser.configuration()
        self.root._snapshot = self
