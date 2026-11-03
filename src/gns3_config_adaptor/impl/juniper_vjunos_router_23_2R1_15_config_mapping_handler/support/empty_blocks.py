"""加载 vMX Junos 原配置空容器的输出策略。"""

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

_ACTIONS = (
    EmptyBlockAction.OMIT,
    EmptyBlockAction.SEMICOLON,
    EmptyBlockAction.BRACES,
)


def load_empty_block_policy(path: Path) -> EmptyBlockPolicy:
    """按 vMX 镜像加载并校验空块输出策略。

    厂商参数在此固定，调用方只需提供规则文件，避免每次调用重复声明镜像身份。
    """
    return _load_empty_block_policy(
        path,
        vendor="juniper_junos",
        image=default_target_image("juniper_junos"),
        actions=_ACTIONS,
    )


@lru_cache(maxsize=1)
def vmx_empty_block_policy() -> EmptyBlockPolicy:
    """返回缓存后的 vMX 默认空块策略。

    策略文件随程序发布且不会在一次运行中变化，缓存可避免每台设备重复读取和校验。
    """
    return load_empty_block_policy(empty_blocks_path("juniper_junos"))
