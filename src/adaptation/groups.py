"""Group 展开的业务调度。"""

from __future__ import annotations

from collections import defaultdict

from impl.common.domain.interface import interface_parent

from .models import ConversionContext, EventKind


class GroupExpansionHandler:
    """按 Group 策略把厂商继承配置物化到后续可分析的配置树中。

    处理器把活动拓扑接口作为通配 group 的候选对象，逐设备执行厂商专用展开，
    并汇总警告、冲突、语义规则覆盖率和失败状态。该阶段必须早于接口分类与迁移，
    因为接口地址、聚合关系或业务绑定可能只存在于 group 中；若不先展开，后续会
    基于不完整配置误判接口角色。策略要求保留 group 时则完全跳过物化。
    """

    def process(self, context: ConversionContext) -> None:
        """收集活动拓扑接口并逐设备展开 group，将结果写入共享上下文。

        把拓扑中的父接口加入展开候选，随后记录厂商展开器返回的普通事件、
        冲突、歧义和规则命中率。
        展开失败会同时写入设备及全局错误，使流水线停止，因为继续使用部分展开的
        配置进行接口迁移可能丢失继承命令或覆盖显式配置。
        """
        # 拓扑接口即使未显式出现在配置中，也要作为正则/通配 group 的候选对象。
        known_by_device: dict[str, set[str]] = defaultdict(set)
        for link in context.topology.links:
            if not link.active:
                continue
            for device_name, interface in link.endpoints():
                if device_name in context.devices:
                    known_by_device[device_name].add(
                        interface_parent(interface)
                    )

        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            outcome = device.document.expand_groups(
                known_by_device[device_name],
                policy=context.washing_policy,
            )
            self._record_outcome(context, device_name, device, outcome)

    @staticmethod
    def _record_outcome(context, device_name, device, outcome) -> None:
        """把一次 group 展开结果转换为经验证的事件、警告和错误。

        所有结果在调度层统一落地，使厂商展开器只负责计算，并让失败状态终止后续处理。
        """
        device.warnings.extend(outcome.warnings)
        for message in outcome.events:
            context.add_event(
                EventKind.GROUP_EXPANSION,
                f"设备 {device_name}: {message}",
                device=device_name,
            )
        for conflict in outcome.conflicts:
            context.add_event(
                EventKind.GROUP_CONFLICT,
                f"设备 {device_name} 的 group 配置冲突已按继承优先级处理",
                device=device_name,
                **conflict,
            )
        for ambiguity in outcome.ambiguities:
            context.add_event(
                EventKind.GROUP_IDENTITY_AMBIGUOUS,
                f"设备 {device_name} 存在规则未覆盖的潜在 group 语义冲突",
                device=device_name,
                **ambiguity,
            )
        identity_total = outcome.identity_rule_hits + outcome.identity_fallbacks
        if identity_total:
            context.add_event(
                EventKind.GROUP_IDENTITY_COVERAGE,
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
