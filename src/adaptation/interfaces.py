"""接口分类、NNI 保留范围处理、兼容适配与最终校验。"""

from __future__ import annotations

from typing import cast

from impl.cisco.document import CiscoDocument
from impl.common.interface import InterfaceKind, interface_parent
from impl.common.outcomes import InterfaceAdaptationOutcome

from .cleaning_rules import target_parents
from .models import (
    ConversionContext,
    EventKind,
    InterfaceMapping,
    MappingAction,
    MappingRole,
    Vendor,
)


def _report(
    context: ConversionContext,
    name: str,
    kind: EventKind,
    outcome: InterfaceAdaptationOutcome,
) -> None:
    """把厂商接口处理结果写入设备状态和全局结构化事件。"""
    device = context.devices[name]
    device.errors.extend(
        f"设备 {name}: {message}" for message in outcome.errors
    )
    device.warnings.extend(
        f"设备 {name}: {message}" for message in outcome.warnings
    )
    for change in outcome.changes:
        details = dict(change)
        target = str(change.get("interface", ""))
        if target:
            details["source_interfaces"] = sorted(
                {
                    mapping.source_interface
                    for mapping in device.mappings
                    if mapping.target_interface == target
                }
            )
            details.setdefault("target_interface", target)
        context.add_event(
            kind,
            f"设备 {name}: {change.get('reason', kind.value)}",
            device=name,
            **details,
        )
    _report_diagnostics(context, name, kind, outcome)


def _report_diagnostics(
    context: ConversionContext,
    name: str,
    kind: EventKind,
    outcome: InterfaceAdaptationOutcome,
) -> None:
    """把规则级错误和告警转换为带来源 ID 的处理器事件。"""
    for level, messages in (
        ("error", outcome.errors),
        ("warning", outcome.warnings),
    ):
        event_kind = EventKind(f"{kind.value}-{level}")
        for message in messages:
            rule_ids = list(
                dict.fromkeys(
                    item["rule_id"]
                    for item in outcome.diagnostics
                    if item["level"] == level and item["message"] == message
                )
            )
            context.add_event(
                event_kind,
                message,
                device=name,
                rule_ids=rule_ids,
            )


def _source_semantic_parents(
    context: ConversionContext,
    device_name: str,
    device,
) -> set[str]:
    """收集映射前需要审计的拓扑接口、聚合闭包和网关父口。

    仅消费活动链路，并纳入已选 Bundle 的全部物理成员，避免审计遗漏
    未直接出现在拓扑表中的同 Bundle 成员。
    """
    members = device.document.bundle_members()
    parents = set()
    selected_bundles = set()
    for link in context.topology.links:
        if not link.active:
            continue
        for endpoint_name, raw_interface in link.endpoints():
            if endpoint_name != device_name:
                continue
            parent = interface_parent(
                device.document.resolve_interface(raw_interface)
            )
            parents.add(parent)
            if bundle := members.get(parent):
                parents.add(bundle)
                selected_bundles.add(bundle)
    parents.update(
        member
        for member, bundle in members.items()
        if bundle in selected_bundles
    )
    parents.update(
        spec.parent
        for spec in device.document.interface_specs()
        if spec.kind == InterfaceKind.GATEWAY
    )
    return parents


class PreconfiguredInterfaceCleanupHandler:
    """在接口分类前删除 IOS XR 尚未实例化的接口候选块。

    预配置接口不代表当前拓扑真实存在，提前移除以避免错误占用端口或干扰分类。
    """

    def process(self, context: ConversionContext) -> None:
        """逐台 Cisco 设备删除预配置接口并记录汇总事件。

        删除通过事务一次提交，避免部分设备或部分接口被移出后影响后续分类一致性。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            if device.device.vendor != Vendor.CISCO_IOSXR:
                continue
            document = cast(CiscoDocument, device.document)
            removed = 0
            transaction = document.rewrite()
            for block in document.root.nodes:
                if block.is_preconfigured_interface:
                    transaction.delete(block, "忽略未实例化的预配置接口")
                    removed += 1
            transaction.commit()
            if removed:
                context.add_event(
                    EventKind.PRECONFIGURED_INTERFACE_CLEANUP,
                    f"设备 {device_name} 已忽略未实例化的预配置接口",
                    device=device_name,
                    removed_by_type={"preconfigured-interface": removed},
                )


class SourceSemanticValidationHandler:
    """在接口裁剪和映射前检查源配置语义。

    默认模式只对既有源问题告警，严格模式则在任何映射前阻断。
    """

    def process(self, context: ConversionContext) -> None:
        """审计拓扑相关源接口，并按严格策略报告既有语义问题。

        默认模式把问题写成 warning，严格模式直接写成 error 并终止流水线。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            parents = _source_semantic_parents(
                context,
                device_name,
                device,
            )
            outcome = device.document.validate_source_semantics(parents)
            if context.strict_input_validation:
                _report(
                    context,
                    device_name,
                    EventKind.INTERFACE_VALIDATION,
                    outcome,
                )
                continue
            source_errors = set(outcome.errors)
            outcome.warnings.extend(outcome.errors)
            outcome.errors.clear()
            for diagnostic in outcome.diagnostics:
                if (
                    diagnostic["level"] == "error"
                    and diagnostic["message"] in source_errors
                ):
                    diagnostic["level"] = "warning"
            _report(
                context,
                device_name,
                EventKind.INTERFACE_VALIDATION,
                outcome,
            )


