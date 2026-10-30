"""核心路由实验的接入裁剪、接口兼容处理与最终校验。"""

from __future__ import annotations

from ..common.interface import interface_parent, InterfaceKind
from ..common.outcomes import InterfaceAdaptationOutcome
from .models import ConversionContext, DeviceContext, InterfaceMapping


def _targets(device: DeviceContext) -> set[str]:
    return {
        interface_parent(mapping.target_interface)
        for mapping in device.mappings
        if mapping.role == "NNI" and mapping.target_interface and mapping.action != "skip"
    }


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
    for message in outcome.errors:
        context.add_event(f"{kind}-error", message, device=name)
    for message in outcome.warnings:
        context.add_event(f"{kind}-warning", message, device=name)


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
            members = device.document.bundle_members()
            business = device.document.business_interface_names()
            orphans = {spec.parent for spec in device.document.interface_specs()
                       if spec.kind == InterfaceKind.BUNDLE and spec.name in business
                       and spec.parent not in set(members.values())}
            if orphans:
                device.errors.append(f"设备 {name} 业务聚合没有可解析的物理成员，归属不明: {', '.join(sorted(orphans))}")
                continue
            outcome = device.document.prune_non_nni(protected)
            device.removed_interfaces.update(outcome.removed_interfaces)
            for interface in sorted(outcome.removed_interfaces):
                device.mappings.append(InterfaceMapping(
                    device=name, source_interface=interface, role="NON_NNI", action="remove",
                    target_interface=None, reason="非 NNI 接口不参与核心路由实验",
                ))
            _report(context, name, "non-nni-cleanup", outcome)


class InterfaceCompatibilityHandler:
    """对最终目标接口执行必要的类型兼容处理，独立于 BFD 模拟策略。"""

    def process(self, context: ConversionContext) -> None:
        for name in sorted(context.devices):
            device = context.devices[name]
            outcome = device.document.adapt_interfaces(_targets(device))
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
            outcome = device.document.validate_interfaces(_targets(device), removed)
            _report(context, name, "interface-validation", outcome)
