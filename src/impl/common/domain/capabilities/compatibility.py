"""目标接口兼容能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..outcomes import InterfaceAdaptationOutcome


class CompatibilityOperations(Protocol):
    """定义目标镜像接口兼容适配能力。

    该能力执行必要配置调整，不受模拟参数模式影响。
    """

    def adapt_interfaces(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """适配最终目标接口配置。

        返回结果应包含所有修改、告警和错误。
        """
        ...
