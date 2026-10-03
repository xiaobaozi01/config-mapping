"""无厂商业务语义的 path/token 模板匹配器。"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache

from .models import SemanticRule, StatementContext


_TOKEN_RE = re.compile(r'''(?:[^\s'\"]+|'[^']*'|"[^"]*")+''')
_CAPTURE_RE = re.compile(
    r"^\{(?P<name>[a-zA-Z_][a-zA-Z0-9_-]*)(?P<rest>\.\.\.)?(?::(?P<constraint>[^}]+))?\}$"
)


def normalize(value: str) -> str:
    """与现有 parser 一致地归一化空白、分号和大小写。"""
    return " ".join(value.strip().rstrip(";").split()).lower()


def tokenize(value: str) -> tuple[str, ...]:
    """按空白分词，但保留引号中的空格。"""
    normalized = normalize(value)
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(normalized):
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            token = token[1:-1]
        tokens.append(token)
    return tuple(tokens)


def _valid_capture(value: str, constraint: str | None) -> bool:
    if not constraint or constraint == "word":
        return bool(value)
    if constraint == "uint":
        return value.isdigit()
    if constraint in {"ip", "ip-address"}:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return False
        return True
    if constraint in {"prefix", "ip-prefix"}:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
        return True
    if "|" in constraint:
        return value in constraint.split("|")
    raise ValueError(f"未知的 identity 规则 capture 类型: {constraint}")


def match_tokens(
    pattern: tuple[str, ...],
    actual: tuple[str, ...],
    captures: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, tuple[str, ...]] | None:
    """匹配一段 token 模板并返回命名 capture。"""
    result = dict(captures or {})
    actual_index = 0
    for pattern_index, expected in enumerate(pattern):
        capture = _CAPTURE_RE.match(expected)
        if capture and capture.group("rest"):
            if pattern_index != len(pattern) - 1:
                raise ValueError(f"只有最后一个 token 才能使用 ... capture: {expected}")
            values = actual[actual_index:]
            name = capture.group("name")
            previous = result.get(name)
            if previous is not None and previous != values:
                return None
            result[name] = values
            return result
        if actual_index >= len(actual):
            return None
        value = actual[actual_index]
        if capture:
            if not _valid_capture(value, capture.group("constraint")):
                return None
            name = capture.group("name")
            values = (value,)
            previous = result.get(name)
            if previous is not None and previous != values:
                return None
            result[name] = values
        elif expected.lower() != value:
            return None
        actual_index += 1
    return result if actual_index == len(actual) else None


def _match_path_recursive(
    pattern: tuple[str, ...],
    actual: tuple[str, ...],
    pattern_index: int,
    actual_index: int,
    captures: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]] | None:
    if pattern_index == len(pattern):
        return captures if actual_index == len(actual) else None
    component = pattern[pattern_index]
    if component == "**":
        for next_index in range(actual_index, len(actual) + 1):
            matched = _match_path_recursive(
                pattern,
                actual,
                pattern_index + 1,
                next_index,
                dict(captures),
            )
            if matched is not None:
                return matched
        return None
    if actual_index >= len(actual):
        return None
    matched_component = match_tokens(
        tokenize(component),
        tokenize(actual[actual_index]),
        captures,
    )
    if matched_component is None:
        return None
    return _match_path_recursive(
        pattern,
        actual,
        pattern_index + 1,
        actual_index + 1,
        matched_component,
    )


def match_rule(rule: SemanticRule, context: StatementContext) -> dict[str, tuple[str, ...]] | None:
    """返回规则对当前语句的 capture；不匹配时返回 None。"""
    if rule.node_kind != context.node_kind:
        return None
    path_captures = _match_path_recursive(rule.path, context.path, 0, 0, {})
    if path_captures is None:
        return None
    return match_tokens(rule.statement, tokenize(context.statement), path_captures)


def expand_identity(
    template: tuple[str, ...],
    captures: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """将 identity 模板中的 capture 替换为实际值。"""
    result: list[str] = []
    for token in template:
        capture = _CAPTURE_RE.match(token)
        if not capture:
            result.append(token.lower())
            continue
        result.extend(captures[capture.group("name")])
    return tuple(result)


@lru_cache(maxsize=None)
def rule_specificity(rule: SemanticRule) -> tuple[int, int, int, int, int]:
    """计算稳定的规则具体度，不依赖 YAML 顺序。"""
    path_tokens = [token for component in rule.path if component != "**" for token in tokenize(component)]
    statement_tokens = list(rule.statement)
    path_literals = sum(not _CAPTURE_RE.match(token) for token in path_tokens)
    statement_literals = sum(not _CAPTURE_RE.match(token) for token in statement_tokens)
    typed = sum(
        bool((capture := _CAPTURE_RE.match(token)) and capture.group("constraint"))
        for token in [*path_tokens, *statement_tokens]
    )
    rest = sum(
        bool((capture := _CAPTURE_RE.match(token)) and capture.group("rest"))
        for token in [*path_tokens, *statement_tokens]
    )
    concrete_path = sum(component != "**" for component in rule.path)
    return path_literals, concrete_path, statement_literals, typed, -rest
