"""配置语义 identity 规则的内部数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MergeKind(StrEnum):
    """同一配置路径下语句的合并类型。"""

    REPLACE = "replace"
    KEYED_SET = "keyed-set"
    SET = "set"
    DIRECTIONAL = "directional"
    PRESENCE = "presence"
    ORDERED_LIST = "ordered-list"
    OPAQUE = "opaque"


@dataclass(frozen=True, slots=True)
class StatementContext:
    """一条待识别配置及其所在完整路径。"""

    vendor: str
    path: tuple[str, ...]
    statement: str
    node_kind: str = "leaf"


@dataclass(frozen=True, slots=True)
class SemanticRule:
    """从 YAML 加载并校验后的语义规则。"""

    rule_id: str
    node_kind: str
    path: tuple[str, ...]
    statement: tuple[str, ...]
    identity: tuple[str, ...]
    merge_kind: MergeKind


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    """规则引擎对一条配置的识别结果。"""

    identity: tuple[str, ...]
    merge_kind: MergeKind
    rule_id: str | None
    matched: bool
    normalized: str

    @property
    def key(self) -> str:
        """转换为可写入 report.json 的稳定键。"""
        return f"{self.merge_kind.value}:{' '.join(self.identity)}"
