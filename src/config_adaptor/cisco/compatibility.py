"""迁移后的 IOS XR 接口兼容处理与保守的语义检查。

这些规则只覆盖已确认的接口类型约束，不是 XRv9000 CLI 的完整能力表。
未知命令保留并告警；静态检查通过不代表已经在实际镜像上 commit 验证。
"""

from __future__ import annotations

import re

from ..common.interface import InterfaceKind, interface_parent, interface_unit
from ..common.outcomes import InterfaceAdaptationOutcome
from ..common.semantic_identity.models import MergeKind
from .document import CiscoDocument, CiscoNode
from .identity import resolve_cisco_identity
from .interfaces import ensure_parent_interface, find_interface_node, interface_nodes


_AGGREGATION = re.compile(r"^(?:no\s+)?(?:bundle\b|lacp\b|aggregated-)", re.IGNORECASE)
_HARDWARE = re.compile(
    r"^(?:no\s+)?(?:speed|duplex|negotiation|fec|transceiver|carrier-delay|dampening)\b",
    re.IGNORECASE,
)
_ETHERTYPE = re.compile(
    r"^(?:ethernet\s+)?(?:dot1q\s+)?tunneling\s+ethertype\b", re.IGNORECASE
)
_ADDRESSING = re.compile(
    r"^(?:ipv[46]\s+(?:address|unnumbered|enable)\b|vrf\b)", re.IGNORECASE
)
# This list suppresses noise for familiar command families; it is not an allowlist
# certifying all their options on every XRv9000 image.
_FAMILIAR = re.compile(
    r"^(?:no\s+)?(?:description|shutdown|mtu|bandwidth|ipv4|ipv6|vrf|arp|"
    r"proxy-arp|local-proxy-arp|mac-address|encapsulation|rewrite|l2transport|"
    r"load-interval|flow-control|keepalive|service-policy|bfd|lldp|cdp|"
    r"xconnect|mpls|rsvp|ethernet|tunneling)\b",
    re.IGNORECASE,
)


def _normal(command: str) -> str:
    return " ".join(command.lower().split())


def _is_l2(block: CiscoNode) -> bool:
    return block.l2transport or any(
        node.active and _normal(node.header) == "l2transport" for node in block.children
    )


def _scope(document: CiscoDocument, targets: set[str]) -> set[str]:
    return {interface_parent(document.resolve_interface(name)) for name in targets}


def _in_scope(name: str, parents: set[str]) -> bool:
    return interface_parent(name) in parents


def _remove_known(
    nodes: list[CiscoNode], name: str, outcome: InterfaceAdaptationOutcome, member: bool = False
) -> list[CiscoNode]:
    retained: list[CiscoNode] = []
    for node in nodes:
        if not node.active or node.is_formatting:
            retained.append(node)
            continue
        reason = (
            "目标非聚合接口不保留聚合属性" if _AGGREGATION.match(node.header) and not member
            else "移除模拟镜像不兼容的硬件属性" if _HARDWARE.match(node.header)
            else None
        )
        if reason is not None:
            outcome.changes.append({
                "action": "removed", "interface": name,
                "command": node.header, "reason": reason,
            })
            continue
        node.children = _remove_known(node.children, name, outcome, member)
        retained.append(node)
    return retained


def adapt_interfaces(
    document: CiscoDocument, target_interfaces: set[str]
) -> InterfaceAdaptationOutcome:
    """仅清理目标接口树的确定性残留，并把 ethertype 移到物理父口。"""
    outcome = InterfaceAdaptationOutcome()
    parents = _scope(document, target_interfaces)
    members = document.bundle_members()
    for block in list(interface_nodes(document)):
        name = block.interface_name
        if not name or not _in_scope(name, parents):
            continue
        kind = document.interface_kind(name)
        if kind not in {InterfaceKind.PHYSICAL, InterfaceKind.GATEWAY, InterfaceKind.BUNDLE}:
            continue
        if kind != InterfaceKind.BUNDLE:
            block.children = _remove_known(block.children, name, outcome, name in members)
        if kind != InterfaceKind.PHYSICAL or interface_unit(name) is None:
            continue
        for node in list(block.children):
            if not node.active or not _ETHERTYPE.match(node.header):
                continue
            parent_name = interface_parent(name)
            parent = find_interface_node(document, parent_name)
            existing = [] if parent is None else [
                child for child in parent.children
                if child.active and _ETHERTYPE.match(child.header)
            ]
            if any(_normal(child.header) != _normal(node.header) for child in existing):
                outcome.errors.append(
                    f"接口 {name} 的 {node.header} 与父接口 {parent_name} 的 tunneling ethertype 冲突"
                )
                continue
            if parent is None:
                ensure_parent_interface(document, parent_name)
                parent = find_interface_node(document, parent_name)
            if parent is not None and not existing:
                parent.children.append(node.clone())
            block.children.remove(node)
            outcome.changes.append({
                "action": "moved", "interface": name,
                "target_interface": parent_name, "command": node.header,
                "reason": "tunneling ethertype 只能配置在物理主口",
            })
    return outcome


