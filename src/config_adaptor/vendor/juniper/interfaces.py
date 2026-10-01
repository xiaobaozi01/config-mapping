"""Juniper Junos 接口分析与修改函数。"""

from __future__ import annotations

import re

from ...parsers.common import InterfaceKind, InterfaceSpec, interface_parent, interface_unit
from ...parsers.juniper_junos import (
    JunosDocument,
    JunosNode,
    _junos_interface_kind,
    canonical_junos_interface,
)


def interface_specs(document: JunosDocument) -> list[InterfaceSpec]:
    result: list[InterfaceSpec] = []
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        units = document._unit_nodes(node)
        if not units:
            result.append(
                InterfaceSpec(parent, parent, None, None, _junos_interface_kind(parent))
            )
            continue
        for unit in units:
            number = document._unit_number(unit)
            vlan = document._find_vlan(unit)
            if (
                vlan is None
                and parent.lower() == "irb"
                and number.isdigit()
                and 1 <= int(number) <= 4094
            ):
                vlan = int(number)
            result.append(
                InterfaceSpec(
                    name=f"{parent}.{number}",
                    parent=parent,
                    unit=number,
                    vlan=vlan,
                    kind=_junos_interface_kind(parent),
                    inner_vlan=document._find_inner_vlan(unit),
                )
            )
    return result


def interface_kind(document: JunosDocument, name: str) -> InterfaceKind:
    return _junos_interface_kind(canonical_junos_interface(name))


def bundle_members(document: JunosDocument) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        if _junos_interface_kind(parent) != InterfaceKind.PHYSICAL:
            continue
        rendered = document._render_node(node, 0)
        match = re.search(r"\b802\.3ad\s+(ae\d+)\s*;", rendered)
        if match:
            result[parent] = match.group(1)
    return result


def resolve_interface(document: JunosDocument, value: str) -> str:
    return canonical_junos_interface(value)


def logical_names_under(document: JunosDocument, parent: str) -> list[str]:
    parent = canonical_junos_interface(parent)
    return sorted(
        {spec.name for spec in interface_specs(document) if spec.parent == parent},
        key=lambda value: (interface_unit(value) is not None, value),
    )


def business_interface_names(document: JunosDocument) -> set[str]:
    specs = interface_specs(document)
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active: set[str] = set()
    business_pattern = re.compile(
        r"\b(?:family\s+(?:inet6?|ccc|bridge|ethernet-switching)|"
        r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|input-vlan-map|output-vlan-map)\b"
    )
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        units = document._unit_nodes(node)
        if not units:
            rendered = document._render_node(node, 0)
            if _junos_interface_kind(parent) in mappable_kinds and business_pattern.search(rendered):
                active.add(parent)
            continue
        for unit in units:
            rendered = document._render_node(unit, 0)
            name = f"{parent}.{document._unit_number(unit)}"
            if kinds.get(name) in mappable_kinds and business_pattern.search(rendered):
                active.add(name)

    external = "\n".join(
        document._render_node(node, 0)
        for node in (document.root.children or [])
        if node.active
        and document._base_header(node.header) not in {"interfaces", "groups"}
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

    spec_by_name = {spec.name: spec for spec in specs}
    active_vlans: set[int] = set()
    active_vlan_names: set[str] = set()
    for name in list(active):
        spec = spec_by_name.get(name)
        if not spec or spec.kind == InterfaceKind.GATEWAY:
            continue
        node = find_interface_node(document, spec.parent)
        if not node:
            continue
        units = document._unit_nodes(node)
        unit = next(
            (
                item
                for item in units
                if spec.unit is not None
                and document._unit_number(item) == spec.unit
            ),
            node if spec.unit is None else None,
        )
        if unit is None:
            continue
        rendered = document._render_node(unit, 0)
        is_l2 = bool(
            re.search(
                r"\b(?:family\s+(?:ccc|bridge|ethernet-switching)|"
                r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
                r"vlan-id-list|vlan\s+members|input-vlan-map|output-vlan-map)\b",
                rendered,
            )
        )
        if is_l2 and spec.vlan is not None:
            active_vlans.add(spec.vlan)
        for match in re.finditer(r"vlan-id-list\s+\[([^\]]+)\]", rendered):
            active_vlans.update(_expand_vlan_tokens(match.group(1)))
        for match in re.finditer(
            r"vlan\s+members\s+(?:\[([^\]]+)\]|([^;\s]+))\s*;",
            rendered,
        ):
            payload = match.group(1) or match.group(2) or ""
            active_vlans.update(_expand_vlan_tokens(payload))
            active_vlan_names.update(
                token
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload)
                if not token.isdigit()
            )

    _activate_domain_gateways(
        document,
        document.root,
        active,
        active_vlans,
        active_vlan_names,
        known,
    )
    for spec in specs:
        if spec.kind == InterfaceKind.GATEWAY and spec.vlan in active_vlans:
            active.add(spec.name)
    return active


