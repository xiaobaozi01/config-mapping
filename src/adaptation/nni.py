"""NNI 聚合规划、目标端口分配和配置迁移。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from impl.common.interface import BundleMode, InterfaceKind, interface_parent

from .interfaces import _report
from .models import (
    MIN_PROFILE_INTERFACE_COUNT,
    RESERVED_NNI_INTERFACE_COUNT,
    ConversionContext,
    EventKind,
    InterfaceMapping,
    Link,
    MappingAction,
    MappingRole,
)
from .profiles import normalize_data_interfaces


class _UnionFind:
    """用并查集合并属于同一聚合链路的 Excel 行号。

    成员关系可能经多个端点传递，并查集能在接近常数的开销下形成稳定组件。
    """

    def __init__(self, values: list[int]):
        """为每个链路行号建立独立的并查集节点。

        初始父节点指向自身，使尚未发现共享 bundle 的行保持为单行组件。
        """
        self._parent = {value: value for value in values}

    def find(self, value: int) -> int:
        """返回行号所在组件的根节点并压缩搜索路径。

        路径压缩让后续多成员聚合的查找保持高效，同时不改变组件归属。
        """
        while self._parent[value] != value:
            self._parent[value] = self._parent[self._parent[value]]
            value = self._parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        """合并两个行号所属的组件。

        根节点相同时保持原状，避免无意义地改变最终用于选择保留行的组件结构。
        """
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


@dataclass(slots=True, frozen=True)
class NniEndpointPlan:
    """保存某设备端点从源接口迁移到目标物理口的计划。

    计划在配置树修改前完整生成，使容量和聚合校验失败时不会留下部分迁移。
    """

    bundles: tuple[str, ...]
    target: str
    link_rows: tuple[int, ...]
    source_members: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class NniComponentPlan:
    """保存聚合链路组件、冗余行和成员一致性警告。

    保留行作为组件键让后续分配稳定复用最小 Excel 行，并保持输入排序语义。
    """

    component_rows: dict[int, tuple[int, ...]]
    redundant_rows: dict[int, int]
    warnings: tuple[str, ...]


# 按 (行号, 设备名) 查询该端点聚合到的 bundle 名；None 表示该端点未聚合。
BundleLookup = dict[tuple[int, str], str | None]


def plan_nni_components(
    links: list[Link],
    bundles: BundleLookup,
) -> NniComponentPlan:
    """把共享 bundle 的 NNI 链接行规划为稳定组件。

    Excel 常把一条聚合链路拆成多个成员行，先按物理关系合并并校验成员一致性，
    后续才能安全选择保留行并缩减冗余成员。
    """
    if not links:
        return NniComponentPlan({}, {}, ())

    components = _component_memberships(links, bundles)
    by_row = {link.row: link for link in links}
    component_rows: dict[int, tuple[int, ...]] = {}
    redundant_rows: dict[int, int] = {}
    warnings: list[str] = []
    for rows in components.values():
        keep, ordered_rows, warning, redundant = _plan_component(
            rows,
            by_row,
            bundles,
        )
        component_rows[keep] = ordered_rows
        if warning is not None:
            warnings.append(warning)
        redundant_rows.update(redundant)

    return NniComponentPlan(component_rows, redundant_rows, tuple(warnings))


def _component_memberships(
    links: list[Link],
    bundles: BundleLookup,
) -> dict[int, list[int]]:
    """计算共享 bundle 的链路行组件。

    同时按两端设备和聚合名建立关系，使传递关联的成员进入同一连通块。
    """
    union = _UnionFind([link.row for link in links])
    rows_by_bundle: dict[tuple[tuple[str, str], str, str], list[int]] = (
        defaultdict(list)
    )
    for link in links:
        pair = tuple(sorted((link.a_device, link.z_device)))
        for device_name, _ in link.endpoints():
            bundle = bundles.get((link.row, device_name))
            if bundle:
                rows_by_bundle[(*pair, device_name, bundle)].append(link.row)
    for rows in rows_by_bundle.values():
        for row in rows[1:]:
            union.union(rows[0], row)
    components: dict[int, list[int]] = defaultdict(list)
    for link in links:
        components[union.find(link.row)].append(link.row)
    return components


def _plan_component(
    rows: list[int],
    by_row: dict[int, Link],
    bundles: BundleLookup,
) -> tuple[int, tuple[int, ...], str | None, dict[int, int]]:
    """为单个聚合组件选择保留行并计算冗余关系。

    以最小行号作为稳定保留行，并先验证成员绑定一致，避免删除仍有歧义的成员。
    """
    ordered_rows = tuple(sorted(rows))
    keep = ordered_rows[0]
    if len(ordered_rows) <= 1:
        return keep, ordered_rows, None, {}
    warning = _validate_component_bundles(ordered_rows, by_row, bundles)
    redundant = {row: keep for row in ordered_rows if row != keep}
    return keep, ordered_rows, warning, redundant


def _validate_component_bundles(
    ordered_rows: tuple[int, ...],
    by_row: dict[int, Link],
    bundles: BundleLookup,
) -> str | None:
    """校验组件内每条成员行在各设备端点上的 bundle 关系是否一致。

    成员缺失或指向多个聚合时无法确定应保留哪种绑定，因此返回可审计的失败原因。
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
        local_bundles = [
            bundles.get((row, device_name)) for row in ordered_rows
        ]
        if (
            any(value is None for value in local_bundles)
            or len(set(local_bundles)) != 1
        ):
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

        端点、容量和聚合兼容性都在同一入口内完成校验后才进入迁移，确保任一错误
        都在配置树发生变化前终止，避免留下只适配了一部分设备的中间状态。
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

        if not self._validate_unique_sources(context, links, analysis):
            return
        skipped = self._skipped_protection(context, analysis.member_maps)
        if not self._validate_skipped_overlap(
            context, links, analysis, skipped
        ):
            return

        component_rows = self._plan_components(context, links, analysis.bundles)
        if context.has_errors:
            return
        plans = self._allocate_targets(
            context, links, component_rows, analysis, skipped
        )
        if context.has_errors:
            return
        if not self._validate_bundle_modes(context, links, analysis):
            return
        for device_name, device_plans in plans.items():
            self._apply_device_plans(
                context,
                device_name,
                device_plans,
                analysis.member_maps[device_name],
            )

    @staticmethod
    def _validate_unique_sources(
        context: ConversionContext,
        links: list[Link],
        analysis: _NniAnalysis,
    ) -> bool:
        """检查活动链路是否重复占用同一设备的物理端口。

        重复占用会让源配置被迁移两次，因此在修改任何 AST 前终止流程。
        """
        seen_sources: set[tuple[str, str]] = set()
        for link in links:
            for name, _ in link.endpoints():
                if name not in context.devices:
                    continue
                key = (name, analysis.resolved[(link.row, name)])
                if key in seen_sources:
                    context.errors.append(
                        f"设备 {name} 物理端口 {key[1]} "
                        "被多条活动拓扑链路重复占用"
                    )
                seen_sources.add(key)
        return not context.has_errors

    @staticmethod
    def _validate_skipped_overlap(
        context: ConversionContext,
        links: list[Link],
        analysis: _NniAnalysis,
        skipped: dict[str, set[str]],
    ) -> bool:
        """检查活动 NNI 源接口是否同时属于跳过链路保护集合。

        两类链路争夺同一接口时无法保持两边拓扑语义，必须提前报错而非任意选择。
        """
        for link in links:
            for name, _ in link.endpoints():
                if name not in context.devices:
                    continue
                source = analysis.resolved[(link.row, name)]
                if source in skipped[name]:
                    message = (
                        f"设备 {name} 的 NNI 源接口 {source} "
                        "同时属于活动链路和跳过链路的保护接口，不能安全迁移"
                    )
                    context.devices[name].errors.append(message)
                    context.errors.append(message)
        return not context.has_errors

    def _validate_bundle_modes(
        self,
        context: ConversionContext,
        links: list[Link],
        analysis: _NniAnalysis,
    ) -> bool:
        """校验聚合归属和每条链路两端的协商方式。

        先排除跨对端聚合，再按链路检查成员业务和模式，避免不安全组合进入分配阶段。
        """
        self._reject_cross_peer_bundles(context, links, analysis)
        if context.has_errors:
            return False
        for link in links:
            self._validate_link_modes(context, link, analysis)
        return not context.has_errors

    @staticmethod
    def _reject_cross_peer_bundles(
        context: ConversionContext,
        links: list[Link],
        analysis: _NniAnalysis,
    ) -> None:
        """标记同一聚合被多个对端共同引用的情况。

        当前没有可验证的 MC-LAG 还原条件，伪造拆分或复制会破坏业务语义。
        """
        peers_by_bundle: dict[tuple[str, str], set[str]] = defaultdict(set)
        for link in links:
            for name, _ in link.endpoints():
                bundle = analysis.bundles.get((link.row, name))
                if bundle:
                    peers_by_bundle[(name, bundle)].update(
                        peer for peer, _ in link.endpoints() if peer != name
                    )
        for (name, bundle), peers in peers_by_bundle.items():
            if len(peers) > 1:
                message = (
                    f"设备 {name} 聚合 {bundle} 跨多个对端，缺少可验证的 "
                    "MC-LAG 还原条件，不能自动拆分或复制"
                )
                context.errors.append(message)
                context.devices[name].errors.append(message)

    @staticmethod
    def _validate_link_modes(
        context: ConversionContext,
        link: Link,
        analysis: _NniAnalysis,
    ) -> None:
        """校验单条活动链路两端的成员约束和协商模式。

        模式或子接口不兼容时写入精确上下文，让用户能在迁移前修正源配置。
        """
        modes: list[str] = []
        for name, _ in link.endpoints():
            if name not in context.devices:
                continue
            doc = context.devices[name].document
            member = analysis.resolved[(link.row, name)]
            bundle = analysis.bundles[(link.row, name)]
            mode = doc.bundle_mode(member) if bundle else BundleMode.ON
            modes.append(mode)
            if mode == BundleMode.UNKNOWN:
                context.errors.append(
                    f"设备 {name} 成员 {member} 的聚合绑定或协商方式无法安全识别"
                )
            if bundle and any(
                spec.parent == member and spec.unit is not None
                for spec in doc.interface_specs()
            ):
                context.errors.append(
                    f"设备 {name} 聚合成员 {member} 含业务子接口，无法安全迁移"
                )
            if bundle and any(
                peer not in context.devices for peer, _ in link.endpoints()
            ):
                context.warnings.append(
                    f"设备 {name} 聚合 {bundle} 对端未参与配置转换，"
                    f"协商方式 {mode} 未核验"
                )
        if len(modes) == 2 and (
            (
                BundleMode.ON in modes
                and any(mode != BundleMode.ON for mode in modes)
            )
            or modes == [BundleMode.PASSIVE, BundleMode.PASSIVE]
        ):
            context.errors.append(
                f"链接表第 {link.row} 行两端聚合协商方式不兼容: {modes}"
            )

    @staticmethod
    def _skipped_protection(
        context: ConversionContext,
        members_by_device: dict[str, dict[str, str]],
    ) -> dict[str, set[str]]:
        """计算跳过快照必须保留的接口及完整聚合树。

        目标分配不能占用这些受保护端口，否则会覆盖预检明确跳过的拓扑端点。
        """
        protected: dict[str, set[str]] = {
            name: set() for name in context.devices
        }
        for link in context.topology.links:
            if link.active:
                continue
            for name, interface in link.endpoints():
                if name in context.devices:
                    protected[name].add(
                        interface_parent(
                            context.devices[name].document.resolve_interface(
                                interface
                            )
                        )
                    )
        for name, device in context.devices.items():
            protected[name].update(
                interface_parent(
                    device.document.resolve_interface(mapping.source_interface)
                )
                for mapping in device.mappings
                if mapping.action == MappingAction.SKIP
            )
            members = members_by_device[name]
            protected[name].update(
                bundle
                for member, bundle in members.items()
                if member in protected[name]
            )
            protected[name].update(
                member
                for member, bundle in members.items()
                if bundle in protected[name]
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
                bundles[(link.row, device_name)] = member_maps[device_name].get(
                    parent
                )
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
            if len(rows) > 1 and any(
                name not in context.devices
                for row in rows
                for link in links
                if link.row == row
                for name, _ in link.endpoints()
            ):
                context.errors.append(
                    f"聚合成员行 {list(rows)} 的外部对端聚合关系无法核验，不压缩未知外部链路"
                )
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
                EventKind.REDUCE_MEMBER_LINK,
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

        分配结果先保存在计划和待回写集合中，只有全部设备容量校验通过后才更新拓扑；
        这样接口不足时不会出现部分链路已指向新端口、部分链路仍保留旧端口的状态。
        """
        available = _available_targets(context, skipped)
        if context.has_errors:
            return {}
        plans, assignments = _assign_targets(
            context,
            links,
            component_rows,
            analysis,
            available,
        )
        if context.has_errors:
            return {}
        for link, device_name, target in assignments:
            link.set_interface_for(device_name, target)
        return plans

    def _apply_device_plans(
        self,
        context: ConversionContext,
        device_name: str,
        plans: list[NniEndpointPlan],
        members: dict[str, str],
    ) -> None:
        """按计划迁移物理成员并保留聚合主口及业务子树。

        先暂存全部源树再删除和改名，避免多个源目标彼此覆盖，并维持引用稳定。
        """
        device = context.devices[device_name]
        doc = device.document
        bundles = {bundle for plan in plans for bundle in plan.bundles}
        staged, selected = _stage_plans(doc, plans)
        removed_members = {
            member for member, bundle in members.items() if bundle in bundles
        }
        for source in selected | removed_members:
            doc.remove_interface(source, include_children=True)
        if not self._clean_removed_members(
            context, device_name, removed_members - selected
        ):
            return
        _apply_staged_plans(device, device_name, staged)
        _record_removed_members(device, device_name, removed_members - selected)
        _record_preserved_bundles(context, device, device_name, bundles)

    @staticmethod
    def _clean_removed_members(context, device_name, removed):
        """清理聚合缩减后不再保留的物理成员依赖。

        删除集合先写入设备上下文再报告，使最终引用校验能看到完整失效范围。
        """
        if not removed:
            return True
        device = context.devices[device_name]
        outcome = device.document.prune_interface_dependencies(removed)
        device.removed_interfaces.update(removed)
        _report(
            context, device_name, EventKind.REDUNDANT_MEMBER_CLEANUP, outcome
        )
        return not outcome.errors

    @staticmethod
    def _record_endpoint_mappings(device, device_name, source, plan):
        """记录目标父口及其逻辑子口的接口映射。

        同一后缀逐一映射可保留已有 unit 业务，同时让审计记录与最终接口树一致。
        """
        target_names = dict.fromkeys(
            [plan.target, *device.document.logical_names_under(plan.target)]
        )
        for target_name in target_names:
            suffix = target_name[len(plan.target) :]
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=source + suffix,
                    role=MappingRole.NNI,
                    action=(
                        MappingAction.MAP_MEMBER
                        if plan.bundles
                        else MappingAction.MAP
                    ),
                    target_interface=target_name,
                    link_rows=list(plan.link_rows),
                    reason="聚合物理成员映射，逻辑聚合保留"
                    if plan.bundles
                    else "NNI 物理接口映射",
                )
            )


def _available_targets(
    context: ConversionContext,
    skipped: dict[str, set[str]],
) -> dict[str, list[str]]:
    """计算每台设备可用于 NNI 迁移的目标接口。

    预留末尾接口并排除跳过链路保护端口，避免分配覆盖实验保留口或已知缺失端点。
    """
    available: dict[str, list[str]] = {}
    for name, device in context.devices.items():
        try:
            if len(device.profile.interfaces) < MIN_PROFILE_INTERFACE_COUNT:
                raise ValueError("镜像接口列表至少需要四个接口")
            targets = normalize_data_interfaces(
                device.device.vendor, device.profile.interfaces
            )[:-RESERVED_NNI_INTERFACE_COUNT]
        except ValueError as exc:
            message = f"设备 {name} 的 NNI 目标接口无效: {exc}"
            device.errors.append(message)
            context.errors.append(message)
            available[name] = []
            continue
        available[name] = [
            value
            for value in targets
            if interface_parent(value) not in skipped[name]
        ]
    return available


def _assign_targets(
    context: ConversionContext,
    links: list[Link],
    component_rows: dict[int, tuple[int, ...]],
    analysis: _NniAnalysis,
    available: dict[str, list[str]],
) -> tuple[dict[str, list[NniEndpointPlan]], list[tuple[Link, str, str]]]:
    """按拓扑顺序为设备端点分配目标接口并生成迁移计划。

    分配结果先暂存，只有全部设备容量充足时才回写拓扑，避免部分端点发生迁移。
    """
    plans: dict[str, list[NniEndpointPlan]] = defaultdict(list)
    allocated: dict[str, int] = defaultdict(int)
    assignments: list[tuple[Link, str, str]] = []
    for link in sorted(
        (item for item in links if item.active), key=lambda item: item.row
    ):
        for device_name, _ in link.endpoints():
            if device_name not in context.devices:
                continue
            device = context.devices[device_name]
            index = allocated[device_name]
            if index >= len(available[device_name]):
                message = (
                    f"设备 {device_name} 的 NNI 数量超过可用物理接口数 "
                    f"{len(available[device_name])}"
                    "（已排除跳过链路的保护接口）"
                )
                device.errors.append(message)
                context.errors.append(message)
                continue
            target = available[device_name][index]
            allocated[device_name] += 1
            rows = component_rows.get(link.row, (link.row,))
            plans[device_name].append(
                _build_endpoint_plan(device_name, target, rows, analysis)
            )
            assignments.append((link, device_name, target))
    return plans, assignments


def _build_endpoint_plan(
    device_name: str,
    target: str,
    rows: tuple[int, ...],
    analysis: _NniAnalysis,
) -> NniEndpointPlan:
    """构造一个聚合组件在单台设备上的端点计划。

    同时对逻辑父口和物理成员去重，使普通链路与聚合链路共享同一计划模型。
    """
    return NniEndpointPlan(
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


def _stage_plans(
    doc,
    plans: list[NniEndpointPlan],
) -> tuple[list[tuple[str, str, NniEndpointPlan]], set[str]]:
    """把计划涉及的源接口克隆到唯一占位名称。

    暂存全部源后再删除，可避免源目标重名时原地改名破坏后续成员。
    """
    staged = []
    selected = set()
    for index, plan in enumerate(plans):
        source = plan.source_members[0]
        selected.add(source)
        placeholder = f"ADAPT-NNI-{index}"
        doc.clone_interface_tree(source, placeholder, strip_bundle=False)
        staged.append((source, placeholder, plan))
    return staged, selected


def _apply_staged_plans(device, device_name: str, staged) -> None:
    """把暂存接口移动到目标名称并写入审计映射。

    每个占位名称只使用一次，确保重命名不会再次发生目标冲突。
    """
    doc = device.document
    for source, placeholder, plan in staged:
        doc.rename_interface_tree(placeholder, plan.target, strip_bundle=False)
        doc.ensure_parent_interface(plan.target)
        NNIHandler._record_endpoint_mappings(device, device_name, source, plan)


def _record_removed_members(
    device,
    device_name: str,
    removed: set[str],
) -> None:
    """记录聚合缩减中被删除的物理成员。

    删除记录保留原接口名称和原因，使报告可以解释没有进入输出配置的成员。
    """
    for member in sorted(removed):
        device.mappings.append(
            InterfaceMapping(
                device=device_name,
                source_interface=member,
                role=MappingRole.NNI,
                action=MappingAction.REMOVE,
                target_interface=None,
                reason="缩减冗余聚合成员",
            )
        )


def _record_preserved_bundles(
    context: ConversionContext,
    device,
    device_name: str,
    bundles: set[str],
) -> None:
    """记录聚合主口及子接口的保留映射并适配门限。

    聚合业务保持原名称和类型，只调整影响模拟成员数的属性，避免破坏跨设备引用。
    """
    doc = device.document
    for bundle in sorted(bundles):
        for name in dict.fromkeys([bundle, *doc.logical_names_under(bundle)]):
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=name,
                    role=MappingRole.NNI,
                    action=MappingAction.PRESERVE,
                    target_interface=name,
                    reason="保留聚合业务接口类型和引用",
                )
            )
        outcome = doc.adapt_bundle(bundle)
        device.errors.extend(outcome.errors)
        device.warnings.extend(outcome.warnings)
        for warning in outcome.warnings:
            context.add_event(
                EventKind.BUNDLE_ADAPTATION_WARNING,
                warning,
                device=device_name,
                interface=bundle,
            )
        for change in outcome.changes:
            context.add_event(
                EventKind.BUNDLE_ADAPTATION,
                f"设备 {device_name} 调整聚合门限",
                device=device_name,
                **change,
            )
