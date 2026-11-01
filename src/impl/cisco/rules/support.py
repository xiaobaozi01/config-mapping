"""规则编写入口：托管会话、ANTLR Visitor、范围选择与文本编辑工具。"""

from ..antlr.context import CiscoContext
from ..antlr.generated.CiscoParserListener import CiscoParserListener
from ..antlr.generated.CiscoParserVisitor import CiscoParserVisitor
from ..antlr.rewriting import RewriteConflictError, format_fragment
from ..rewrite_helpers import append_top_level, delete_matching
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
