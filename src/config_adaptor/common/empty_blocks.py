"""厂商共用的空块输出策略模型与 YAML 加载校验。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

from .cleaning import matches_cleaning_path


@dataclass(frozen=True)
class EmptyBlockPolicy:
    default: str
    rules: tuple[tuple[tuple[str, ...], str], ...]

    def action_for(self, path: tuple[str, ...]) -> str:
        """按 YAML 顺序返回首条命中完整路径的动作，未命中时使用默认动作。

        路径语法与清洗规则一致：普通元素按不区分大小写的正则匹配一级块 header，
        ``*`` 恰好匹配一级，``**`` 匹配零级或多级；规则须覆盖块自身在内的完整路径。
        """
        return next(
            (action for rule_path, action in self.rules if matches_cleaning_path(path, rule_path)),
            self.default,
        )


def load_empty_block_policy(
    path: Path,
    *,
    vendor: str,
    image: str,
    actions: tuple[str, ...],
) -> EmptyBlockPolicy:
    """读取并校验规则，由厂商入口提供目标镜像和允许动作。

    ``actions`` 的顺序仅用于错误提示；默认动作和规则动作均须在其中。
    """
    action_choices = "、".join(actions[:-1]) + " 或 " + actions[-1]
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "target", "default_when_empty", "rules"
    }:
        raise ValueError(f"{path}: 空块规则字段无效")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError(f"{path}: 空块规则版本无效")
    if payload["target"] != {"vendor": vendor, "image": image}:
        raise ValueError(f"{path}: 空块规则目标镜像无效")
    default = payload["default_when_empty"]
    if not isinstance(default, str) or default not in actions:
        raise ValueError(f"{path}: default_when_empty 必须是 {action_choices}")
    entries = payload["rules"]
    if not isinstance(entries, list):
        raise ValueError(f"{path}: rules 必须是列表")
    rules: list[tuple[tuple[str, ...], str]] = []
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
            raise ValueError(f"{path}: 空块路径包含无效正则 {rule_path}: {exc}") from exc
        if rule_path in seen:
            raise ValueError(f"{path}: 重复的空块路径 {rule_path}")
        action = entry["action"]
        if not isinstance(action, str) or action not in actions:
            raise ValueError(f"{path}: 空块动作必须是 {action_choices}")
        seen.add(rule_path)
        rules.append((rule_path, action))
    return EmptyBlockPolicy(default, tuple(rules))
