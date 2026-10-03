"""配置 group 冲突判定使用的共享语义规则引擎。"""

from .loader import load_rule_pack
from .models import MergeKind, SemanticDecision, SemanticRule, StatementContext
from .resolver import SemanticResolver, find_opaque_ambiguity

__all__ = [
    "MergeKind",
    "SemanticDecision",
    "SemanticResolver",
    "SemanticRule",
    "StatementContext",
    "find_opaque_ambiguity",
    "load_rule_pack",
]
