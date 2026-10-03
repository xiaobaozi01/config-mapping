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

        # 前两个阶段只构造索引和计划。任何错误都必须阻止后面的 AST 改写，
        # 避免生成一半成功、一半失败的设备配置。
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
        inactive；成员关系不一致时把错误写入 ``context``。
        """
        # planner 只根据拓扑和聚合关系做纯计算，不接触配置 AST。若一组
        # 成员在任一设备侧无法归入同一聚合，它会返回错误而不是猜测。
        plan = plan_nni_components(links, bundles)
        context.errors.extend(plan.errors)
        if context.has_errors:
            return plan.component_rows

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
        列表，供调用方在删除所有源接口后完成最终重命名。
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


class UNIHandler:
    """把剩余的有效 UNI 业务汇聚到最后一个接口的 QinQ 子接口。

    NNI 阶段完成后，未被 NNI 占用且确有业务绑定的物理口、聚合口和网关
    接口才是 UNI 源。每个业务逻辑单元获得唯一外层运输 VLAN，原业务 VLAN
    尽量作为内层标签保留，最终全部挂到 Profile 指定的 UNI 父接口下。
    """

    def process(self, context: ConversionContext) -> None:
        """按设备名稳定执行 UNI 候选筛选、VLAN 分配和配置迁移。"""
        for device_name in sorted(context.devices):
            self._process_device(context, device_name)

    def _process_device(
        self,
        context: ConversionContext,
        device_name: str,
    ) -> None:
        """处理一台设备的 UNI，并在 VLAN 耗尽时记录错误后停止迁移。

        方法会先清理范围内的无业务接口，再为有效业务分配 VLAN；只有分配
        成功才会修改这些业务接口的配置树。
        """
        device = context.devices[device_name]
        # candidates 用于找出应清理的裸口；sources 是真正需要迁移的业务口。
        # 两者必须分开，否则“没有业务”与“不属于 UNI 范围”会被混为一谈。
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
            # 分配器按 sources 的稳定顺序优先保留原 VLAN，重复、缺失或越界
            # 时改用最小可用 VLAN；失败时尚未开始迁移配置树。
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
        """筛选 UNI 范围内接口及其中真正承载业务的迁移源。

        返回 ``(candidates, sources, member_map)``：候选接口用于裸口清理，
        业务源用于迁移，成员映射用于随聚合父口清理物理成员。
        """
        document = device.document
        specs = document.interface_specs()
        # NNI 映射中的源口和目标口都必须排除，防止再次被当作 UNI 迁移。
        # 排除目标口同样重要：NNI 阶段已将配置写到镜像接口，UNI 阶段若再次
        # 选择它，会把刚生成的 NNI 配置迁到 UNI 汇聚口。
        nni_interfaces = {
            interface_parent(value)
            for mapping in device.mappings
            if mapping.role == "NNI"
            for value in (mapping.source_interface, mapping.target_interface)
            if value
        }
        member_map = document.bundle_members()
        # business_interface_names 由厂商 AST 根据 IP、二层绑定及相关业务引用
        # 计算；不能简单地把所有配置中出现的接口都视为活跃 UNI。
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
        """删除 UNI 范围内未承载业务的接口并写入审计映射。

        对仍有活跃业务的父口只记录未使用 unit；整个父口无业务时删除父口、
        子接口及其聚合成员。未知接口和控制类虚拟接口不在候选范围内。
        """
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

        # 整个父口都没有业务时，父口及其聚合物理成员可一并删除。注意这里
        # 只处理 candidates，未知接口和虚拟控制接口不会被误删。
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
        """删除指定 UNI 聚合口的物理成员，并记录删除原因。

        ``inactive=True`` 表示整个聚合无业务；否则表示业务已从聚合口迁移，
        物理成员因扁平化而删除。
        """
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
        """为无法确定接入 VLAN 的 IOS XR 网关接口生成告警。

        BVI 编号不等同于业务 VLAN，因此此处只说明后续将分配运输 VLAN，
        不修改接口配置；Junos 设备无需执行该检查。
        """
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
        """把一台设备的有效 UNI 业务迁移到 Profile 指定的汇聚父口。

        先将源接口树改为占位名并移除聚合成员，再生成 QinQ 子接口、清理
        占位树，最后确保目标物理父接口存在。
        """
        source_parents = list(dict.fromkeys(spec.parent for spec in sources))
        # 先改到不会与目标 UNI 冲突的占位名，再逐个拆成 QinQ 子接口。
        # 同一父口下可能有多个 unit；父口只暂存一次，各 unit 随接口树一起
        # 移动，之后再按各自的 vlan_plan 分别生成目标子接口。
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
        # map_uni 已把活跃业务复制/改写到最终 UNI 子接口。此时清除占位树，
        # 并补记只有子接口参与迁移时的父口折叠映射。
        UNIHandler._finalize_sources(device_name, device, sources, placeholders)
        # 即使所有业务都落在子接口，目标配置仍需显式存在 UNI 物理父口。
        device.document.ensure_parent_interface(device.profile.uni_parent)

    @staticmethod
    def _map_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        placeholders: dict[str, str],
        vlan_plan: dict[str, int],
    ) -> None:
        """逐业务单元生成目标 QinQ 子接口并记录源到目标的映射。

        ``vlan_plan`` 提供唯一外层运输 VLAN；内层优先使用原 inner VLAN，
        其次使用合法原 VLAN，否则回退到新运输 VLAN。
        """
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
        """清理 UNI 临时占位接口，并补记父接口折叠关系。

        当实际迁移对象只有父口下的 unit 时，额外把原父口映射到统一 UNI
        父口，供后续非接口配置引用改写使用。
        """
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