class InterfaceClassificationHandler:
    """审计厂商接口分类结果，并对未知接口采用保守的保留策略。

    处理器从每台设备的接口规格中找出无法归类的父接口，为其生成设备警告和结构化
    事件，但不修改配置。未知类型可能是尚未覆盖的新硬件或虚拟接口；在没有可靠
    语义时排除映射比按物理口猜测更安全，可避免删除控制接口或错误搬迁业务。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备汇总未知父接口，并为每个接口记录一次告警和保留事件。

        先按父接口去重并排序，可避免多个 unit 重复告警，也让相同输入产生稳定的
        报告顺序。事件明确写入 ``classification=unknown`` 和 ``action=preserve``，
        使用户能够区分“有意保留”与“处理器遗漏”，而后续映射阶段会自然忽略这些
        不属于物理口、聚合口或网关口的接口。
        """
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
                    EventKind.INTERFACE_CLASSIFICATION_WARNING,
                    message,
                    device=device_name,
                    interface=interface,
                    classification=InterfaceKind.UNKNOWN.value,
                    action=MappingAction.PRESERVE.value,
                )


class NonNNICleanupHandler:
    """在端口映射前裁剪不属于 NNI 保留闭包的接口业务。

    保护范围直接来自原始拓扑，包含预检跳过的链路，避免迁移改变 NNI 身份后误删端点。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备执行非 NNI 裁剪并记录删除接口与审计结果。

        接口删除先写入设备共享集合，后续引用改写和最终校验才能看到完整的失效范围。
        """
        for name in sorted(context.devices):
            device = context.devices[name]
            # inactive 的预检跳过行同样保护，避免把暂时缺失的拓扑端点当作 UNI。
            protected = {
                interface_parent(device.document.resolve_interface(interface))
                for link in context.topology.links
                for endpoint, interface in link.endpoints()
                if endpoint == name
            }
            outcome = device.document.prune_non_nni(
                protected,
                strict_input_validation=context.strict_input_validation,
            )
            device.removed_interfaces.update(outcome.removed_interfaces)
            device.deferred_interface_references.update(
                outcome.deferred_interface_references
            )
            for interface in sorted(outcome.removed_interfaces):
                device.mappings.append(
                    InterfaceMapping(
                        device=name,
                        source_interface=interface,
                        role=MappingRole.NON_NNI,
                        action=MappingAction.REMOVE,
                        target_interface=None,
                        reason="非 NNI 接口不参与核心路由实验",
                    )
                )
            _report(context, name, EventKind.NON_NNI_CLEANUP, outcome)


class ReferenceRewriteHandler:
    """在接口树迁移完成后修正配置其他位置保存的接口引用。

    NNI 阶段只负责接口定义及映射记录，路由协议、策略、L2VPN 等配置仍可能
    引用旧名称。该处理器使用最终映射统一改写这些非接口定义，既避免在目标接口
    尚未确定时过早替换，也支持 M-LAG 将一个旧逻辑口展开到多个新接口。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备生成最终替换表，并委托厂商实现改写非接口配置引用。

        ``replacement_map`` 会过滤删除和跳过记录，同时保留同一源接口的一对多目标；
        厂商文档对象再按自身语法安全替换。按设备分别执行可防止跨设备同名接口互相
        污染，并确保改写依据的是前序 NNI 阶段已经完整生成的映射结果。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            device.document.replace_references(device.replacement_map)


class InterfaceCompatibilityHandler:
    """对最终目标接口执行必要的类型兼容处理。

    该处理独立于模拟策略，因为基础接口语法即使关闭可选参数适配也必须可导入。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备适配目标接口并汇总变更、告警和错误。

        只传成功 NNI 映射的目标父口，避免兼容规则修改未参与转换的原始业务接口。
        """
        for name in sorted(context.devices):
            device = context.devices[name]
            outcome = device.document.adapt_interfaces(
                set(target_parents(device))
            )
            _report(context, name, EventKind.INTERFACE_COMPATIBILITY, outcome)


class FinalInterfaceValidationHandler:
    """检查最终接口类型和残留引用，阻止非法配置输出。

    校验只读地在所有改写完成后运行，使前面阶段无法用继续修改掩盖已知错误。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备收集目标接口和已删除来源并执行最终验证。

        删除集合在验证时即时汇总，确保 preserve 和 skip 映射不会被误判为残留引用。
        """
        for name in sorted(context.devices):
            device = context.devices[name]
            removed = device.removed_interfaces | {
                mapping.source_interface
                for mapping in device.mappings
                if mapping.role == MappingRole.NNI
                and mapping.action
                not in {MappingAction.SKIP, MappingAction.PRESERVE}
            }
            outcome = device.document.validate_final_integrity(
                set(target_parents(device)),
                removed,
                device.deferred_interface_references,
            )
            _report(context, name, EventKind.INTERFACE_VALIDATION, outcome)
