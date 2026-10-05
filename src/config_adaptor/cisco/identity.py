"""XRv9000 配置语句的语义 identity 适配。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..common.semantic_identity import SemanticDecision, SemanticResolver, StatementContext, load_rule_pack
from ..common.semantic_identity.matcher import normalize


@lru_cache(maxsize=1)
def _resolver() -> SemanticResolver:
    directory = Path(__file__).with_name("rules") / "xrv9000"
    return SemanticResolver(
        load_rule_pack(directory, vendor="cisco_iosxr", image="xrv9000")
    )


def resolve_cisco_identity(
    statement: str,
    *,
    path: list[str] | tuple[str, ...] = (),
    node_kind: str = "leaf",
) -> SemanticDecision:
    """解析 IOS XR 语句；已知否定命令与对应正向命令共用 identity。"""
    normalized = normalize(statement)
    positive = normalized.removeprefix("no ") if normalized.startswith("no ") else normalized
    positive_decision = _resolver().resolve(
        StatementContext(
            vendor="cisco_iosxr",
            path=tuple(path),
            statement=positive,
            node_kind=node_kind,
        )
    )
    if positive != normalized and not positive_decision.matched:
        return _resolver().resolve(
            StatementContext(
                vendor="cisco_iosxr",
                path=tuple(path),
                statement=normalized,
                node_kind=node_kind,
            )
        )
    return positive_decision
