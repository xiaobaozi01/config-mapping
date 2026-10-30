"""删除未连接 NNI 的 Junos 数据接口及能够明确归属的依赖。"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

from ..common.interface import InterfaceKind, interface_parent, referenced_interface_names
from ..common.outcomes import InterfacePruningOutcome
from .interfaces import _collect_active_vlans, _expand_vlan_tokens

if TYPE_CHECKING:
    from .document import JunosDocument, JunosNode


def _walk(document: JunosDocument, node: JunosNode, path: tuple[str, ...] = ()):
    """仅遍历活动配置，路径保留厂商语法用于报告。"""
    for child in node.children or []:
        if not document._parser.is_effective(child):
            continue
        header = document._parser.base_header(child.header).rstrip(";")
        child_path = (*path, header)
        yield child, child_path
        yield from _walk(document, child, child_path)


def _address(value: str) -> str | None:
    try:
        return str(ipaddress.ip_interface(value.rstrip(";")).ip)
    except ValueError:
        return None


def _routing_scope(path: tuple[str, ...]) -> tuple[str, ...]:
    """default 与每个 routing-instance 的地址和 VLAN 名称空间独立。"""
    for index, header in enumerate(path[:-1]):
        if header == "routing-instances":
            return path[:index + 2]
    return ()


def prune_non_nni(
    document: JunosDocument, protected_parents: set[str]
) -> InterfacePruningOutcome:
    """在改名之前剪除非 NNI 数据口，保留依赖于 NNI 广播域的原生 IRB。"""
    result = InterfacePruningOutcome()
    protected = {interface_parent(document.resolve_interface(name)) for name in protected_parents}
    members = document.bundle_members()
    # 拓扑引用成员口时，聚合业务及其全部成员均留给 NNI 阶段处理。
    protected.update(bundle for member, bundle in members.items() if member in protected)
    protected.update(member for member, bundle in members.items() if bundle in protected)
    nodes = list(document._interface_nodes())
    specs = document.interface_specs()
    bindings: dict[str, set[tuple[str, ...]]] = {}
    for container, path in _walk(document, document.root):
        if path[-1] != "routing-instances":
            continue
        for instance in container.children or []:
            if not document._parser.is_effective(instance):
                continue
            scope = (*path, document._parser.base_header(instance.header).rstrip(";"))
            for child in instance.children or []:
                if not document._parser.is_effective(child):
                    continue
                header = document._parser.base_header(child.header).rstrip(";")
                match = re.fullmatch(r"interface\s+(.+)", header)
                if match:
                    for name in re.findall(r"[^\s\[\]]+", match.group(1)):
                        bindings.setdefault(name, set()).add(scope)

    def owners(name: str) -> set[tuple[str, ...]]:
        return bindings.get(name, bindings.get(interface_parent(name), {()}))

    payloads = {}
    source_addresses: dict[tuple[tuple[str, ...], str], set[str]] = {}
    for node in nodes:
        parent = document._interface_name(node)
        units = list(document._unit_nodes(node))
        for payload in units or [node]:
            name = f"{parent}.{document._unit_number(payload)}" if units else parent
            payloads[name] = payload
            for child, _ in _walk(document, payload):
                match = re.match(r"address\s+(\S+)", document._parser.base_header(child.header))
                if match and (address := _address(match.group(1))):
                    for scope in owners(name):
                        source_addresses.setdefault((scope, address), set()).add(name)
    kept_names = {spec.name for spec in specs if spec.parent in protected}
    kept_names.update(protected)
    data_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    discarded_names = {
        value for spec in specs
        if spec.kind in data_kinds and spec.parent not in protected
        for value in (spec.name, spec.parent)
    }

    vlan_cache: dict[tuple[tuple[str, ...], bool], tuple[set[int], set[str]]] = {}

    def vlans_for(scope: tuple[str, ...], discarded: bool = False) -> tuple[set[int], set[str]]:
        key = (scope, discarded)
        if key in vlan_cache:
            return vlan_cache[key]
        names = discarded_names if discarded else kept_names
        scoped = {name for name in names if scope in owners(name)}
        ids, names_for_vlans = _collect_active_vlans(document, scoped, specs)
        for name in scoped:
            node = payloads.get(name)
            if node is None:
                continue
            for _, path in _walk(document, node):
                match = re.fullmatch(r"members\s+(.+)", path[-1])
                if match and "vlan" in path[:-1]:
                    payload = match.group(1)
                    ids.update(_expand_vlan_tokens(payload))
                    names_for_vlans.update(re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload))
        vlan_cache[key] = (ids, names_for_vlans)
        return ids, names_for_vlans

    kept_gateways: set[str] = set()

    def record(node: JunosNode, path: tuple[str, ...], reason: str, names: set[str] | None = None) -> None:
        node.active = False
        result.changes.append({
            "action": "remove", "path": " / ".join(path), "reason": reason,
            "interfaces": sorted(names or set()),
        })

    # 广播域以保留的接入成员为锚点；混合域稍后只删除丢弃成员。
    domains = []
    for container, container_path in list(_walk(document, document.root)):
        if container_path[-1] not in {"bridge-domains", "vlans"}:
            continue
        for domain in container.children or []:
            if not document._parser.is_effective(domain):
                continue
            header = document._parser.base_header(domain.header).rstrip(";")
            path = (*container_path, header)
            scope = _routing_scope(path)
            vlan_ids, vlan_names = vlans_for(scope)
            discarded_vlan_ids, discarded_vlan_names = vlans_for(scope, discarded=True)
            statements = [document._parser.base_header(n.header).rstrip(";") for n, _ in _walk(document, domain)]
            explicit = set()
            gateways = set()
            ids = set()
            for statement in statements:
                match = re.fullmatch(r"interface\s+(.+)", statement)
                if match:
                    explicit.update(re.findall(r"[^\s\[\]]+", match.group(1)))
                match = re.fullmatch(r"(?:routing-interface|l3-interface)\s+(\S+)", statement)
                if match:
                    gateways.add(match.group(1))
                match = re.fullmatch(r"vlan-id\s+(\d+)", statement)
                if match:
                    ids.add(int(match.group(1)))
            # 显式接入成员给出本域的准确边界，不允许其他域/实例的同号 VLAN 激活它。
            active = bool(explicit & kept_names) if explicit else bool(
                ids & vlan_ids or header.strip('"') in vlan_names or "all" in vlan_names
            )
            if active:
                kept_gateways.update(gateways)
            domains.append((domain, path, explicit, gateways, ids, active,
                            discarded_vlan_ids, discarded_vlan_names))
    discarded_gateways = {spec.name for spec in specs if spec.kind == InterfaceKind.GATEWAY} - kept_gateways
    for domain, path, explicit, gateways, ids, active, discarded_vlan_ids, discarded_vlan_names in domains:
        depends_on_removed = bool(
            explicit & discarded_names or gateways & discarded_gateways
            or ids & discarded_vlan_ids or path[-1].strip('"') in discarded_vlan_names
        )
        if not active and depends_on_removed:
            record(domain, path, "广播域不再含保留的 NNI 接入成员", explicit)

    for node in nodes:
        parent = document._interface_name(node)
        kind = document.interface_kind(parent)
        units = list(document._unit_nodes(node))
        if kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE} and parent not in protected:
            removed = {parent, *(f"{parent}.{document._unit_number(unit)}" for unit in units)}
            result.removed_interfaces.update(removed)
            for child, _ in _walk(document, node):
                match = re.match(r"address\s+(\S+)", document._parser.base_header(child.header))
                if match and (address := _address(match.group(1))):
                    result.removed_addresses.add(address)
            record(node, ("interfaces", parent), "非 NNI 数据接口", removed)
        elif kind == InterfaceKind.GATEWAY:
            for unit in units:
                name = f"{parent}.{document._unit_number(unit)}"
                if name in kept_gateways:
                    continue
                result.removed_interfaces.add(name)
                for child, _ in _walk(document, unit):
                    match = re.match(r"address\s+(\S+)", document._parser.base_header(child.header))
                    if match and (address := _address(match.group(1))):
                        result.removed_addresses.add(address)
                record(unit, ("interfaces", parent, document._parser.base_header(unit.header)), "无保留 NNI 广播域依赖的 IRB", {name})
            if not any(document._parser.is_effective(unit) for unit in units):
                result.removed_interfaces.add(parent)
                record(node, ("interfaces", parent), "IRB 无保留逻辑单元", {parent})

    removed = result.removed_interfaces

    def references(node: JunosNode) -> set[str]:
        return referenced_interface_names(document._parser.base_header(node.header), removed)

    def subtree_references(node: JunosNode) -> set[str]:
        found = references(node)
        for child, _ in _walk(document, node):
            if not document._parser.base_header(child.header).startswith("description "):
                found.update(references(child))
        return found

    # 仅同一 routing-instance 中仍有该地址时才视为可用，不能借用别的 VRF 的同号地址。
    def lost_source(scope: tuple[str, ...], address: str | None) -> bool:
        return bool(
            address in result.removed_addresses
            and not (source_addresses.get((scope, address), set()) - removed)
        )

    for container, container_path in list(_walk(document, document.root)):
        if container_path[-1] != "routing-instances":
            continue
        for instance in container.children or []:
            if not document._parser.is_effective(instance):
                continue
            statements = [document._parser.base_header(n.header).rstrip(";") for n, _ in _walk(document, instance)]
            if "instance-type evpn-vpws" not in statements:
                continue
            interfaces = set()
            for statement in statements:
                match = re.fullmatch(r"interface\s+(.+)", statement)
                if match:
                    interfaces.update(re.findall(r"[^\s\[\]]+", match.group(1)))
            if interfaces and interfaces <= removed:
                record(instance, (*container_path, document._parser.base_header(instance.header)), "EVPN VPWS 的全部接入接口已删除", interfaces)

    def local_source(node: JunosNode):
        for child in node.children or []:
            if document._parser.is_effective(child):
                match = re.fullmatch(r"local-address\s+(\S+)", document._parser.base_header(child.header).rstrip(";"))
                if match:
                    return child, _address(match.group(1))
        return None, None

    for group, path in list(_walk(document, document.root)):
        if "bgp" not in path or not re.match(r"group\s+", path[-1]):
            continue
        scope = _routing_scope(path)
        default_node, default_address = local_source(group)
        default_lost = lost_source(scope, default_address)
        remaining = 0
        for neighbor in group.children or []:
            if not document._parser.is_effective(neighbor):
                continue
            header = document._parser.base_header(neighbor.header).rstrip(";")
            if re.match(r"neighbor\s+", header):
                own_node, own_address = local_source(neighbor)
                lost = lost_source(scope, own_address) if own_node else default_lost
                if lost:
                    record(neighbor, (*path, header), "BGP 本地源地址属于已删除接口")
                else:
                    remaining += 1
            elif default_lost and re.match(r"allow\b", header):
                record(neighbor, (*path, header), "动态 BGP 邻居继承已删除的本地源地址")
        if default_lost:
            if remaining:
                record(default_node, (*path, document._parser.base_header(default_node.header)),
                       "删除失效的组默认源，保留使用有效显式源的邻居")
            else:
                record(group, path, "BGP 组无使用有效源的保留邻居")

    def clean(node: JunosNode, path: tuple[str, ...] = ()) -> None:
        for child in node.children or []:
            if not document._parser.is_effective(child):
                continue
            header = document._parser.base_header(child.header).rstrip(";")
            child_path = (*path, header)
            if not path and header == "interfaces":
                # 接口定义已经处理，保留接口内部的未知依赖交给最终审计。
                continue
            refs = references(child)
            is_static = "static" in path and re.match(r"route\s+", header)
            if is_static and (route_refs := subtree_references(child)):
                record(child, child_path, "静态路由显式出接口已删除", route_refs)
                continue
            interface_match = re.fullmatch(r"interface\s+(.+)", header)
            known_interface_context = (
                "protocols" in path or "routing-instances" in path
                or "bridge-domains" in path or "vlans" in path
            )
            if refs and interface_match and known_interface_context:
                values = re.findall(r"[^\s\[\]]+", interface_match.group(1))
                retained = [value for value in values if value not in removed]
                if retained and child.children is None:
                    child.header = f"interface [ {' '.join(retained)} ];" if len(retained) > 1 else f"interface {retained[0]};"
                    result.changes.append({"action": "remove-members", "path": " / ".join(child_path), "interfaces": sorted(refs)})
                else:
                    record(child, child_path, "接口依赖已删除", refs)
                continue
            if refs and "class-of-service" in path and path[-1] == "interfaces":
                record(child, child_path, "接口 QoS 目标已删除", refs)
                continue
            clean(child, child_path)

    clean(document.root)
    result.errors.extend(_validate_pruned_references(document, removed).errors)
    return result


def validate_pruned_references(
    document: JunosDocument, removed: set[str]
) -> InterfacePruningOutcome:
    """只读复核最终引用；实际存在的目标接口可合法复用已删源口的旧名。"""
    actual_names = {spec.name for spec in document.interface_specs()}
    actual_names.update(document._interface_name(node) for node in document._interface_nodes())
    return _validate_pruned_references(document, removed - actual_names)


def _validate_pruned_references(
    document: JunosDocument, removed: set[str]
) -> InterfacePruningOutcome:
    """原始剪枝阶段使用严格名称集合，最终阶段由公开入口排除重用名称。"""
    result = InterfacePruningOutcome()
    actual_names = {spec.name for spec in document.interface_specs()}
    for node, path in _walk(document, document.root):
        header = document._parser.base_header(node.header)
        if header.startswith("description "):
            continue
        # 全局 interfaces 容器本身不是引用，已移除定义不会出现在有效遍历中。
        refs = referenced_interface_names(header, removed)
        if refs:
            result.errors.append(f"未知活动接口引用：{' / '.join(path)} 引用了已删除接口 {', '.join(sorted(refs))}")
        gateway = re.fullmatch(r"(?:routing-interface|l3-interface)\s+(\S+)", header.rstrip(";"))
        if gateway:
            name = gateway.group(1)
            if not re.fullmatch(r"irb\.\d+", name):
                result.errors.append(f"非法网关引用：{' / '.join(path)} 必须引用原生 irb 逻辑接口")
            elif name not in actual_names:
                result.errors.append(f"网关接口缺失：{' / '.join(path)} 引用的 {name} 没有有效定义")
    for parent in document._interface_nodes():
        seen = set()
        for unit in document._unit_nodes(parent):
            number = document._unit_number(unit)
            path = f"interfaces / {document._interface_name(parent)} / unit {number}"
            if number in seen:
                result.errors.append(f"重复逻辑接口：{path}")
            seen.add(number)
            families = {
                document._parser.base_header(child.header).rstrip(";").removeprefix("family ")
                for child in unit.children or []
                if document._parser.is_effective(child)
                and document._parser.base_header(child.header).startswith("family ")
            }
            if families & {"bridge", "ethernet-switching", "ccc", "vpls"} and families & {"inet", "inet6"}:
                result.errors.append(f"逻辑接口二三层模式冲突：{path}")
    return result
