"""加载 XRv9000/vMX 镜像能够提供的数据接口列表。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from impl.common.domain.interface import InterfaceKind, interface_parent
from impl.common.domain.policies import (
    SimulationAdaptationPolicy,
    SimulationMode,
)

from .factory import parse_document
from .models import (
    MIN_PROFILE_INTERFACE_COUNT,
    ImageProfile,
    Vendor,
)

# 未提供 YAML 时使用的默认值；倒数第三个数据口空置，最后两个预留。
DEFAULT_INTERFACES = {
    Vendor.CISCO_IOSXR: [f"GigabitEthernet0/0/0/{index}" for index in range(8)],
    Vendor.JUNIPER_JUNOS: [f"ge-0/0/{index}" for index in range(8)],
}

DEFAULT_IMAGES = {
    vendor: vendor.target_image
    for vendor in (Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS)
}

_SIMULATION_ADAPTATION_KEYS = {
    "mode",
    "ensure_data_interfaces_enabled",
    "remove_physical_interface_knobs",
    "bfd_minimum_interval_ms",
    "bfd_minimum_multiplier",
}


def normalize_data_interfaces(
    vendor: Vendor, interfaces: list[str]
) -> list[str]:
    """规范化镜像端口表并校验物理接口身份。

    别名只用于识别同一端口，规范化后去重可避免同一物理口被重复分配。
    """
    if any(
        not isinstance(name, str) or not name.strip() for name in interfaces
    ):
        raise ValueError(f"{vendor.value} 镜像接口名称必须是非空字符串")
    document = parse_document(vendor.value, "")
    normalized = [
        document.interfaces.analysis.resolve_interface(name)
        for name in interfaces
    ]
    # 分类器负责厂商接口族；完整形态约束防止前缀匹配把垃圾后缀当作物理口。
    shape = (
        r"[A-Za-z-]+\d+(?:/\d+)*(?::\d+)?"
        if vendor == Vendor.CISCO_IOSXR
        else r"[A-Za-z0-9]+-\d+/\d+/\d+(?::\d+)?"
    )
    invalid = [
        name
        for name in normalized
        if document.interfaces.analysis.interface_kind(name)
        != InterfaceKind.PHYSICAL
        or interface_parent(name) != name
        or re.fullmatch(shape, name) is None
    ]
    if invalid:
        raise ValueError(
            f"{vendor.value} 镜像接口必须是可识别的物理父口: {', '.join(invalid)}"
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{vendor.value} 镜像接口列表规范化后存在重复项")
    return normalized


def _load_simulation_adaptation(
    raw: object,
    vendor: Vendor,
) -> SimulationAdaptationPolicy:
    """读取并校验单个镜像的模拟参数适配策略。

    在加载 profile 时完成范围和类型检查，避免错误策略进入转换阶段后才中断。
    """
    if raw is None:
        return SimulationAdaptationPolicy()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{vendor.value}: simulation_adaptation 必须是键值映射"
        )
    unknown = sorted(set(raw) - _SIMULATION_ADAPTATION_KEYS)
    if unknown:
        raise ValueError(
            f"{vendor.value}: 未知参数适配配置: {', '.join(unknown)}"
        )
    return SimulationAdaptationPolicy(**_parse_simulation_values(raw, vendor))


def _parse_simulation_values(
    raw: dict[str, object], vendor: Vendor
) -> dict[str, object]:
    """校验模拟适配开关和 BFD 下限并返回构造字段。

    将限制值集中在 profile 加载阶段，使所有设备采用同一镜像安全边界。
    """
    mode = raw.get("mode", SimulationMode.STABLE)
    # PyYAML 按 YAML 1.1 会把未加引号的 ``off`` 解析为 False。
    if mode is False:
        mode = SimulationMode.OFF
    try:
        mode = SimulationMode(mode)
    except ValueError as exc:
        raise ValueError(
            f"{vendor.value}: simulation_adaptation.mode "
            "必须是 off、compatible 或 stable"
        ) from exc

    values: dict[str, object] = {"mode": mode}
    for key in (
        "ensure_data_interfaces_enabled",
        "remove_physical_interface_knobs",
    ):
        value = raw.get(key, True)
        if not isinstance(value, bool):
            raise ValueError(f"{vendor.value}: {key} 必须是 true 或 false")
        values[key] = value

    interval = raw.get("bfd_minimum_interval_ms", 300)
    multiplier = raw.get("bfd_minimum_multiplier", 3)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 50 <= interval <= 60_000
    ):
        raise ValueError(
            f"{vendor.value}: bfd_minimum_interval_ms 必须是 50..60000 的整数"
        )
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, int)
        or not 1 <= multiplier <= 255
    ):
        raise ValueError(
            f"{vendor.value}: bfd_minimum_multiplier 必须是 1..255 的整数"
        )
    values["bfd_minimum_interval_ms"] = interval
    values["bfd_minimum_multiplier"] = multiplier
    return values


def load_profiles(path: Path | None) -> dict[Vendor, ImageProfile]:
    """读取并验证各厂商镜像 profile。

    缺少自定义文件时使用内置值，同时逐厂商校验接口数量和唯一性以隔离配置错误。
    """
    raw_profiles: dict[str, object] = {}
    if path:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        raw_profiles = payload.get("profiles", {})

    # 两个厂商分别验证，避免一家的错误配置污染另一家。
    result: dict[Vendor, ImageProfile] = {}
    for vendor in (Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS):
        item = (
            raw_profiles.get(vendor.value, {})
            if isinstance(raw_profiles, dict)
            else {}
        )
        if not isinstance(item, dict):
            raise ValueError(f"{vendor.value}: profile 必须是键值映射")
        configured = item.get("interfaces") if isinstance(item, dict) else None
        interfaces = (
            list(configured) if configured else list(DEFAULT_INTERFACES[vendor])
        )
        if len(interfaces) < MIN_PROFILE_INTERFACE_COUNT:
            raise ValueError(f"{vendor.value} 镜像接口列表至少需要四个接口")
        interfaces = normalize_data_interfaces(vendor, interfaces)
        image = item.get("image", DEFAULT_IMAGES[vendor])
        version = item.get("version")
        if not isinstance(image, str) or not image.strip():
            raise ValueError(f"{vendor.value}: image 必须是非空字符串")
        if version is not None and not isinstance(version, str):
            raise ValueError(f"{vendor.value}: version 必须是字符串")
        if "param_adjustment" in item:
            raise ValueError(
                f"{vendor.value}: 请使用 simulation_adaptation，"
                "不再支持 param_adjustment"
            )
        adaptation = item.get("simulation_adaptation")
        result[vendor] = ImageProfile(
            vendor=vendor,
            interfaces=interfaces,
            image=image.strip(),
            version=version,
            simulation_adaptation=_load_simulation_adaptation(
                adaptation, vendor
            ),
        )
    return result
