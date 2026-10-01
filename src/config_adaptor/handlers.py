"""拓扑预检、配置迁移、清洗和模拟参数适配责任链。"""

from __future__ import annotations

from collections import defaultdict

from .application.nni import NniEndpointPlan, plan_nni_components
from .application.pipeline import ConversionPipeline
from .application.uni import VlanSpaceExhausted, allocate_uni_vlans
from .parsers import InterfaceKind, interface_parent, interface_unit
from .models import ConversionContext, InterfaceMapping, Link


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


class NNIHandler:
    """识别并扁平化聚合 NNI，再分配目标镜像物理接口。"""

    def process(self, context: ConversionContext) -> None:
        """识别聚合成员、分配目标端口并改写设备配置。"""
        supported_links = [link for link in context.topology.links if link.active]
        original_by_row = {link.row: link for link in context.topology.links}

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
                kind = device.document.interface_kind(parent)
                if kind not in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}:
                    message = (
                        f"链接表第 {link.row} 行：设备 {device_name} 的端点 {raw_interface} "
                        f"属于 {kind.value} 接口，不能作为物理 NNI 端点"
                    )
                    device.errors.append(message)
                    context.errors.append(message)

        # 端点分类不安全时不得开始链路合并或配置改写。
        if context.has_errors:
            return

        # 仅在“同一对端设备”范围内合并聚合成员。同一 Bundle/ae
        # 的成员若连到不同对端，视为 M-LAG，必须分配不同模拟器物理口。
        component_plan = plan_nni_components(supported_links, bundles)
        context.errors.extend(component_plan.errors)
        component_rows = component_plan.component_rows
        # 规划阶段不修改输入；只有计划通过校验后才把冗余链路标为 inactive。
        if not context.has_errors:
            for row, keep in component_plan.redundant_rows.items():
                link = original_by_row[row]
                link.active = False
                link.skip_reason = f"聚合 NNI 扁平化，保留第 {keep} 行"
                context.add_event(
                    "flatten-link",
                    f"删除聚合冗余成员链路第 {row} 行",
                    row=row,
                    retained_row=keep,
                )

        # 保守策略：成员关系有歧义时不开始接口配置改写。
        if context.has_errors:
            return

        # 按原 Excel 行号稳定分配镜像接口，确保相同输入始终得到相同结果。
        active_links = sorted((link for link in supported_links if link.active), key=lambda item: item.row)
        plans: dict[str, list[NniEndpointPlan]] = defaultdict(list)
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
                rows = component_rows.get(link.row, [link.row])
                logical_sources = list(
                    dict.fromkeys(
                        bundles[(row, device_name)] or resolved[(row, device_name)]
                        for row in rows
                    )
                )
                plans[device_name].append(
                    NniEndpointPlan(
                        logical_sources=tuple(logical_sources),
                        bundles=tuple(
                            dict.fromkeys(
                                bundle
                                for row in rows
                                if (bundle := bundles[(row, device_name)])
                            )
                        ),
                        target=target,
                        link_rows=tuple(rows),
                        source_members=tuple(
                            resolved[(row, device_name)]
                            for row in rows
                        ),
                    )
                )
                link.set_interface_for(device_name, target)

        for device_name, device_plans in plans.items():
            device = context.devices[device_name]
            document = device.document
            members = member_maps[device_name]
            plans_by_logical: dict[str, list[NniEndpointPlan]] = defaultdict(list)
            for plan in device_plans:
                for logical in plan.logical_sources:
                    plans_by_logical[logical].append(plan)
            staged_targets: list[tuple[str, str]] = []

            # 同一逻辑聚合出现在多个对端计划中即为 M-LAG。
            for logical, logical_plans in plans_by_logical.items():
                if len({plan.target for plan in logical_plans}) > 1:
                    context.add_event(
                        "mlag-split",
                        f"设备 {device_name} 的聚合 {logical} 已按对端拆分",
                        device=device_name,
                        source_interface=logical,
                        target_interfaces=[plan.target for plan in logical_plans],
                        link_rows=sorted({row for plan in logical_plans for row in plan.link_rows}),
                    )

            # 每个对端计划只使用一个占位口。计划中通常只有一个聚合
            # 逻辑源；logical_sources 保留列表结构，用于诊断异常成员关系。
            for stage_index, plan in enumerate(device_plans):
                placeholder = f"ADAPT-NNI-{stage_index}"
                staged_targets.append((placeholder, plan.target))
                for logical in plan.logical_sources:
                    logical_names = document.logical_names_under(logical) or [logical]
                    document.clone_interface_tree(logical, placeholder, strip_bundle=True)
                    split_count = len({item.target for item in plans_by_logical[logical]})
                    is_bundle = logical in plan.bundles
                    for source_name in logical_names:
                        suffix = source_name[len(logical) :]
                        device.mappings.append(
                            InterfaceMapping(
                                device=device_name,
                                source_interface=source_name,
                                role="NNI",
                                action="clone-flatten" if split_count > 1 else ("flatten" if is_bundle else "map"),
                                target_interface=plan.target + suffix,
                                link_rows=plan.link_rows,
                                reason="M-LAG 按对端拆分" if split_count > 1 else ("聚合接口扁平化" if is_bundle else "NNI 物理接口映射"),
                            )
                        )
                    if logical not in logical_names:
                        device.mappings.append(
                            InterfaceMapping(
                                device=device_name,
                                source_interface=logical,
                                role="NNI",
                                action="clone-parent" if split_count > 1 else ("flatten-parent" if is_bundle else "map-parent"),
                                target_interface=plan.target,
                                link_rows=plan.link_rows,
                                reason="NNI 父接口引用映射",
                            )
                        )

            # 所有占位克隆完成后再统一删除原逻辑接口，避免 M-LAG
            # 第一个目标处理后就失去后续目标的复制源。
            for logical in plans_by_logical:
                document.remove_interface(logical, include_children=True)

            # 物理成员按“对端分组”记录目标；未出现在拓扑的剩余成员只删除。
            planned_member_targets: dict[str, tuple[str, list[int]]] = {}
            transformed_bundles = {
                bundle for plan in device_plans for bundle in plan.bundles
            }
            for plan in device_plans:
                if not plan.bundles:
                    continue
                for member in plan.source_members:
                    planned_member_targets[member] = (plan.target, plan.link_rows)
            for member, bundle in sorted(members.items()):
                if bundle not in transformed_bundles:
                    continue
                document.remove_interface(member, include_children=True)
                target_info = planned_member_targets.get(member)
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

            # 原物理成员已全部删除，此时再把占位接口落到真实目标名。
            for placeholder, target in staged_targets:
                document.rename_interface_tree(placeholder, target, strip_bundle=True)