def _expand_vlan_tokens(value: str) -> set[int]:
    result: set[int] = set()
    for start, end, single in re.findall(r"(?:(\d+)\s*-\s*(\d+))|(\d+)", value):
        if single:
            number = int(single)
            if 1 <= number <= 4094:
                result.add(number)
            continue
        lower, upper = int(start), int(end)
        if 1 <= lower <= upper <= 4094:
            result.update(range(lower, upper + 1))
    return result


def _activate_domain_gateways(
    document: JunosDocument,
    node: JunosNode,
    active: set[str],
    active_vlans: set[int],
    active_vlan_names: set[str],
    known: set[str],
) -> None:
    if node.children is None:
        return
    base = document._base_header(node.header).rstrip(";")
    if base in {"bridge-domains", "vlans"}:
        for domain in node.children:
            if not domain.active or domain.children is None:
                continue
            rendered = document._render_node(domain, 0)
            interfaces = {
                canonical_junos_interface(value)
                for value in re.findall(
                    r"(?<![-\w])interface\s+([^;\s]+)\s*;",
                    rendered,
                )
            }
            gateways = {
                canonical_junos_interface(value)
                for value in re.findall(
                    r"\b(?:routing-interface|l3-interface)\s+([^;\s]+)\s*;",
                    rendered,
                )
            }
            vlan_ids = {
                int(value)
                for value in re.findall(r"\bvlan-id\s+(\d+)\s*;", rendered)
                if 1 <= int(value) <= 4094
            }
            domain_name = document._base_header(domain.header).split()[0].rstrip(";")
            if (
                interfaces & active
                or vlan_ids & active_vlans
                or domain_name in active_vlan_names
            ):
                active.update(gateway for gateway in gateways if gateway in known)
    for child in node.children:
        if child.active:
            _activate_domain_gateways(
                document,
                child,
                active,
                active_vlans,
                active_vlan_names,
                known,
            )


def find_interface_node(document: JunosDocument, name: str) -> JunosNode | None:
    canonical = canonical_junos_interface(interface_parent(name))
    return next(
        (
            node
            for node in document._interface_nodes()
            if document._interface_name(node) == canonical
        ),
        None,
    )


def remove_interface(document: JunosDocument, name: str, include_children: bool = False) -> None:
    node = find_interface_node(document, name)
    if node:
        node.active = False


