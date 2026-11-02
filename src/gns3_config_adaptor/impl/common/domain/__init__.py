"""跨厂商共享的领域协议、数据类型和策略。"""

from .contracts import VendorConfiguration
from .interface import (
    BundleMode,
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    interface_unit,
    normalized_command,
)
from .outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from .policies import SimulationAdaptationPolicy, WashingPolicy

__all__ = [
    "BundleMode",
    "CleanupOutcome",
    "GroupExpansionOutcome",
    "InterfaceAdaptationOutcome",
    "InterfaceKind",
    "InterfacePruningOutcome",
    "InterfaceSpec",
    "SimulationAdaptationOutcome",
    "SimulationAdaptationPolicy",
    "VendorConfiguration",
    "WashingPolicy",
    "interface_parent",
    "interface_unit",
    "normalized_command",
]
