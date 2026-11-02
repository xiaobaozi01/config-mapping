"""Cisco IOS XR 接口能力兼容门面。"""

from .access import find_interface_node, interface_nodes, resolve_interface
from .analysis import (
    CiscoInterfaceAnalysis,
    bundle_members,
    interface_kind,
    interface_specs,
    logical_names_under,
)
from .business import business_interface_names
from .capabilities import CiscoInterfaceCapabilities
from .transform import (
    CiscoInterfaceTransform,
    clone_interface_tree,
    ensure_parent_interface,
    remove_interface,
    rename_interface_tree,
)

__all__ = [
    "CiscoInterfaceAnalysis",
    "CiscoInterfaceCapabilities",
    "CiscoInterfaceTransform",
    "bundle_members",
    "business_interface_names",
    "clone_interface_tree",
    "ensure_parent_interface",
    "find_interface_node",
    "interface_kind",
    "interface_nodes",
    "interface_specs",
    "logical_names_under",
    "remove_interface",
    "rename_interface_tree",
    "resolve_interface",
]
