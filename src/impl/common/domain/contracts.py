"""跨厂商文档能力的兼容导出。"""

from .capabilities.document import DocumentCore, VendorDocument

VendorConfiguration = VendorDocument

__all__ = ["DocumentCore", "VendorConfiguration", "VendorDocument"]
