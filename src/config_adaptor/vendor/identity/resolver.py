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
    """查找与候选语句同命令族但内容不同的未匹配项。

    未命中规则的语句按首关键字归为 opaque 命令族；同族却不同内容意味着 group
    继承可能产生无法可靠合并的冲突，因此返回该项供上层据此告警或回滚。
    """
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
    """将编译后规则应用到配置路径和语句。

    解析器持有已校验的规则集并提供带缓存的 ``resolve``；缓存以不可变上下文为键，
    避免同一语句在不同 group 展开位置被重复匹配，同时把规则命中、回退和冲突统一
    收敛成一个 ``SemanticDecision``。
    """

    def __init__(self, rules: tuple[SemanticRule, ...]):
        """创建持有编译后规则的解析器实例。

        规则在构造前已由 ``load_rule_pack`` 校验，这里只保存引用；保持构造无副作用
        可让解析器安全地被模块级 ``lru_cache`` 复用。
        """
        self._rules = rules

    @lru_cache(maxsize=16_384)
    def resolve(self, context: StatementContext) -> SemanticDecision:
        """把一条语句解析成 ``SemanticDecision``。

        先归一化并收集所有命中规则，再按具体度取最精确者；等具体度的平局属于规则
        歧义，必须报错而非任选一条，以免不同输入得到不确定的合并结果。无命中时
        回退为以原文为 identity 的 opaque 决策。
        """
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
