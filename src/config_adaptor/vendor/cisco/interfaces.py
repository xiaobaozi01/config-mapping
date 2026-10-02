"""Cisco IOS XR 接口分析与修改函数。"""

from __future__ import annotations

import copy
import re
from typing import Any

from ...parsers.cisco_iosxr import (
    CiscoBlock,
    CiscoDocument,
    _cisco_interface_kind,
    canonical_cisco_interface,
)
from ...parsers.common import InterfaceKind, InterfaceSpec, interface_parent, interface_unit


def interface_blocks(document: CiscoDocument) -> list[CiscoBlock]:
    return [
        block
        for block in document.blocks
        if block.active and block.interface_name
    ]


def interface_specs(document: CiscoDocument) -> list[InterfaceSpec]:
    result: list[InterfaceSpec] = []
    for block in interface_blocks(document):
        name = block.interface_name
        assert name is not None
        vlan = None
        inner_vlan = None
        for line in block.lines:
            match = re.match(
                r"\s*encapsulation\s+dot1q\s+(\d+)"
                r"(?:\s+second-dot1q\s+(\d+))?",
                line,
                re.IGNORECASE,
            )
            if match:
                vlan = int(match.group(1))
                inner_vlan = int(match.group(2)) if match.group(2) else None
                break
        result.append(
            InterfaceSpec(
                name=name,
                parent=interface_parent(name),
                unit=interface_unit(name),
                vlan=vlan,
                kind=_cisco_interface_kind(name),
                inner_vlan=inner_vlan,
            )
        )

    # BVI 编号只是接口标识，不能当作 VLAN。仅当同一 bridge-domain 的
    # attachment circuit 给出唯一明确的标签时，才把它作为网关的内层业务 VLAN。
    gateway_hints = _gateway_inner_vlan_hints(document, result)
    for spec in result:
        if spec.kind == InterfaceKind.GATEWAY:
            spec.inner_vlan = gateway_hints.get(spec.name)
    return result


def _descendants(node: Any) -> list[Any]:
    result: list[Any] = []
    for child in node.children:
        result.append(child)
        result.extend(_descendants(child))
    return result


def _bridge_domain_bindings(
    document: CiscoDocument,
) -> list[tuple[set[str], set[str]]]:
    """返回每个 bridge-domain 中的 attachment circuit 与 routed interface。"""
    result: list[tuple[set[str], set[str]]] = []
    for block in document.blocks:
        if not block.active or not re.match(
            r"^l2vpn\b",
            block.header.strip(),
            re.IGNORECASE,
        ):
            continue
        for node in document._parse_cisco_nodes(block.lines):
            stack = [node]
            while stack:
                current = stack.pop()
                stack.extend(current.children)
                if not re.match(r"^bridge-domain\b", current.command, re.IGNORECASE):
                    continue
                attachments: set[str] = set()
                gateways: set[str] = set()
                for child in _descendants(current):
                    gateway_match = re.match(
                        r"^routed\s+interface\s+(.+)$",
                        child.command,
                        re.IGNORECASE,
                    )
                    if gateway_match:
                        gateways.add(canonical_cisco_interface(gateway_match.group(1)))
                        continue
                    interface_match = re.match(
                        r"^interface\s+(.+)$",
                        child.command,
                        re.IGNORECASE,
                    )
                    if interface_match:
                        attachments.add(canonical_cisco_interface(interface_match.group(1)))
                result.append((attachments, gateways))
    return result


def _gateway_inner_vlan_hints(
    document: CiscoDocument,
    specs: list[InterfaceSpec],
) -> dict[str, int]:
    """从显式 bridge-domain 关系推导唯一的 BVI 内层业务 VLAN。"""
    spec_by_name = {spec.name: spec for spec in specs}
    hints: dict[str, set[int]] = {}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    for attachments, gateways in _bridge_domain_bindings(document):
        service_vlans: set[int] = set()
        for attachment in attachments:
            attachment_spec = spec_by_name.get(attachment)
            if attachment_spec is None or attachment_spec.kind not in mappable_kinds:
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


def interface_kind(document: CiscoDocument, name: str) -> InterfaceKind:
    return _cisco_interface_kind(canonical_cisco_interface(name))


def bundle_members(document: CiscoDocument) -> dict[str, str]:
    result: dict[str, str] = {}
    for block in interface_blocks(document):
        name = block.interface_name
        assert name is not None
        if _cisco_interface_kind(name) != InterfaceKind.PHYSICAL or interface_unit(name) is not None:
            continue
        for line in block.lines:
            match = re.match(r"\s*bundle\s+id\s+(\d+)\b", line, re.IGNORECASE)
            if match:
                result[name] = f"Bundle-Ether{match.group(1)}"
                break
    return result


def resolve_interface(document: CiscoDocument, value: str) -> str:
    return canonical_cisco_interface(value)


def logical_names_under(document: CiscoDocument, parent: str) -> list[str]:
    parent = canonical_cisco_interface(parent)
    return sorted(
        {spec.name for spec in interface_specs(document) if spec.parent == parent},
        key=lambda value: (interface_unit(value) is not None, value),
    )


