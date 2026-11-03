"""加载 XRv9000 IOS XR 原配置空容器的输出策略。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ...common.rules.empty_blocks import (
    EmptyBlockAction,
    EmptyBlockPolicy,
)
from ...common.rules.empty_blocks import (
    load_empty_block_policy as _load_empty_block_policy,
)
from ...common.support.config_paths import (
    default_target_image,
    empty_blocks_path,
)

_ACTIONS = (EmptyBlockAction.OMIT, EmptyBlockAction.KEEP)


def load_empty_block_policy(path: Path) -> EmptyBlockPolicy:
    """读取 XRv9000 镜像的空块输出策略。

    厂商入口固定目标和允许动作，使通用加载器无需了解 Cisco 镜像约束。
    """
    return _load_empty_block_policy(
        path,
        vendor="cisco_iosxr",
        image=default_target_image("cisco_iosxr"),
        actions=_ACTIONS,
    )


@lru_cache(maxsize=1)
def xrv9000_empty_block_policy() -> EmptyBlockPolicy:
    """返回缓存的 XRv9000 空块策略。

    规则文件在一次进程中不变，缓存可避免每次渲染 Cisco 文档都重新读取和校验 YAML。
    """
    return load_empty_block_policy(empty_blocks_path("cisco_iosxr"))
