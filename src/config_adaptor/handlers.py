"""拓扑预检、配置迁移、清洗和模拟参数适配责任链。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .application.nni import NniEndpointPlan, plan_nni_components
from .application.pipeline import ConversionPipeline
from .application.uni import VlanSpaceExhausted, allocate_uni_vlans
from .parsers import InterfaceKind, InterfaceSpec, interface_parent, interface_unit
from .models import ConversionContext, DeviceContext, InterfaceMapping, Link, Vendor


class TopologyPreflightHandler:
    """在任何配置改写前校验链路端点，并确定后续阶段可消费的链路。"""

    def process(self, context: ConversionContext) -> None:
        """把无效或暂不支持的链路标记为 inactive，并记录诊断。"""
        def record_skipped_endpoints(link: Link, reason: str) -> None:
            """保留已跳过端点的 NNI 角色，防止 UNI 阶段误分类。"""
            for device_name, raw_interface in link.endpoints():
                device = context.devices.get(device_name)
                if not device:
                    continue
                source = interface_parent(device.document.resolve_interface(raw_interface))
                logical = device.document.bundle_members().get(source)
                for reserved in dict.fromkeys((source, logical)):
                    if not reserved:
                        continue
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=reserved,
                            role="NNI",
                            action="skip",
                            target_interface=None,
                            link_rows=[link.row],
                            reason=reason,
                        )
                    )

        topology_devices = {device.name for device in context.topology.devices}
        for link in context.topology.links:
            missing = [name for name, _ in link.endpoints() if name not in topology_devices]
            if missing:
                link.active = False
                link.skip_reason = f"端点设备未出现在设备列表: {', '.join(missing)}"
                context.errors.append(f"链接表第 {link.row} 行：{link.skip_reason}")
                record_skipped_endpoints(link, link.skip_reason)
                continue
            if all(device_name in context.devices for device_name, _ in link.endpoints()):
                continue
            link.active = False
            link.skip_reason = "端点包含不在首版范围内的设备（可能为华为或未知厂商）"
            record_skipped_endpoints(link, link.skip_reason)
            context.warnings.append(f"链接表第 {link.row} 行已跳过：{link.skip_reason}")
            context.add_event(
                "skip-link",
                f"跳过链接表第 {link.row} 行",
                row=link.row,
                reason=link.skip_reason,
            )

        context.add_event(
            "topology-preflight",
            "拓扑链路预检完成",
            active_links=sum(1 for link in context.topology.links if link.active),
            skipped_links=sum(1 for link in context.topology.links if not link.active),
        )


class GroupExpansionHandler:
    """在接口分类前展开厂商 group，确保继承配置也参与后续转换。"""

    def process(self, context: ConversionContext) -> None:
        """逐设备展开 group，并把冲突与失败写入共享上下文。"""
        mode = context.washing_policy.group_handling
        if mode == "preserve":
            context.add_event("group-expansion", "已按策略保留所有厂商 group，不执行静态展开")
            return

        # 拓扑接口即使未显式出现在配置中，也要作为正则/通配 group 的候选对象。
        known_by_device: dict[str, set[str]] = defaultdict(set)
        for link in context.topology.links:
            if not link.active:
                continue
            for device_name, interface in link.endpoints():
                if device_name in context.devices:
                    known_by_device[device_name].add(interface_parent(interface))

        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            outcome = device.document.expand_groups(
                known_by_device[device_name],
                mode=mode,
                policy=context.washing_policy,
            )
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
            for ambiguity in outcome.ambiguities:
                context.add_event(
                    "group-identity-ambiguous",
                    f"设备 {device_name} 存在规则未覆盖的潜在 group 语义冲突",
                    device=device_name,
                    **ambiguity,
                )
            identity_total = outcome.identity_rule_hits + outcome.identity_fallbacks
            if identity_total:
                context.add_event(
                    "group-identity-coverage",
                    f"设备 {device_name} 的 group 语义规则命中统计",
                    device=device_name,
                    matched=outcome.identity_rule_hits,
                    fallback=outcome.identity_fallbacks,
                    total=identity_total,
                    coverage=round(outcome.identity_rule_hits / identity_total, 4),
                )
            if not outcome.success:
                message = f"设备 {device_name} 的配置 group 无法安全完整展开"
                device.errors.append(message)
                context.errors.append(message)


class InterfaceClassificationHandler:
    """审计厂商接口分类，未知类型保留配置但不参与端口映射。"""

    def process(self, context: ConversionContext) -> None:
        """为每个未知父接口生成一次可审计告警。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            unknown_parents = sorted(
                {
                    spec.parent
                    for spec in device.document.interface_specs()
                    if spec.kind == InterfaceKind.UNKNOWN
                }
            )
            for interface in unknown_parents:
                message = (
                    f"设备 {device_name} 的接口 {interface} 类型无法识别，"
                    "已保留原配置且不参与接口映射"
                )
                device.warnings.append(message)
                context.add_event(
                    "interface-classification-warning",
                    message,
                    device=device_name,
                    interface=interface,
                    classification=InterfaceKind.UNKNOWN.value,
                    action="preserve",
                )