def business_interface_names(document: CiscoDocument) -> set[str]:
    specs = interface_specs(document)
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active: set[str] = set()
    direct = re.compile(
        r"^(?:ipv4\s+address|ipv6\s+address|xconnect\b|l2transport\b|"
        r"bridge-domain\b|l2vpn\b|ethernet-services\b)",
        re.IGNORECASE,
    )
    for block in interface_blocks(document):
        name = block.interface_name
        if (
            name
            and kinds.get(name) in mappable_kinds
            and (block.l2transport or any(direct.match(line.strip()) for line in block.lines))
        ):
            active.add(name)

    external = "\n".join(
        text
        for block in document.blocks
        if block.active and not block.interface_name
        for text in [block.header, *block.lines]
    )
    for name in known:
        if kinds.get(name) in mappable_kinds and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
            external,
        ):
            active.add(name)
    for parent in {spec.parent for spec in specs}:
        parent_specs = [spec for spec in specs if spec.parent == parent]
        if parent_specs and parent_specs[0].kind in mappable_kinds and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(parent)}(?![A-Za-z0-9_.-])",
            external,
        ):
            active.update(spec.name for spec in parent_specs)

    for attachments, gateways in _bridge_domain_bindings(document):
        if attachments & active:
            active.update(gateway for gateway in gateways if gateway in known)
    return active


def find_interface_block(document: CiscoDocument, name: str) -> CiscoBlock | None:
    canonical = canonical_cisco_interface(name)
    return next(
        (
            block
            for block in interface_blocks(document)
            if block.interface_name == canonical
        ),
        None,
    )


def remove_interface(
    document: CiscoDocument,
    name: str,
    include_children: bool = False,
) -> None:
    canonical = canonical_cisco_interface(name)
    for block in interface_blocks(document):
        current = block.interface_name
        if current == canonical or (
            include_children and current and current.startswith(canonical + ".")
        ):
            block.active = False


def rename_interface_tree(
    document: CiscoDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    source = canonical_cisco_interface(source)
    target = canonical_cisco_interface(target)
    for block in list(interface_blocks(document)):
        current = block.interface_name
        if current != source and not (current and current.startswith(source + ".")):
            continue
        suffix = current[len(source) :] if current else ""
        new_name = target + suffix
        block.header = f"interface {new_name}" + (
            " l2transport" if block.l2transport else ""
        )
        if strip_bundle:
            block.lines = [
                line
                for line in block.lines
                if not re.match(
                    r"\s*(?:bundle\b|lacp\b|aggregated-)",
                    line,
                    re.IGNORECASE,
                )
            ]
        _merge_duplicate_interface(document, block)


def clone_interface_tree(
    document: CiscoDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    source = canonical_cisco_interface(source)
    target = canonical_cisco_interface(target)
    originals = [
        block
        for block in interface_blocks(document)
        if block.interface_name == source
        or (block.interface_name and block.interface_name.startswith(source + "."))
    ]
    if not originals:
        document.blocks.append(
            CiscoBlock(header=f"interface {target}", lines=[" no shutdown"])
        )
        document.blocks.append(CiscoBlock(header="!"))
        return
    insert_at = max(document.blocks.index(block) for block in originals) + 1
    clones: list[CiscoBlock] = []
    for original in originals:
        clone = copy.deepcopy(original)
        current = original.interface_name or source
        new_name = target + current[len(source) :]
        clone.header = f"interface {new_name}" + (
            " l2transport" if original.l2transport else ""
        )
        if strip_bundle:
            clone.lines = [
                line
                for line in clone.lines
                if not re.match(
                    r"\s*(?:bundle\b|lacp\b|aggregated-)",
                    line,
                    re.IGNORECASE,
                )
            ]
        clones.extend([clone, CiscoBlock(header="!")])
    document.blocks[insert_at:insert_at] = clones
    for clone in clones:
        if clone.interface_name:
            _merge_duplicate_interface(document, clone)


def _merge_duplicate_interface(
    document: CiscoDocument,
    preferred: CiscoBlock,
) -> None:
    name = preferred.interface_name
    duplicates = [
        block for block in interface_blocks(document) if block.interface_name == name
    ]
    if len(duplicates) < 2:
        return
    merged: list[str] = []
    for block in duplicates:
        for line in block.lines:
            if line not in merged:
                merged.append(line)
        if block is not preferred:
            block.active = False
    preferred.lines = merged


def map_uni(
    document: CiscoDocument,
    source: str,
    target_parent: str,
    vlan: int,
    inner_vlan: int,
) -> str:
    source = canonical_cisco_interface(source)
    target = f"{canonical_cisco_interface(target_parent)}.{vlan}"
    block = find_interface_block(document, source)
    if not block:
        return target
    block.header = f"interface {target}" + (
        " l2transport" if block.l2transport else ""
    )
    filtered = [
        line
        for line in block.lines
        if not re.match(
            r"\s*(?:encapsulation\b|rewrite\b|bundle\b|lacp\b)",
            line,
            re.IGNORECASE,
        )
    ]
    insertion = (
        1
        if filtered and re.match(r"\s*description\b", filtered[0], re.IGNORECASE)
        else 0
    )
    filtered.insert(
        insertion,
        f" encapsulation dot1q {vlan} second-dot1q {inner_vlan}",
    )
    block.lines = filtered
    _merge_duplicate_interface(document, block)
    return target


def ensure_parent_interface(document: CiscoDocument, name: str) -> None:
    canonical = canonical_cisco_interface(name)
    if find_interface_block(document, canonical):
        return
    document.blocks.append(
        CiscoBlock(header=f"interface {canonical}", lines=[" no shutdown"])
    )
    document.blocks.append(CiscoBlock(header="!"))
