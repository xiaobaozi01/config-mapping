"""Juniper Junos 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from ...models import SimulationAdaptationPolicy
from ...parsers.common import SimulationAdaptationOutcome, interface_parent
from ...parsers.juniper_junos import JunosDocument, JunosNode


def adapt_to_simulation(
    document: JunosDocument,
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
    blocked_leaf = re.compile(
        r"^(?:speed|link-mode|fec|no-auto-negotiation|loopback|clocking)\b",
        re.IGNORECASE,
    )
    blocked_blocks = {"aggregated-ether-options", "gigether-options", "ether-options"}
    for interface in document._interface_nodes():
        if (
            document._interface_name(interface) not in targets
            or interface.children is None
        ):
            continue
        retained: list[JunosNode] = []
        for child in interface.children:
            if not child.effective:
                retained.append(child)
                continue
            base = document._base_header(child.header).rstrip(";")
            first = base.split(maxsplit=1)[0] if base else ""
            if policy.ensure_data_interfaces_enabled and first == "disable":
                outcome.record("removed", "interface-disable")
                continue
            if policy.remove_physical_interface_knobs and (
                blocked_leaf.match(base) or (child.is_block and base in blocked_blocks)
            ):
                outcome.record("removed", "physical-interface-knob")
                continue
            retained.append(child)
        interface.children = retained

    if policy.mode != "stable":
        return outcome

    interval = re.compile(
        r"^(?P<key>minimum-interval|minimum-receive-interval|transmit-interval)\s+"
        r"(?P<value>\d+)\s*;$",
        re.IGNORECASE,
    )
    multiplier = re.compile(
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
                (interval, policy.bfd_minimum_interval_ms, "bfd-minimum-interval"),
                (multiplier, policy.bfd_minimum_multiplier, "bfd-multiplier"),
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
    return outcome
