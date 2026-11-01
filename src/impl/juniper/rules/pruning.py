"""删除未连接 NNI 的 Junos 数据接口及能够明确归属的依赖。"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

from impl.common.domain.interface import (
    InterfaceKind,
    interface_parent,
    referenced_interface_names,
)
from impl.common.domain.outcomes import InterfacePruningOutcome
from impl.common.rules import merge_interface_results

from ..operations.interfaces import _collect_active_vlans, _expand_vlan_tokens
from .compatibility import _validate_pruned_references, _walk

if TYPE_CHECKING:
    from ..document import JuniperNode, JunosDocument


def _address(value: str) -> str | None:
    """把接口地址文本归一化为主机地址。

    无法解析的协商地址或非地址值返回 ``None``，避免影响后续可识别引用的裁剪。
    """
    try:
        return str(ipaddress.ip_interface(value.rstrip(";")).ip)
    except ValueError:
        return None


def _routing_scope(path: tuple[str, ...]) -> tuple[str, ...]:
    """返回当前路径所属的默认或实例路由作用域。

    不同 routing-instance 的地址和 VLAN 名称空间独立，作用域键可防止跨实例误匹配。
    """
    for index, header in enumerate(path[:-1]):
        if header == "routing-instances":
            return path[: index + 2]
    return ()


class _JunosPruner:
    """保存单台 Junos 设备的一次裁剪状态。

    状态不跨设备共享，使保护集合、删除集合和查找缓存始终对应同一份配置修订。
    """

    def __init__(
        self,
        document: JunosDocument,
        protected_parents: set[str],
        *,
        strict_input_validation: bool = True,
    ):
        """建立接口保护闭包和业务绑定索引。

        聚合成员与父口双向纳入保护，避免仅因拓扑引用其中一个名称而删除整个聚合。
        """
        self.document = document
        self.strict_input_validation = strict_input_validation
        self.result = InterfacePruningOutcome()
        self.protected = {
            interface_parent(self.document.resolve_interface(name))
            for name in protected_parents
        }
        members = self.document.bundle_members()
        # 拓扑引用成员口时，聚合业务及其全部成员均留给 NNI 阶段处理。
        self.protected.update(
            bundle
            for member, bundle in members.items()
            if member in self.protected
        )
        self.protected.update(
            member
            for member, bundle in members.items()
            if bundle in self.protected
        )
        self.nodes = list(self.document._interface_nodes())
        self.specs = self.document.interface_specs()
        self.bindings = self._collect_bindings()
        self.removed = self.result.removed_interfaces
        self.vlan_cache = {}

    def _collect_bindings(self) -> dict[str, set[tuple[str, ...]]]:
        """建立接口到默认或实例路由作用域的绑定索引。

        索引供后续依赖裁剪定位接口所属作用域，不修改配置。
        """
        bindings: dict[str, set[tuple[str, ...]]] = {}
        for container, path in _walk(self.document, self.document.root):
            if path[-1] != "routing-instances":
                continue
            for instance in container.nodes:
                if not self.document._parser.is_effective(instance):
                    continue
                scope = (
                    *path,
                    self.document._parser.base_header(instance.header).rstrip(
                        ";"
                    ),
                )
                for child in instance.children or []:
                    if not self.document._parser.is_effective(child):
                        continue
                    header = self.document._parser.base_header(
                        child.header
                    ).rstrip(";")
                    match = re.fullmatch(r"interface\s+(.+)", header)
                    if match:
                        for name in re.findall(r"[^\s\[\]]+", match.group(1)):
                            bindings.setdefault(name, set()).add(scope)
        return bindings

    def owners(self, name: str) -> set[tuple[str, ...]]:
        """返回接口所属的默认或实例路由作用域。

        未显式绑定时回退到父接口作用域，兼容 Junos 对 unit 和父口的引用差异。
        """
        return self.bindings.get(
            name, self.bindings.get(interface_parent(name), {()})
        )

    def collect_payloads(self):
        """建立接口定义和源地址索引，并计算保留与丢弃名称。

        一次扫描为后续 VLAN、地址和依赖裁剪提供稳定快照，避免边删除边重新查询。
        """
        self.payloads = {}
        self.source_addresses: dict[tuple[tuple[str, ...], str], set[str]] = {}
        for node in self.nodes:
            parent = self.document._interface_name(node)
            units = list(self.document._unit_nodes(node))
            for payload in units or [node]:
                name = (
                    f"{parent}.{self.document._unit_number(payload)}"
                    if units
                    else parent
                )
                self.payloads[name] = payload
                for child, _ in _walk(self.document, payload):
                    match = re.match(
                        r"address\s+(\S+)",
                        self.document._parser.base_header(child.header),
                    )
                    if match and (address := _address(match.group(1))):
                        for scope in self.owners(name):
                            self.source_addresses.setdefault(
                                (scope, address), set()
                            ).add(name)
        self.kept_names = {
            spec.name for spec in self.specs if spec.parent in self.protected
        }
        self.kept_names.update(self.protected)
        data_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
        self.discarded_names = {
            value
            for spec in self.specs
            if spec.kind in data_kinds and spec.parent not in self.protected
            for value in (spec.name, spec.parent)
        }

    def vlans_for(
        self, scope: tuple[str, ...], discarded: bool = False
    ) -> tuple[set[int], set[str]]:
        """返回作用域内保留或丢弃接口关联的 VLAN。

        结果按作用域和状态缓存，避免大量广播域重复执行相同的接口子树分析。
        """
        key = (scope, discarded)
        if key in self.vlan_cache:
            return self.vlan_cache[key]
        names = self.discarded_names if discarded else self.kept_names
        scoped = {name for name in names if scope in self.owners(name)}
        ids, names_for_vlans = _collect_active_vlans(
            self.document, scoped, self.specs
        )
        for name in scoped:
            node = self.payloads.get(name)
            if node is None:
                continue
            for _, path in _walk(self.document, node):
                match = re.fullmatch(r"members\s+(.+)", path[-1])
                if match and "vlan" in path[:-1]:
                    payload = match.group(1)
                    ids.update(_expand_vlan_tokens(payload))
                    names_for_vlans.update(
                        re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload)
                    )
        self.vlan_cache[key] = (ids, names_for_vlans)
        return ids, names_for_vlans

    def record(
        self,
        node: JuniperNode,
        path: tuple[str, ...],
        reason: str,
        names: set[str] | None = None,
    ) -> None:
        """停用节点并登记统一删除事件。

        审计记录保留路径、原因和关联接口，节点本身仅改变活动状态以保持树结构稳定。
        """
        node.active = False
        self.result.changes.append(
            {
                "action": "remove",
                "path": " / ".join(path),
                "reason": reason,
                "interfaces": sorted(names or set()),
            }
        )

    def collect_domains(self):
        """收集活动 bridge-domain 和 VLAN 的保留状态。

        先完成全量域分析再执行删除，使网关和接入接口的判断不受处理顺序影响。
        """
        self.kept_gateways = set()
        self.domains = []
        for container, container_path in list(
            _walk(self.document, self.document.root)
        ):
            if container_path[-1] not in {"bridge-domains", "vlans"}:
                continue
            for domain in container.nodes:
                if not self.document._parser.is_effective(domain):
                    continue
                state = self._domain_state(domain, container_path)
                self.domains.append(state)

    def _domain_state(self, domain, container_path):
        """构造广播域或 VLAN 的保留判定状态。

        显式成员优先于 VLAN 自动匹配，避免其他实例中的同号 VLAN 激活当前域。
        """
        header = self.document._parser.base_header(domain.header).rstrip(";")
        path = (*container_path, header)
        scope = _routing_scope(path)
        vlan_ids, vlan_names = self.vlans_for(scope)
        discarded_vlan_ids, discarded_vlan_names = self.vlans_for(
            scope, discarded=True
        )
        statements = [
            self.document._parser.base_header(node.header).rstrip(";")
            for node, _ in _walk(self.document, domain)
        ]
        explicit, gateways, ids = self._domain_statements(statements)
        # 显式接入成员给出本域的准确边界，不允许其他域/实例的同号 VLAN 激活它。
        active = (
            bool(explicit & self.kept_names)
            if explicit
            else bool(
                ids & vlan_ids
                or header.strip('"') in vlan_names
                or "all" in vlan_names
            )
        )
        if active:
            self.kept_gateways.update(gateways)
        return (
            domain,
            path,
            explicit,
            gateways,
            ids,
            active,
            discarded_vlan_ids,
            discarded_vlan_names,
        )

    @staticmethod
    def _domain_statements(statements):
        """从广播域语句提取显式接口、网关和 VLAN ID。

        三类字段分别保存，使保留判断能够区分显式成员与仅由 VLAN 标签产生的关联。
        """
        explicit = set()
        gateways = set()
        ids = set()
        for statement in statements:
            match = re.fullmatch(r"interface\s+(.+)", statement)
            if match:
                explicit.update(re.findall(r"[^\s\[\]]+", match.group(1)))
            match = re.fullmatch(
                r"(?:routing-interface|l3-interface)\s+(\S+)", statement
            )
            if match:
                gateways.add(match.group(1))
            match = re.fullmatch(r"vlan-id\s+(\d+)", statement)
            if match:
                ids.add(int(match.group(1)))
        return explicit, gateways, ids

    def prune_domains(self):
        """删除仅依赖已删接口、网关或 VLAN 的非活动域。

        仍含保留 NNI 成员的域不删除，即使同时包含已删对象，以免破坏剩余业务。
        """
        self.discarded_gateways = {
            spec.name
            for spec in self.specs
            if spec.kind == InterfaceKind.GATEWAY
        } - self.kept_gateways
        for (
            domain,
            path,
            explicit,
            gateways,
            ids,
            active,
            discarded_vlan_ids,
            discarded_vlan_names,
        ) in self.domains:
            depends_on_removed = bool(
                explicit & self.discarded_names
                or gateways & self.discarded_gateways
                or ids & discarded_vlan_ids
                or path[-1].strip('"') in discarded_vlan_names
            )
            if not active and depends_on_removed:
                self.record(
                    domain, path, "广播域不再含保留的 NNI 接入成员", explicit
                )

    def prune_interfaces(self):
        """按接口类别删除不在 NNI 保护闭包中的数据口和网关。

        父口保护集合同时覆盖子 unit，使拓扑引用父口或子口都能保留完整业务树。
        """
        for node in self.nodes:
            parent = self.document._interface_name(node)
            kind = self.document.interface_kind(parent)
            units = list(self.document._unit_nodes(node))
            if (
                kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
                and parent not in self.protected
            ):
                self._prune_data_interface(node, parent, units)
            elif kind == InterfaceKind.GATEWAY:
                self._prune_gateway_interface(node, parent, units)

    def _prune_data_interface(self, node, parent: str, units: list) -> None:
        """删除不属于 NNI 保留闭包的物理或聚合接口。

        父口与全部 unit 一并处理，避免子接口失去定义后仍被其他配置引用。
        """
        names = {
            parent,
            *(f"{parent}.{self.document._unit_number(unit)}" for unit in units),
        }
        self.result.removed_interfaces.update(names)
        for child, _ in _walk(self.document, node):
            self._record_address(child)
        self.record(node, ("interfaces", parent), "非 NNI 数据接口", names)

    def _prune_gateway_interface(self, node, parent: str, units: list) -> None:
        """删除不再被保留广播域依赖的网关 unit。

        被保留业务引用的 unit 单独保留；所有 unit 都失效时才删除整个父接口。
        """
        for unit in units:
            name = f"{parent}.{self.document._unit_number(unit)}"
            if name in self.kept_gateways:
                continue
            self.result.removed_interfaces.add(name)
            for child, _ in _walk(self.document, unit):
                self._record_address(child)
            self.record(
                unit,
                (
                    "interfaces",
                    parent,
                    self.document._parser.base_header(unit.header),
                ),
                "无保留 NNI 广播域依赖的 IRB",
                {name},
            )
        if not any(self.document._parser.is_effective(unit) for unit in units):
            self.result.removed_interfaces.add(parent)
            self.record(
                node,
                ("interfaces", parent),
                "IRB 无保留逻辑单元",
                {parent},
            )

    def _record_address(self, node) -> None:
        """记录待删除接口子树中的 IP 地址。

        汇总地址供 BGP 本地源检查使用，避免删除接口后仍保留指向失效地址的会话。
        """
        match = re.match(
            r"address\s+(\S+)",
            self.document._parser.base_header(node.header),
        )
        if match and (address := _address(match.group(1))):
            self.result.removed_addresses.add(address)

    def references(self, node: JuniperNode) -> set[str]:
        """返回单个节点的 header 中可识别的已删除接口引用。

        只检查当前语句，避免父节点误吞子树引用而提前删除本应单独审计的配置。
        """
        return referenced_interface_names(
            self.document._parser.base_header(node.header), self.removed
        )

    def subtree_references(self, node: JuniperNode) -> set[str]:
        """返回节点及其有效子树中的已删除接口引用。

        静态路由可能在子语句保存下一跳或出接口，汇总检查后才能安全删除父节点。
        """
        found = self.references(node)
        for child, _ in _walk(self.document, node):
            if not self.document._parser.base_header(child.header).startswith(
                "description "
            ):
                found.update(self.references(child))
        return found

    def lost_source(self, scope: tuple[str, ...], address: str | None) -> bool:
        """判断地址是否已删除且没有其他保留接口继续提供。

        同一地址可能被多个接口复用，只有所有来源都被删除时才能认定 BGP 源已失效。
        """
        return bool(
            address in self.result.removed_addresses
            and not (
                self.source_addresses.get((scope, address), set())
                - self.removed
            )
        )

    def prune_vpws(self):
        """删除所有接入接口均已失效的 EVPN VPWS 实例。

        必须确认实例确有接入接口且全部属于删除集合，避免误删仍可运行的空配置。
        """
        for container, container_path in list(
            _walk(self.document, self.document.root)
        ):
            if container_path[-1] != "routing-instances":
                continue
            for instance in container.nodes:
                if not self.document._parser.is_effective(instance):
                    continue
                statements = [
                    self.document._parser.base_header(n.header).rstrip(";")
                    for n, _ in _walk(self.document, instance)
                ]
                if "instance-type evpn-vpws" not in statements:
                    continue
                interfaces = set()
                for statement in statements:
                    match = re.fullmatch(r"interface\s+(.+)", statement)
                    if match:
                        interfaces.update(
                            re.findall(r"[^\s\[\]]+", match.group(1))
                        )
                if interfaces and interfaces <= self.removed:
                    self.record(
                        instance,
                        (
                            *container_path,
                            self.document._parser.base_header(instance.header),
                        ),
                        "EVPN VPWS 的全部接入接口已删除",
                        interfaces,
                    )

    def local_source(self, node: JuniperNode):
        """读取节点下首个有效 local-address 及其地址值。

        返回值同时携带节点和标准化地址，使调用方能够删除具体语句而不重新定位。
        """
        for child in node.nodes:
            if self.document._parser.is_effective(child):
                match = re.fullmatch(
                    r"local-address\s+(\S+)",
                    self.document._parser.base_header(child.header).rstrip(";"),
                )
                if match:
                    return child, _address(match.group(1))
        return None, None

    def prune_bgp(self):
        """删除依赖失效接口或地址的 BGP 邻居和默认源。

        显式源优先于组默认源，保留仍有有效邻居的组默认配置可避免扩大删除范围。
        """
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
                header = self.document._parser.base_header(
                    neighbor.header
                ).rstrip(";")
                if re.match(r"neighbor\s+", header):
                    own_node, own_address = self.local_source(neighbor)
                    lost = (
                        self.lost_source(scope, own_address)
                        if own_node
                        else default_lost
                    )
                    if lost:
                        self.record(
                            neighbor,
                            (*path, header),
                            "BGP 本地源地址属于已删除接口",
                        )
                    else:
                        remaining += 1
                elif default_lost and re.match(r"allow\b", header):
                    self.record(
                        neighbor,
                        (*path, header),
                        "动态 BGP 邻居继承已删除的本地源地址",
                    )
            if default_lost:
                if remaining:
                    self.record(
                        default_node,
                        (
                            *path,
                            self.document._parser.base_header(
                                default_node.header
                            ),
                        ),
                        "删除失效的组默认源，保留使用有效显式源的邻居",
                    )
                else:
                    self.record(group, path, "BGP 组无使用有效源的保留邻居")

    def clean(self, node: JuniperNode, path: tuple[str, ...] = ()) -> None:
        """递归删除指向已删接口的明确业务依赖。

        仅处理已知协议、VPN、桥域和 QoS 上下文，未知引用保留给最终校验审计。
        """
        for child in node.nodes:
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
                self.record(
                    child, child_path, "静态路由显式出接口已删除", route_refs
                )
                continue
            interface_match = re.fullmatch(r"interface\s+(.+)", header)
            known_interface_context = (
                "protocols" in path
                or "routing-instances" in path
                or "bridge-domains" in path
                or "vlans" in path
            )
            if refs and interface_match and known_interface_context:
                values = re.findall(r"[^\s\[\]]+", interface_match.group(1))
                retained = [
                    value for value in values if value not in self.removed
                ]
                if retained and not child.is_block:
                    child.header = (
                        f"interface [ {' '.join(retained)} ];"
                        if len(retained) > 1
                        else f"interface {retained[0]};"
                    )
                    self.result.changes.append(
                        {
                            "action": "remove-members",
                            "path": " / ".join(child_path),
                            "interfaces": sorted(refs),
                        }
                    )
                else:
                    self.record(child, child_path, "接口依赖已删除", refs)
                continue
            if refs and "class-of-service" in path and path[-1] == "interfaces":
                self.record(child, child_path, "接口 QoS 目标已删除", refs)
                continue
            self.clean(child, child_path)

    def run(self):
        """按依赖顺序执行完整 Junos 非 NNI 裁剪并返回审计结果。

        先建立索引和域状态，再删除接口及业务引用，最后统一验证残留引用。
        """
        self.collect_payloads()
        self.collect_domains()
        self.prune_domains()
        self.prune_interfaces()
        self.prune_vpws()
        self.prune_bgp()
        self.clean(self.document.root)
        validation = _validate_pruned_references(
            self.document,
            self.removed,
            self.result.deferred_interface_references,
            defer_unknown=True,
            strict_input_validation=self.strict_input_validation,
        )
        self.result.errors.extend(validation.errors)
        self.result.warnings.extend(validation.warnings)
        self.result.deferred_interface_references.update(
            validation.deferred_interface_references
        )
        return self.result


def prune_non_nni(
    document: JunosDocument,
    protected_parents: set[str],
    *,
    strict_input_validation: bool = True,
) -> InterfacePruningOutcome:
    """执行 Junos 非 NNI 接口裁剪规则。

    保护集合来自原始拓扑，确保接口迁移不会改变裁剪闭包。
    """
    from . import prune_non_nni as execute

    return execute(
        document,
        protected_parents,
        strict_input_validation=strict_input_validation,
    )


def prune_interface_dependencies(
    document,
    removed_interfaces,
    *,
    strict_input_validation: bool = True,
):
    """清理聚合缩减后已删除成员口遗留的业务依赖。

    复用完整裁剪器但关闭非 NNI 接口删除，只修改给定成员对应的引用和业务容器。
    """

    def collect():
        """在临时工作树上收集成员依赖裁剪结果。

        延迟到调用点再读取当前根，使规则执行器和直接调用入口共享同一实现。
        """
        worker = _JunosPruner(
            document,
            set(),
            strict_input_validation=strict_input_validation,
        )
        worker.result.removed_interfaces.update(
            document.resolve_interface(name) for name in removed_interfaces
        )
        worker.clean(document.root)
        validation = _validate_pruned_references(
            document,
            worker.removed,
            worker.result.deferred_interface_references,
            defer_unknown=True,
            strict_input_validation=strict_input_validation,
        )
        worker.result.errors.extend(validation.errors)
        worker.result.warnings.extend(validation.warnings)
        worker.result.deferred_interface_references.update(
            validation.deferred_interface_references
        )
        return merge_interface_results(
            [("juniper.prune.member_dependencies", worker.result)],
            InterfacePruningOutcome(),
        )

    return collect()
