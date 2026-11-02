"""根据规范化厂商标识创建配置文档。"""

from __future__ import annotations

from ..impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from ..impl.common.domain.contracts import VendorConfiguration
from ..impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document import (
    JunosDocument,
)
from .models import Vendor


def parse_document(vendor: str, text: str) -> VendorConfiguration:
    """按规范厂商名称创建对应的配置文档。

    工厂集中处理厂商分派，使上层流程只依赖统一协议而无需条件导入具体解析器。
    """
    if vendor == Vendor.CISCO_IOSXR.value:
        return CiscoDocument(text)
    if vendor == Vendor.JUNIPER_JUNOS.value:
        return JunosDocument(text)
    raise ValueError(f"不支持的配置厂商: {vendor}")
