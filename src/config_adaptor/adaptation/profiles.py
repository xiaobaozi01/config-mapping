"""加载 XRv9000/vMX 镜像能够提供的数据接口列表。"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..common.policies import SimulationAdaptationPolicy
from .models import ImageProfile, Vendor


# 未提供 YAML 时使用的默认值；最后一个数据口专用于 UNI。
DEFAULT_INTERFACES = {
    Vendor.CISCO_IOSXR: [f"GigabitEthernet0/0/0/{index}" for index in range(8)],
    Vendor.JUNIPER_JUNOS: [f"ge-0/0/{index}" for index in range(8)],
}

DEFAULT_IMAGES = {
    Vendor.CISCO_IOSXR: "xrv9000",
    Vendor.JUNIPER_JUNOS: "vmx",
}

_SIMULATION_ADAPTATION_KEYS = {
    "mode",
    "ensure_data_interfaces_enabled",
    "remove_physical_interface_knobs",
    "bfd_minimum_interval_ms",
    "bfd_minimum_multiplier",
}


def _load_simulation_adaptation(
    raw: object,
    vendor: Vendor,
) -> SimulationAdaptationPolicy:
    """读取并严格校验单个镜像的参数适配策略。"""
    if raw is None:
        return SimulationAdaptationPolicy()
    if not isinstance(raw, dict):
        raise ValueError(f"{vendor.value}: simulation_adaptation 必须是键值映射")
    unknown = sorted(set(raw) - _SIMULATION_ADAPTATION_KEYS)
    if unknown:
        raise ValueError(f"{vendor.value}: 未知参数适配配置: {', '.join(unknown)}")

    mode = raw.get("mode", "stable")
    # PyYAML 按 YAML 1.1 会把未加引号的 ``off`` 解析为 False。
    if mode is False:
        mode = "off"
    if mode not in {"off", "compatible", "stable"}:
        raise ValueError(
            f"{vendor.value}: simulation_adaptation.mode 必须是 off、compatible 或 stable"
        )

    values: dict[str, object] = {"mode": mode}
    for key in ("ensure_data_interfaces_enabled", "remove_physical_interface_knobs"):
        value = raw.get(key, True)
        if not isinstance(value, bool):
            raise ValueError(f"{vendor.value}: {key} 必须是 true 或 false")
        values[key] = value

    interval = raw.get("bfd_minimum_interval_ms", 300)
    multiplier = raw.get("bfd_minimum_multiplier", 3)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 50 <= interval <= 60_000:
        raise ValueError(f"{vendor.value}: bfd_minimum_interval_ms 必须是 50..60000 的整数")
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or not 1 <= multiplier <= 255:
        raise ValueError(f"{vendor.value}: bfd_minimum_multiplier 必须是 1..255 的整数")
    values["bfd_minimum_interval_ms"] = interval
    values["bfd_minimum_multiplier"] = multiplier
    return SimulationAdaptationPolicy(**values)


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
        if not isinstance(item, dict):
            raise ValueError(f"{vendor.value}: profile 必须是键值映射")
        configured = item.get("interfaces") if isinstance(item, dict) else None
        interfaces = list(configured) if configured else list(DEFAULT_INTERFACES[vendor])
        if len(interfaces) < 2:
            raise ValueError(f"{vendor.value} 镜像接口列表至少需要两个接口")
        if len(set(interfaces)) != len(interfaces):
            raise ValueError(f"{vendor.value} 镜像接口列表存在重复项")
        image = item.get("image", DEFAULT_IMAGES[vendor])
        version = item.get("version")
        if not isinstance(image, str) or not image.strip():
            raise ValueError(f"{vendor.value}: image 必须是非空字符串")
        if version is not None and not isinstance(version, str):
            raise ValueError(f"{vendor.value}: version 必须是字符串")
        if "simulation_adaptation" in item and "param_adjustment" in item:
            raise ValueError(
                f"{vendor.value}: simulation_adaptation 与旧 param_adjustment 不能同时配置"
            )
        adaptation = item.get("simulation_adaptation", item.get("param_adjustment"))
        result[vendor] = ImageProfile(
            vendor=vendor,
            interfaces=interfaces,
            image=image.strip(),
            version=version,
            simulation_adaptation=_load_simulation_adaptation(adaptation, vendor),
        )
    return result
