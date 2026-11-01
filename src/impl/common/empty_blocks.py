"""厂商共用的空块输出策略模型与 YAML 加载校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from .cleaning import matches_cleaning_path


class EmptyBlockAction(StrEnum):
    """定义空容器允许的厂商输出形式。"""

    KEEP = "keep"
    OMIT = "omit"
    SEMICOLON = "semicolon"
    BRACES = "braces"


@dataclass(frozen=True)
class EmptyBlockPolicy:
    """保存空容器的默认输出动作和按完整路径匹配的规则。

    规则按声明顺序求值，使更具体的镜像策略可以先于通用默认动作命中。
    """

    default: EmptyBlockAction
    rules: tuple[tuple[tuple[str, ...], EmptyBlockAction], ...]

    def __post_init__(self) -> None:
        """校验默认动作和全部路径规则动作。"""
        try:
            default = EmptyBlockAction(self.default)
        except ValueError as exc:
            raise ValueError(f"不支持的空块 action: {self.default}") from exc
        rules: list[tuple[tuple[str, ...], EmptyBlockAction]] = []
        for path, raw_action in self.rules:
            try:
                action = EmptyBlockAction(raw_action)
            except ValueError as exc:
                raise ValueError(
                    f"路径 {path} 使用不支持的空块 action: {raw_action}"
                ) from exc
            rules.append((path, action))
        object.__setattr__(self, "default", default)
        object.__setattr__(self, "rules", tuple(rules))

    def action_for(self, path: tuple[str, ...]) -> EmptyBlockAction:
        """按 YAML 顺序返回首条命中完整路径的动作，未命中时使用默认动作。

        路径语法与清洗规则一致：普通元素按不区分大小写的正则匹配一级块 header，
        ``*`` 恰好匹配一级，``**`` 匹配零级或多级；规则须覆盖块自身在内的完整路径。
        """
        return next(
            (
                action
                for rule_path, action in self.rules
                if matches_cleaning_path(path, rule_path)
            ),
            self.default,
        )


def load_empty_block_policy(
    path: Path,
    *,
    vendor: str,
    image: str,
    actions: tuple[str | EmptyBlockAction, ...],
) -> EmptyBlockPolicy:
    """读取并校验规则，由厂商入口提供目标镜像和允许动作。

    ``actions`` 的顺序仅用于错误提示；默认动作和规则动作均须在其中。
    """
    normalized_actions = tuple(EmptyBlockAction(action) for action in actions)
    action_choices = _format_action_choices(normalized_actions)
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    default = _validate_policy_header(
        payload,
        path,
        vendor,
        image,
        normalized_actions,
        action_choices,
    )
    rules = _load_empty_block_rules(payload["rules"], path, normalized_actions)
    return EmptyBlockPolicy(default, tuple(rules))


def _validate_policy_header(
    payload: object,
    path: Path,
    vendor: str,
    image: str,
    actions: tuple[EmptyBlockAction, ...],
    action_choices: str,
) -> EmptyBlockAction:
    """校验空块策略文件的 schema、目标镜像和默认动作。

    这些字段共同决定规则是否属于当前镜像，必须在读取规则内容前统一验证。
    """
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "target",
        "default_when_empty",
        "rules",
    }:
        raise ValueError(f"{path}: 空块规则字段无效")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise ValueError(f"{path}: 空块规则版本无效")
    if payload["target"] != {"vendor": vendor, "image": image}:
        raise ValueError(f"{path}: 空块规则目标镜像无效")
    default = _parse_action(
        payload["default_when_empty"],
        actions,
        f"{path}: default_when_empty 必须是 {action_choices}",
    )
    entries = payload["rules"]
    if not isinstance(entries, list):
        raise ValueError(f"{path}: rules 必须是列表")
    return default


def _load_empty_block_rules(
    entries: list[object],
    path: Path,
    actions: tuple[EmptyBlockAction, ...],
) -> list[tuple[tuple[str, ...], EmptyBlockAction]]:
    """校验并读取空块路径规则。

    路径和动作在加载阶段全部编译检查，避免渲染文档时才因规则错误中断输出。
    """
    action_choices = _format_action_choices(actions)
    rules: list[tuple[tuple[str, ...], EmptyBlockAction]] = []
    seen: set[tuple[str, ...]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "action"}:
            raise ValueError(f"{path}: 空块规则必须包含 path 和 action")
        parts = entry["path"]
        if (
            not isinstance(parts, list)
            or not parts
            or any(
                not isinstance(part, str) or not part or part != part.strip()
                for part in parts
            )
        ):
            raise ValueError(f"{path}: 空块路径必须是非空字符串列表")
        rule_path = tuple(parts)
        try:
            for part in rule_path:
                if part not in {"*", "**"}:
                    re.compile(part, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"{path}: 空块路径包含无效正则 {rule_path}: {exc}"
            ) from exc
        if rule_path in seen:
            raise ValueError(f"{path}: 重复的空块路径 {rule_path}")
        action = _parse_action(
            entry["action"],
            actions,
            f"{path}: 空块动作必须是 {action_choices}",
        )
        seen.add(rule_path)
        rules.append((rule_path, action))
    return rules


def _format_action_choices(actions: tuple[EmptyBlockAction, ...]) -> str:
    """返回面向配置维护者的允许动作文本。"""
    values = [action.value for action in actions]
    return "、".join(values[:-1]) + " 或 " + values[-1]


def _parse_action(
    raw: object,
    actions: tuple[EmptyBlockAction, ...],
    error_message: str,
) -> EmptyBlockAction:
    """把配置文本动作转换为允许共享类型，并拒绝镜像不支持的取值。"""
    if not isinstance(raw, str):
        raise ValueError(error_message)
    try:
        action = EmptyBlockAction(raw)
    except ValueError as exc:
        raise ValueError(error_message) from exc
    if action not in actions:
        raise ValueError(error_message)
    return action
