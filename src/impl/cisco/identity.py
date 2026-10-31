"""XRv9000 配置语句的语义 identity 适配。"""

from __future__ import annotations

from functools import lru_cache

from impl.common.config_paths import config_path
from impl.common.semantic_identity import (
    SemanticDecision,
    SemanticResolver,
    StatementContext,
    load_rule_pack,
)
from impl.common.semantic_identity.matcher import normalize


@lru_cache(maxsize=1)
def _resolver() -> SemanticResolver:
    """构建并缓存 XRv9000 语义 identity 解析器。

    规则包只依赖镜像静态文件，单例缓存可复用已校验规则和解析缓存。
    """
    directory = config_path("rules", "cisco", "xrv9000")
    return SemanticResolver(
        load_rule_pack(directory, vendor="cisco_iosxr", image="xrv9000")
    )


def resolve_cisco_identity(
    statement: str,
    *,
    path: list[str] | tuple[str, ...] = (),
    node_kind: str = "leaf",
) -> SemanticDecision:
    """解析 IOS XR 语句的语义 identity。

    已知 ``no`` 命令优先按对应正向命令识别，使正反配置在继承覆盖时落到同一槽位。
    """
    normalized = normalize(statement)
    positive = (
        normalized.removeprefix("no ")
        if normalized.startswith("no ")
        else normalized
    )
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
