"""可选的保守清洗规则加载与厂商配置树匹配。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import ConversionContext, DeviceContext


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


def _apply_cisco(device: DeviceContext, rule: CleaningRule) -> int:
    """在 IOS XR 顶层配置块上应用规则并返回命中数。"""
    pattern = re.compile(rule.match, re.IGNORECASE)
    hits = 0
    for block in device.document.blocks:
        if not block.active or not pattern.search(block.header.strip()):
            continue
        hits += 1
        if rule.action == "delete":
            block.active = False
        elif rule.action == "replace":
            block.header = pattern.sub(rule.value or "", block.header)
        elif rule.action == "mask":
            block.header = pattern.sub("<masked>", block.header)
    return hits


def _apply_junos(device: DeviceContext, rule: CleaningRule) -> int:
    """递归遍历 Junos 层级树，并同时匹配路径和原始语句。"""
    pattern = re.compile(rule.match, re.IGNORECASE)
    hits = 0

    def walk(node: Any, path: list[str]) -> None:
        """深度优先遍历，path 保存当前配置层级。"""
        nonlocal hits
        if node is device.document.root:
            next_path = path
        else:
            component = device.document._base_header(node.header).split(maxsplit=1)[0].rstrip(";")
            next_path = [*path, component] if component else path
            dotted = ".".join(next_path)
            if node.active and (pattern.search(dotted) or pattern.search(node.header)):
                hits += 1
                if rule.action == "delete":
                    node.active = False
                elif rule.action == "replace":
                    node.header = pattern.sub(rule.value or "", node.header)
                elif rule.action == "mask":
                    node.header = "<masked>;"
        if node.children:
            for child in node.children:
                walk(child, next_path)

    walk(device.document.root, [])
    return hits


def apply_rules(context: ConversionContext, rules: list[CleaningRule]) -> None:
    """按设备厂商执行规则，并把命中信息写入转换事件。"""
    for device_name in sorted(context.devices):
        device = context.devices[device_name]
        for rule in rules:
            if rule.vendor != device.device.vendor.value:
                continue
            if device.device.vendor.value == "cisco_iosxr":
                hits = _apply_cisco(device, rule)
            else:
                hits = _apply_junos(device, rule)
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
