"""加载 XRv9000/vMX 镜像能够提供的数据接口列表。"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import ImageProfile, Vendor


# 未提供 YAML 时使用的保守默认值；最后两口分别留给 UNI 和保留用途。
DEFAULT_INTERFACES = {
    Vendor.CISCO_IOSXR: [f"GigabitEthernet0/0/0/{index}" for index in range(8)],
    Vendor.JUNIPER_JUNOS: [f"ge-0/0/{index}" for index in range(8)],
}


def load_profiles(path: Path | None) -> dict[Vendor, ImageProfile]:
    """读取镜像接口配置，并验证数量与唯一性。"""
    raw_profiles: dict[str, object] = {}
    if path:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        raw_profiles = payload.get("profiles", {})

    # 两个厂商分别验证，避免一家的错误配置污染另一家。
    result: dict[Vendor, ImageProfile] = {}
    for vendor in (Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS):
        item = raw_profiles.get(vendor.value, {}) if isinstance(raw_profiles, dict) else {}
        configured = item.get("interfaces") if isinstance(item, dict) else None
        interfaces = list(configured) if configured else list(DEFAULT_INTERFACES[vendor])
        if len(interfaces) < 2:
            raise ValueError(f"{vendor.value} 镜像接口列表至少需要两个接口")
        if len(set(interfaces)) != len(interfaces):
            raise ValueError(f"{vendor.value} 镜像接口列表存在重复项")
        result[vendor] = ImageProfile(vendor=vendor, interfaces=interfaces)
    return result
