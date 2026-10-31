"""删除未连接 NNI 的 Junos 数据接口及能够明确归属的依赖。"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

from impl.common.interface import InterfaceKind, interface_parent, referenced_interface_names
from impl.common.outcomes import InterfacePruningOutcome
from impl.common.rules import merge_interface_results
from ..interfaces import _collect_active_vlans, _expand_vlan_tokens
from .compatibility import _validate_pruned_references, _walk

if TYPE_CHECKING:
    from ..document import JunosDocument, JunosNode


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


class _JunosPruner:
    """一次裁剪的工作状态；按依赖顺序处理业务，不跨设备共享。"""

    def __init__(self, document: JunosDocument, protected_parents: set[str]):
        self.document = document
        self.result = InterfacePruningOutcome()
        self.protected = {interface_parent(self.document.resolve_interface(name)) for name in protected_parents}
        members = self.document.bundle_members()
        # 拓扑引用成员口时，聚合业务及其全部成员均留给 NNI 阶段处理。
        self.protected.update(bundle for member, bundle in members.items() if member in self.protected)
        self.protected.update(member for member, bundle in members.items() if bundle in self.protected)
        self.nodes = list(self.document._interface_nodes())
        self.specs = self.document.interface_specs()
        self.bindings: dict[str, set[tuple[str, ...]]] = {}
        for container, path in _walk(self.document, self.document.root):
            if path[-1] != "routing-instances":
                continue
            for instance in container.children or []:
                if not self.document._parser.is_effective(instance):
                    continue
                scope = (*path, self.document._parser.base_header(instance.header).rstrip(";"))
                for child in instance.children or []:
                    if not self.document._parser.is_effective(child):
                        continue
                    header = self.document._parser.base_header(child.header).rstrip(";")
                    match = re.fullmatch(r"interface\s+(.+)", header)
                    if match:
                        for name in re.findall(r"[^\s\[\]]+", match.group(1)):
                            self.bindings.setdefault(name, set()).add(scope)

        self.removed = self.result.removed_interfaces
        self.vlan_cache = {}

    def owners(self, name: str) -> set[tuple[str, ...]]:
        return self.bindings.get(name, self.bindings.get(interface_parent(name), {()}))

    def collect_payloads(self):
        self.payloads = {}
        self.source_addresses: dict[tuple[tuple[str, ...], str], set[str]] = {}
        for node in self.nodes:
            parent = self.document._interface_name(node)
            units = list(self.document._unit_nodes(node))
            for payload in units or [node]:
                name = f"{parent}.{self.document._unit_number(payload)}" if units else parent
                self.payloads[name] = payload
                for child, _ in _walk(self.document, payload):
                    match = re.match(r"address\s+(\S+)", self.document._parser.base_header(child.header))
                    if match and (address := _address(match.group(1))):
                        for scope in self.owners(name):
                            self.source_addresses.setdefault((scope, address), set()).add(name)
        self.kept_names = {spec.name for spec in self.specs if spec.parent in self.protected}
        self.kept_names.update(self.protected)
        data_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
        self.discarded_names = {
            value for spec in self.specs
            if spec.kind in data_kinds and spec.parent not in self.protected
            for value in (spec.name, spec.parent)
        }

    def vlans_for(self, scope: tuple[str, ...], discarded: bool = False) -> tuple[set[int], set[str]]:
        key = (scope, discarded)
        if key in self.vlan_cache:
            return self.vlan_cache[key]
        names = self.discarded_names if discarded else self.kept_names
        scoped = {name for name in names if scope in self.owners(name)}
        ids, names_for_vlans = _collect_active_vlans(self.document, scoped, self.specs)
        for name in scoped:
            node = self.payloads.get(name)
            if node is None:
                continue
            for _, path in _walk(self.document, node):
                match = re.fullmatch(r"members\s+(.+)", path[-1])
                if match and "vlan" in path[:-1]:
                    payload = match.group(1)
                    ids.update(_expand_vlan_tokens(payload))
                    names_for_vlans.update(re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload))
        self.vlan_cache[key] = (ids, names_for_vlans)
        return ids, names_for_vlans

    def record(self, node: JunosNode, path: tuple[str, ...], reason: str, names: set[str] | None = None) -> None:
        node.active = False
        self.result.changes.append({
            "action": "remove", "path": " / ".join(path), "reason": reason,
            "interfaces": sorted(names or set()),
        })

    def collect_domains(self):
        self.kept_gateways = set()
        self.domains = []
        for container, container_path in list(_walk(self.document, self.document.root)):
            if container_path[-1] not in {"bridge-domains", "vlans"}:
                continue
            for domain in container.children or []:
                if not self.document._parser.is_effective(domain):
                    continue
                header = self.document._parser.base_header(domain.header).rstrip(";")
                path = (*container_path, header)
                scope = _routing_scope(path)
                vlan_ids, vlan_names = self.vlans_for(scope)
                discarded_vlan_ids, discarded_vlan_names = self.vlans_for(scope, discarded=True)
                statements = [self.document._parser.base_header(n.header).rstrip(";") for n, _ in _walk(self.document, domain)]
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
                active = bool(explicit & self.kept_names) if explicit else bool(
                    ids & vlan_ids or header.strip('"') in vlan_names or "all" in vlan_names
                )
                if active:
                    self.kept_gateways.update(gateways)
                self.domains.append((domain, path, explicit, gateways, ids, active,
                                discarded_vlan_ids, discarded_vlan_names))

    def prune_domains(self):
        self.discarded_gateways = {spec.name for spec in self.specs if spec.kind == InterfaceKind.GATEWAY} - self.kept_gateways
        for domain, path, explicit, gateways, ids, active, discarded_vlan_ids, discarded_vlan_names in self.domains:
            depends_on_removed = bool(
                explicit & self.discarded_names or gateways & self.discarded_gateways
                or ids & discarded_vlan_ids or path[-1].strip('"') in discarded_vlan_names
            )
            if not active and depends_on_removed:
                self.record(domain, path, "广播域不再含保留的 NNI 接入成员", explicit)

    def prune_interfaces(self):
        for node in self.nodes:
            parent = self.document._interface_name(node)
            kind = self.document.interface_kind(parent)
            units = list(self.document._unit_nodes(node))
            if kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE} and parent not in self.protected:
                names = {parent, *(f"{parent}.{self.document._unit_number(unit)}" for unit in units)}
                self.result.removed_interfaces.update(names)
                for child, _ in _walk(self.document, node):
                    match = re.match(r"address\s+(\S+)", self.document._parser.base_header(child.header))
                    if match and (address := _address(match.group(1))):
                        self.result.removed_addresses.add(address)
                self.record(node, ("interfaces", parent), "非 NNI 数据接口", names)
            elif kind == InterfaceKind.GATEWAY:
                for unit in units:
                    name = f"{parent}.{self.document._unit_number(unit)}"
                    if name in self.kept_gateways:
                        continue
                    self.result.removed_interfaces.add(name)
                    for child, _ in _walk(self.document, unit):
                        match = re.match(r"address\s+(\S+)", self.document._parser.base_header(child.header))
                        if match and (address := _address(match.group(1))):
                            self.result.removed_addresses.add(address)
                    self.record(unit, ("interfaces", parent, self.document._parser.base_header(unit.header)), "无保留 NNI 广播域依赖的 IRB", {name})
                if not any(self.document._parser.is_effective(unit) for unit in units):
                    self.result.removed_interfaces.add(parent)
                    self.record(node, ("interfaces", parent), "IRB 无保留逻辑单元", {parent})

    def references(self, node: JunosNode) -> set[str]:
        return referenced_interface_names(self.document._parser.base_header(node.header), self.removed)

    def subtree_references(self, node: JunosNode) -> set[str]:
        found = self.references(node)
        for child, _ in _walk(self.document, node):
            if not self.document._parser.base_header(child.header).startswith("description "):
                found.update(self.references(child))
        return found

    def lost_source(self, scope: tuple[str, ...], address: str | None) -> bool:
        return bool(
            address in self.result.removed_addresses
            and not (self.source_addresses.get((scope, address), set()) - self.removed)
        )

    def prune_vpws(self):
        for container, container_path in list(_walk(self.document, self.document.root)):
            if container_path[-1] != "routing-instances":
                continue
            for instance in container.children or []:
                if not self.document._parser.is_effective(instance):
                    continue
                statements = [self.document._parser.base_header(n.header).rstrip(";") for n, _ in _walk(self.document, instance)]
                if "instance-type evpn-vpws" not in statements:
                    continue
                interfaces = set()
                for statement in statements:
                    match = re.fullmatch(r"interface\s+(.+)", statement)
                    if match:
                        interfaces.update(re.findall(r"[^\s\[\]]+", match.group(1)))
                if interfaces and interfaces <= self.removed:
                    self.record(instance, (*container_path, self.document._parser.base_header(instance.header)), "EVPN VPWS 的全部接入接口已删除", interfaces)

    def local_source(self, node: JunosNode):
        for child in node.children or []:
            if self.document._parser.is_effective(child):
                match = re.fullmatch(r"local-address\s+(\S+)", self.document._parser.base_header(child.header).rstrip(";"))
                if match:
                    return child, _address(match.group(1))
        return None, None

    def prune_bgp(self):
        for group, path in list(_walk(self.document, self.document.root)):
            if "bgp" not in path or not re.match(r"group\s+", path[-1]):
                continue
            scope = _routing_scope(path)
            default_node, default_address = self.local_source(group)
            default_lost = self.lost_source(scope, default_address)
            remaining = 0
            for neighbor in group.children or []:
                if not self.document._parser.is_effective(neighbor):
                    continue
                header = self.document._parser.base_header(neighbor.header).rstrip(";")
                if re.match(r"neighbor\s+", header):
                    own_node, own_address = self.local_source(neighbor)
                    lost = self.lost_source(scope, own_address) if own_node else default_lost
                    if lost:
                        self.record(neighbor, (*path, header), "BGP 本地源地址属于已删除接口")
                    else:
                        remaining += 1
                elif default_lost and re.match(r"allow\b", header):
                    self.record(neighbor, (*path, header), "动态 BGP 邻居继承已删除的本地源地址")
            if default_lost:
                if remaining:
                    self.record(default_node, (*path, self.document._parser.base_header(default_node.header)),
                           "删除失效的组默认源，保留使用有效显式源的邻居")
                else:
                    self.record(group, path, "BGP 组无使用有效源的保留邻居")

    def clean(self, node: JunosNode, path: tuple[str, ...] = ()) -> None:
        for child in node.children or []:
            if not self.document._parser.is_effective(child):
                continue
            header = self.document._parser.base_header(child.header).rstrip(";")
            child_path = (*path, header)
            if not path and header == "interfaces":
                # 接口定义已经处理，保留接口内部的未知依赖交给最终审计。
                continue
            refs = self.references(child)
            is_static = "static" in path and re.match(r"route\s+", header)
            if is_static and (route_refs := self.subtree_references(child)):
                self.record(child, child_path, "静态路由显式出接口已删除", route_refs)
                continue
            interface_match = re.fullmatch(r"interface\s+(.+)", header)
            known_interface_context = (
                "protocols" in path or "routing-instances" in path
                or "bridge-domains" in path or "vlans" in path
            )
            if refs and interface_match and known_interface_context:
                values = re.findall(r"[^\s\[\]]+", interface_match.group(1))
                retained = [value for value in values if value not in self.removed]
                if retained and child.children is None:
                    child.header = f"interface [ {' '.join(retained)} ];" if len(retained) > 1 else f"interface {retained[0]};"
                    self.result.changes.append({"action": "remove-members", "path": " / ".join(child_path), "interfaces": sorted(refs)})
                else:
                    self.record(child, child_path, "接口依赖已删除", refs)
                continue
            if refs and "class-of-service" in path and path[-1] == "interfaces":
                self.record(child, child_path, "接口 QoS 目标已删除", refs)
                continue
            self.clean(child, child_path)

    def check_bundle_ownership(self):
        members = self.document.bundle_members()
        business = self.document.business_interface_names()
        orphans = {
            spec.parent for spec in self.document.interface_specs()
            if spec.kind == InterfaceKind.BUNDLE and spec.name in business
            and spec.parent not in set(members.values())
        }
        if orphans:
            self.result.errors.append(
                f"业务聚合没有可解析的物理成员，归属不明: {', '.join(sorted(orphans))}"
            )
        return not orphans

    def run(self):
        if not self.check_bundle_ownership():
            return self.result
        self.collect_payloads()
        self.collect_domains()
        self.prune_domains()
        self.prune_interfaces()
        self.prune_vpws()
        self.prune_bgp()
        self.clean(self.document.root)
        self.result.errors.extend(
            _validate_pruned_references(self.document, self.removed).errors
        )
        return self.result


def prune_non_nni(document: JunosDocument, protected_parents: set[str]) -> InterfacePruningOutcome:
    from . import prune_non_nni as execute

    return execute(document, protected_parents)


def prune_interface_dependencies(document, removed_interfaces):
    """复用已知绑定裁剪，处理聚合缩减后已删除的无独立业务成员口。"""
    worker = _JunosPruner(document, set())
    worker.result.removed_interfaces.update(document.resolve_interface(name) for name in removed_interfaces)
    worker.clean(document.root)
    worker.result.errors.extend(_validate_pruned_references(document, worker.removed).errors)
    return merge_interface_results(
        [("juniper.prune.member_dependencies", worker.result)], InterfacePruningOutcome(),
    )
