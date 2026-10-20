"""加载 XRv9000 IOS XR 原配置空容器的输出策略。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..common.empty_blocks import (
    EmptyBlockPolicy,
    load_empty_block_policy as _load_empty_block_policy,
)


_RULE_FILE = Path(__file__).with_name("rules") / "xrv9000" / "empty_blocks.yaml"
_ACTIONS = ("omit", "keep")


def load_empty_block_policy(path: Path) -> EmptyBlockPolicy:
    """按本厂商的目标镜像和动作集合加载策略，保留原单参数入口。"""
    return _load_empty_block_policy(
        path, vendor="cisco_iosxr", image="xrv9000", actions=_ACTIONS
    )


@lru_cache(maxsize=1)
def xrv9000_empty_block_policy() -> EmptyBlockPolicy:
    return load_empty_block_policy(_RULE_FILE)
