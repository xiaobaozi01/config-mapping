"""NNI 聚合规划、目标端口分配和配置迁移。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..common.interface import InterfaceKind, interface_parent
from .models import ConversionContext, DeviceContext, InterfaceMapping, Link


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
    """做什么：把 NNI 链接行按 bundle 聚合成组件，产出保留/冗余/警告计划。

    为什么：Excel 中一条聚合 NNI 常拆成多行（每行一个成员接口），只有先识别
    它们属于同一条物理链路，后续才能决定保留哪一行、把其余行标记为冗余。
    流程：并查集合并共享 bundle 的行 → 按根行号分组 → 逐组件选保留行、标冗余、
    校验成员一致性。成员关系不一致只产生警告，不阻止后续转换。
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
            bundle = bundles[(link.row, device_name)]
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
    device_names = sorted(
        {name for row_link in row_links for name, _ in row_link.endpoints()}
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
    """识别并扁平化聚合 NNI，再分配目标镜像物理接口。

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

        component_rows = self._plan_components(context, links, analysis.bundles)

        plans = self._allocate_targets(context, links, component_rows, analysis)
        for device_name, device_plans in plans.items():
            self._apply_device_plans(
                context,
                device_name,
                device_plans,
                analysis.member_maps[device_name],
            )

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
        # 是否属于 Bundle/ae，也用它删除扁平化后不再需要的物理成员。
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
                device = context.devices[device_name]
                # resolve_interface 处理厂商别名，interface_parent 再去掉 unit/
                # 子接口后缀；NNI 的物理合法性必须在父接口层面判断。
                source = device.document.resolve_interface(raw_interface)
                parent = interface_parent(source)
                resolved[(link.row, device_name)] = parent
                bundles[(link.row, device_name)] = member_maps[device_name].get(parent)
                kind = device.document.interface_kind(parent)
                if kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}:
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
        inactive；成员关系不一致只写警告，不阻止转换。
        """
        # planner 只根据拓扑和聚合关系做纯计算，不接触配置 AST。成员关系
        # 不一致被视为可接受的输入差异，仅记录警告，仍按既有分组继续处理。
        plan = plan_nni_components(links, bundles)
        context.warnings.extend(plan.warnings)

        links_by_row = {link.row: link for link in context.topology.links}
        # 一个聚合可能在 Excel 中表现为多条成员链路。转换后的模拟拓扑只
        # 保留行号最小的一条，其余行标为 inactive，同时保留审计原因。
        for row, keep in plan.redundant_rows.items():
            link = links_by_row[row]
            link.active = False
            link.skip_reason = f"聚合 NNI 扁平化，保留第 {keep} 行"
            context.add_event(
                "flatten-link",
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
    ) -> dict[str, list[NniEndpointPlan]]:
        """按设备为每个有效 NNI 组件分配一个镜像物理接口。

        返回按设备分组的迁移计划，同时把分配结果回写到保留的拓扑链路；
        可用接口不足时记录错误，尚不修改设备配置树。
        """
        plans: dict[str, list[NniEndpointPlan]] = defaultdict(list)
        allocated: dict[str, int] = defaultdict(int)
        # 按 Excel 行号稳定分配目标口，保证相同输入每次产生相同结果。
        for link in sorted((item for item in links if item.active), key=lambda item: item.row):
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
                # 同一聚合组件可以对应多行物理成员，但只占用一个目标 NNI。
                rows = component_rows.get(link.row, (link.row,))
                plans[device_name].append(
                    NniEndpointPlan(
                        # logical_sources 是需要复制配置的逻辑父口。普通链路取
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
                # 配置和 Excel 拓扑必须引用同一个目标口，因此端口分配后立即
                # 回写保留链路；冗余成员行已经在规划阶段停用。
                link.set_interface_for(device_name, target)
        return plans

    def _apply_device_plans(
        self,
        context: ConversionContext,
        device_name: str,
        plans: list[NniEndpointPlan],
        members: dict[str, str],
    ) -> None:
        """将一台设备的 NNI 计划真正应用到厂商配置树。

        方法先暂存或克隆逻辑接口，再删除源接口及聚合成员，最后改成目标
        接口名；同时记录 M-LAG 拆分事件和接口映射审计信息。
        """
        device = context.devices[device_name]
        plans_by_logical: dict[str, list[NniEndpointPlan]] = defaultdict(list)
        for plan in plans:
            for logical in plan.logical_sources:
                plans_by_logical[logical].append(plan)
        # 同一个逻辑聚合映射到多个目标口意味着 M-LAG：配置树需要复制多份，
        # replacement_map 也会据此把外部引用展开为一对多。
        self._record_mlag_splits(context, device_name, plans_by_logical)

        # 先将所有逻辑口克隆到占位口：M-LAG 的同一源聚合需被多次复制，
        # 若过早删除源口，后续对端将失去可复制的配置树。
        staged_targets = self._stage_logical_interfaces(
            device_name,
            device,
            plans,
            plans_by_logical,
        )

        # 克隆全部成功后再删源口和聚合成员，最后落到真实目标名，
        # 避免源名、目标名互相占用导致覆盖。
        for logical in plans_by_logical:
            device.document.remove_interface(logical, include_children=True)
        self._remove_bundle_members(device_name, device, plans, members)
        for placeholder, target in staged_targets:
            device.document.rename_interface_tree(
                placeholder,
                target,
                strip_bundle=True,
            )

    @staticmethod
    def _record_mlag_splits(
        context: ConversionContext,
        device_name: str,
        plans_by_logical: dict[str, list[NniEndpointPlan]],
    ) -> None:
        """记录同一逻辑聚合被映射到多个目标口的 M-LAG 拆分事件。

        普通的一对一映射不会产生事件；该方法只写审计记录，不改配置。
        """
        for logical, plans in plans_by_logical.items():
            if len({plan.target for plan in plans}) <= 1:
                continue
            context.add_event(
                "mlag-split",
                f"设备 {device_name} 的聚合 {logical} 已按对端拆分",
                device=device_name,
                source_interface=logical,
                target_interfaces=[plan.target for plan in plans],
                link_rows=sorted({row for plan in plans for row in plan.link_rows}),
            )

    @staticmethod
    def _stage_logical_interfaces(
        device_name: str,
        device: DeviceContext,
        plans: list[NniEndpointPlan],
        plans_by_logical: dict[str, list[NniEndpointPlan]],
    ) -> list[tuple[str, str]]:
        """把待迁移逻辑接口克隆到临时占位接口并生成映射记录。

        M-LAG 场景会从同一源接口克隆多份。返回 ``(占位名, 目标名)``
        列表，供调用方在删除所有源接口后完成最终重命名。先使用不会与真实接口
        重名的占位名，是为了同时支持一对多克隆以及“某个目标恰好也是另一源口”
        的情况，避免迁移顺序导致配置被提前覆盖或后续副本失去复制来源。
        """
        staged_targets: list[tuple[str, str]] = []
        for stage_index, plan in enumerate(plans):
            # 一个对端计划使用一个占位口；M-LAG 会为同一逻辑源创建多个占位口。
            # 占位名还隔离了“某个目标口恰好也是另一个源口”的重命名碰撞。
            placeholder = f"ADAPT-NNI-{stage_index}"
            staged_targets.append((placeholder, plan.target))
            for logical in plan.logical_sources:
                logical_names = device.document.logical_names_under(logical) or [logical]
                device.document.clone_interface_tree(
                    logical,
                    placeholder,
                    strip_bundle=True,
                )
                split_count = len(
                    {item.target for item in plans_by_logical[logical]}
                )
                is_bundle = logical in plan.bundles
                # 计算M-LAG的所有目标口（用于元数据）
                mlag_peers = (
                    sorted({item.target for item in plans_by_logical[logical]})
                    if split_count > 1
                    else []
                )
                mlag_group_id = (
                    f"{logical}:mlag-{'-'.join(sorted(mlag_peers))}"
                    if split_count > 1
                    else None
                )
                # 父口和子接口都需审计；suffix 保留原有子接口编号。
                for source_name in logical_names:
                    suffix = source_name[len(logical) :]
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=source_name,
                            role="NNI",
                            action=(
                                "clone-flatten"
                                if split_count > 1
                                else "flatten" if is_bundle else "map"
                            ),
                            target_interface=plan.target + suffix,
                            link_rows=plan.link_rows,
                            reason=(
                                "M-LAG 按对端拆分"
                                if split_count > 1
                                else "聚合接口扁平化"
                                if is_bundle
                                else "NNI 物理接口映射"
                            ),
                            is_mlag_clone=split_count > 1,
                            mlag_group_id=mlag_group_id,
                            mlag_peers=mlag_peers,
                        )
                    )
                if logical not in logical_names:
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=logical,
                            role="NNI",
                            action=(
                                "clone-parent"
                                if split_count > 1
                                else "flatten-parent" if is_bundle else "map-parent"
                            ),
                            target_interface=plan.target,
                            link_rows=plan.link_rows,
                            reason="NNI 父接口引用映射",
                            is_mlag_clone=split_count > 1,
                            mlag_group_id=mlag_group_id,
                            mlag_peers=mlag_peers,
                        )
                    )
        return staged_targets

    @staticmethod
    def _remove_bundle_members(
        device_name: str,
        device: DeviceContext,
        plans: list[NniEndpointPlan],
        members: dict[str, str],
    ) -> None:
        """删除已扁平化聚合的物理成员，并为每个成员记录处理结果。

        拓扑明确选中的成员会关联到目标 NNI，其余同聚合成员仅删除；不属于
        本次迁移聚合的物理接口保持不变。
        """
        # 拓扑中实际被选中的成员会记录目标口；同聚合的其余成员只删除。
        planned_targets: dict[str, tuple[str, tuple[int, ...]]] = {}
        transformed_bundles = {bundle for plan in plans for bundle in plan.bundles}
        for plan in plans:
            if not plan.bundles:
                continue
            for member in plan.source_members:
                planned_targets[member] = (plan.target, plan.link_rows)

        for member, bundle in sorted(members.items()):
            if bundle not in transformed_bundles:
                continue
            device.document.remove_interface(member, include_children=True)
            target_info = planned_targets.get(member)
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=member,
                    role="NNI",
                    action="selected-member" if target_info else "remove",
                    target_interface=target_info[0] if target_info else None,
                    link_rows=target_info[1] if target_info else [],
                    reason="聚合 NNI 扁平化",
                )
            )
