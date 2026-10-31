"""规则编写入口：托管会话、ANTLR Visitor、范围选择与文本编辑工具。"""

from ..antlr.context import CiscoContext
from ..antlr.generated.CiscoParserListener import CiscoParserListener
from ..antlr.generated.CiscoParserVisitor import CiscoParserVisitor
from ..antlr.rewriting import RewriteConflictError, format_fragment
from .runtime import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    RuleContractError,
    RuleSession,
    simulation_result,
)
from .scopes import (
    InterfaceScope,
    compatibility_interfaces_scope,
    document_scope,
    select_interfaces,
)


def append_top_level(transaction, document, text: str, reason: str):
    """在终止命令前追加顶层配置，或在没有终止命令时直接追加根部。

    保持 ``end``/``commit`` 位于末尾可让输出继续符合 Cisco 配置结构。
    """
    terminal = next(
        (
            node
            for node in document.root.nodes
            if node.header.lower() in {"end", "commit"}
        ),
        None,
    )
    if terminal is None:
        transaction.append_child(document.root, text, reason)
    else:
        transaction.insert_before(terminal, text, reason)


def delete_matching(
    transaction, nodes, pattern, *, match_anywhere=False, reason="删除匹配命令"
):
    """递归删除匹配正则的配置节点并返回删除对象数量。

    递归前保留完整子树计数，使父节点删除能够准确报告受影响命令数量。
    """
    test = pattern.search if match_anywhere else pattern.match
    removed = 0
    for node in nodes:
        if node.is_formatting:
            continue
        if test(node.header):
            removed += sum(1 for _ in node.walk(include_self=True))
            transaction.delete(node, reason)
        else:
            removed += delete_matching(
                transaction,
                node.nodes,
                pattern,
                match_anywhere=match_anywhere,
                reason=reason,
            )
    return removed


__all__ = [
    "CiscoContext",
    "CiscoParserListener",
    "CiscoParserVisitor",
    "CiscoRuleVisitor",
    "CiscoVisitorRule",
    "InterfaceScope",
    "RewriteConflictError",
    "RuleContractError",
    "RuleSession",
    "append_top_level",
    "compatibility_interfaces_scope",
    "delete_matching",
    "document_scope",
    "format_fragment",
    "select_interfaces",
    "simulation_result",
]
