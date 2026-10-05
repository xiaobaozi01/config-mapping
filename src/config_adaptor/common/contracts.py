"""跨厂商配置文档共同实现的能力协议。

协议只描述转换流程真正需要的能力。Cisco/Junos 的 AST、节点和语法辅助函数
均属于实现细节，不应泄漏到责任链和外部清洗规则中。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .interface import InterfaceKind, InterfaceSpec
from .outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    SimulationAdaptationOutcome,
)
from .policies import SimulationAdaptationPolicy, WashingPolicy


@runtime_checkable
class VendorConfiguration(Protocol):
    """单台设备配置的厂商无关能力集合。

    这是有意保留的 Facade 边界：应用层只认识业务动作，不认识 Cisco block
    或 Junos node。具体实现内部再按 group、接口、清洗和模拟适配模块组合。
    """

    vendor: str

    def hostname(self) -> str | None: ...

    def expand_groups(
        self,
        known_interfaces: set[str],
        *,
        policy: WashingPolicy,
    ) -> GroupExpansionOutcome: ...

    def interface_specs(self) -> list[InterfaceSpec]: ...

    def interface_kind(self, name: str) -> InterfaceKind: ...

    def bundle_members(self) -> dict[str, str]: ...

    def resolve_interface(self, value: str) -> str: ...

    def logical_names_under(self, parent: str) -> list[str]: ...

    def business_interface_names(self) -> set[str]: ...

    def remove_interface(self, name: str, include_children: bool = False) -> None: ...

    def rename_interface_tree(
        self,
        source: str,
        target: str,
        strip_bundle: bool = False,
    ) -> None: ...

    def clone_interface_tree(
        self,
        source: str,
        target: str,
        strip_bundle: bool = False,
    ) -> None: ...

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str: ...

    def finalize_uni_source(self, source_parent: str) -> None: ...

    def ensure_parent_interface(self, name: str) -> None: ...

    def replace_references(self, replacements: dict[str, list[str]]) -> None: ...

    def clean_management_access(self) -> CleanupOutcome: ...

    def clean_optional_features(self, policy: WashingPolicy) -> CleanupOutcome: ...

    def add_lab_account(self) -> None: ...

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome: ...

    def apply_cleaning_rule(
        self,
        match: str,
        action: str,
        value: str | None,
    ) -> int: ...

    def render(self) -> str: ...
