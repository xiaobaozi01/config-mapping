"""Cisco IOS XR 模拟镜像参数适配函数。"""

from __future__ import annotations

import re

from ...models import SimulationAdaptationPolicy
from ...parsers.cisco_iosxr import CiscoDocument
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

    for block in document._interface_blocks():
        name = block.interface_name
        if not name or interface_parent(name) not in targets:
            continue
        if policy.remove_physical_interface_knobs:
            retained = [line for line in block.lines if not physical_knob.match(line.strip())]
            outcome.record("removed", "physical-interface-knob", len(block.lines) - len(retained))
            block.lines = retained
        if policy.ensure_data_interfaces_enabled:
            retained = [line for line in block.lines if not shutdown.fullmatch(line.strip())]
            outcome.record("removed", "interface-shutdown", len(block.lines) - len(retained))
            block.lines = retained
            if interface_unit(name) is None and not any(
                no_shutdown.fullmatch(line.strip()) for line in block.lines
            ):
                insertion = (
                    1
                    if block.lines
                    and block.lines[0].strip().lower().startswith("description ")
                    else 0
                )
                block.lines.insert(insertion, " no shutdown")
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

    for block in document.blocks:
        if not block.active:
            continue
        block.lines = [
            clamp(
                clamp(
                    line,
                    interval,
                    policy.bfd_minimum_interval_ms,
                    "bfd-minimum-interval",
                ),
                multiplier,
                policy.bfd_minimum_multiplier,
                "bfd-multiplier",
            )
            for line in block.lines
        ]
    return outcome
