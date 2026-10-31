"""由配置自适应流程传给各厂商实现的共享策略。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SimulationAdaptationPolicy:
    """定义目标镜像使用的保守模拟适配策略。

    所有能力都通过显式字段控制，使调用方能够关闭不必要的改写并保留可审计边界。
    """

    mode: str = "stable"
    ensure_data_interfaces_enabled: bool = True
    remove_physical_interface_knobs: bool = True
    bfd_minimum_interval_ms: int = 300
    bfd_minimum_multiplier: int = 3


@dataclass(slots=True, frozen=True)
class WashingPolicy:
    """定义 group 未知语义和排除模板的处理策略。

    策略对象不依赖具体文档，使同一转换流程可按镜像选择保留、告警或失败的语义。
    """

    group_unknown_identity: str = "warn"
    junos_excluded_groups: tuple[str, ...] = ("re1",)
