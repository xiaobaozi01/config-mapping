"""Juniper Junos 接口业务关联分析。"""

from __future__ import annotations

import re

from ....common.domain.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    referenced_interface_names,
)
from ...document import JuniperNode, JunosDocument
from .access import (
    interface_name,
    interface_nodes,
    unit_nodes,
    unit_number,
    walk_effective_nodes,
)

MAX_VLAN_ID = 4094

Scope = tuple[str, ...]
ScopedVlan = tuple[Scope, int]
ScopedVlanName = tuple[Scope, str]


def interface_has_business(document: JunosDocument, name: str) -> bool:
    """判断物理聚合成员是否包含独立业务。

    unit、family、地址、广播域和 VLAN 都会阻止成员压缩。
    """
    parent = document._parser.canonical_interface(name)
    if any(
        spec.parent == parent and spec.name != parent
        for spec in document.interfaces.analysis.interface_specs()
    ):
        return True
    node = document.interfaces.analysis.resolve_interface(parent)
    from .access import find_interface_node

    node = find_interface_node(document, node)
    if node is None:
        return False
    return any(
        re.match(
            r"^(?:unit|family|address|unnumbered-address|"
            r"bridge-domains|vlan-id)\b",
            document._parser.base_header(child.header),
        )
        for child in node.nodes
        if document._parser.is_effective(child)
    )


def bundle_binding_is_ambiguous(document: JunosDocument, name: str) -> bool:
    """判断物理成员是否声明了多个不同 ae 绑定。

    多个绑定无法唯一映射。
    """
    from .access import find_interface_node

    node = find_interface_node(
        document, document._parser.canonical_interface(name)
    )
    if node is None:
        return False
    bindings = {
        match.group(1)
        for child in walk_effective_nodes(document, node)
        if (
            match := re.match(
                r"802\.3ad\s+(ae\d+)\b",
                document._parser.base_header(child.header),
            )
        )
    }
    return len(bindings) > 1


def business_interface_names(document: JunosDocument) -> set[str]:
    """识别实际承载三层、二层或活跃网关业务的 Junos 接口。

    接口自身业务、外部引用和活跃广播域网关共同决定结果。
    """
    specs = document.interfaces.analysis.interface_specs()
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active = collect_interface_business(document, kinds, mappable_kinds)
    active.update(collect_reference_business(document, specs, known, kinds))

    scopes = interface_business_scopes(document)
    active_vlans, active_vlan_names = collect_active_vlans(
        document, active, specs, scopes
    )

    activate_domain_gateways(
        document,
        document.root,
        active,
        active_vlans,
        active_vlan_names,
        known,
    )
    for spec in specs:
        if spec.kind == InterfaceKind.GATEWAY and any(
            (scope, spec.vlan) in active_vlans
            for scope in scopes.get(spec.name, {()})
        ):
            active.add(spec.name)
    return active


def collect_interface_business(
    document: JunosDocument,
    kinds: dict[str, InterfaceKind],
    mappable_kinds: set[InterfaceKind],
) -> set[str]:
    """根据接口自身有效业务语句识别活跃接口。

    父口业务传播到 unit，独立 unit 只按自身内容判断。
    """
    parser = document._parser
    active: set[str] = set()
    business_pattern = re.compile(
        r"\b(?:family\s+(?:inet6?|ccc|bridge|ethernet-switching)|"
        r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
        r"input-vlan-map|output-vlan-map)\b"
    )
    for node in interface_nodes(document):
        parent = interface_name(document, node)
        units = unit_nodes(document, node)
        if not units:
            rendered = document.writer.effective(node, document._parser)
            if document._parser.interface_kind(
                parent
            ) in mappable_kinds and business_pattern.search(rendered):
                active.add(parent)
            continue
        parent_business = any(
            parser.is_effective(child)
            and child not in units
            and business_pattern.search(
                document.writer.effective(child, document._parser)
            )
            for child in node.nodes
        )
        for unit in units:
            rendered = document.writer.effective(unit, document._parser)
            name = f"{parent}.{unit_number(document, unit)}"
            if kinds.get(name) in mappable_kinds and (
                parent_business or business_pattern.search(rendered)
            ):
                active.add(name)
    return active


