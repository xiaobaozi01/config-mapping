"""XRv9000 配置语句的语义 identity 适配。"""

from __future__ import annotations

from functools import lru_cache

from ...common.semantic_identity import (
    SemanticDecision,
    SemanticResolver,
    StatementContext,
    load_rule_pack,
)
from ...common.semantic_identity.matcher import normalize
from ...common.support.config_paths import (
    default_target_image,
    identity_rules_path,
)
from ...common.support.optimization_cache import memoized


@lru_cache(maxsize=1)
def _resolver() -> SemanticResolver:
    """构建并缓存 XRv9000 语义 identity 解析器。

    规则包只依赖镜像静态文件，单例复用已校验规则和候选桶；输入结果只保留在任务作用域中。
    """
    directory = identity_rules_path("cisco_iosxr")
    return SemanticResolver(
        load_rule_pack(
            directory,
            vendor="cisco_iosxr",
            image=default_target_image("cisco_iosxr"),
        ),
        cache_budget_bytes=0,
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
    normalized = memoized(
        "normalize.cisco", statement, lambda: normalize(statement)
    )
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