def validate_interfaces(
    document: CiscoDocument, target_interfaces: set[str]
) -> InterfaceAdaptationOutcome:
    """检查类型与引用矛盾，保留未覆盖的命令并说明静态检查的边界。"""
    outcome = InterfaceAdaptationOutcome()
    parents = _scope(document, target_interfaces)
    members = document.bundle_members()
    ended = False
    for node in document.root.children:
        if not node.active or node.is_formatting:
            continue
        if node.header.strip().lower() == "end":
            ended = True
        elif ended:
            outcome.errors.append(f"配置终止标记 end 后存在活动配置: {node.header.split()[0]}")
    blocks = {
        block.interface_name: block for block in interface_nodes(document)
        if block.interface_name is not None
    }
    for name, block in blocks.items():
        kind = document.interface_kind(name)
        if not _in_scope(name, parents) and kind != InterfaceKind.GATEWAY:
            continue
        if name in members and document.bundle_mode(name) == "unknown":
            outcome.errors.append(f"聚合成员 {name} 含非法业务或冲突绑定")
        l2 = _is_l2(block)
        unit = interface_unit(name)
        if kind == InterfaceKind.GATEWAY and (l2 or unit is not None):
            outcome.errors.append(f"网关接口 {name} 必须保留 BVI 三层接口类型")
        scalar_values: dict[tuple[str, ...], str] = {}
        for node in block.children:
            if not node.active or node.is_formatting:
                continue
            command = node.header
            decision = resolve_cisco_identity(
                command, path=(block.header,), node_kind="block" if node.is_block else "leaf",
            )
            if decision.matched and decision.merge_kind == MergeKind.REPLACE:
                previous = scalar_values.setdefault(decision.identity, decision.normalized)
                if previous != decision.normalized:
                    outcome.errors.append(f"接口 {name} 的单值配置 {' '.join(decision.identity)} 存在冲突")
            if l2 and _ADDRESSING.match(command):
                outcome.errors.append(f"L2 接口 {name} 不能包含三层地址或 VRF 命令: {command}")
            if l2 and re.match(r"^mac(?:-|\s+)address\b", command, re.IGNORECASE):
                outcome.errors.append(f"L2 接口 {name} 不能包含三层 MAC 地址命令: {command}")
            if not l2 and re.match(r"^rewrite\b", command, re.IGNORECASE):
                outcome.errors.append(f"三层接口 {name} 不能包含 L2 标签改写命令: {command}")
            if kind == InterfaceKind.GATEWAY and re.match(
                r"^(?:encapsulation|rewrite)\b", command, re.IGNORECASE
            ):
                outcome.errors.append(f"BVI 接口 {name} 不能包含以太网子接口命令: {command}")
            if kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE} and unit is None and re.match(
                r"^encapsulation\b", command, re.IGNORECASE
            ):
                outcome.errors.append(f"物理主口 {name} 不能包含子接口封装命令: {command}")
            if kind == InterfaceKind.PHYSICAL and unit is not None and _ETHERTYPE.match(command):
                outcome.errors.append(f"子接口 {name} 残留主口专属命令: {command}")
            if kind != InterfaceKind.BUNDLE and ((_AGGREGATION.match(command) and name not in members) or _HARDWARE.match(command)):
                outcome.errors.append(f"接口 {name} 残留不兼容属性: {command}")
            if not _FAMILIAR.match(command) and not _ETHERTYPE.match(command):
                outcome.warnings.append(
                    f"接口 {name} 命令族 {command.split()[0]} 未覆盖兼容规则，已保留，需镜像核验"
                )
        if kind == InterfaceKind.PHYSICAL and unit is not None:
            parent = blocks.get(interface_parent(name))
            if parent is not None and _is_l2(parent):
                outcome.errors.append(f"子接口 {name} 的物理父口不能配置为 l2transport")

    for block in document.root.children:
        if not block.active or not re.match(r"^l2vpn\b", block.header, re.IGNORECASE):
            continue
        for node in block.walk():
            routed = re.match(r"^routed\s+interface\s+(.+)$", node.header, re.IGNORECASE)
            if routed:
                name = document.resolve_interface(routed.group(1))
                if document.interface_kind(name) != InterfaceKind.GATEWAY or interface_unit(name) is not None:
                    outcome.errors.append(f"L2VPN routed interface 必须引用 BVI，不能引用 {name}")
                elif name not in blocks:
                    outcome.errors.append(f"L2VPN routed interface 引用不存在的 BVI: {name}")
                continue
            attachment = re.match(r"^interface\s+(.+)$", node.header, re.IGNORECASE)
            if attachment:
                name = document.resolve_interface(attachment.group(1))
                target = blocks.get(name)
                if target is not None and not _is_l2(target):
                    outcome.errors.append(f"L2VPN 接入接口 {name} 必须配置 l2transport")
                elif target is None and _in_scope(name, parents):
                    outcome.errors.append(f"L2VPN 引用不存在的目标接入接口: {name}")
    outcome.errors = list(dict.fromkeys(outcome.errors))
    outcome.warnings = list(dict.fromkeys(outcome.warnings))
    return outcome