def rename_interface_tree(
    document: JunosDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    node = find_interface_node(document, source)
    if not node:
        return
    node.header = canonical_junos_interface(target)
    if strip_bundle and node.children is not None:
        node.children = [
            child
            for child in node.children
            if not (
                child.is_block
                and document._base_header(child.header)
                in {"aggregated-ether-options", "gigether-options", "ether-options"}
            )
        ]
    _merge_duplicate_interface(document, node)


def clone_interface_tree(
    document: JunosDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    source_node = find_interface_node(document, source)
    interfaces = document._interfaces_block(create=True)
    assert interfaces is not None and interfaces.children is not None
    if source_node is None:
        clone = JunosNode(canonical_junos_interface(target), [])
    else:
        clone = source_node.clone()
        clone.header = canonical_junos_interface(target)
        clone.active = True
        if strip_bundle and clone.children is not None:
            clone.children = [
                child
                for child in clone.children
                if not (
                    child.is_block
                    and document._base_header(child.header)
                    in {"aggregated-ether-options", "gigether-options", "ether-options"}
                )
            ]
    interfaces.children.append(clone)
    _merge_duplicate_interface(document, clone)


def _merge_duplicate_interface(document: JunosDocument, preferred: JunosNode) -> None:
    name = document._interface_name(preferred)
    duplicates = [
        node
        for node in document._interface_nodes()
        if document._interface_name(node) == name
    ]
    if len(duplicates) < 2:
        return
    assert preferred.children is not None
    existing = {
        document._render_node(child, 0) for child in preferred.children
    }
    for node in duplicates:
        if node is preferred or node.children is None:
            continue
        for child in node.children:
            rendered = document._render_node(child, 0)
            if rendered not in existing:
                preferred.children.append(child)
                existing.add(rendered)
        node.active = False


def _strip_vlan_termination(document: JunosDocument, nodes: list[JunosNode]) -> list[JunosNode]:
    blocked = re.compile(
        r"^(?:vlan-id(?:-list)?|vlan-tags|native-vlan-id|"
        r"input-vlan-map|output-vlan-map|interface-mode|"
        r"flexible-vlan-tagging|stacked-vlan-tagging|vlan-tagging)\b"
    )
    retained: list[JunosNode] = []
    for node in nodes:
        base = document._base_header(node.header).rstrip(";")
        if blocked.match(base) or re.match(r"^vlan\s+members\b", base):
            continue
        if node.is_block and base == "vlan":
            continue
        clone = node.clone()
        if clone.children is not None:
            clone.children = _strip_vlan_termination(document, clone.children)
        retained.append(clone)
    return retained


def _ensure_target_parent(document: JunosDocument, target_parent: str) -> JunosNode:
    existing = find_interface_node(document, target_parent)
    if existing:
        node = existing
    else:
        interfaces = document._interfaces_block(create=True)
        assert interfaces is not None and interfaces.children is not None
        node = JunosNode(target_parent, [])
        interfaces.children.append(node)
    assert node.children is not None
    node.children = [
        child
        for original in node.children
        for child in (
            [original]
            if original.is_block
            and document._base_header(original.header).startswith("unit ")
            else _strip_vlan_termination(document, [original])
        )
    ]
    node.children = [
        child
        for child in node.children
        if not re.match(
            r"^encapsulation\s+(?:ethernet-bridge|vlan-bridge)\b",
            document._base_header(child.header),
        )
    ]
    required = ["flexible-vlan-tagging;", "encapsulation flexible-ethernet-services;"]
    existing_headers = {
        document._base_header(child.header) for child in node.children
    }
    for statement in reversed(required):
        if statement not in existing_headers:
            node.children.insert(0, JunosNode(statement))
    return node


def map_uni(
    document: JunosDocument,
    source: str,
    target_parent: str,
    vlan: int,
    inner_vlan: int,
) -> str:
    source_parent = interface_parent(canonical_junos_interface(source))
    unit_number = interface_unit(source)
    source_node = find_interface_node(document, source_parent)
    target_node = _ensure_target_parent(document, target_parent)
    assert target_node.children is not None
    unit_node = None
    if source_node:
        units = document._unit_nodes(source_node)
        if unit_number is not None:
            unit_node = next(
                (
                    item
                    for item in units
                    if document._unit_number(item) == unit_number
                ),
                None,
            )
        elif len(units) == 1:
            unit_node = units[0]
        elif not units:
            payload = [child.clone() for child in (source_node.children or [])]
            unit_node = JunosNode("unit 0", payload)
    if unit_node is None:
        unit_node = JunosNode("unit 0", [])
    else:
        unit_node = unit_node.clone()
    unit_node.header = f"unit {vlan}"
    assert unit_node.children is not None
    unit_node.children = _strip_vlan_termination(document, unit_node.children)
    unit_node.children.insert(
        0,
        JunosNode(f"vlan-tags outer {vlan} inner {inner_vlan};"),
    )
    target_node.children.append(unit_node)
    return f"{target_parent}.{vlan}"


def ensure_parent_interface(document: JunosDocument, name: str) -> None:
    _ensure_target_parent(document, name)
