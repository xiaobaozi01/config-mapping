"""规则编写入口：托管会话、AST Visitor、范围选择与文本编辑工具。"""

from ...common.config_ast import ConfigListener, ConfigVisitor
from ...common.config_ast.editor import EditConflictError, format_fragment
from ..syntax.ast import CiscoNode
from ..syntax.rewrite_helpers import append_top_level, delete_matching
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
    "CiscoNode",
    "CiscoRuleVisitor",
    "CiscoVisitorRule",
    "ConfigListener",
    "ConfigVisitor",
    "EditConflictError",
    "InterfaceScope",
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
