"""转换流程使用的领域模型和共享上下文。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Vendor(StrEnum):
    """首版识别的设备厂商；枚举值同时作为解析器和 profile 键。"""
    CISCO_IOSXR = "cisco_iosxr"
    JUNIPER_JUNOS = "juniper_junos"
    HUAWEI = "huawei"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "Vendor":
        """兼容中英文及常见缩写，将 Excel 厂商文本归一化。"""
        normalized = "".join(value.lower().split()).replace("-", "_")
        if normalized in {"cisco", "思科", "iosxr", "ios_xr", "ciscoiosxr", "cisco_iosxr"}:
            return cls.CISCO_IOSXR
        if normalized in {"juniper", "瞻博", "junos", "vmx", "juniperjunos", "juniper_junos"}:
            return cls.JUNIPER_JUNOS
        if normalized in {"huawei", "华为"}:
            return cls.HUAWEI
        return cls.UNKNOWN


@dataclass(slots=True)
class Device:
    """设备列表中的一行。"""
    name: str
    vendor: Vendor
    config_file: str | None
    row: int


@dataclass(slots=True)
class Link:
    """链接表中的一条物理 NNI；active=False 表示输出时删除。"""
    row: int
    a_device: str
    a_interface: str
    z_device: str
    z_interface: str
    active: bool = True
    skip_reason: str | None = None

    def endpoints(self) -> tuple[tuple[str, str], tuple[str, str]]:
        """以统一顺序返回 A/Z 两端的设备名和接口名。"""
        return (
            (self.a_device, self.a_interface),
            (self.z_device, self.z_interface),
        )

    def interface_for(self, device_name: str) -> str | None:
        """读取指定设备在这条链路上的接口。"""
        if self.a_device == device_name:
            return self.a_interface
        if self.z_device == device_name:
            return self.z_interface
        return None

    def set_interface_for(self, device_name: str, interface: str) -> None:
        """映射完成后回写指定端点的目标接口。"""
        if self.a_device == device_name:
            self.a_interface = interface
        elif self.z_device == device_name:
            self.z_interface = interface


@dataclass(slots=True)
class InterfaceMapping:
    """一条可审计的源接口到目标接口转换记录。"""
    device: str
    source_interface: str
    role: str
    action: str
    target_interface: str | None
    link_rows: list[int] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换成可直接写入 JSON 的字典。"""
        return asdict(self)


@dataclass(slots=True)
class ImageProfile:
    """某厂商 GNS3 镜像按顺序暴露的数据接口。"""
    vendor: Vendor
    interfaces: list[str]

    @property
    def nni_interfaces(self) -> list[str]:
        """除 UNI 专用口和最后保留口之外，其余接口均可分配给 NNI。"""
        if len(self.interfaces) < 2:
            return []
        return self.interfaces[:-2]

    @property
    def uni_parent(self) -> str:
        """倒数第二个接口统一承载所有 UNI 子接口。"""
        if len(self.interfaces) < 2:
            raise ValueError(f"{self.vendor}: 镜像至少需要两个数据接口")
        return self.interfaces[-2]

    @property
    def reserved_interface(self) -> str:
        """最后一个接口保留给未来用途，当前转换不触碰。"""
        if not self.interfaces:
            raise ValueError(f"{self.vendor}: 镜像没有数据接口")
        return self.interfaces[-1]


@dataclass(slots=True)
class DeviceContext:
    """单台设备在转换过程中的文档、规格、映射和诊断信息。"""
    device: Device
    config_path: Path
    document: Any
    profile: ImageProfile
    mappings: list[InterfaceMapping] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def replacement_map(self) -> dict[str, str]:
        """汇总有效接口改名，用于更新协议等全局引用。"""
        return {
            item.source_interface: item.target_interface
            for item in self.mappings
            if item.target_interface and item.action not in {"remove", "skip"}
        }


@dataclass(slots=True)
class TopologyWorkbook:
    """Excel 原对象、字段位置和已解析设备/链路的组合。"""
    path: Path
    workbook: Any
    device_sheet: str
    link_sheet: str
    device_headers: dict[str, int]
    link_headers: dict[str, int]
    devices: list[Device]
    links: list[Link]


@dataclass(slots=True)
class ConversionContext:
    """责任链各处理器共享的一次转换状态。"""
    topology: TopologyWorkbook
    devices: dict[str, DeviceContext]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, kind: str, message: str, **details: Any) -> None:
        """追加结构化事件；details 中禁止放入认证秘密。"""
        event = {"kind": kind, "message": message}
        event.update(details)
        self.events.append(event)

    @property
    def mappings(self) -> list[InterfaceMapping]:
        """按设备名稳定汇总所有接口映射。"""
        result: list[InterfaceMapping] = []
        for name in sorted(self.devices):
            result.extend(self.devices[name].mappings)
        return result

    @property
    def has_errors(self) -> bool:
        """全局或任一设备有错误时，整次转换视为失败。"""
        return bool(self.errors or any(device.errors for device in self.devices.values()))