def collect_reference_business(
    document: JunosDocument,
    specs: list[InterfaceSpec],
    known: set[str],
    kinds: dict[str, InterfaceKind],
) -> set[str]:
    """找出被接口树之外有效配置引用的业务接口。

    路由和 VPN 等全局配置也必须参与业务闭包判断。
    """
    parser = document._parser
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    external = "\n".join(
        document.writer.effective(node, document._parser)
        for node in document.root.nodes
        if parser.is_effective(node)
        and parser.base_header(node.header) not in {"interfaces", "groups"}
    )
    referenced = referenced_interface_names(
        external, known | {spec.parent for spec in specs}
    )
    active = {
        name for name in known & referenced if kinds.get(name) in mappable_kinds
    }
    specs_by_parent: dict[str, list[InterfaceSpec]] = {}
    for spec in specs:
        specs_by_parent.setdefault(spec.parent, []).append(spec)
    for parent, parent_specs in specs_by_parent.items():
        if parent in referenced and parent_specs[0].kind in mappable_kinds:
            active.update(spec.name for spec in parent_specs)
    return active


def expand_vlan_tokens(value: str) -> set[int]:
    """把 VLAN 单值和闭区间展开为合法 VLAN 集合。

    只接受 1 到 4094 且起点不大于终点的值。
    """
    result: set[int] = set()
    for start, end, single in re.findall(r"(?:(\d+)\s*-\s*(\d+))|(\d+)", value):
        if single:
            number = int(single)
            if 1 <= number <= MAX_VLAN_ID:
                result.add(number)
            continue
        lower, upper = int(start), int(end)
        if 1 <= lower <= upper <= MAX_VLAN_ID:
            result.update(range(lower, upper + 1))
    return result


def activate_domain_gateways(
    document: JunosDocument,
    node: JuniperNode,
    active: set[str],
    active_vlans: set[ScopedVlan],
    active_vlan_names: set[ScopedVlanName],
    known: set[str],
) -> None:
    """递归查找活跃广播域并激活其已知三层网关。

    只有接口、VLAN ID 或 VLAN 名与活跃二层业务匹配时才激活网关。
    """
    parser = document._parser
    if not node.is_block:
        return
    base = parser.base_header(node.header).rstrip(";")
    if base in {"bridge-domains", "vlans"}:
        scope = configuration_scope(document, node)
        for domain in node.nodes:
            if not parser.is_effective(domain) or not domain.is_block:
                continue
            interfaces, gateways, vlan_ids, domain_name = domain_gateways(
                document, domain
            )
            if (
                interfaces & active
                or any((scope, vlan) in active_vlans for vlan in vlan_ids)
                or (scope, domain_name) in active_vlan_names
            ):
                active.update(
                    gateway for gateway in gateways if gateway in known
                )
    for child in node.nodes:
        if parser.is_effective(child):
            activate_domain_gateways(
                document,
                child,
                active,
                active_vlans,
                active_vlan_names,
                known,
            )


def domain_gateways(
    document: JunosDocument,
    domain: JuniperNode,
) -> tuple[set[str], set[str], set[int], str]:
    """提取广播域的接入接口、网关、VLAN ID 和域名。

    所有字段来自同一份规范化渲染文本。
    """
    parser = document._parser
    rendered = document.writer.effective(domain, document._parser)
    interfaces = {
        document._parser.canonical_interface(value)
        for value in re.findall(
            r"(?<![-\w])interface\s+([^;\s]+)\s*;",
            rendered,
        )
    }
    gateways = {
        document._parser.canonical_interface(value)
        for value in re.findall(
            r"\b(?:routing-interface|l3-interface)\s+([^;\s]+)\s*;",
            rendered,
        )
    }
    vlan_ids = {
        int(value)
        for value in re.findall(r"\bvlan-id\s+(\d+)\s*;", rendered)
        if 1 <= int(value) <= MAX_VLAN_ID
    }
    domain_name = parser.base_header(domain.header).split()[0].rstrip(";")
    return interfaces, gateways, vlan_ids, domain_name


def configuration_scope(document: JunosDocument, node: JuniperNode) -> Scope:
    """返回配置节点所属逻辑系统及路由实例的完整路径。

    默认实例用空元组表示，同名实例在不同逻辑系统中仍能区分。
    """
    parser = document._parser
    parts: list[str] = []
    current = node
    while current.parent is not None:
        parent = current.parent
        container = parser.base_header(parent.header).rstrip(";")
        if container in {"logical-systems", "routing-instances"}:
            parts.append(parser.base_header(current.header).rstrip(";"))
            parts.append(container)
        current = parent
    return tuple(reversed(parts))


