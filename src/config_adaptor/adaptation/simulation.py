"""目标镜像模拟参数适配的业务调度。"""

from __future__ import annotations

from ..common.interface import interface_parent
from .models import ConversionContext

class SimulationAdaptationHandler:
    """根据目标镜像 Profile 调整最终数据口及平台相关的模拟运行参数。

    真机配置中的接口启停、BFD 定时器或物理硬件选项可能不适合 GNS3 镜像。该阶段
    只把已经完成 NNI/UNI 映射的目标父接口交给厂商适配器，并使用 Profile 中的
    明确策略做保守修改，避免对管理口或未参与迁移的业务进行全局、无差别改写。
    """

    def process(self, context: ConversionContext) -> None:
        """汇总最终数据接口，执行厂商模拟适配并记录逐类变更结果。

        数据口集合只来自成功映射的 NNI/UNI 目标，排除删除、裸口清理和跳过记录，
        因而不会误触未纳入拓扑的接口。适配安排在引用改写和配置清洗之后，是为了让
        厂商实现看到最终接口结构；事件同时记录镜像、版本、策略模式和增删改统计，
        方便确认模拟兼容性调整的依据与影响范围。
        """
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