class UNIHandler:
    """把剩余的有效 UNI 业务汇聚到最后一个接口的 QinQ 子接口。"""

    def process(self, context: ConversionContext) -> None:
        """为每台设备规划唯一 VLAN，并迁移所有 UNI 配置。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            document = device.document
            specs = document.interface_specs()
            nni_interfaces = {
                interface_parent(value)
                for mapping in device.mappings
                if mapping.role == "NNI"
                for value in (mapping.source_interface, mapping.target_interface)
                if value
            }
            member_map = document.bundle_members()
            member_parents = set(member_map)
            business_names = document.business_interface_names()

            # Loopback、管理口、NNI、UNI 目标父口和聚合成员不能被当成独立 UNI。
            # gateway 只会在厂商解析器确认其广播域仍有活跃业务后进入 business_names。
            candidates = [
                spec
                for spec in specs
                if spec.kind
                in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE, InterfaceKind.GATEWAY}
                and spec.parent not in nni_interfaces
                and spec.parent != device.profile.uni_parent
                and spec.parent not in member_parents
            ]
            source_specs = [spec for spec in candidates if spec.name in business_names]

            source_parents = {spec.parent for spec in source_specs}
            # 同一父接口（典型是 irb）可能同时包含活跃和非活跃 unit。
            # 父接口会整体进入占位迁移，因此在此单独记录未迁移 unit，
            # 避免它随源父接口删除时没有审计记录。
            for spec in candidates:
                if spec.name in business_names or spec.parent not in source_parents:
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
            bare_parents = sorted({spec.parent for spec in candidates} - source_parents)
            for parent in bare_parents:
                document.remove_interface(parent, include_children=True)
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
                # 裸聚合本身删除后，其物理成员也不应残留在模拟器配置中。
                for member, bundle in sorted(member_map.items()):
                    if bundle != parent:
                        continue
                    document.remove_interface(member, include_children=True)
                    device.mappings.append(
                        InterfaceMapping(
                            device=device_name,
                            source_interface=member,
                            role="UNI",
                            action="remove-bare",
                            target_interface=None,
                            reason=f"无业务聚合 {parent} 的物理成员",
                        )
                    )
            if not source_specs:
                continue

            # interface_specs 保留源配置顺序，业务映射也沿用该顺序。
            try:
                vlan_plan = allocate_uni_vlans(source_specs)
            except VlanSpaceExhausted as exc:
                message = f"设备 {device_name} 的 {exc}"
                device.errors.append(message)
                context.errors.append(message)
                continue

            source_parents = list(dict.fromkeys(spec.parent for spec in source_specs))
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
                # 外层 VLAN 是模拟器内唯一的运输标签；内层优先保留原业务 VLAN。
                # 原配置无可识别 VLAN 时，两层使用同一自动分配值。
                inner_vlan = spec.inner_vlan or (
                    spec.vlan if spec.vlan is not None and 1 <= spec.vlan <= 4094 else vlan
                )
                target = document.map_uni(staged_name, device.profile.uni_parent, vlan, inner_vlan)
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
                document.finalize_uni_source(placeholder)
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