def interface_business_scopes(document: JunosDocument) -> dict[str, set[Scope]]:
    """从有效外部接口引用建立接口所属实例索引。

    接入口和 IRB 使用同一作用域定义，避免跨实例的 VLAN 关联。
    """
    parser = document._parser
    scopes: dict[str, set[Scope]] = {}
    for block in document.root.nodes:
        if not parser.is_effective(block) or parser.base_header(
            block.header
        ) in {"interfaces", "groups"}:
            continue
        for node in walk_effective_nodes(document, block):
            match = re.fullmatch(
                r"(?:interface|routing-interface|l3-interface)\s+([^;\s]+)\s*;?",
                parser.base_header(node.header),
            )
            if match:
                name = parser.canonical_interface(match.group(1))
                scopes.setdefault(name, set()).add(
                    configuration_scope(document, node)
                )
    return scopes


def collect_active_vlans(
    document: JunosDocument,
    active: set[str],
    specs: list[InterfaceSpec],
    scopes: dict[str, set[Scope]] | None = None,
) -> tuple[set[ScopedVlan], set[ScopedVlanName]]:
    """从活跃二层接口收集带实例作用域的 VLAN ID 和名称。

    结果用于广播域网关激活。
    """
    spec_by_name = {spec.name: spec for spec in specs}
    active_vlans: set[ScopedVlan] = set()
    active_vlan_names: set[ScopedVlanName] = set()
    scopes = interface_business_scopes(document) if scopes is None else scopes
    parents, units = interface_index(document)
    for name in list(active):
        spec = spec_by_name.get(name)
        if not spec or spec.kind == InterfaceKind.GATEWAY:
            continue
        parent = interface_parent(
            document._parser.canonical_interface(spec.parent)
        )
        node = parents.get(parent)
        if not node:
            continue
        unit = node if spec.unit is None else units.get((parent, spec.unit))
        if unit is None:
            continue
        rendered = document.writer.effective(unit, document._parser)
        vlans: set[int] = set()
        names: set[str] = set()
        update_active_vlans(rendered, spec.vlan, vlans, names)
        for scope in scopes.get(name, scopes.get(parent, {()})):
            active_vlans.update((scope, vlan) for vlan in vlans)
            active_vlan_names.update((scope, value) for value in names)
    return active_vlans, active_vlan_names


def interface_index(
    document: JunosDocument,
) -> tuple[dict[str, JuniperNode], dict[tuple[str, str], JuniperNode]]:
    """建立父接口和 unit 的查找索引。

    单次遍历避免逐个活跃 unit 重复线性查找。
    """
    parents: dict[str, JuniperNode] = {}
    units: dict[tuple[str, str], JuniperNode] = {}
    for node in interface_nodes(document):
        parent = interface_name(document, node)
        if parent in parents:
            continue
        parents[parent] = node
        for unit in unit_nodes(document, node):
            units.setdefault((parent, unit_number(document, unit)), unit)
    return parents, units


def update_active_vlans(
    rendered: str,
    own_vlan: int | None,
    active_vlans: set[int],
    active_vlan_names: set[str],
) -> None:
    """从一个活跃接口文本提取 VLAN ID 和名称。

    只解析二层终结和 VLAN 成员语句。
    """
    is_l2 = bool(
        re.search(
            r"\b(?:family\s+(?:ccc|bridge|ethernet-switching)|"
            r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
            r"vlan-id-list|vlan\s+members|input-vlan-map|output-vlan-map)\b",
            rendered,
        )
    )
    if is_l2 and own_vlan is not None:
        active_vlans.add(own_vlan)
    for match in re.finditer(r"vlan-id-list\s+\[([^\]]+)\]", rendered):
        active_vlans.update(expand_vlan_tokens(match.group(1)))
    for match in re.finditer(
        r"\bvlan\s+(?:\{\s*)?members\s+(?:\[([^\]]+)\]|([^;\s]+))\s*;",
        rendered,
    ):
        payload = match.group(1) or match.group(2) or ""
        active_vlans.update(expand_vlan_tokens(payload))
        active_vlan_names.update(
            token
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload)
            if not token.isdigit()
        )
