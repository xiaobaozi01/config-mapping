"""Juniper Junos 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from ..common.interface import interface_parent
from ..common.outcomes import SimulationAdaptationOutcome
from ..common.policies import SimulationAdaptationPolicy
from .document import JunosDocument, JunosNode


def adapt_to_simulation(
    document: JunosDocument,
    policy: SimulationAdaptationPolicy,
    data_interfaces: set[str],
) -> SimulationAdaptationOutcome:
    """按镜像策略适配已迁移数据口及 Junos 模拟运行参数，并返回分类统计。

    ``off`` 模式不动文档；接口兼容清理始终执行，而 BFD 稳定性收敛只在
    ``stable`` 模式下进行，避免 ``compatible`` 等模式误改协议定时器。
    """
    outcome = SimulationAdaptationOutcome()
    if policy.mode == "off":
        return outcome

    target_parents = {
        interface_parent(document.resolve_interface(name))
        for name in data_interfaces
    }
    _adapt_interface_knobs(document, policy, target_parents, outcome)
    if policy.mode == "stable":
        _clamp_bfd_timers(document, policy, outcome)
    return outcome


def _adapt_interface_knobs(
    document: JunosDocument,
    policy: SimulationAdaptationPolicy,
    target_parents: set[str],
    outcome: SimulationAdaptationOutcome,
) -> None:
    """在目标接口上清除模拟镜像不兼容的物理属性并解除 ``disable``。

    真机上的 speed/fec/gigether-options 等物理参数在 vMX 镜像上可能无效；按策略
    开关只处理命中 ``target_parents`` 的接口，避免误改未参与映射的端口。
    """
    knob_leaf_pattern = re.compile(
        r"^(?:speed|link-mode|fec|no-auto-negotiation|loopback|clocking)\b",
        re.IGNORECASE,
    )
    knob_block_names = {"aggregated-ether-options", "gigether-options", "ether-options"}
    for interface in document._interface_nodes():
        if (
            document._interface_name(interface) not in target_parents
            or interface.children is None
        ):
            continue
        retained: list[JunosNode] = []
        for child in interface.children:
            if not child.effective:
                retained.append(child)
                continue
            base = document._base_header(child.header).rstrip(";")
            token = base.split(maxsplit=1)[0] if base else ""
            if policy.ensure_data_interfaces_enabled and token == "disable":
                outcome.record("removed", "interface-disable")
                continue
            if policy.remove_physical_interface_knobs and (
                knob_leaf_pattern.match(base)
                or (child.is_block and base in knob_block_names)
            ):
                outcome.record("removed", "physical-interface-knob")
                continue
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
        base = document._base_header(node.header)
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
