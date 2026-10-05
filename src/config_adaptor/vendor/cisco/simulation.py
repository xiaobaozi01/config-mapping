"""Cisco IOS XR 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from ...models import SimulationAdaptationPolicy
from ...parsers.cisco_iosxr import CiscoDocument, CiscoNode, strip_matching_nodes
from ...parsers.common import SimulationAdaptationOutcome, interface_parent, interface_unit


def adapt_to_simulation(
    document: CiscoDocument,
    policy: SimulationAdaptationPolicy,
    data_interfaces: set[str],
) -> SimulationAdaptationOutcome:
    """按镜像策略适配已迁移数据口及 IOS XR 模拟运行参数，并返回分类统计。

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
    document: CiscoDocument,
    policy: SimulationAdaptationPolicy,
    target_parents: set[str],
    outcome: SimulationAdaptationOutcome,
) -> None:
    """在目标接口上清除模拟镜像不兼容的物理属性并确保接口启用。

    真机上的 speed/fec/carrier-delay 等物理参数在 XRv9000 镜像上可能无效；删除
    ``shutdown`` 并为物理口补 ``no shutdown``，避免迁移后接口仍处于关闭状态。
    """
    physical_knob = re.compile(
        r"^(?:speed|duplex|negotiation|fec|transceiver|carrier-delay|dampening)\b",
        re.IGNORECASE,
    )
    shutdown = re.compile(r"^shutdown$", re.IGNORECASE)
    no_shutdown = re.compile(r"^no\s+shutdown$", re.IGNORECASE)

    for block in document._interface_nodes():
        name = block.interface_name
        if not name or interface_parent(name) not in target_parents:
            continue
        if policy.remove_physical_interface_knobs:
            block.children, removed = strip_matching_nodes(
                block.children,
                physical_knob,
            )
            outcome.record("removed", "physical-interface-knob", removed)
        if policy.ensure_data_interfaces_enabled:
            block.children, removed = strip_matching_nodes(block.children, shutdown)
            outcome.record("removed", "interface-shutdown", removed)
            if interface_unit(name) is None and not any(
                no_shutdown.fullmatch(node.header) for node in block.walk()
            ):
                insert_at = (
                    1
                    if block.children
                    and block.children[0].header.lower().startswith("description ")
                    else 0
                )
                block.children.insert(insert_at, CiscoNode("no shutdown"))
                outcome.record("added", "interface-no-shutdown")


def _clamp_bfd_timers(
    document: CiscoDocument,
    policy: SimulationAdaptationPolicy,
    outcome: SimulationAdaptationOutcome,
) -> None:
    """把低于阈值的 BFD 定时器提升到策略规定的最小值。

    真机配置常使用激进的 BFD 间隔，GNS3 虚拟环境无法稳定维持；遍历所有活动节点，
    仅上修过小值、不降低已合规值。
    """
    bfd_interval_pattern = re.compile(
        r"^(?P<indent>\s*bfd\s+(?:minimum-interval|minimum-receive-interval)\s+)"
        r"(?P<value>\d+)(?P<suffix>\s*)$",
        re.IGNORECASE,
    )
    bfd_multiplier_pattern = re.compile(
        r"^(?P<indent>\s*bfd\s+multiplier\s+)(?P<value>\d+)(?P<suffix>\s*)$",
        re.IGNORECASE,
    )

    def clamp(line: str, pattern: re.Pattern[str], minimum: int, category: str) -> str:
        match = pattern.match(line)
        if not match or int(match.group("value")) >= minimum:
            return line
        outcome.record("replaced", category)
        return f'{match.group("indent")}{minimum}{match.group("suffix")}'

    for block in document.root.children:
        if not block.active:
            continue
        for node in block.walk():
            node.header = clamp(
                clamp(
                    node.header,
                    bfd_interval_pattern,
                    policy.bfd_minimum_interval_ms,
                    "bfd-minimum-interval",
                ),
                bfd_multiplier_pattern,
                policy.bfd_minimum_multiplier,
                "bfd-multiplier",
            )
