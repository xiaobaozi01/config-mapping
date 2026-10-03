"""Cisco IOS XR 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from ...models import SimulationAdaptationPolicy
from ...parsers.cisco_iosxr import CiscoDocument, CiscoNode
from ...parsers.common import SimulationAdaptationOutcome, interface_parent, interface_unit


def adapt_to_simulation(
    document: CiscoDocument,
    policy: SimulationAdaptationPolicy,
    data_interfaces: set[str],
) -> SimulationAdaptationOutcome:
    outcome = SimulationAdaptationOutcome()
    if policy.mode == "off":
        return outcome

    targets = {
        interface_parent(document.resolve_interface(name))
        for name in data_interfaces
    }
    physical_knob = re.compile(
        r"^(?:speed|duplex|negotiation|fec|transceiver|carrier-delay|dampening)\b",
        re.IGNORECASE,
    )
    shutdown = re.compile(r"^shutdown$", re.IGNORECASE)
    no_shutdown = re.compile(r"^no\s+shutdown$", re.IGNORECASE)

    for block in document._interface_nodes():
        name = block.interface_name
        if not name or interface_parent(name) not in targets:
            continue
        if policy.remove_physical_interface_knobs:
            block.children, removed = _remove_matching_nodes(
                block.children,
                physical_knob,
            )
            outcome.record("removed", "physical-interface-knob", removed)
        if policy.ensure_data_interfaces_enabled:
            block.children, removed = _remove_matching_nodes(block.children, shutdown)
            outcome.record("removed", "interface-shutdown", removed)
            if interface_unit(name) is None and not any(
                no_shutdown.fullmatch(node.header) for node in block.walk()
            ):
                insertion = (
                    1
                    if block.children
                    and block.children[0].header.lower().startswith("description ")
                    else 0
                )
                block.children.insert(insertion, CiscoNode("no shutdown"))
                outcome.record("added", "interface-no-shutdown")

    if policy.mode != "stable":
        return outcome

    interval = re.compile(
        r"^(?P<indent>\s*bfd\s+(?:minimum-interval|minimum-receive-interval)\s+)"
        r"(?P<value>\d+)(?P<suffix>\s*)$",
        re.IGNORECASE,
    )
    multiplier = re.compile(
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
                    interval,
                    policy.bfd_minimum_interval_ms,
                    "bfd-minimum-interval",
                ),
                multiplier,
                policy.bfd_minimum_multiplier,
                "bfd-multiplier",
            )
    return outcome


def _remove_matching_nodes(
    nodes: list[CiscoNode],
    pattern: re.Pattern[str],
) -> tuple[list[CiscoNode], int]:
    """递归删除命中模拟不兼容模式的节点及其子树。

    物理参数可能位于接口的嵌套配置模式中；按 AST 节点删除能同时清理相关子命令，
    并返回语义节点数量供适配报告统计。
    """
    retained: list[CiscoNode] = []
    removed = 0
    for node in nodes:
        if not node.is_formatting and pattern.match(node.header):
            removed += sum(1 for _ in node.walk(include_self=True))
            continue
        node.children, child_removed = _remove_matching_nodes(node.children, pattern)
        removed += child_removed
        retained.append(node)
    return retained, removed
