"""由配置自适应流程传给各厂商实现的共享策略。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SimulationMode(StrEnum):
    """定义模拟参数适配允许使用的稳定模式。"""

    OFF = "off"
    COMPATIBLE = "compatible"
    STABLE = "stable"


class GroupIdentityMode(StrEnum):
    """定义 group 未覆盖语义出现潜在冲突时的处理策略。"""

    PRESERVE = "preserve"
    WARN = "warn"
    FAIL = "fail"


@dataclass(slots=True)
class SimulationAdaptationPolicy:
    """定义目标镜像使用的保守模拟适配策略。

    所有能力都通过显式字段控制，使调用方能够关闭不必要的改写并保留可审计边界。
    """

    mode: SimulationMode = SimulationMode.STABLE
    ensure_data_interfaces_enabled: bool = True
    remove_physical_interface_knobs: bool = True
    bfd_minimum_interval_ms: int = 300
    bfd_minimum_multiplier: int = 3

    def __post_init__(self) -> None:
        """把配置入口的字符串规范化为受限模式。"""
        try:
            self.mode = SimulationMode(self.mode)
        except ValueError as exc:
            raise ValueError(
                f"simulation_adaptation.mode 不支持: {self.mode}"
            ) from exc


@dataclass(slots=True, frozen=True)
class WashingPolicy:
    """定义 group 未知语义和排除模板的处理策略。

    策略对象不依赖具体文档，使同一转换流程可按镜像选择保留、告警或失败的语义。
    """

    group_unknown_identity: GroupIdentityMode = GroupIdentityMode.WARN
    junos_excluded_groups: tuple[str, ...] = ("re1",)

    def __post_init__(self) -> None:
        """把配置入口的字符串规范化为受限处理策略。"""
        try:
            mode = GroupIdentityMode(self.group_unknown_identity)
        except ValueError as exc:
            raise ValueError(
                "group_unknown_identity 不支持: "
                f"{self.group_unknown_identity}"
            ) from exc
        object.__setattr__(self, "group_unknown_identity", mode)
