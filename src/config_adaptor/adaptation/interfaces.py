"""接口分类和最终引用改写。"""

from __future__ import annotations

from ..common.interface import InterfaceKind
from .models import ConversionContext

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


class ReferenceRewriteHandler:
    """在接口树迁移完成后修正配置其他位置保存的接口引用。

    NNI/UNI 阶段只负责接口定义及映射记录，路由协议、策略、L2VPN 等配置仍可能
    引用旧名称。该处理器使用最终映射统一改写这些非接口定义，既避免在目标接口
    尚未确定时过早替换，也支持 M-LAG 将一个旧逻辑口展开到多个新接口。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备生成最终替换表，并委托厂商实现改写非接口配置引用。

        ``replacement_map`` 会过滤删除和跳过记录，同时保留同一源接口的一对多目标；
        厂商文档对象再按自身语法安全替换。按设备分别执行可防止跨设备同名接口互相
        污染，并确保改写依据的是前序 NNI/UNI 阶段已经完整生成的映射结果。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            device.document.replace_references(device.replacement_map)
