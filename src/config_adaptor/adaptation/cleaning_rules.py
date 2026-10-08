"""统一加载和执行 IOS XR/Junos 配置清洗规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import ConversionContext, Vendor


_PACKAGE_ROOT = Path(__file__).parent.parent
DEFAULT_RULES_PATHS = (
    _PACKAGE_ROOT / "cisco" / "rules" / "xrv9000" / "cleaning.yaml",
    _PACKAGE_ROOT / "juniper" / "rules" / "vmx" / "cleaning.yaml",
)
_ACTIONS = {"delete", "replace", "mask", "warn"}
_VENDORS = {Vendor.CISCO_IOSXR.value, Vendor.JUNIPER_JUNOS.value}
_VENDOR_ID_PREFIXES = {
    Vendor.CISCO_IOSXR.value: "cisco",
    Vendor.JUNIPER_JUNOS.value: "juniper",
}
_LOCAL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*$")


@dataclass(slots=True, frozen=True)
class CleaningRule:
    """一条声明式清洗规则。"""

    rule_id: str
    vendor: str
    enable: bool
    category: str
    path: tuple[str, ...]
    match: str
    action: str
    reason: str
    value: str | None = None


def _required_string(raw: dict[str, object], field: str, rule_id: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"清洗规则 {rule_id} 的 {field} 必须是非空字符串")
    return value


def _parse_rule(raw: object, seen: set[str]) -> CleaningRule:
    if not isinstance(raw, dict):
        raise ValueError("清洗规则必须是键值映射")
    local_id = _required_string(raw, "id", "<unknown>")
    vendor = _required_string(raw, "vendor", local_id)
    if vendor not in _VENDORS:
        raise ValueError(f"清洗规则 {local_id} 使用不支持的厂商: {vendor}")
    if not _LOCAL_ID_PATTERN.fullmatch(local_id):
        raise ValueError(f"清洗规则 {local_id} 的 id 必须由小写字母、数字和点号组成")
    rule_id = f"{_VENDOR_ID_PREFIXES[vendor]}.{local_id}"
    if rule_id in seen:
        raise ValueError(f"重复的清洗规则 id: {rule_id}")
    seen.add(rule_id)

    enable = raw.get("enable")
    if not isinstance(enable, bool):
        raise ValueError(f"清洗规则 {rule_id} 的 enable 必须是布尔值")
    category = _required_string(raw, "category", rule_id)
    match = _required_string(raw, "match", rule_id)
    action = _required_string(raw, "action", rule_id)
    if action not in _ACTIONS:
        raise ValueError(f"清洗规则 {rule_id} 使用不支持的动作: {action}")

    path_value = raw.get("path")
    if not isinstance(path_value, list) or not all(
        isinstance(component, str) and component for component in path_value
    ):
        raise ValueError(f"清洗规则 {rule_id} 的 path 必须是字符串列表")
    path = tuple(path_value)
    try:
        re.compile(match, re.IGNORECASE)
        for component in path:
            if component not in {"*", "**"}:
                re.compile(component, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"清洗规则 {rule_id} 包含无效正则: {exc}") from exc

    value = raw.get("value")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"清洗规则 {rule_id} 的 value 必须是字符串")
    if action == "replace" and value is None:
        raise ValueError(f"清洗规则 {rule_id} 的 replace 动作必须提供 value")
    reason = _required_string(raw, "reason", rule_id)

    allowed = {
        "id", "vendor", "enable", "category", "path", "match",
        "action", "reason", "value",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"清洗规则 {rule_id} 包含未知字段: {', '.join(unknown)}")
    return CleaningRule(
        rule_id=rule_id,
        vendor=vendor,
        enable=enable,
        category=category,
        path=path,
        match=match,
        action=action,
        reason=reason,
        value=value,
    )


def load_rules() -> tuple[CleaningRule, ...]:
    """读取并完整校验随程序发布的两份厂商清洗规则。"""
    sources = (
        (DEFAULT_RULES_PATHS[0], Vendor.CISCO_IOSXR.value),
        (DEFAULT_RULES_PATHS[1], Vendor.JUNIPER_JUNOS.value),
    )
    rules: list[CleaningRule] = []
    seen: set[str] = set()
    for source, expected_vendor in sources:
        with source.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict) or set(payload) != {"rules"}:
            raise ValueError(f"{source}: 顶层必须且只能包含 rules")
        raw_rules = payload["rules"]
        if not isinstance(raw_rules, list):
            raise ValueError(f"{source}: rules 必须是列表")
        for raw in raw_rules:
            rule = _parse_rule(raw, seen)
            if rule.vendor != expected_vendor:
                raise ValueError(
                    f"{source}: 默认厂商规则只能包含 vendor: {expected_vendor}"
                )
            rules.append(rule)
    return tuple(rules)


class CleaningRulesHandler:
    """在接口及引用迁移完成后统一执行声明式配置清洗规则。"""

    def __init__(self, rules: tuple[CleaningRule, ...]):
        self._rules = rules

    def process(self, context: ConversionContext) -> None:
        """按设备执行所有已启用规则，并记录每条命中的分类和数量。"""
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            for rule in self._rules:
                if not rule.enable or rule.vendor != device.device.vendor.value:
                    continue
                hits = device.document.apply_cleaning_rule(
                    rule.path,
                    rule.match,
                    rule.action,
                    rule.value,
                )
                if not hits:
                    continue
                context.add_event(
                    "cleaning-rule",
                    f"设备 {device_name} 命中规则 {rule.rule_id}",
                    device=device_name,
                    rule_id=rule.rule_id,
                    category=rule.category,
                    action=rule.action,
                    hits=hits,
                    reason=rule.reason,
                )
