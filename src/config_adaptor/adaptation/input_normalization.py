"""按厂商规则移除原始采集文本两端的命令回显。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from .models import Vendor


_PACKAGE_ROOT = Path(__file__).parent.parent
_RULE_PATHS = {
    Vendor.CISCO_IOSXR: _PACKAGE_ROOT / "cisco" / "rules" / "xrv9000" / "input_normalization.yaml",
    Vendor.JUNIPER_JUNOS: _PACKAGE_ROOT / "juniper" / "rules" / "vmx" / "input_normalization.yaml",
}
_VENDOR_PREFIX = {
    Vendor.CISCO_IOSXR: "cisco",
    Vendor.JUNIPER_JUNOS: "juniper",
}


class InputRegion(StrEnum):
    LEADING = "leading"
    TRAILING = "trailing"


@dataclass(frozen=True, slots=True)
class InputRule:
    rule_id: str
    region: InputRegion
    match: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class RemovedInput:
    rule_id: str
    start_line: int
    end_line: int

    @property
    def removed_lines(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    text: str
    removed: tuple[RemovedInput, ...]


def _load_rules(vendor: Vendor) -> tuple[InputRule, ...]:
    """加载随程序发布的规则，并在处理配置前验证规则结构。"""
    path = _RULE_PATHS[vendor]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"version", "vendor", "rules"}:
        raise ValueError(f"输入整理规则 {path} 顶层字段无效")
    if type(raw["version"]) is not int or raw["version"] != 1 or raw["vendor"] != vendor.value:
        raise ValueError(f"输入整理规则 {path} 的版本或厂商不匹配")
    if not isinstance(raw["rules"], list):
        raise ValueError(f"输入整理规则 {path} 的 rules 必须是列表")

    rules: list[InputRule] = []
    seen: set[str] = set()
    prefix = _VENDOR_PREFIX[vendor]
    for item in raw["rules"]:
        if not isinstance(item, dict) or set(item) != {"id", "region", "match"}:
            raise ValueError(f"输入整理规则 {path} 存在无效字段")
        local_id = item["id"]
        if not isinstance(local_id, str) or not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", local_id):
            raise ValueError(f"输入整理规则 {path} 的 id 无效: {local_id!r}")
        rule_id = local_id if local_id.startswith(f"{prefix}.") else f"{prefix}.{local_id}"
        if rule_id in seen:
            raise ValueError(f"输入整理规则 {path} 的 id 重复: {rule_id}")
        seen.add(rule_id)
        if not isinstance(item["region"], str):
            raise ValueError(f"输入整理规则 {rule_id} 的 region 无效")
        try:
            region = InputRegion(item["region"])
        except ValueError as exc:
            raise ValueError(f"输入整理规则 {rule_id} 的 region 无效") from exc
        patterns = item["match"]
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ValueError(f"输入整理规则 {rule_id} 的 match 必须是非空正则列表")
        try:
            compiled = tuple(re.compile(pattern) for pattern in patterns)
        except re.error as exc:
            raise ValueError(f"输入整理规则 {rule_id} 的正则无效: {exc}") from exc
        rules.append(InputRule(rule_id, region, compiled))
    return tuple(rules)


def _matches(lines: list[str], start: int, rule: InputRule) -> bool:
    return start >= 0 and start + len(rule.match) <= len(lines) and all(
        pattern.fullmatch(lines[start + offset].rstrip("\r\n"))
        for offset, pattern in enumerate(rule.match)
    )


def normalize_input(vendor: Vendor, text: str) -> NormalizedInput:
    """仅删除文件两端完整匹配的行序列，保留中间的未知配置。"""
    rules = _load_rules(vendor)
    lines = text.splitlines(keepends=True)
    left, right = 0, len(lines)
    removed: list[RemovedInput] = []

    for region in InputRegion:
        is_leading = region is InputRegion.LEADING
        while left < right:
            if is_leading:
                edge = left
                while edge < right and not lines[edge].strip():
                    edge += 1
            else:
                edge = right
                while edge > left and not lines[edge - 1].strip():
                    edge -= 1

            matched = False
            for rule in rules:
                if rule.region != region:
                    continue
                start = edge if is_leading else edge - len(rule.match)
                if start < left or start + len(rule.match) > right or not _matches(lines, start, rule):
                    continue
                end = start + len(rule.match)
                # 两端的空行也会随匹配片段一同删除，行号统计覆盖真实删除范围。
                removed.append(RemovedInput(
                    rule.rule_id,
                    left + 1 if is_leading else start + 1,
                    end if is_leading else right,
                ))
                if is_leading:
                    left = end
                else:
                    right = start
                matched = True
                break
            if not matched:
                break

    return NormalizedInput("".join(lines[left:right]) if removed else text, tuple(removed))
