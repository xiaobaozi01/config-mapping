"""根据规范化厂商标识创建配置文档。"""

from __future__ import annotations

from impl.cisco.document import CiscoDocument
from impl.common.contracts import VendorConfiguration
from impl.juniper.document import JunosDocument


def parse_document(vendor: str, text: str) -> VendorConfiguration:
    if vendor == "cisco_iosxr":
        return CiscoDocument(text)
    if vendor == "juniper_junos":
        return JunosDocument(text)
    raise ValueError(f"不支持的配置厂商: {vendor}")
