"""Juniper Junos 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from impl.common.domain.interface import interface_parent
from impl.common.domain.outcomes import (
    SimulationAdaptationOutcome,
    SimulationChangeAction,
)
from impl.common.domain.policies import SimulationAdaptationPolicy

from ..document import JuniperNode, JunosDocument
from ..operations.interfaces.access import interface_name, interface_nodes


def adapt_to_simulation(
    document: JunosDocument,
    policy: SimulationAdaptationPolicy,
    data_interfaces: set[str],
) -> SimulationAdaptationOutcome:
    """执行 Junos 模拟环境适配规则。

    固定硬件属性由声明式清洗规则处理，本入口只负责需要结构判断的接口和 BFD 调整。
    """
    from . import adapt_to_simulation as execute

    return execute(document, policy, data_interfaces)


def enable_data_interfaces(document, context):
    """解除已映射数据父接口上的 disable 状态。

    仅处理目标父口，避免意外启用未参与拓扑的管理口或业务接口。
    """
    outcome = SimulationAdaptationOutcome()
    parents = {
        interface_parent(document.interfaces.analysis.resolve_interface(name))
        for name in context.data_interfaces
    }
    _enable_data_interfaces(document, parents, outcome)
    return outcome


def clamp_bfd_timers(document, context):
    """按镜像策略上修过小的 BFD 定时器。

    只提高稳定性下限而不降低已合规值，使转换结果适合虚拟环境且保持幂等。
    """
    outcome = SimulationAdaptationOutcome()
    _clamp_bfd_timers(document, context.policy, outcome)
    return outcome


def _enable_data_interfaces(document, target_parents, outcome):
    """解除目标数据接口的 disable 状态。

    只删除顶层 disable 叶子并保留聚合 options，避免改变成员的协商与封装属性。
    """
    for interface in interface_nodes(document):
        if (
            interface_name(document, interface) not in target_parents
            or not interface.is_block
        ):
            continue
        retained = []
        for child in interface.children:
            base = document._parser.base_header(child.header).rstrip(";")
            token = base.split(maxsplit=1)[0] if base else ""
            if child.effective and token == "disable":
                outcome.record(
                    SimulationChangeAction.REMOVED, "interface-disable"
                )
            else:
                retained.append(child)
        interface.children = retained


def _clamp_bfd_timers(
    document: JunosDocument,
    policy: SimulationAdaptationPolicy,
    outcome: SimulationAdaptationOutcome,
) -> None:
    """把低于阈值的 BFD 定时器提升到策略规定的最小值。

    真机配置常使用激进的 BFD 间隔，GNS3 虚拟环境无法稳定维持；遍历
    ``bfd-liveness-detection`` 子树，仅上修过小值、不降低已合规值。
    """
    parser = document._parser
    bfd_interval_pattern = re.compile(
        r"^(?P<key>minimum-interval|minimum-receive-interval|"
        r"transmit-interval)\s+"
        r"(?P<value>\d+)\s*$",
        re.IGNORECASE,
    )
    bfd_multiplier_pattern = re.compile(
        r"^(?P<key>multiplier)\s+(?P<value>\d+)\s*$",
        re.IGNORECASE,
    )

    def walk(node: JuniperNode, inside_bfd: bool = False) -> None:
        """递归处理 BFD 子树中的可调整叶子。

        BFD 标记只从祖先继承，遇到新的无关分支后不会错误修改其定时器。
        """
        if not node.effective:
            return
        base = parser.base_header(node.header)
        current_bfd = inside_bfd or base.rstrip(";") == "bfd-liveness-detection"
        if current_bfd and not node.is_block:
            for pattern, minimum, category in (
                (
                    bfd_interval_pattern,
                    policy.bfd_minimum_interval_ms,
                    "bfd-minimum-interval",
                ),
                (
                    bfd_multiplier_pattern,
                    policy.bfd_minimum_multiplier,
                    "bfd-multiplier",
                ),
            ):
                match = pattern.match(base)
                if match and int(match.group("value")) < minimum:
                    node.header = f'{match.group("key")} {minimum};'
                    outcome.record(SimulationChangeAction.REPLACED, category)
                    break
        if node.children:
            for child in node.children:
                walk(child, current_bfd)

    walk(document.root)


def remove_empty_interface_options(document, context):
    """省略目标接口下已经变空的 options 容器。

    含 802.3ad 或 LACP 语义的配置不会被删除，因为它们仍决定聚合成员关系。
    """
    outcome = SimulationAdaptationOutcome()
    parents = {
        interface_parent(document.interfaces.analysis.resolve_interface(name))
        for name in context.data_interfaces
    }
    options = {"aggregated-ether-options", "gigether-options", "ether-options"}
    for interface in interface_nodes(document):
        if (
            interface_name(document, interface) not in parents
            or not interface.is_block
        ):
            continue
        interface.children = [
            child
            for child in interface.children
            if not (
                child.effective
                and child.is_block
                and document._parser.base_header(child.header).rstrip(";")
                in options
                and not any(leaf.effective for leaf in child.children or [])
            )
        ]
    return outcome