@dataclass(slots=True)
class _NniAnalysis:
    """NNI 端点解析结果，集中传递后续规划所需的只读索引。"""

    member_maps: dict[str, dict[str, str]]
    resolved: dict[tuple[int, str], str]
    bundles: dict[tuple[int, str], str | None]


class NNIHandler:
    """识别并扁平化聚合 NNI，再分配目标镜像物理接口。"""

    def process(self, context: ConversionContext) -> None:
        """编排 NNI 分析、规划、分配和配置改写。"""
        links = [link for link in context.topology.links if link.active]
        if not links:
            return

        analysis = self._analyze_endpoints(context, links)
        if context.has_errors:
            return

        component_rows = self._plan_components(context, links, analysis.bundles)
        if context.has_errors:
            return

        plans = self._allocate_targets(context, links, component_rows, analysis)
        for device_name, device_plans in plans.items():
            self._apply_device_plans(
                context,
                device_name,
                device_plans,
                analysis.member_maps[device_name],
            )

    @staticmethod
    def _analyze_endpoints(
        context: ConversionContext,
        links: list[Link],
    ) -> _NniAnalysis:
        member_maps = {
            name: item.document.bundle_members()
            for name, item in context.devices.items()
        }
        resolved: dict[tuple[int, str], str] = {}
        bundles: dict[tuple[int, str], str | None] = {}
        for link in links:
            for device_name, raw_interface in link.endpoints():
                device = context.devices[device_name]
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
        plan = plan_nni_components(links, bundles)
        context.errors.extend(plan.errors)
        if context.has_errors:
            return plan.component_rows

        links_by_row = {link.row: link for link in context.topology.links}
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
                link.set_interface_for(device_name, target)
        return plans

    def _apply_device_plans(
        self,
        context: ConversionContext,
        device_name: str,
        plans: list[NniEndpointPlan],
        members: dict[str, str],
    ) -> None:
        device = context.devices[device_name]
        plans_by_logical: dict[str, list[NniEndpointPlan]] = defaultdict(list)
        for plan in plans:
            for logical in plan.logical_sources:
                plans_by_logical[logical].append(plan)
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
        staged_targets: list[tuple[str, str]] = []
        for stage_index, plan in enumerate(plans):
            # 一个对端计划使用一个占位口；M-LAG 会为同一逻辑源创建多个占位口。
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


