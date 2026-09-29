"""Group、NNI、UNI、认证四阶段责任链。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass

from .parsers import interface_parent, interface_unit
from .models import ConversionContext, InterfaceMapping, Vendor


class ConversionHandler(ABC):
    """责任链基类：当前阶段成功后才把上下文传给下一阶段。"""

    def __init__(self) -> None:
        self._next: ConversionHandler | None = None

    def set_next(self, handler: "ConversionHandler") -> "ConversionHandler":
        """连接下一处理器，并返回它以支持链式组装。"""
        self._next = handler
        return handler

    def handle(self, context: ConversionContext) -> ConversionContext:
        """执行本阶段；出现错误时立即截断后续变更。"""
        self.process(context)
        if self._next and not context.has_errors:
            return self._next.handle(context)
        return context

    @abstractmethod
    def process(self, context: ConversionContext) -> None:
        """由具体处理器实现本阶段的原地转换。"""
        raise NotImplementedError


class _UnionFind:
    """用并查集把属于同一聚合链路的多行 Excel 成员归为一组。"""
    def __init__(self, values: list[int]):
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        """查找集合代表，并做路径压缩。"""
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        """合并两个链路行号所属的集合。"""
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


@dataclass(slots=True)
class _EndpointPlan:
    """某设备端点从源接口迁移到目标物理口的执行计划。"""
    source: str
    bundle: str | None
    target: str
    link_row: int


class GroupExpansionHandler(ConversionHandler):
    """在接口分类前展开厂商 group，确保继承配置也参与后续转换。"""

    def process(self, context: ConversionContext) -> None:
        """逐设备展开 group，并把冲突与失败写入共享上下文。"""
        # 拓扑接口即使未显式出现在配置中，也要作为正则/通配 group 的候选对象。
        known_by_device: dict[str, set[str]] = defaultdict(set)
        for link in context.topology.links:
            for device_name, interface in link.endpoints():
                if device_name in context.devices:
                    known_by_device[device_name].add(interface_parent(interface))

        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            outcome = device.document.expand_groups(known_by_device[device_name])
            device.warnings.extend(outcome.warnings)
            for message in outcome.events:
                context.add_event(
                    "group-expansion",
                    f"设备 {device_name}: {message}",
                    device=device_name,
                )
            for conflict in outcome.conflicts:
                context.add_event(
                    "group-conflict",
                    f"设备 {device_name} 的 group 配置冲突已按继承优先级处理",
                    device=device_name,
                    **conflict,
                )
            if not outcome.success:
                message = f"设备 {device_name} 的配置 group 无法安全完整展开"
                device.errors.append(message)
                context.errors.append(message)


class NNIHandler(ConversionHandler):
    """识别并扁平化聚合 NNI，再分配目标镜像物理接口。"""

    def process(self, context: ConversionContext) -> None:
        """校验链路、识别聚合成员、分配端口并改写设备配置。"""
        supported_links = []
        original_by_row = {link.row: link for link in context.topology.links}
        topology_devices = {device.name: device for device in context.topology.devices}

        # 先筛掉缺失设备、华为或未知厂商端点，避免污染后面的分配结果。
        for link in context.topology.links:
            missing = [name for name, _ in link.endpoints() if name not in topology_devices]
            if missing:
                link.active = False
                link.skip_reason = f"端点设备未出现在设备列表: {', '.join(missing)}"
                context.errors.append(f"链接表第 {link.row} 行：{link.skip_reason}")
                continue
            left = context.devices.get(link.a_device)
            right = context.devices.get(link.z_device)
            if not left or not right:
                link.active = False
                link.skip_reason = "端点包含不在首版范围内的设备（可能为华为或未知厂商）"
                context.warnings.append(f"链接表第 {link.row} 行已跳过：{link.skip_reason}")
                context.add_event("skip-link", f"跳过链接表第 {link.row} 行", row=link.row, reason=link.skip_reason)
                continue
            supported_links.append(link)

        if not supported_links:
            return

        # 源接口可能是聚合成员；需要先解析为真正承载三层配置的逻辑接口。
        member_maps = {name: item.document.bundle_members() for name, item in context.devices.items()}
        resolved: dict[tuple[int, str], str] = {}
        bundles: dict[tuple[int, str], str | None] = {}
        for link in supported_links:
            for device_name, raw_interface in link.endpoints():
                device = context.devices[device_name]
                source = device.document.resolve_interface(raw_interface)
                parent = interface_parent(source)
                resolved[(link.row, device_name)] = parent
                bundles[(link.row, device_name)] = member_maps[device_name].get(parent)

        # 任一端属于同一聚合口的 Excel 行都视为同一逻辑链路。
        union = _UnionFind([link.row for link in supported_links])
        bundle_rows: dict[tuple[str, str], list[int]] = defaultdict(list)
        for link in supported_links:
            for device_name, _ in link.endpoints():
                bundle = bundles[(link.row, device_name)]
                if bundle:
                    bundle_rows[(device_name, bundle)].append(link.row)
        for rows in bundle_rows.values():
            for row in rows[1:]:
                union.union(rows[0], row)

        components: dict[int, list[int]] = defaultdict(list)
        for link in supported_links:
            components[union.find(link.row)].append(link.row)
        # 每组只保留最早的成员行，并验证两端聚合关系是否对称。
        for rows in components.values():
            keep = min(rows)
            if len(rows) > 1:
                row_links = [original_by_row[row] for row in rows]
                device_pairs = {
                    tuple(sorted((link.a_device, link.z_device))) for link in row_links
                }
                asymmetric: list[str] = []
                for device_name in sorted({name for link in row_links for name, _ in link.endpoints()}):
                    local_bundles = [bundles.get((row, device_name)) for row in rows]
                    if any(value is None for value in local_bundles) or len(set(local_bundles)) != 1:
                        asymmetric.append(device_name)
                if len(device_pairs) != 1 or asymmetric:
                    message = (
                        f"聚合 NNI 行 {sorted(rows)} 两端成员关系不对称"
                        + (f"，涉及设备: {', '.join(asymmetric)}" if asymmetric else "")
                    )
                    context.errors.append(message)
            for row in sorted(rows):
                if row == keep:
                    continue
                link = original_by_row[row]
                link.active = False
                link.skip_reason = f"聚合 NNI 扁平化，保留第 {keep} 行"
                context.add_event(
                    "flatten-link",
                    f"删除聚合冗余成员链路第 {row} 行",
                    row=row,
                    retained_row=keep,
                )

        # 按原 Excel 行号稳定分配镜像接口，确保相同输入始终得到相同结果。
        active_links = sorted((link for link in supported_links if link.active), key=lambda item: item.row)
        plans: dict[str, list[_EndpointPlan]] = defaultdict(list)
        allocated: dict[str, int] = defaultdict(int)
        for link in active_links:
            for device_name, _ in link.endpoints():
                device = context.devices[device_name]
                index = allocated[device_name]
                if index >= len(device.profile.nni_interfaces):
                    message = (
                        f"设备 {device_name} 的 NNI 数量超过可用物理接口数 "
                        f"{len(device.profile.nni_interfaces)}"
                    )
                    device.errors.append(message)
                    context.errors.append(message)
                    continue
                target = device.profile.nni_interfaces[index]
                allocated[device_name] += 1
                plans[device_name].append(
                    _EndpointPlan(
                        source=resolved[(link.row, device_name)],
                        bundle=bundles[(link.row, device_name)],
                        target=target,
                        link_row=link.row,
                    )
                )
                link.set_interface_for(device_name, target)

        for device_name, device_plans in plans.items():
            device = context.devices[device_name]
            document = device.document
            members = member_maps[device_name]
            transformed_bundles: set[str] = set()
            staged: list[tuple[str, str]] = []

            # 先删除聚合成员，再把逻辑 NNI 暂时改成占位名。
            # 两阶段改名可以避免 A→B、B→A 等接口互换发生级联覆盖。
            for index, plan in enumerate(device_plans):
                logical = plan.bundle or plan.source
                if plan.bundle:
                    if plan.bundle in transformed_bundles:
                        continue
                    transformed_bundles.add(plan.bundle)
                    bundle_members = sorted(name for name, bundle in members.items() if bundle == plan.bundle)
                    for member in bundle_members:
                        document.remove_interface(member, include_children=True)
                        action = "selected-member" if member == plan.source else "remove"
                        target = plan.target if action == "selected-member" else None
                        device.mappings.append(
                            InterfaceMapping(
                                device=device_name,
                                source_interface=member,
                                role="NNI",
                                action=action,
                                target_interface=target,
                                link_rows=[plan.link_row],
                                reason="聚合 NNI 扁平化",
                            )
                        )
                logical_names = document.logical_names_under(logical) or [logical]
                placeholder = f"ADAPT-NNI-{index}"
                document.rename_interface_tree(logical, placeholder, strip_bundle=True)
                staged.append((placeholder, plan.target))
                for source_name in logical_names:
                    suffix = source_name[len(logical) :]
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=source_name,
                            role="NNI",
                            action="flatten" if plan.bundle else "map",
                            target_interface=plan.target + suffix,
                            link_rows=[plan.link_row],
                            reason="聚合接口扁平化" if plan.bundle else "NNI 物理接口映射",
                        )
                    )
                if logical not in logical_names:
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=logical,
                            role="NNI",
                            action="flatten-parent" if plan.bundle else "map-parent",
                            target_interface=plan.target,
                            link_rows=[plan.link_row],
                            reason="NNI 父接口引用映射",
                        )
                    )

            for placeholder, target in staged:
                document.rename_interface_tree(placeholder, target, strip_bundle=True)


class UNIHandler(ConversionHandler):
    """把所有非 NNI 业务接口汇聚到倒数第二个接口的子接口。"""

    def process(self, context: ConversionContext) -> None:
        """为每台设备规划唯一 VLAN，并迁移所有 UNI 配置。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            document = device.document
            specs = document.interface_specs()
            nni_targets = {
                interface_parent(mapping.target_interface)
                for mapping in device.mappings
                if mapping.role == "NNI" and mapping.target_interface
            }
            member_map = document.bundle_members()
            member_parents = set(member_map)

            # Loopback、管理口、保留口、NNI 和聚合成员均不能被当成独立 UNI。
            source_specs = [
                spec
                for spec in specs
                if spec.kind in {"physical", "bundle"}
                and spec.parent not in nni_targets
                and spec.parent != device.profile.reserved_interface
                and spec.parent not in member_parents
            ]
            if not source_specs:
                continue

            source_specs.sort(key=lambda item: item.name)
            used_vlans: set[int] = set()
            vlan_plan: dict[str, int] = {}
            # 优先沿用合法且未冲突的原 VLAN，否则从 2 起分配最小空闲值。
            for spec in source_specs:
                requested = spec.vlan
                if requested is not None and 2 <= requested <= 4094 and requested not in used_vlans:
                    vlan = requested
                else:
                    vlan = next((candidate for candidate in range(2, 4095) if candidate not in used_vlans), 0)
                if not vlan:
                    message = f"设备 {device_name} 的 UNI 数量超过 VLAN 可用空间"
                    device.errors.append(message)
                    context.errors.append(message)
                    break
                used_vlans.add(vlan)
                vlan_plan[spec.name] = vlan
            if device.errors:
                continue

            source_parents = sorted({spec.parent for spec in source_specs})
            placeholder_by_parent = {
                parent: f"ADAPT-UNI-{index}" for index, parent in enumerate(source_parents)
            }

            # 聚合 UNI 的物理成员不会各自生成子接口，只迁移聚合逻辑配置。
            for member, bundle in sorted(member_map.items()):
                if bundle not in source_parents:
                    continue
                document.remove_interface(member, include_children=True)
                device.mappings.append(
                    InterfaceMapping(
                        device=device_name,
                        source_interface=member,
                        role="UNI",
                        action="remove",
                        target_interface=None,
                        reason=f"UNI 聚合 {bundle} 扁平化，删除物理成员",
                    )
                )

            for parent in source_parents:
                document.rename_interface_tree(parent, placeholder_by_parent[parent], strip_bundle=True)

            for spec in source_specs:
                staged_name = placeholder_by_parent[spec.parent]
                if spec.unit is not None:
                    staged_name += f".{spec.unit}"
                vlan = vlan_plan[spec.name]
                target = document.map_uni(staged_name, device.profile.uni_parent, vlan)
                action = "map" if spec.vlan == vlan else "re-vlan"
                device.mappings.append(
                    InterfaceMapping(
                        device=device_name,
                        source_interface=spec.name,
                        role="UNI",
                        action=action,
                        target_interface=target,
                        reason="保留原 VLAN" if action == "map" else "无 VLAN 或 VLAN 冲突，自动分配",
                    )
                )

            for parent, placeholder in placeholder_by_parent.items():
                document.finalize_uni_source(placeholder) if hasattr(document, "finalize_uni_source") else document.remove_interface(placeholder, True)
                # 只有逻辑单元、没有独立父接口块时，仍需记录父级引用映射。
                if not any(spec.name == parent for spec in source_specs):
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=parent,
                            role="UNI",
                            action="collapse-parent",
                            target_interface=device.profile.uni_parent,
                            reason="UNI 父接口汇聚",
                        )
                    )

            document.ensure_parent_interface(device.profile.uni_parent)


class AuthHandler(ConversionHandler):
    """清除原认证和授权体系，再添加统一实验账号。"""

    def process(self, context: ConversionContext) -> None:
        """调用厂商实现清理旧认证，并记录分项删除数量。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            cleanup = device.document.clean_authentication()
            device.document.add_lab_account()
            context.add_event(
                "authentication",
                f"设备 {device_name} 已替换实验认证配置",
                device=device_name,
                removed_sections=cleanup.total,
                removed_by_type=cleanup.removed,
            )


def build_default_chain() -> ConversionHandler:
    """按强制顺序组装默认责任链并返回链首。"""
    groups = GroupExpansionHandler()
    nni = NNIHandler()
    uni = UNIHandler()
    auth = AuthHandler()
    groups.set_next(nni).set_next(uni).set_next(auth)
    return groups
