"""UNI 业务规划、源树暂存及共享端口迁移。"""

from __future__ import annotations

from collections import defaultdict

from ..impl.common.domain.interface import (
    InterfaceKind,
    InterfaceSpec,
    UniInterfacePlan,
    interface_parent,
)
from .interfaces import _report
from .models import (
    ConversionContext,
    DeviceContext,
    EventKind,
    InterfaceMapping,
    MappingAction,
    MappingRole,
)

MIN_VLAN = 2
MAX_VLAN = 4094


def allocate_uni_vlans(sources: list[InterfaceSpec]) -> dict[str, int]:
    """稳定分配独占外层 VLAN，优先保留全部业务的合法原标签。

    先预留原 VLAN，避免无标签业务提前占用后续业务可以保留的标签。
    """
    reserved = {
        spec.vlan
        for spec in sources
        if spec.vlan is not None and MIN_VLAN <= spec.vlan <= MAX_VLAN
    }
    free = iter(
        vlan for vlan in range(MIN_VLAN, MAX_VLAN + 1) if vlan not in reserved
    )
    used: set[int] = set()
    result = {}
    for spec in sorted(sources, key=lambda item: item.name):
        if spec.vlan in reserved and spec.vlan not in used:
            vlan = spec.vlan
        else:
            vlan = next(free, None)
        if vlan is None:
            raise ValueError("UNI 数量超过 VLAN 可用空间 2～4094")
        used.add(vlan)
        result[spec.name] = vlan
    return result


def _protected_parents(context: ConversionContext, name: str) -> set[str]:
    """从原始拓扑计算 NNI 接口的完整聚合保护范围。

    不以迁移后的接口名判断身份，预检跳过的端点同样受到保护。
    """
    analysis = context.devices[name].document.interfaces.analysis
    protected = {
        interface_parent(analysis.resolve_interface(interface))
        for link in context.topology.links
        for endpoint, interface in link.endpoints()
        if endpoint == name
    }
    members = analysis.bundle_members()
    protected.update(
        bundle for member, bundle in members.items() if member in protected
    )
    protected.update(
        member for member, bundle in members.items() if bundle in protected
    )
    return protected


def _record_removal(
    device: DeviceContext,
    source: str,
    reason: str,
    action: MappingAction = MappingAction.REMOVE_BARE,
) -> None:
    """记录 UNI 原接口退出及裸口删除，供最终引用校验使用。

    业务迁移与删除使用不同映射动作，避免报告将保留的业务误标为裁剪。
    """
    device.removed_interfaces.add(source)
    device.mappings.append(
        InterfaceMapping(
            device=device.device.name,
            source_interface=source,
            role=MappingRole.UNI,
            action=action,
            target_interface=None,
            reason=reason,
        )
    )