class UNIHandler:
    """把剩余的有效 UNI 业务汇聚到最后一个接口的 QinQ 子接口。"""

    def process(self, context: ConversionContext) -> None:
        """逐设备编排 UNI 选择、VLAN 分配和配置迁移。"""
        for device_name in sorted(context.devices):
            self._process_device(context, device_name)

    def _process_device(
        self,
        context: ConversionContext,
        device_name: str,
    ) -> None:
        device = context.devices[device_name]
        candidates, sources, member_map = self._select_sources(device)
        self._remove_inactive_interfaces(
            device_name,
            device,
            candidates,
            sources,
            member_map,
        )
        if not sources:
            return

        self._warn_ambiguous_gateway_vlans(context, device_name, device, sources)
        try:
            vlan_plan = allocate_uni_vlans(sources)
        except VlanSpaceExhausted as exc:
            message = f"设备 {device_name} 的 {exc}"
            device.errors.append(message)
            context.errors.append(message)
            return
        self._migrate_sources(device_name, device, sources, member_map, vlan_plan)

    @staticmethod
    def _select_sources(
        device: DeviceContext,
    ) -> tuple[list[InterfaceSpec], list[InterfaceSpec], dict[str, str]]:
        document = device.document
        specs = document.interface_specs()
        # NNI 映射中的源口和目标口都必须排除，防止再次被当作 UNI 迁移。
        nni_interfaces = {
            interface_parent(value)
            for mapping in device.mappings
            if mapping.role == "NNI"
            for value in (mapping.source_interface, mapping.target_interface)
            if value
        }
        member_map = document.bundle_members()
        business_names = document.business_interface_names()
        # 聚合物理成员由聚合父口统一处理，不能作为独立 UNI 候选。
        candidates = [
            spec
            for spec in specs
            if spec.kind
            in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE, InterfaceKind.GATEWAY}
            and spec.parent not in nni_interfaces
            and spec.parent != device.profile.uni_parent
            and spec.parent not in member_map
        ]
        sources = [spec for spec in candidates if spec.name in business_names]
        return candidates, sources, member_map

    @staticmethod
    def _remove_inactive_interfaces(
        device_name: str,
        device: DeviceContext,
        candidates: list[InterfaceSpec],
        sources: list[InterfaceSpec],
        member_map: dict[str, str],
    ) -> None:
        source_names = {spec.name for spec in sources}
        source_parents = {spec.parent for spec in sources}
        # 活跃父口下可能同时存在未被业务引用的 unit；父口会迁移，
        # 但这些 unit 不迁移，因此先单独留下删除审计记录。
        for spec in candidates:
            if spec.name in source_names or spec.parent not in source_parents:
                continue
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=spec.name,
                    role="UNI",
                    action="remove-bare",
                    target_interface=None,
                    reason="同一父接口下未关联活跃业务的逻辑单元",
                )
            )

        # 整个父口都没有业务时，父口及其聚合物理成员可一并删除。
        bare_parents = sorted({spec.parent for spec in candidates} - source_parents)
        for parent in bare_parents:
            device.document.remove_interface(parent, include_children=True)
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=parent,
                    role="UNI",
                    action="remove-bare",
                    target_interface=None,
                    reason="无 IP、无 L2 绑定且无业务引用的裸口",
                )
            )
        UNIHandler._remove_bundle_members(
            device_name,
            device,
            bare_parents,
            member_map,
            inactive=True,
        )

    @staticmethod
    def _remove_bundle_members(
        device_name: str,
        device: DeviceContext,
        bundles: list[str],
        member_map: dict[str, str],
        *,
        inactive: bool,
    ) -> None:
        bundle_names = set(bundles)
        for member, bundle in sorted(member_map.items()):
            if bundle not in bundle_names:
                continue
            device.document.remove_interface(member, include_children=True)
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=member,
                    role="UNI",
                    action="remove-bare" if inactive else "remove",
                    target_interface=None,
                    reason=(
                        f"无业务聚合 {bundle} 的物理成员"
                        if inactive
                        else f"UNI 聚合 {bundle} 扁平化，删除物理成员"
                    ),
                )
            )

    @staticmethod
    def _warn_ambiguous_gateway_vlans(
        context: ConversionContext,
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
    ) -> None:
        if device.device.vendor != Vendor.CISCO_IOSXR:
            return
        for spec in sources:
            if (
                spec.kind != InterfaceKind.GATEWAY
                or spec.vlan is not None
                or spec.inner_vlan is not None
            ):
                continue
            message = (
                f"设备 {device_name} 的网关接口 {spec.name} 未找到唯一明确的"
                "接入口 VLAN；不使用 BVI 编号，将自动分配运输 VLAN"
            )
            device.warnings.append(message)
            context.add_event(
                "gateway-vlan-warning",
                message,
                device=device_name,
                interface=spec.name,
                action="allocate-transport-vlan",
            )

    @staticmethod
    def _migrate_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        member_map: dict[str, str],
        vlan_plan: dict[str, int],
    ) -> None:
        source_parents = list(dict.fromkeys(spec.parent for spec in sources))
        # 先改到不会与目标 UNI 冲突的占位名，再逐个拆成 QinQ 子接口。
        placeholders = {
            parent: f"ADAPT-UNI-{index}"
            for index, parent in enumerate(source_parents)
        }
        UNIHandler._remove_bundle_members(
            device_name,
            device,
            source_parents,
            member_map,
            inactive=False,
        )
        for parent in source_parents:
            device.document.rename_interface_tree(
                parent,
                placeholders[parent],
                strip_bundle=True,
            )
        UNIHandler._map_sources(device_name, device, sources, placeholders, vlan_plan)
        UNIHandler._finalize_sources(device_name, device, sources, placeholders)
        device.document.ensure_parent_interface(device.profile.uni_parent)

    @staticmethod
    def _map_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        placeholders: dict[str, str],
        vlan_plan: dict[str, int],
    ) -> None:
        for spec in sources:
            staged_name = placeholders[spec.parent]
            if spec.unit is not None:
                staged_name += f".{spec.unit}"
            vlan = vlan_plan[spec.name]
            # 外层 VLAN 是新的运输标签；内层优先保留原业务 VLAN。
            # 原标签缺失或非法时，使用新分配的 VLAN 保证生成配置可用。
            inner_vlan = spec.inner_vlan or (
                spec.vlan
                if spec.vlan is not None and 1 <= spec.vlan <= 4094
                else vlan
            )
            target = device.document.map_uni(
                staged_name,
                device.profile.uni_parent,
                vlan,
                inner_vlan,
            )
            action = "map" if spec.vlan == vlan else "re-vlan"
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=spec.name,
                    role="UNI",
                    action=action,
                    target_interface=target,
                    reason=(
                        "保留原 VLAN"
                        if action == "map"
                        else "无 VLAN 或 VLAN 冲突，自动分配"
                    ),
                )
            )

    @staticmethod
    def _finalize_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        placeholders: dict[str, str],
    ) -> None:
        source_names = {spec.name for spec in sources}
        for parent, placeholder in placeholders.items():
            device.document.finalize_uni_source(placeholder)
            if parent in source_names:
                continue
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


