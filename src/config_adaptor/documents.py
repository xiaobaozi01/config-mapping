"""兼容入口；厂商实现已拆分到 :mod:`config_adaptor.parsers`。"""

from .parsers import (
    AuthenticationCleanupOutcome,
    CiscoDocument,
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceSpec,
    JunosDocument,
    ParameterAdjustmentOutcome,
    SimulationAdaptationOutcome,
    canonical_cisco_interface,
    canonical_junos_interface,
    interface_parent,
    interface_unit,
    parse_document,
)

__all__ = [
    "CiscoDocument",
    "AuthenticationCleanupOutcome",
    "GroupExpansionOutcome",
    "CleanupOutcome",
    "InterfaceSpec",
    "JunosDocument",
    "ParameterAdjustmentOutcome",
    "SimulationAdaptationOutcome",
    "canonical_cisco_interface",
    "canonical_junos_interface",
    "interface_parent",
    "interface_unit",
    "parse_document",
]
