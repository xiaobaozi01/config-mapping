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
    """与现有 parser 一致地归一化空白、分号和大小写。

    identity 匹配必须忽略书写差异，否则同一命令因空白或分号不同就被判为不同语义；
    统一小写还能让规则模板中的关键字与任意大小写输入匹配。
    """
    return " ".join(value.strip().rstrip(";").split()).lower()


def tokenize(value: str) -> tuple[str, ...]:
    """按空白分词，但保留引号中的空格。

    描述文本、正则值等可能含空格，直接 split 会错误拆散它们；先按引号感知分词，
    再去掉最外层引号，才能让模板与带空格的值正确匹配。
    """
    normalized = normalize(value)
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(normalized):
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
            token = token[1:-1]
        tokens.append(token)
    return tuple(tokens)


def _valid_capture(value: str, constraint: str | None) -> bool:
    """校验单个 capture 值是否满足规则声明的约束。

    约束（word/uint/ip/prefix/枚举）在 YAML 里以 ``{name:constraint}`` 表达；提前
    校验可避免把不合法 IP、前缀或非数字当作有效 identity 参与后续合并判定。
    """
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
    """匹配一段 token 模板，返回命中时的命名 capture 字典，否则返回 None。

    同一路径里已收集的 capture 会继续累积并做一致性检查，因此重复出现的命名捕获
    必须在各处取值一致；末尾的 ``...`` 捕获贪婪吸收剩余 token。
    """
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
    """递归沿路径模板逐层匹配，返回累积的 capture 或 None。

    ``**`` 需要尝试吸收任意多级路径组件，因此用递归回退实现；每层匹配成功后把
    capture 传给下一层，保证路径与语句共享同一份命名捕获。
    """
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
    """判断一条规则是否命中当前语句，命中时返回 capture 字典。

    先校验 node_kind 再匹配路径，可快速淘汰类型不符的规则；路径和语句使用同一
    capture 集，确保 identity 模板里引用的名字都来自本次匹配。
    """
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
    """把 identity 模板中的 capture 占位符替换为本次匹配得到的实际值。

    替换后的元组就是该语句的语义 identity；用捕获值而非原文本填充，可让同一类
    命令（如不同前缀的地址）共享同一身份骨架，便于 group 冲突按语义合并。
    """
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
    """计算稳定的规则具体度，不依赖 YAML 顺序。

    多个规则可能同时命中，具体度用于挑选更精确的一条（字面量多、路径更完整、带
    类型约束者优先，``...`` 越宽泛越靠后）；规则文件顺序因此不会影响最终判定。
    """
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
