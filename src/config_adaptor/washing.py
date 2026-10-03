"""加载默认关闭的扩展配置清洗策略。"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import WashingPolicy


_OPTIONAL_KEYS = {
    "protocol_authentication",
    "pki",
    "hardware",
    "nat",
    "flow_statistics",
}


def load_washing_policy(path: Path | None) -> WashingPolicy:
    """读取 group 处理模式和清洗开关。"""
    if path is None:
        return WashingPolicy()
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    optional = payload.get("optional_washing", {})
    if not isinstance(optional, dict):
        raise ValueError("optional_washing 必须是键值映射")
    unknown = sorted(set(optional) - _OPTIONAL_KEYS)
    if unknown:
        raise ValueError(f"未知可选清洗开关: {', '.join(unknown)}")
    group_handling = payload.get("group_handling", {})
    if not isinstance(group_handling, dict):
        raise ValueError("group_handling 必须是键值映射")
    unknown_group_keys = sorted(set(group_handling) - {"mode", "unknown_identity"})
    if unknown_group_keys:
        raise ValueError(f"未知 group 处理配置: {', '.join(unknown_group_keys)}")
    group_mode = group_handling.get("mode", "relevant")
    if group_mode not in {"relevant", "strict", "preserve"}:
        raise ValueError("group_handling.mode 必须是 relevant、strict 或 preserve")
    unknown_identity = group_handling.get("unknown_identity", "warn")
    if unknown_identity not in {"preserve", "warn", "fail"}:
        raise ValueError(
            "group_handling.unknown_identity 必须是 preserve、warn 或 fail"
        )

    values: dict[str, bool | str] = {
        "group_handling": group_mode,
        "group_unknown_identity": unknown_identity,
    }
    for key in _OPTIONAL_KEYS:
        value = optional.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"清洗开关 {key} 必须是 true 或 false")
        values[key] = value
    return WashingPolicy(**values)
