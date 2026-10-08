"""由配置自适应流程传给各厂商实现的共享策略。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SimulationAdaptationPolicy:
    """目标镜像的保守参数适配策略。"""

    mode: str = "stable"
    ensure_data_interfaces_enabled: bool = True
    remove_physical_interface_knobs: bool = True
    bfd_minimum_interval_ms: int = 300
    bfd_minimum_multiplier: int = 3


ParamAdjustmentPolicy = SimulationAdaptationPolicy


@dataclass(slots=True, frozen=True)
class WashingPolicy:
    """Group 未知语义处理策略。"""

    group_unknown_identity: str = "warn"
