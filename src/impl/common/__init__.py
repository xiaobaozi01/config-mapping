"""Cisco 与 Juniper 共同依赖的基础类型和通用能力。"""

from .contracts import VendorConfiguration
from .interface import InterfaceKind, InterfaceSpec, interface_parent, interface_unit, normalized_command
from .outcomes import CleanupOutcome, GroupExpansionOutcome, SimulationAdaptationOutcome
from .policies import SimulationAdaptationPolicy, WashingPolicy

__all__ = [
    "CleanupOutcome",
    "GroupExpansionOutcome",
    "InterfaceKind",
    "InterfaceSpec",
    "SimulationAdaptationOutcome",
    "SimulationAdaptationPolicy",
    "VendorConfiguration",
    "WashingPolicy",
    "interface_parent",
    "interface_unit",
    "normalized_command",
]
