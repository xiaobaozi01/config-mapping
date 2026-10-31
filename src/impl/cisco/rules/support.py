"""规则编写入口：托管会话、ANTLR Visitor、范围选择与文本编辑工具。"""

from ..antlr.context import CiscoContext
from ..antlr.generated.CiscoParserVisitor import CiscoParserVisitor
from ..antlr.generated.CiscoParserListener import CiscoParserListener
from ..antlr.rewriting import RewriteConflictError, format_fragment
from .runtime import CiscoVisitorRule, CiscoRuleVisitor, RuleSession, RuleContractError, simulation_result
from .scopes import document_scope, InterfaceScope, compatibility_interfaces_scope, select_interfaces


def append_top_level(transaction, document, text: str, reason: str):
    terminal = next((node for node in document.root.nodes if node.header.lower() in {"end", "commit"}), None)
    if terminal is None:
        transaction.append_child(document.root, text, reason)
    else:
        transaction.insert_before(terminal, text, reason)


def delete_matching(transaction, nodes, pattern, *, match_anywhere=False, reason="删除匹配命令"):
    test = pattern.search if match_anywhere else pattern.match
    removed = 0
    for node in nodes:
        if node.is_formatting:
            continue
        if test(node.header):
            removed += sum(1 for _ in node.walk(include_self=True))
            transaction.delete(node, reason)
        else:
            removed += delete_matching(transaction, node.nodes, pattern, match_anywhere=match_anywhere, reason=reason)
    return removed


__all__ = [
    "CiscoContext", "CiscoParserVisitor", "CiscoParserListener", "RewriteConflictError",
    "select_interfaces", "append_top_level", "delete_matching", "format_fragment",
    "CiscoVisitorRule", "CiscoRuleVisitor", "RuleSession", "RuleContractError", "simulation_result",
    "document_scope", "InterfaceScope", "compatibility_interfaces_scope",
]
