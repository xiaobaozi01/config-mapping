"""Cisco header 语义解析和文档级缓存。"""

from __future__ import annotations

import re
from functools import lru_cache

from impl.common.domain.interface import InterfaceKind, interface_parent
from impl.common.support.interface_cache import PARSER_HEADER_CACHE_SIZE

from .semantics import (
    _interface_name_from_header,
    _is_l2transport_header,
    canonical_cisco_interface,
)

_CISCO_PHYSICAL_INTERFACE = re.compile(
    r"^(?:Ethernet|FastEthernet|GigabitEthernet|TenGigE|TwentyFiveGigE|"
    r"FortyGigE|FiftyGigE|HundredGigE|FourHundredGigE|POS|Serial)\d",
    re.IGNORECASE,
)
_CISCO_VIRTUAL_INTERFACE = re.compile(
    r"^(?:Tunnel(?:-ip|-te)?|Null|PW-Ether|VASI(?:Left|Right)?|"
    r"NVE|Multilink)\d",
    re.IGNORECASE,
)


def _cisco_interface_kind(name: str) -> InterfaceKind:
    """按 IOS XR 父接口名前缀返回接口类别。

    无法识别的名称返回未知类型，不乐观地当作物理接口。
    """
    parent = interface_parent(name).lower()
    if re.fullmatch(r"loopback\d+", parent):
        return InterfaceKind.LOOPBACK
    if re.match(r"^mgmteth\d", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"bundle-ether\d+", parent):
        return InterfaceKind.BUNDLE
    if re.fullmatch(r"bvi\d+", parent):
        return InterfaceKind.GATEWAY
    if re.match(r"^ptp(?:\d|$)", parent, re.IGNORECASE):
        return InterfaceKind.VIRTUAL
    if _CISCO_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _CISCO_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


def _resolved_interface_kind(name: str) -> InterfaceKind:
    """先规范化外部接口名，再解析接口类别。

    该函数可安全进入文档级缓存，因为它不依赖文档实例状态。
    """
    return _cisco_interface_kind(canonical_cisco_interface(name))


class _CiscoHeaderParser:
    """保存单份 Cisco 文档使用的字符串解析缓存。

    缓存只依赖不可变 header，不持有文档或节点。
    """

    def __init__(self) -> None:
        """建立接口名、类别和二层模式的有界缓存。

        每份文档拥有独立缓存，避免不同配置之间互相污染。
        """
        self.canonical_interface = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            canonical_cisco_interface
        )
        self.interface_kind = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _resolved_interface_kind
        )
        self.interface_name = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _interface_name_from_header
        )
        self.l2transport = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _is_l2transport_header
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> _CiscoHeaderParser:
        """为树副本创建空解析缓存。

        不共享包装器可避免旧缓存键进入新的文档修订。
        """
        return type(self)()
