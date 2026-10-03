"""路径感知的配置语义 identity 解析器。"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import TypeVar

from .matcher import expand_identity, match_rule, normalize, rule_specificity
from .models import MergeKind, SemanticDecision, SemanticRule, StatementContext


_T = TypeVar("_T")


def find_opaque_ambiguity(
    candidate: SemanticDecision,
    existing: Iterable[tuple[_T, SemanticDecision]],
) -> _T | None:
    """查找同命令族但内容不同的未匹配语句。"""
    if candidate.matched or not candidate.normalized:
        return None
    family = candidate.normalized.split(maxsplit=1)[0]
    for item, decision in existing:
        if decision.matched or decision.normalized == candidate.normalized:
            continue
        existing_family = (
            decision.normalized.split(maxsplit=1)[0]
            if decision.normalized
            else ""
        )
        if existing_family == family:
            return item
    return None


class SemanticResolver:
    """将编译后规则应用到配置路径和语句。"""

    def __init__(self, rules: tuple[SemanticRule, ...]):
        self._rules = rules

    @lru_cache(maxsize=16_384)
    def resolve(self, context: StatementContext) -> SemanticDecision:
        normalized = normalize(context.statement)
        matches: list[
            tuple[
                tuple[int, int, int, int, int],
                SemanticRule,
                dict[str, tuple[str, ...]],
            ]
        ] = []
        for rule in self._rules:
            captures = match_rule(rule, context)
            if captures is not None:
                matches.append((rule_specificity(rule), rule, captures))
        if not matches:
            return SemanticDecision(
                identity=("exact", normalized),
                merge_kind=MergeKind.OPAQUE,
                rule_id=None,
                matched=False,
                normalized=normalized,
            )
        matches.sort(key=lambda item: item[0], reverse=True)
        top_score = matches[0][0]
        winners = [item for item in matches if item[0] == top_score]
        if len(winners) > 1:
            names = ", ".join(sorted(item[1].rule_id for item in winners))
            raise ValueError(
                f"配置语句同时命中多条等优先级 identity 规则: {names}; "
                f"path={' / '.join(context.path) or '<root>'}; statement={context.statement}"
            )
        _score, rule, captures = winners[0]
        return SemanticDecision(
            identity=expand_identity(rule.identity, captures),
            merge_kind=rule.merge_kind,
            rule_id=rule.rule_id,
            matched=True,
            normalized=normalized,
        )
