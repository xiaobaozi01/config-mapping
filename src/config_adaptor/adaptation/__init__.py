"""拓扑驱动的跨厂商配置自适应业务。"""

from .models import DeviceAdaptationResult, DeviceLink, PhysicalPortMapping
from .service import adapt_device, convert, prepare_context

__all__ = [
    "DeviceAdaptationResult",
    "DeviceLink",
    "PhysicalPortMapping",
    "adapt_device",
    "convert",
    "prepare_context",
]
