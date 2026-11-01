"""模拟参数适配能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..outcomes import SimulationAdaptationOutcome
from ..policies import SimulationAdaptationPolicy


class SimulationOperations(Protocol):
    """定义目标模拟环境的参数适配能力。

    适配只作用于已映射数据口，并遵循镜像策略设定的边界。
    """

    def adapt(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按策略调整模拟参数。

        返回结果应记录每类修改及审计计数。
        """
        ...
