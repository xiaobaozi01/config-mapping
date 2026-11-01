"""配置 Group 展开能力协议。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..outcomes import GroupExpansionOutcome
from ..policies import WashingPolicy


class GroupOperations(Protocol):
    """定义活动 Group 的展开能力。

    展开必须在接口分析前完成，并以事务方式提交或回滚整台设备结果。
    """

    def expand(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """展开配置引用的全部 Group。

        返回结果包含继承事件、冲突、歧义和整体成功状态。
        """
        ...