class ReferenceRewriteHandler:
    """在接口迁移完成后统一更新协议、策略和业务中的接口引用。"""

    def process(self, context: ConversionContext) -> None:
        """使用结构化一对多映射改写每台设备的非接口定义引用。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            device.document.replace_references(device.replacement_map)


class OptionalFeatureWashingHandler:
    """按显式策略清理会改变业务能力的可选配置类别。"""

    def process(self, context: ConversionContext) -> None:
        """把协议认证、PKI、硬件、NAT 和流量统计与认证替换分离。"""
        policy = context.washing_policy
        enabled = [
            name
            for name in (
                "protocol_authentication",
                "pki",
                "hardware",
                "nat",
                "flow_statistics",
            )
            if getattr(policy, name)
        ]
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            cleanup = device.document.clean_optional_features(policy)
            context.add_event(
                "optional-washing",
                f"设备 {device_name} 已执行可选能力清洗",
                device=device_name,
                enabled=enabled,
                removed_sections=cleanup.total,
                removed_by_type=cleanup.removed,
            )


class AuthWashingHandler:
    """清除原认证和授权体系，再添加统一实验账号。"""

    def process(self, context: ConversionContext) -> None:
        """调用厂商实现清理旧认证，并记录分项删除数量。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            cleanup = device.document.clean_management_access()
            device.document.add_lab_account()
            context.add_event(
                "authentication",
                f"设备 {device_name} 已替换实验认证配置",
                device=device_name,
                removed_sections=cleanup.total,
                removed_by_type=cleanup.removed,
            )


class SimulationAdaptationHandler:
    """根据目标镜像 Profile 执行保守、可审计的模拟参数适配。"""

    def process(self, context: ConversionContext) -> None:
        """只调整已经迁移的数据口和显式存在的激进稳定性参数。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            policy = device.profile.simulation_adaptation
            data_interfaces = {
                interface_parent(mapping.target_interface)
                for mapping in device.mappings
                if mapping.target_interface
                and mapping.role in {"NNI", "UNI"}
                and mapping.action not in {"remove", "remove-bare", "skip"}
            }
            outcome = device.document.adapt_to_simulation(policy, data_interfaces)
            context.add_event(
                "simulation-adaptation",
                f"设备 {device_name} 已按 {policy.mode} 模式适配模拟参数",
                device=device_name,
                image=device.profile.image,
                version=device.profile.version,
                mode=policy.mode,
                target_interfaces=sorted(data_interfaces),
                change_count=outcome.total,
                added=outcome.added,
                replaced=outcome.replaced,
                removed=outcome.removed,
            )


def build_default_pipeline() -> ConversionPipeline:
    """按强制顺序组装默认转换流水线。"""
    return ConversionPipeline(
        [
            TopologyPreflightHandler(),
            GroupExpansionHandler(),
            InterfaceClassificationHandler(),
            NNIHandler(),
            UNIHandler(),
            ReferenceRewriteHandler(),
            OptionalFeatureWashingHandler(),
            AuthWashingHandler(),
            SimulationAdaptationHandler(),
        ]
    )


def build_default_chain() -> ConversionPipeline:
    """兼容旧入口；新代码使用 ``build_default_pipeline``。"""
    return build_default_pipeline()
