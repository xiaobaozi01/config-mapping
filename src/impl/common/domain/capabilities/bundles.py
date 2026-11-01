"""聚合接口能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..interface import BundleMode
from ..outcomes import InterfaceAdaptationOutcome


class BundleOperations(Protocol):
    """定义聚合接口查询和适配能力。

    聚合只处理已有绑定和模拟门限，不改变成员的链路身份。
    """

    def mode(self, member: str) -> BundleMode:
        """返回成员的聚合协商模式。

        缺失或冲突信息必须返回未知模式。
        """
        ...

    def adapt(self, name: str) -> InterfaceAdaptationOutcome:
        """按模拟环境要求调整聚合门限。

        结果应包含所有修改、告警和错误。
        """
        ...
