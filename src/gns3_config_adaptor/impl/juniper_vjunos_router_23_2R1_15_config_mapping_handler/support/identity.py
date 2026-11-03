"""vMX Junos 配置语句的语义 identity 适配。"""

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
    """返回共享的 vMX 语义解析器。

    规则包和编译缓存创建成本较高且内容固定，单例缓存可避免每次语句解析重复加载。
    """
    directory = identity_rules_path("juniper_junos")
    return SemanticResolver(
        load_rule_pack(
            directory,
            vendor="juniper_junos",
            image=default_target_image("juniper_junos"),
        ),
        cache_budget_bytes=0,
    )


def _strip_annotations_uncached(statement: str) -> str:
    """去掉不改变配置槽位的 Junos 节点标记。

    inactive、protect 和 replace 描述编辑状态而非身份，忽略它们才能正确比较同一槽位。
    """
    result = normalize(statement)
    while True:
        for prefix in ("inactive:", "protect:", "replace:"):
            if result.startswith(prefix):
                result = result[len(prefix) :].strip()
                break
        else:
            return result


def _strip_annotations(statement: str) -> str:
    """只在活动任务内缓存由输入派生的规范化结果。

    共享的已编译解析器不保留调用方传入的独立语句字符串，避免跨任务留存输入。
    """
    return memoized(
        "normalize.junos",
        statement,
        lambda: _strip_annotations_uncached(statement),
    )


def resolve_junos_identity(
    statement: str,
    *,
    path: list[str] | tuple[str, ...] = (),
    node_kind: str = "leaf",
) -> SemanticDecision:
    """解析 Junos 语句并返回路径感知的语义决策。

    路径和语句使用同一套去标记规则，避免继承位置相同但显示前缀不同导致错误冲突。
    """
    return _resolver().resolve(
        StatementContext(
            vendor="juniper_junos",
            path=tuple(_strip_annotations(component) for component in path),
            statement=_strip_annotations(statement),
            node_kind=node_kind,
        )
    )
