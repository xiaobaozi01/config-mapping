"""接口分类、NNI 保留范围处理、兼容适配与最终校验。"""

from __future__ import annotations

from typing import cast

from impl.common.interface import InterfaceKind, interface_parent
from impl.common.outcomes import InterfaceAdaptationOutcome
from impl.cisco.document import CiscoDocument
from .cleaning_rules import target_parents
from .models import ConversionContext, InterfaceMapping, Vendor


def _report(
    context: ConversionContext, name: str, kind: str, outcome: InterfaceAdaptationOutcome,
) -> None:
    device = context.devices[name]
    device.errors.extend(f"设备 {name}: {message}" for message in outcome.errors)
    device.warnings.extend(f"设备 {name}: {message}" for message in outcome.warnings)
    for change in outcome.changes:
        details = dict(change)
        target = str(change.get("interface", ""))
        if target:
            details["source_interfaces"] = sorted({
                mapping.source_interface for mapping in device.mappings
                if mapping.target_interface == target
            })
            details.setdefault("target_interface", target)
        context.add_event(kind, f"设备 {name}: {change.get('reason', kind)}", device=name, **details)
    _report_diagnostics(context, name, kind, outcome)


def _report_diagnostics(context, name, kind, outcome):
    for level, messages in (("error", outcome.errors), ("warning", outcome.warnings)):
        for message in messages:
            rule_ids = list(dict.fromkeys(
                item["rule_id"] for item in outcome.diagnostics
                if item["level"] == level and item["message"] == message
            ))
            context.add_event(
                f"{kind}-{level}", message, device=name, rule_ids=rule_ids,
            )


class PreconfiguredInterfaceCleanupHandler:
    """在接口分类前忽略 IOS XR 尚未实例化的物理接口候选配置。"""

    def process(self, context: ConversionContext) -> None:
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
                    "preconfigured-interface-cleanup",
                    f"设备 {device_name} 已忽略未实例化的预配置接口",
                    device=device_name,
                    removed_by_type={"preconfigured-interface": removed},
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
                    "interface-classification-warning",
                    message,
                    device=device_name,
                    interface=interface,
                    classification=InterfaceKind.UNKNOWN.value,
                    action="preserve",
                )


class NonNNICleanupHandler:
    """在端口映射前按原始拓扑确定 NNI 保护闭包，裁剪其余接入业务。"""

    def process(self, context: ConversionContext) -> None:
        for name in sorted(context.devices):
            device = context.devices[name]
            # inactive 的预检跳过行同样保护，避免把暂时缺失的拓扑端点当作 UNI。
            protected = {
                interface_parent(device.document.resolve_interface(interface))
                for link in context.topology.links
                for endpoint, interface in link.endpoints()
                if endpoint == name
            }
            outcome = device.document.prune_non_nni(protected)
            device.removed_interfaces.update(outcome.removed_interfaces)
            for interface in sorted(outcome.removed_interfaces):
                device.mappings.append(InterfaceMapping(
                    device=name, source_interface=interface, role="NON_NNI", action="remove",
                    target_interface=None, reason="非 NNI 接口不参与核心路由实验",
                ))
            _report(context, name, "non-nni-cleanup", outcome)


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
    """对最终目标接口执行必要的类型兼容处理，独立于 BFD 模拟策略。"""

    def process(self, context: ConversionContext) -> None:
        for name in sorted(context.devices):
            device = context.devices[name]
            outcome = device.document.adapt_interfaces(set(target_parents(device)))
            _report(context, name, "interface-compatibility", outcome)


class FinalInterfaceValidationHandler:
    """最后一道检查，不能把已知非法配置输出为转换成功。"""

    def process(self, context: ConversionContext) -> None:
        for name in sorted(context.devices):
            device = context.devices[name]
            removed = device.removed_interfaces | {
                mapping.source_interface for mapping in device.mappings
                if mapping.role == "NNI" and mapping.action not in {"skip", "preserve"}
            }
            outcome = device.document.validate_interfaces(set(target_parents(device)), removed)
            _report(context, name, "interface-validation", outcome)
