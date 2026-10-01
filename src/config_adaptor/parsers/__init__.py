"""厂商文档对象的统一入口；具体语法实现保持相互独立。"""

from __future__ import annotations

from .cisco_iosxr import CiscoDocument, canonical_cisco_interface
from .common import (
    AuthenticationCleanupOutcome,
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceSpec,
    ParameterAdjustmentOutcome,
    SimulationAdaptationOutcome,
    interface_parent,
    interface_unit,
)
from .juniper_junos import JunosDocument, canonical_junos_interface


def parse_document(vendor: str, text: str) -> CiscoDocument | JunosDocument:
    """按规范化厂商标识创建对应的配置文档对象。"""
    if vendor == "cisco_iosxr":
        return CiscoDocument(text)
    if vendor == "juniper_junos":
        return JunosDocument(text)
    raise ValueError(f"不支持的配置厂商: {vendor}")


__all__ = [
    "CiscoDocument",
    "AuthenticationCleanupOutcome",
    "CleanupOutcome",
    "GroupExpansionOutcome",
    "InterfaceSpec",
    "ParameterAdjustmentOutcome",
    "SimulationAdaptationOutcome",
    "JunosDocument",
    "canonical_cisco_interface",
    "canonical_junos_interface",
    "interface_parent",
    "interface_unit",
    "parse_document",
]
