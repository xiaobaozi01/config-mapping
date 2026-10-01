"""UNI 外层 VLAN 的确定性分配策略。"""

from __future__ import annotations

from collections.abc import Iterable

from ...parsers.common import InterfaceSpec


class VlanSpaceExhausted(ValueError):
    """设备的可用 UNI VLAN 空间已经耗尽。"""


MIN_VLAN = 2
MAX_VLAN = 4094


def allocate_uni_vlans(interfaces: Iterable[InterfaceSpec]) -> dict[str, int]:
    """优先保留合法源 VLAN，冲突时分配最小空闲 VLAN。"""
    used: set[int] = set()
    result: dict[str, int] = {}
    for interface in interfaces:
        requested = interface.vlan
        if (
            requested is not None
            and MIN_VLAN <= requested <= MAX_VLAN
            and requested not in used
        ):
            vlan = requested
        else:
            vlan = next(
                (
                    candidate
                    for candidate in range(MIN_VLAN, MAX_VLAN + 1)
                    if candidate not in used
                ),
                None,
            )
        if vlan is None:
            raise VlanSpaceExhausted("UNI 数量超过 VLAN 可用空间")
        used.add(vlan)
        result[interface.name] = vlan
    return result
