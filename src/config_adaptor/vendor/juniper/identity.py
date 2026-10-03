"""vMX Junos 配置语句的语义 identity 适配。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..identity import SemanticDecision, SemanticResolver, StatementContext, load_rule_pack
from ..identity.matcher import normalize


@lru_cache(maxsize=1)
def _resolver() -> SemanticResolver:
    directory = Path(__file__).with_name("rules") / "vmx"
    return SemanticResolver(
        load_rule_pack(directory, vendor="juniper_junos", image="vmx")
    )


def _strip_annotations(statement: str) -> str:
    """去掉不改变配置槽位的 Junos 节点标记。"""
    result = normalize(statement)
    while True:
        for prefix in ("inactive:", "protect:", "replace:"):
            if result.startswith(prefix):
                result = result[len(prefix) :].strip()
                break
        else:
            return result


def resolve_junos_identity(
    statement: str,
    *,
    path: list[str] | tuple[str, ...] = (),
    node_kind: str = "leaf",
) -> SemanticDecision:
    """解析 Junos 语句，标记前缀不参与 identity 计算。"""
    return _resolver().resolve(
        StatementContext(
            vendor="juniper_junos",
            path=tuple(_strip_annotations(component) for component in path),
            statement=_strip_annotations(statement),
            node_kind=node_kind,
        )
    )
