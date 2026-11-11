"""Cisco IOS XR 接口业务关联分析。"""

from __future__ import annotations

import re

from ....common.domain.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    referenced_interface_names,
)
from ...document import CiscoDocument
from .access import interface_nodes

_MEMBER_BUSINESS = re.compile(
    r"^(?:ipv[46]\s+(?:address|unnumbered|enable)|vrf|l2transport|xconnect)\b",
    re.IGNORECASE,
)


def bridge_domain_bindings(
    document: CiscoDocument,
) -> list[tuple[set[str], set[str]]]:
    """返回每个活动 bridge-domain 的接入接口和三层网关集合。

    只解析活动 ``l2vpn`` 配置，并用规范接口名表示关系。
    """
    result: list[tuple[set[str], set[str]]] = []
    for block in document.root.nodes:
        if not block.active or not re.match(
            r"^l2vpn\b",
            block.header.strip(),
            re.IGNORECASE,
        ):
            continue
        for node in block.nodes:
            if node.is_formatting:
                continue
            stack = [node]
            while stack:
                current = stack.pop()
                stack.extend(current.nodes)
                if not re.match(
                    r"^bridge-domain\b", current.header, re.IGNORECASE
                ):
                    continue
                attachments: set[str] = set()
                gateways: set[str] = set()
                for child in current.walk():
                    gateway_match = re.match(
                        r"^routed\s+interface\s+(.+)$",
                        child.header,
                        re.IGNORECASE,
                    )
                    if gateway_match:
                        gateways.add(
                            document._parser.canonical_interface(
                                gateway_match.group(1)
                            )
                        )
                        continue
                    interface_match = re.match(
                        r"^interface\s+(.+)$",
                        child.header,
                        re.IGNORECASE,
                    )
                    if interface_match:
                        attachments.add(
                            document._parser.canonical_interface(
                                interface_match.group(1)
                            )
                        )
                result.append((attachments, gateways))
    return result


def gateway_inner_vlan_hints(
    document: CiscoDocument,
    specs: list[InterfaceSpec],
) -> dict[str, int]:
    """从 bridge-domain 关系推导无歧义的 BVI 业务 VLAN。

    只有接入接口给出唯一业务标签，且网关只得到一个候选值时才返回提示。
    """
    spec_by_name = {spec.name: spec for spec in specs}
    hints: dict[str, set[int]] = {}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    for attachments, gateways in bridge_domain_bindings(document):
        service_vlans: set[int] = set()
        for attachment in attachments:
            attachment_spec = spec_by_name.get(attachment)
            if (
                attachment_spec is None
                or attachment_spec.kind not in mappable_kinds
            ):
                continue
            service_vlan = attachment_spec.inner_vlan or attachment_spec.vlan
            if service_vlan is not None:
                service_vlans.add(service_vlan)
        if len(service_vlans) != 1:
            continue
        service_vlan = next(iter(service_vlans))
        for gateway in gateways:
            hints.setdefault(gateway, set()).add(service_vlan)
    return {
        gateway: next(iter(vlans))
        for gateway, vlans in hints.items()
        if len(vlans) == 1
    }


def interface_has_business(document: CiscoDocument, name: str) -> bool:
    """判断物理聚合成员是否承载独立业务。

    地址、VRF、L2 业务和子接口都会阻止成员压缩。
    """
    parent = interface_parent(document._parser.canonical_interface(name))
    parser = document._parser
    for block in interface_nodes(document):
        block_name = parser.interface_name(block.header)
        if block_name is None:
            continue
        if block_name == parent:
            if block.l2transport or any(
                _MEMBER_BUSINESS.match(node.header) for node in block.walk()
            ):
                return True
        elif interface_parent(block_name) == parent:
            return True
    return False


def bundle_binding_is_ambiguous(document: CiscoDocument, name: str) -> bool:
    """判断物理成员是否声明了多个不同 Bundle 编号。

    多个绑定无法选择唯一聚合关系。
    """
    parent = interface_parent(document._parser.canonical_interface(name))
    bindings = set()
    for block in interface_nodes(document):
        if document._parser.interface_name(block.header) != parent:
            continue
        bindings.update(
            match.group(1)
            for node in block.walk()
            if (
                match := re.match(
                    r"^bundle\s+id\s+(\d+)\b",
                    node.header,
                    re.IGNORECASE,
                )
            )
        )
    return len(bindings) > 1


def external_configuration_text(document: CiscoDocument) -> str:
    """提取接口定义之外的有效配置文本。

    协议、VRF 和 L2VPN 的接口引用位于接口块之外。
    """
    return "\n".join(
        text
        for block in document.root.nodes
        if not block.interface_name
        for text in [block.header, *(node.header for node in block.walk())]
    )


def business_interface_names(document: CiscoDocument) -> set[str]:
    """识别实际承载三层、二层或网关业务的 IOS XR 接口。

    接口自身业务和外部配置引用都会激活业务；BVI 仅在关联 bridge-domain
    仍有活跃接入口时保留。
    """
    parser = document._parser
    specs = document.interfaces.analysis.interface_specs()
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active: set[str] = set()
    direct = re.compile(
        r"^(?:ipv4\s+(?:address|unnumbered)\b|ipv6\s+address\b|"
        r"xconnect\b|ethernet-services\b)",
        re.IGNORECASE,
    )
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        if (
            name
            and kinds.get(name) in mappable_kinds
            and (
                parser.l2transport(block.header)
                or any(direct.match(node.header) for node in block.walk())
            )
        ):
            active.add(name)

    external_text = external_configuration_text(document)
    referenced = referenced_interface_names(
        external_text, known | {spec.parent for spec in specs}
    )
    for name in known & referenced:
        if kinds.get(name) in mappable_kinds:
            active.add(name)
    specs_by_parent: dict[str, list[InterfaceSpec]] = {}
    for spec in specs:
        specs_by_parent.setdefault(spec.parent, []).append(spec)
    for parent, parent_specs in specs_by_parent.items():
        if parent in referenced and parent_specs[0].kind in mappable_kinds:
            active.update(spec.name for spec in parent_specs)

    for attachments, gateways in bridge_domain_bindings(document):
        if attachments & active:
            active.update(gateway for gateway in gateways if gateway in known)
    return active
