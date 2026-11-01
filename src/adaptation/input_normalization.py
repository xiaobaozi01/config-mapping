"""按厂商规则移除原始采集文本两端的命令回显。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from impl.common.support.config_paths import config_path

from .models import Vendor

_RULE_PATHS = {
    Vendor.CISCO_IOSXR: config_path(
        "rules",
        Vendor.CISCO_IOSXR.rule_prefix,
        Vendor.CISCO_IOSXR.target_image,
        "input_normalization.yaml",
    ),
    Vendor.JUNIPER_JUNOS: config_path(
        "rules",
        Vendor.JUNIPER_JUNOS.rule_prefix,
        Vendor.JUNIPER_JUNOS.target_image,
        "input_normalization.yaml",
    ),
}
_VENDOR_PREFIX = {
    vendor: vendor.rule_prefix
    for vendor in (Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS)
}


class InputRegion(StrEnum):
    """定义输入整理规则可删除的文本端。

    两端分别处理命令回显和采集尾部提示，避免规则误删正文中的同形文本。
    """

    LEADING = "leading"
    TRAILING = "trailing"


@dataclass(frozen=True, slots=True)
class InputRule:
    """描述一条绑定方向、标识和连续正则序列的输入整理规则。

    不可变字段让加载后的规则可以跨设备共享，同时保留稳定 ID 供删除行号审计。
    """

    rule_id: str
    region: InputRegion
    match: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class RemovedInput:
    """记录一次输入整理删除的真实原始行范围。

    报告只保存位置与规则来源而不保存配置正文，避免敏感内容进入诊断文件。
    """

    rule_id: str
    start_line: int
    end_line: int

    @property
    def removed_lines(self) -> int:
        """返回删除区间包含的原始行数。

        两端都计入范围，使报告统计与用户看到的物理行号保持一致。
        """
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    """保存整理后的配置文本及其删除记录。

    正文与删除区间成对返回，使解析器使用整理结果时仍能完整审计输入变化。
    """

    text: str
    removed: tuple[RemovedInput, ...]


def _load_rules(vendor: Vendor) -> tuple[InputRule, ...]:
    """加载指定厂商随程序发布的输入整理规则。

    文件结构在解析配置前验证，避免无效规则等到文本处理时才发现并留下不一致状态。
    """
    path = _RULE_PATHS[vendor]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"version", "vendor", "rules"}:
        raise ValueError(f"输入整理规则 {path} 顶层字段无效")
    if (
        type(raw["version"]) is not int
        or raw["version"] != 1
        or raw["vendor"] != vendor.value
    ):
        raise ValueError(f"输入整理规则 {path} 的版本或厂商不匹配")
    if not isinstance(raw["rules"], list):
        raise ValueError(f"输入整理规则 {path} 的 rules 必须是列表")

    rules = _parse_input_rules(raw["rules"], path, vendor)
    return tuple(rules)


def _parse_input_rules(
    items: list[object],
    path: Path,
    vendor: Vendor,
) -> list[InputRule]:
    """解析并校验指定厂商的输入整理规则列表。

    标识补全和正则预编译集中完成，使运行时只处理可信规则并保持稳定错误信息。
    """
    rules: list[InputRule] = []
    seen: set[str] = set()
    prefix = _VENDOR_PREFIX[vendor]
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "region", "match"}:
            raise ValueError(f"输入整理规则 {path} 存在无效字段")
        local_id = item["id"]
        if not isinstance(local_id, str) or not re.fullmatch(
            r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", local_id
        ):
            raise ValueError(f"输入整理规则 {path} 的 id 无效: {local_id!r}")
        rule_id = (
            local_id
            if local_id.startswith(f"{prefix}.")
            else f"{prefix}.{local_id}"
        )
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
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(
                isinstance(pattern, str) and pattern for pattern in patterns
            )
        ):
            raise ValueError(
                f"输入整理规则 {rule_id} 的 match 必须是非空正则列表"
            )
        try:
            compiled = tuple(re.compile(pattern) for pattern in patterns)
        except re.error as exc:
            raise ValueError(
                f"输入整理规则 {rule_id} 的正则无效: {exc}"
            ) from exc
        rules.append(InputRule(rule_id, region, compiled))
    return rules


def _matches(lines: list[str], start: int, rule: InputRule) -> bool:
    """判断规则的正则序列能否从指定行开始连续匹配。

    先检查边界再逐行完整匹配，可避免跨出文件范围或把部分行误判为规则命中。
    """
    return (
        start >= 0
        and start + len(rule.match) <= len(lines)
        and all(
            pattern.fullmatch(lines[start + offset].rstrip("\r\n"))
            for offset, pattern in enumerate(rule.match)
        )
    )


def normalize_input(vendor: Vendor, text: str) -> NormalizedInput:
    """删除配置两端连续命中的输入整理行并返回审计记录。

    只处理文件边缘可避免误删正文中的同形命令，同时保留中间未知配置供后续解析。
    """
    rules = _load_rules(vendor)
    lines = text.splitlines(keepends=True)
    left, right = 0, len(lines)
    removed: list[RemovedInput] = []

    for region in InputRegion:
        is_leading = region is InputRegion.LEADING
        while left < right:
            edge = _content_edge(lines, left, right, is_leading)
            rule = _matching_edge_rule(
                lines,
                left,
                right,
                edge,
                rules,
                region,
                is_leading,
            )
            if rule is None:
                break
            start = edge if is_leading else edge - len(rule.match)
            end = start + len(rule.match)
            # 两端空行随匹配片段删除，行号统计覆盖真实删除范围。
            removed.append(
                RemovedInput(
                    rule.rule_id,
                    left + 1 if is_leading else start + 1,
                    end if is_leading else right,
                )
            )
            left, right = (end, right) if is_leading else (left, start)

    return NormalizedInput(
        "".join(lines[left:right]) if removed else text, tuple(removed)
    )


def _content_edge(
    lines: list[str],
    left: int,
    right: int,
    is_leading: bool,
) -> int:
    """跳过指定文本端的连续空行并返回内容边界索引。

    空行随规则匹配一起裁剪，但边界计算先独立完成，使行号审计和文本切片采用同一逻辑。
    """
    if is_leading:
        edge = left
        while edge < right and not lines[edge].strip():
            edge += 1
        return edge
    edge = right
    while edge > left and not lines[edge - 1].strip():
        edge -= 1
    return edge


def _matching_edge_rule(
    lines: list[str],
    left: int,
    right: int,
    edge: int,
    rules: tuple[InputRule, ...],
    region: InputRegion,
    is_leading: bool,
) -> InputRule | None:
    """返回指定方向中第一条在当前边界完整匹配的规则。

    保持 YAML 声明顺序可让更具体的端部规则优先，同时避免跨区域匹配。
    """
    for rule in rules:
        if rule.region != region:
            continue
        start = edge if is_leading else edge - len(rule.match)
        if (
            start >= left
            and start + len(rule.match) <= right
            and _matches(lines, start, rule)
        ):
            return rule
    return None
