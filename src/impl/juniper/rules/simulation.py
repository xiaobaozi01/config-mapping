"""Juniper Junos 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from impl.common.interface import interface_parent
from impl.common.outcomes import SimulationAdaptationOutcome
from impl.common.policies import SimulationAdaptationPolicy
from ..document import JunosDocument, JunosNode


def adapt_to_simulation(
    document: JunosDocument, policy: SimulationAdaptationPolicy, data_interfaces: set[str],
) -> SimulationAdaptationOutcome:
    """执行厂商模拟规则；固定硬件删除由 cleaning.yaml 统一执行。"""
    from . import adapt_to_simulation as execute

    return execute(document, policy, data_interfaces)


def enable_data_interfaces(document, context):
    outcome = SimulationAdaptationOutcome()
    parents = {interface_parent(document.resolve_interface(name)) for name in context.data_interfaces}
    _enable_data_interfaces(document, parents, outcome)
    return outcome


def clamp_bfd_timers(document, context):
    outcome = SimulationAdaptationOutcome()
    _clamp_bfd_timers(document, context.policy, outcome)
    return outcome


def _enable_data_interfaces(document, target_parents, outcome):
    """解除目标数据接口的 disable，不改动聚合 options。"""
    for interface in document._interface_nodes():
        if document._interface_name(interface) not in target_parents or interface.children is None:
            continue
        retained = []
        for child in interface.children:
            base = document._parser.base_header(child.header).rstrip(";")
            token = base.split(maxsplit=1)[0] if base else ""
            if child.effective and token == "disable":
                outcome.record("removed", "interface-disable")
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
        r"^(?P<key>minimum-interval|minimum-receive-interval|transmit-interval)\s+"
        r"(?P<value>\d+)\s*;$",
        re.IGNORECASE,
    )
    bfd_multiplier_pattern = re.compile(
        r"^(?P<key>multiplier)\s+(?P<value>\d+)\s*;$",
        re.IGNORECASE,
    )

    def walk(node: JunosNode, inside_bfd: bool = False) -> None:
        if not node.effective:
            return
        base = parser.base_header(node.header)
        current_bfd = inside_bfd or base.rstrip(";") == "bfd-liveness-detection"
        if current_bfd and node.children is None:
            for pattern, minimum, category in (
                (bfd_interval_pattern, policy.bfd_minimum_interval_ms, "bfd-minimum-interval"),
                (bfd_multiplier_pattern, policy.bfd_minimum_multiplier, "bfd-multiplier"),
            ):
                match = pattern.match(base)
                if match and int(match.group("value")) < minimum:
                    node.header = f'{match.group("key")} {minimum};'
                    outcome.record("replaced", category)
                    break
        if node.children:
            for child in node.children:
                walk(child, current_bfd)

    walk(document.root)


def remove_empty_interface_options(document, context):
    """清洗后省略空 options，保留含 802.3ad/lacp 等业务语义的容器。"""
    outcome = SimulationAdaptationOutcome()
    parents = {interface_parent(document.resolve_interface(name)) for name in context.data_interfaces}
    options = {"aggregated-ether-options", "gigether-options", "ether-options"}
    for interface in document._interface_nodes():
        if document._interface_name(interface) not in parents or interface.children is None:
            continue
        interface.children = [
            child for child in interface.children
            if not (child.effective and child.is_block
                    and document._parser.base_header(child.header).rstrip(";") in options
                    and not any(leaf.effective for leaf in child.children or []))
        ]
    return outcome
