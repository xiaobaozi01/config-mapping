"""NNI 聚合规划、目标端口分配和配置迁移。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..common.interface import InterfaceKind, interface_parent
from .models import ConversionContext, DeviceContext, InterfaceMapping, Link
from .profiles import normalize_data_interfaces


class _UnionFind:
    """按 Excel 行号合并同一聚合链路的物理成员。"""

    def __init__(self, values: list[int]):
        self._parent = {value: value for value in values}

    def find(self, value: int) -> int:
        while self._parent[value] != value:
            self._parent[value] = self._parent[self._parent[value]]
            value = self._parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


@dataclass(slots=True, frozen=True)
class NniEndpointPlan:
    """某设备端点从源接口迁移到目标物理口的执行计划。"""

    logical_sources: tuple[str, ...]
    bundles: tuple[str, ...]
    target: str
    link_rows: tuple[int, ...]
    source_members: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class NniComponentPlan:
    """聚合链路分组结果；键是保留行，值是组内全部行。"""

    component_rows: dict[int, tuple[int, ...]]
    redundant_rows: dict[int, int]
    warnings: tuple[str, ...]


# 按 (行号, 设备名) 查询该端点聚合到的 bundle 名；None 表示该端点未聚合。
BundleLookup = dict[tuple[int, str], str | None]


def plan_nni_components(
    links: list[Link],
    bundles: BundleLookup,
) -> NniComponentPlan:
    """做什么：把 NNI 链接行按 bundle 聚合成组件，产出保留/冗余/冲突计划。

    为什么：Excel 中一条聚合 NNI 常拆成多行（每行一个成员接口），只有先识别
    它们属于同一条物理链路，后续才能决定保留哪一行、把其余行标记为冗余。
    流程：并查集合并共享 bundle 的行 → 按根行号分组 → 逐组件选保留行、标冗余、
    校验成员一致性。规划器返回冲突原因，由流程将冲突作为错误阻止转换。
    """
    if not links:
        return NniComponentPlan({}, {}, ())

    # 第 1 步：用并查集合并共享同一 bundle 的行。
    # 聚合 NNI 的多行通过「两端设备对 + 端点设备名 + bundle 名」这组键识别为
    # 同一条物理链路；并查集让传递共享的行也归入同一组件（A 与 B 同 bundle、
    # B 与 C 同 bundle，则 A、B、C 归为一组）。
    union = _UnionFind([link.row for link in links])
    rows_by_bundle: dict[tuple[tuple[str, str], str, str], list[int]] = defaultdict(list)
    for link in links:
        sorted_pair = sorted((link.a_device, link.z_device))
        device_pair: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
        for device_name, _ in link.endpoints():
            # 华为等仅作为拓扑对端的设备没有 bundle 条目。
            bundle = bundles.get((link.row, device_name))
            if bundle:
                rows_by_bundle[(device_pair, device_name, bundle)].append(link.row)
    for rows in rows_by_bundle.values():
        for row in rows[1:]:
            union.union(rows[0], row)

    # 第 2 步：按并查集根行号把全部行聚成组件；每个连通块即一个聚合组件。
    components: dict[int, list[int]] = defaultdict(list)
    for link in links:
        components[union.find(link.row)].append(link.row)

    # 第 3 步：逐组件选保留行（最小行号）、把其余行标为冗余，并校验成员一致性。
    by_row = {link.row: link for link in links}
    component_rows: dict[int, tuple[int, ...]] = {}
    redundant_rows: dict[int, int] = {}
    warnings: list[str] = []
    for rows in components.values():
        ordered_rows = tuple(sorted(rows))
        keep = min(ordered_rows)
        component_rows[keep] = ordered_rows
        if len(ordered_rows) <= 1:
            continue

        warning = _validate_component_bundles(ordered_rows, by_row, bundles)
        if warning is not None:
            warnings.append(warning)
        for row in ordered_rows:
            if row != keep:
                redundant_rows[row] = keep

    return NniComponentPlan(component_rows, redundant_rows, tuple(warnings))


def _validate_component_bundles(
    ordered_rows: tuple[int, ...],
    by_row: dict[int, Link],
    bundles: BundleLookup,
) -> str | None:
    """做什么：校验一个组件内各成员行在每个设备端点上 bundle 是否一致。

    为什么：同一物理链路上的所有成员行，在同一设备端点上应指向同一个 bundle；
    任一设备端点上 bundle 缺失或不唯一都说明成员关系不一致。一致时返回
    ``None``，否则返回描述不一致的文本。
    """
    row_links = [by_row[row] for row in ordered_rows]
    # 华为等仅作为拓扑对端的设备没有 bundle 条目，不参与成员一致性校验，
    # 否则会把“无法读取对端聚合”误报成成员关系歧义。
    device_names = sorted(
        {
            name
            for row_link in row_links
            for name, _ in row_link.endpoints()
            if (row_link.row, name) in bundles
        }
    )
    invalid_devices: list[str] = []
    for device_name in device_names:
        local_bundles = [bundles.get((row, device_name)) for row in ordered_rows]
        if any(value is None for value in local_bundles) or len(set(local_bundles)) != 1:
            invalid_devices.append(device_name)
    if not invalid_devices:
        return None
    return (
        f"聚合 NNI 行 {list(ordered_rows)} 在同一对端组内的成员关系不一致，"
        f"涉及设备: {', '.join(invalid_devices)}"
    )


@dataclass(slots=True)
class _NniAnalysis:
    """保存 NNI 配置改写前得到的端点与聚合关系快照。

    ``member_maps`` 记录各设备的物理成员到聚合口映射，``resolved`` 保存按链路行
    和设备定位的规范化父接口，``bundles`` 保存对应逻辑聚合口。把这些只读索引
    集中传给规划阶段，可保证所有校验基于同一份原始配置，避免边修改接口树边重新
    查询而得到前后不一致的结果。
    """

    member_maps: dict[str, dict[str, str]]
    resolved: dict[tuple[int, str], str]
    bundles: dict[tuple[int, str], str | None]


class NNIHandler:
    """识别并保留聚合 NNI，再分配目标镜像物理接口。

    处理分为三段：先解析拓扑端点与聚合成员关系，再把属于同一条逻辑
    NNI 的 Excel 行合并成组件，最后才修改厂商配置树。分析和规划阶段不
    改配置，因而任一端点非法时可以在写入发生前安全终止。
    """

    def process(self, context: ConversionContext) -> None:
        """执行完整 NNI 流程，并在出错时阻止后续配置改写。

        该入口依次完成端点分析、聚合组件规划、目标接口分配和逐设备迁移；
        结果直接写入 ``context`` 中的拓扑、设备配置、映射与诊断信息。
        """
        # 预检阶段已经把越界或不支持的链路设为 inactive；这里不能让它们
        # 消耗镜像的 NNI 端口，也不能据此删除设备配置。
        links = [link for link in context.topology.links if link.active]
        if not links:
            return

        # 分析阶段只构造索引；发现非法端点时必须阻止后面的 AST 改写，
        # 避免生成一半成功、一半失败的设备配置。
        analysis = self._resolve_endpoints(context, links)
        if context.has_errors:
            return

        seen_sources: set[tuple[str, str]] = set()
        for link in links:
            for name, _ in link.endpoints():
                if name not in context.devices:
                    continue
                key = (name, analysis.resolved[(link.row, name)])
                if key in seen_sources:
                    context.errors.append(f"设备 {name} 物理端口 {key[1]} 被多条活动拓扑链路重复占用")
                seen_sources.add(key)
        if context.has_errors:
            return
        skipped = self._skipped_protection(context, analysis.member_maps)
        for link in links:
            for name, _ in link.endpoints():
                if name not in context.devices:
                    continue
                source = analysis.resolved[(link.row, name)]
                if source in skipped[name]:
                    message = (
                        f"设备 {name} 的 NNI 源接口 {source} 同时属于活动链路和跳过链路的"
                        "保护接口，不能安全迁移"
                    )
                    context.devices[name].errors.append(message)
                    context.errors.append(message)
        if context.has_errors:
            return

        component_rows = self._plan_components(context, links, analysis.bundles)

        if context.has_errors:
            return
        plans = self._allocate_targets(context, links, component_rows, analysis, skipped)
        if context.has_errors:
            return
        # 聚合层保持单一对象；跨设备聚合缺少可靠 MC-LAG 还原资料时明确拒绝。
        peers_by_bundle: dict[tuple[str, str], set[str]] = defaultdict(set)
        for link in links:
            for name, _ in link.endpoints():
                bundle = analysis.bundles.get((link.row, name))
                if bundle:
                    peers_by_bundle[(name, bundle)].update(peer for peer, _ in link.endpoints() if peer != name)
        for (name, bundle), peers in peers_by_bundle.items():
            if len(peers) > 1:
                message = f"设备 {name} 聚合 {bundle} 跨多个对端，缺少可验证的 MC-LAG 还原条件，不能自动拆分或复制"
                context.errors.append(message)
                context.devices[name].errors.append(message)
        for link in links:
            modes = []
            for name, _ in link.endpoints():
                if name not in context.devices:
                    continue
                doc = context.devices[name].document
                member = analysis.resolved[(link.row, name)]
                bundle = analysis.bundles[(link.row, name)]
                mode = doc.bundle_mode(member) if bundle else "on"
                modes.append(mode)
                if mode == "unknown":
                    context.errors.append(f"设备 {name} 成员 {member} 的聚合绑定或协商方式无法安全识别")
                if bundle:
                    specs = doc.interface_specs()
                    names = doc.logical_names_under(member)
                    if any(spec.parent == member and (spec.unit is not None) for spec in specs):
                        context.errors.append(f"设备 {name} 聚合成员 {member} 含业务子接口，无法安全迁移")
                if bundle and any(peer not in context.devices for peer, _ in link.endpoints()):
                    context.warnings.append(f"设备 {name} 聚合 {bundle} 对端未参与配置转换，协商方式 {mode} 未核验")
            if len(modes) == 2 and (("on" in modes and any(mode != "on" for mode in modes)) or modes == ["passive", "passive"]):
                context.errors.append(f"链接表第 {link.row} 行两端聚合协商方式不兼容: {modes}")
        if context.has_errors:
            return
        for device_name, device_plans in plans.items():
            self._apply_device_plans(
                context,
                device_name,
                device_plans,
                analysis.member_maps[device_name],
            )

    @staticmethod
    def _skipped_protection(
        context: ConversionContext,
        members_by_device: dict[str, dict[str, str]],
    ) -> dict[str, set[str]]:
        """保存预检跳过端点及其完整聚合树，后续不能把它们当作空闲目标。"""
        protected: dict[str, set[str]] = {name: set() for name in context.devices}
        for link in context.topology.links:
            if link.active:
                continue
            for name, interface in link.endpoints():
                if name in context.devices:
                    protected[name].add(interface_parent(
                        context.devices[name].document.resolve_interface(interface)
                    ))
        for name, device in context.devices.items():
            protected[name].update(
                interface_parent(device.document.resolve_interface(mapping.source_interface))
                for mapping in device.mappings if mapping.action == "skip"
            )
            members = members_by_device[name]
            protected[name].update(
                bundle for member, bundle in members.items() if member in protected[name]
            )
            protected[name].update(
                member for member, bundle in members.items() if bundle in protected[name]
            )
        return protected

    @staticmethod
    def _resolve_endpoints(
        context: ConversionContext,
        links: list[Link],
    ) -> _NniAnalysis:
        """解析并校验链路端点，建立后续规划使用的接口关系索引。

        返回物理成员关系、规范化父接口和所属聚合口；发现非物理/非聚合
        端点时只记录错误，不修改拓扑或设备配置。
        """
        # member_maps 的方向是“物理成员 -> 聚合父口”。后续既用它判断链路
        # 是否属于 Bundle/ae，也用它找出缩减后不再需要的物理成员。
        member_maps = {
            name: item.document.bundle_members()
            for name, item in context.devices.items()
        }
        # 两个索引都以 (Excel 行号, 设备名) 为键，因为同一条链路的 A/Z
        # 两端可能分别采用普通物理口和聚合口，不能只按行号存一个结果。
        resolved: dict[tuple[int, str], str] = {}
        bundles: dict[tuple[int, str], str | None] = {}
        for link in links:
            for device_name, raw_interface in link.endpoints():
                # 华为等仅作为拓扑对端的设备不加载配置，只适配本端。
                if device_name not in context.devices:
                    continue
                device = context.devices[device_name]
                # resolve_interface 处理厂商别名，interface_parent 再去掉 unit/
                # 子接口后缀；NNI 的物理合法性必须在父接口层面判断。
                source = device.document.resolve_interface(raw_interface)
                parent = interface_parent(source)
                resolved[(link.row, device_name)] = parent
                bundles[(link.row, device_name)] = member_maps[device_name].get(parent)
                kind = device.document.interface_kind(parent)
                if kind == InterfaceKind.PHYSICAL:
                    continue
                message = (
                    f"链接表第 {link.row} 行：设备 {device_name} 的端点 {raw_interface} "
                    f"属于 {kind.value} 接口，不能作为物理 NNI 端点"
                )
                device.errors.append(message)
                context.errors.append(message)
        return _NniAnalysis(member_maps, resolved, bundles)

    @staticmethod
    def _plan_components(
        context: ConversionContext,
        links: list[Link],
        bundles: dict[tuple[int, str], str | None],
    ) -> dict[int, tuple[int, ...]]:
        """把同一逻辑聚合的成员链路合并为一个 NNI 组件。

        返回“保留行 -> 组件全部行”的映射，并把冗余 Excel 链路标记为
        inactive；成员关系不一致时终止转换。
        """
        # planner 只根据拓扑和聚合关系做纯计算，不接触配置 AST。成员关系
        # 不一致时不能让多个逻辑聚合共用一个物理目标，必须停止转换。
        plan = plan_nni_components(links, bundles)
        context.errors.extend(plan.warnings)
        for rows in plan.component_rows.values():
            if len(rows) > 1 and any(name not in context.devices for row in rows for link in links if link.row == row for name, _ in link.endpoints()):
                context.errors.append(f"聚合成员行 {list(rows)} 的外部对端聚合关系无法核验，不压缩未知外部链路")
        if context.has_errors:
            return {}

        links_by_row = {link.row: link for link in context.topology.links}
        # 一个聚合可能在 Excel 中表现为多条成员链路。转换后的模拟拓扑只
        # 保留行号最小的一条，其余行标为 inactive，同时保留审计原因。
        for row, keep in plan.redundant_rows.items():
            link = links_by_row[row]
            link.active = False
            link.skip_reason = f"聚合成员缩减，保留第 {keep} 行"
            context.add_event(
                "reduce-member-link",
                f"删除聚合冗余成员链路第 {row} 行",
                row=row,
                retained_row=keep,
            )
        return plan.component_rows

    @staticmethod
    def _allocate_targets(
        context: ConversionContext,
        links: list[Link],
        component_rows: dict[int, tuple[int, ...]],
        analysis: _NniAnalysis,
        skipped: dict[str, set[str]],
    ) -> dict[str, list[NniEndpointPlan]]:
        """按设备为每个有效 NNI 组件分配一个镜像物理接口。

        返回按设备分组的迁移计划，同时把分配结果回写到保留的拓扑链路；
        可用接口不足时记录错误，尚不修改设备配置树。
        """
        plans: dict[str, list[NniEndpointPlan]] = defaultdict(list)
        allocated: dict[str, int] = defaultdict(int)
        available: dict[str, list[str]] = {}
        for name, device in context.devices.items():
            try:
                if len(device.profile.interfaces) < 4:
                    raise ValueError("镜像接口列表至少需要四个接口")
                targets = normalize_data_interfaces(device.device.vendor, device.profile.interfaces)[:-3]
            except ValueError as exc:
                message = f"设备 {name} 的 NNI 目标接口无效: {exc}"
                device.errors.append(message)
                context.errors.append(message)
                available[name] = []
                continue
            available[name] = [
                value for value in targets if interface_parent(value) not in skipped[name]
            ]
        if context.has_errors:
            return {}
        assignments: list[tuple[Link, str, str]] = []
        # 按 Excel 行号稳定分配目标口，保证相同输入每次产生相同结果。
        for link in sorted((item for item in links if item.active), key=lambda item: item.row):
            for device_name, _ in link.endpoints():
                # 仅对已加载配置的本端设备分配目标口。
                if device_name not in context.devices:
                    continue
                device = context.devices[device_name]
                index = allocated[device_name]
                if index >= len(available[device_name]):
                    message = (
                        f"设备 {device_name} 的 NNI 数量超过可用物理接口数 "
                        f"{len(available[device_name])}（已排除跳过链路的保护接口）"
                    )
                    device.errors.append(message)
                    context.errors.append(message)
                    continue
                target = available[device_name][index]
                allocated[device_name] += 1
                # 同一聚合组件可以对应多行物理成员，但只占用一个目标 NNI。
                rows = component_rows.get(link.row, (link.row,))
                plans[device_name].append(
                    NniEndpointPlan(
                        # logical_sources 记录保留的业务父口。普通链路取
                        # 物理父口，聚合链路则取 Bundle/ae；dict.fromkeys 在
                        # 保持拓扑顺序的同时去重。
                        logical_sources=tuple(
                            dict.fromkeys(
                                analysis.bundles[(row, device_name)]
                                or analysis.resolved[(row, device_name)]
                                for row in rows
                            )
                        ),
                        bundles=tuple(
                            dict.fromkeys(
                                bundle
                                for row in rows
                                if (bundle := analysis.bundles[(row, device_name)])
                            )
                        ),
                        target=target,
                        link_rows=rows,
                        source_members=tuple(
                            analysis.resolved[(row, device_name)] for row in rows
                        ),
                    )
                )
                assignments.append((link, device_name, target))
        # 容量错误不能造成部分设备接口迁移，也不能回写半套目标拓扑。
        if context.has_errors:
            return {}
        for link, device_name, target in assignments:
            link.set_interface_for(device_name, target)
        return plans

    def _apply_device_plans(
        self, context: ConversionContext, device_name: str,
        plans: list[NniEndpointPlan], members: dict[str, str],
    ) -> None:
        """仅迁移物理成员；聚合主口、子口和其业务引用保留。"""
        device = context.devices[device_name]
        doc = device.document
        bundles = {bundle for plan in plans for bundle in plan.bundles}
        staged = []
        selected = set()
        for index, plan in enumerate(plans):
            source = plan.source_members[0]
            selected.add(source)
            placeholder = f"ADAPT-NNI-{index}"
            doc.clone_interface_tree(source, placeholder, strip_bundle=False)
            staged.append((source, placeholder, plan))
        # 所有源已暂存，再统一删除，防止源目标同名相互覆盖。
        removed_members = {member for member, bundle in members.items() if bundle in bundles}
        for source in selected | removed_members:
            doc.remove_interface(source, include_children=True)
        for source, placeholder, plan in staged:
            doc.rename_interface_tree(placeholder, plan.target, strip_bundle=False)
            doc.ensure_parent_interface(plan.target)
            # source 已停用，因此普通源的逻辑名称从暂存目标恢复。
            target_names = doc.logical_names_under(plan.target) or [plan.target]
            for target_name in target_names:
                suffix = target_name[len(plan.target):]
                device.mappings.append(InterfaceMapping(
                    device=device_name, source_interface=source + suffix,
                    role="NNI", action="map-member" if plan.bundles else "map",
                    target_interface=target_name, link_rows=list(plan.link_rows),
                    reason="聚合物理成员映射，逻辑聚合保留" if plan.bundles else "NNI 物理接口映射",
                ))
        for member in sorted(removed_members - selected):
            device.mappings.append(InterfaceMapping(
                device=device_name, source_interface=member, role="NNI", action="remove",
                target_interface=None, reason="缩减冗余聚合成员",
            ))
        for bundle in sorted(bundles):
            for name in dict.fromkeys([bundle, *doc.logical_names_under(bundle)]):
                device.mappings.append(InterfaceMapping(
                    device=device_name, source_interface=name, role="NNI", action="preserve",
                    target_interface=name, reason="保留聚合业务接口类型和引用",
                ))
            outcome = doc.adapt_bundle(bundle)
            device.errors.extend(outcome.errors)
            device.warnings.extend(outcome.warnings)
            for warning in outcome.warnings:
                context.add_event("bundle-adaptation-warning", warning, device=device_name, interface=bundle)
            for change in outcome.changes:
                context.add_event("bundle-adaptation", f"设备 {device_name} 调整聚合门限", device=device_name, **change)