class UNIPreparationHandler:
    """在 NNI 目标口落位前规划 UNI 并暂存全部源接口树。

    源 UNI 可能与 NNI 目标同名，提前释放原名称能防止业务被合并。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备选择业务、检查聚合成员，再暂存源树。

        VLAN 分配失败或聚合成员有独立业务时停止后续部署输出。
        """
        for name, device in sorted(context.devices.items()):
            try:
                self._prepare(context, name, device)
            except ValueError as exc:
                device.errors.append(f"设备 {name}: {exc}")
                context.add_event(
                    EventKind.UNI_MIGRATION_ERROR, str(exc), device=name
                )

    def _prepare(self, context, name, device):
        """建立原始接口业务快照并将候选父树改为唯一暂存名称。

        网关接口始终保留，聚合物理成员不作为独立 UNI 迁移。
        """
        analysis = device.document.interfaces.analysis
        protected = _protected_parents(context, name)
        members = analysis.bundle_members()
        specs = list(
            {spec.name: spec for spec in analysis.interface_specs()}.values()
        )
        candidates = [
            spec
            for spec in specs
            if spec.kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
            and spec.parent not in protected
            and spec.parent not in members
        ]
        business = analysis.business_interface_names()
        sources = [spec for spec in candidates if spec.name in business]
        vlans = allocate_uni_vlans(sources)
        self._check_sources(device, candidates, members)
        self._preserve_gateways(device, specs)
        for parent in sorted(
            {
                spec.parent
                for spec in sources
                if spec.kind == InterfaceKind.BUNDLE
            }
        ):
            message = f"设备 {name}: UNI 聚合 {parent} 的业务已规划到普通子接口，原聚合协商与成员冗余不再复现"
            device.warnings.append(message)
            context.add_event(
                EventKind.UNI_MIGRATION_WARNING,
                message,
                device=name,
                source_interface=parent,
            )
        removed = self._stage(device, candidates, sources, members, vlans)
        self._clean_removed_dependencies(context, name, device, removed)

    @staticmethod
    def _clean_removed_dependencies(context, name, device, removed):
        """裁剪真正删除的接口依赖，拒绝无法确认的残留引用。

        在 NNI 落位前执行，使旧引用不会因目标接口复用名称而被保留。
        """
        if not removed:
            return
        outcome = device.document.pruning.prune_interface_dependencies(
            removed,
            strict_input_validation=context.strict_input_validation,
        )
        for reference in sorted(
            outcome.deferred_interface_references,
            key=lambda item: (item.path, item.interface, item.rule_id),
        ):
            message = (
                f"无法安全清理已删除 UNI 接口 {reference.interface} 的引用："
                f"{' / '.join(reference.path)}（{reference.reason}）"
            )
            if message not in outcome.errors:
                outcome.errors.append(message)
            outcome.diagnostics.append(
                dict(level="error", message=message, rule_id=reference.rule_id)
            )
        _report(context, name, EventKind.UNI_MIGRATION, outcome)

    @staticmethod
    def _check_sources(device, candidates, members):
        """检查 UNI 聚合成员绑定和独立业务。

        聚合扁平化前确认成员关系唯一，避免删除成员自身承载的业务。
        """
        analysis = device.document.interfaces.analysis
        parents = {spec.parent for spec in candidates}
        for member, bundle in members.items():
            if bundle not in parents:
                continue
            if analysis.bundle_binding_is_ambiguous(member):
                raise ValueError(f"UNI 成员 {member} 的聚合绑定不唯一")
            if analysis.interface_has_business(member):
                raise ValueError(
                    f"UNI 聚合成员 {member} 有独立业务，不能安全扁平化"
                )

    @staticmethod
    def _preserve_gateways(device, specs):
        """为原生 BVI/IRB 记录保留映射，不改变桥域网关结构。

        网关不占用独立物理端口，无须为了端口压缩转换接口类型。
        """
        for spec in specs:
            if spec.kind == InterfaceKind.GATEWAY:
                device.mappings.append(
                    InterfaceMapping(
                        device=device.device.name,
                        source_interface=spec.name,
                        role=MappingRole.UNI,
                        action=MappingAction.PRESERVE,
                        target_interface=spec.name,
                        reason="保留原生桥域网关",
                    )
                )

    @staticmethod
    def _stage(device, candidates, sources, members, vlans) -> set[str]:
        """暂存源父树，保存目标规划并返回实际裁剪的源接口名。

        只对有业务的父树建立暂存名，原名称全部释放后再允许 NNI 写入。
        """
        transform = device.document.interfaces.transform
        parents = sorted({spec.parent for spec in sources})
        existing = {
            spec.parent
            for spec in device.document.interfaces.analysis.interface_specs()
        }
        placeholders = {}
        for index, parent in enumerate(parents):
            staged = f"ADAPT-UNI-{index}"
            while staged in existing:
                staged += "-X"
            existing.add(staged)
            placeholders[parent] = staged
            transform.rename(parent, staged)
        for spec in sorted(sources, key=lambda item: item.name):
            staged = placeholders[spec.parent]
            if spec.unit is not None:
                staged += f".{spec.unit}"
            device.uni_plan.append(
                UniInterfacePlan(
                    spec.name,
                    staged,
                    spec.parent,
                    vlans[spec.name],
                    spec.inner_vlan,
                    spec.vlan,
                    spec.kind,
                )
            )
        return UNIPreparationHandler._remove_unmigrated(
            device, candidates, sources, members, set(parents)
        )

    @staticmethod
    def _remove_unmigrated(
        device, candidates, sources, members, source_parents
    ) -> set[str]:
        """删除裸口和聚合成员，收集没有迁移目标的接口名。

        有业务迁移的父口引用由后续映射或校验处理，裸子口仍需清理依赖。
        """
        transform = device.document.interfaces.transform
        source_names = {spec.name for spec in sources}
        candidate_parents = {spec.parent for spec in candidates}
        removed = candidate_parents - source_parents
        for parent in sorted(removed):
            transform.remove(parent, include_children=True)
        for spec in candidates:
            device.removed_interfaces.update({spec.name, spec.parent})
            if spec.name not in source_names:
                _record_removal(device, spec.name, "未承载独立业务的 UNI 接口")
                if spec.name not in source_parents:
                    removed.add(spec.name)
        for member, bundle in members.items():
            if bundle in candidate_parents:
                transform.remove(member, include_children=True)
                removed.add(member)
                _record_removal(
                    device,
                    member,
                    "UNI 聚合扁平化，删除物理成员",
                    MappingAction.REMOVE
                    if bundle in source_parents
                    else MappingAction.REMOVE_BARE,
                )
        return removed


