"""UNI 识别与 VLAN 规划。"""

from .vlan_allocator import VlanSpaceExhausted, allocate_uni_vlans

__all__ = ["VlanSpaceExhausted", "allocate_uni_vlans"]
