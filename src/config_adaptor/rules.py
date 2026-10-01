"""可选的保守清洗规则加载与厂商配置树匹配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import ConversionContext


@dataclass(slots=True)
class CleaningRule:
    """一条外部 YAML 规则；核心转换逻辑不依赖这些规则。"""
    rule_id: str
    vendor: str
    match: str
    action: str
    reason: str
    value: str | None = None


def load_rules(path: Path | None) -> list[CleaningRule]:
    """读取并校验规则动作，未指定文件时返回空列表。"""
    if not path:
        return []
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    rules: list[CleaningRule] = []
    for raw in payload.get("rules", []):
        action = str(raw.get("action", "warn"))
        if action not in {"delete", "replace", "mask", "warn"}:
            raise ValueError(f"清洗规则 {raw.get('id')} 使用不支持的动作: {action}")
        rules.append(
            CleaningRule(
                rule_id=str(raw["id"]),
                vendor=str(raw["vendor"]),
                match=str(raw["match"]),
                action=action,
                reason=str(raw.get("reason", "")),
                value=str(raw["value"]) if "value" in raw else None,
            )
        )
    return rules


def apply_rules(context: ConversionContext, rules: list[CleaningRule]) -> None:
    """按设备厂商执行规则，并把命中信息写入转换事件。"""
    for device_name in sorted(context.devices):
        device = context.devices[device_name]
        for rule in rules:
            if rule.vendor != device.device.vendor.value:
                continue
            hits = device.document.apply_cleaning_rule(rule.match, rule.action, rule.value)
            if hits:
                context.add_event(
                    "cleaning-rule",
                    f"设备 {device_name} 命中规则 {rule.rule_id}",
                    device=device_name,
                    rule_id=rule.rule_id,
                    action=rule.action,
                    hits=hits,
                    reason=rule.reason,
                )