class UNIHandler:
    """把暂存的 UNI 业务迁移到共享物理口并生成引用映射。

    NNI 已完成迁移，原 NNI 源口占用共享口时也不会发生写入碰撞。
    """

    def process(self, context: ConversionContext) -> None:
        """迁移全部业务，保留源暂存树供后续正常 YAML 清洗处理。

        成功后才发布源到目标映射，网关及业务容器由后续引用阶段保持关联。
        """
        for name, device in sorted(context.devices.items()):
            for plan in device.uni_plan:
                outcome = device.document.interfaces.transform.map_uni(
                    plan.staged,
                    device.profile.uni_parent,
                    plan.vlan,
                    plan.inner_vlan,
                    original_source=plan.source,
                )
                for messages in (outcome.errors, outcome.warnings):
                    messages[:] = [
                        message.replace(
                            interface_parent(plan.staged), plan.parent
                        )
                        for message in messages
                    ]
                for change in outcome.changes:
                    change["source_interface"] = plan.source
                    change["source_interfaces"] = [plan.source]
                    change["source_kind"] = plan.source_kind.value
                    change["target_kind"] = "physical-subinterface"
                    change["interface"] = (
                        f"{device.profile.uni_parent}.{plan.vlan}"
                    )
                    if "path" in change:
                        change["path"] = [
                            str(part).replace(
                                interface_parent(plan.staged), plan.parent
                            )
                            for part in change["path"]
                        ]
                _report(context, name, EventKind.UNI_MIGRATION, outcome)
                if outcome.errors:
                    return
                self._record_mapping(device, plan)
            self._record_parent_mappings(device)

    @staticmethod
    def _record_mapping(device, plan):
        """记录原业务接口和目标标签，供审计及全局引用改写使用。

        单层 VLAN 重编号和 QinQ 内层保留均有明确映射，不依赖接口号猜测。
        """
        device.mappings.append(
            InterfaceMapping(
                device=device.device.name,
                source_interface=plan.source,
                role=MappingRole.UNI,
                action=MappingAction.MAP,
                target_interface=f"{device.profile.uni_parent}.{plan.vlan}",
                reason="UNI 业务汇聚到共享子接口",
                source_vlan=plan.source_vlan,
                target_vlan=plan.vlan,
                inner_vlan=plan.inner_vlan,
            )
        )

    @staticmethod
    def _record_parent_mappings(device):
        """为单一业务父口补充精确引用映射，不提前删除暂存配置。

        多业务父口不直接改写为共享父口，残留歧义引用交由最终校验阻断。
        """
        grouped = defaultdict(list)
        for plan in device.uni_plan:
            grouped[plan.parent].append(plan)
        for parent, plans in grouped.items():
            if len(plans) == 1 and plans[0].source != parent:
                device.mappings.append(
                    InterfaceMapping(
                        device=device.device.name,
                        source_interface=parent,
                        role=MappingRole.UNI,
                        action=MappingAction.MAP,
                        target_interface=f"{device.profile.uni_parent}.{plans[0].vlan}",
                        reason="原父口仅有一项业务，引用改写到该逻辑接口",
                    )
                )


def finalize_uni_sources(context: ConversionContext, name: str) -> bool:
    """在现有清洗之后检查原父口剩余配置，并移除 UNI 暂存树。

    清洗阶段拥有完整源属性，硬件规则能按原层级执行；此处只拒绝仍无法安全
    迁移的业务配置，不再从 YAML 提取命令表达式。
    """
    device = context.devices[name]
    grouped = defaultdict(list)
    for plan in device.uni_plan:
        grouped[plan.parent].append(plan)
    for parent, plans in grouped.items():
        staged = interface_parent(plans[0].staged)
        outcome = device.document.interfaces.transform.finalize_uni_source(
            staged, any(plan.source != parent for plan in plans)
        )
        for messages in (outcome.errors, outcome.warnings):
            messages[:] = [
                message.replace(staged, parent) for message in messages
            ]
        _report(context, name, EventKind.INTERFACE_COMPATIBILITY, outcome)
        if outcome.errors:
            return False
    return True
