"""配置语义规则的最小协议；顺序由厂商注册表决定，不读取配置文件。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Generic, Protocol, TypeVar

from .policies import SimulationAdaptationPolicy
from .outcomes import InterfaceAdaptationOutcome, InterfacePruningOutcome, SimulationAdaptationOutcome

DocumentT = TypeVar("DocumentT")
ContextT = TypeVar("ContextT")
OutcomeT = TypeVar("OutcomeT")
RuleDocumentT = TypeVar("RuleDocumentT", contravariant=True)
RuleContextT = TypeVar("RuleContextT", contravariant=True)
RuleOutcomeT = TypeVar("RuleOutcomeT", covariant=True)
InterfaceOutcomeT = TypeVar("InterfaceOutcomeT", bound=InterfaceAdaptationOutcome)


@dataclass(frozen=True, slots=True)
class PruneContext:
    protected_parents: frozenset[str]
    removed_interfaces: frozenset[str] = frozenset()
    removed_addresses: frozenset[str] = frozenset()
    remove_non_nni: bool = True


@dataclass(frozen=True, slots=True)
class AdaptContext:
    target_interfaces: frozenset[str]


@dataclass(frozen=True, slots=True)
class ValidationContext:
    target_interfaces: frozenset[str]
    removed_interfaces: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SimulationContext:
    policy: SimulationAdaptationPolicy
    data_interfaces: frozenset[str]


class ConfigurationRule(Protocol[RuleDocumentT, RuleContextT, RuleOutcomeT]):
    @property
    def rule_id(self) -> str: ...

    def applies(self, context: RuleContextT) -> bool: ...

    def apply(self, document: RuleDocumentT, context: RuleContextT) -> RuleOutcomeT: ...


@dataclass(frozen=True, slots=True)
class FunctionRule(Generic[DocumentT, ContextT, OutcomeT]):
    """把内聚的业务函数注册成规则，不为每个简单规则增加包装类。"""

    rule_id: str
    action: Callable[[DocumentT, ContextT], OutcomeT]
    condition: Callable[[ContextT], bool] = field(default=lambda context: True)

    def applies(self, context: ContextT) -> bool:
        return self.condition(context)

    def apply(self, document: DocumentT, context: ContextT) -> OutcomeT:
        return self.action(document, context)


def execute_rules(
    rules: Iterable[ConfigurationRule[DocumentT, ContextT, OutcomeT]],
    document: DocumentT,
    context: ContextT,
) -> list[tuple[str, OutcomeT]]:
    """执行全部适用规则，保留校验的完整诊断；流水线负责阶段间短路。"""
    return [
        (rule.rule_id, rule.apply(document, context))
        for rule in validate_rule_registry(rules) if rule.applies(context)
    ]


def execute_prune_rules(
    rules: Iterable[ConfigurationRule[DocumentT, PruneContext, InterfacePruningOutcome]],
    document: DocumentT,
    context: PruneContext,
) -> list[tuple[str, InterfacePruningOutcome]]:
    """按顺序传递删除结果；每条规则看到的仍是不可变上下文快照。"""
    results = []
    for rule in validate_rule_registry(rules):
        if not rule.applies(context):
            continue
        outcome = rule.apply(document, context)
        results.append((rule.rule_id, outcome))
        context = replace(
            context,
            removed_interfaces=context.removed_interfaces | frozenset(outcome.removed_interfaces),
            removed_addresses=context.removed_addresses | frozenset(outcome.removed_addresses),
        )
    return results


def validate_rule_registry(
    rules: Iterable[ConfigurationRule[DocumentT, ContextT, OutcomeT]],
) -> tuple[ConfigurationRule[DocumentT, ContextT, OutcomeT], ...]:
    """在任何规则执行前校验实际集合；不同执行入口可复用相同规则。"""
    registered = tuple(rules)
    seen: set[str] = set()
    for rule in registered:
        if not isinstance(rule.rule_id, str) or not rule.rule_id.strip():
            raise ValueError("规则 ID 必须为非空字符串")
        if rule.rule_id in seen:
            raise ValueError(f"重复的规则 ID: {rule.rule_id}")
        seen.add(rule.rule_id)
    return registered


def merge_interface_results(
    results: Iterable[tuple[str, InterfaceAdaptationOutcome]], outcome: InterfaceOutcomeT,
) -> InterfaceOutcomeT:
    """聚合厂商接口结果，并把规则来源留给报告层。"""
    for rule_id, result in results:
        outcome.changes.extend(dict(change, rule_id=rule_id) for change in result.changes)
        outcome.errors.extend(result.errors)
        outcome.warnings.extend(result.warnings)
        outcome.diagnostics.extend(
            {"rule_id": rule_id, "level": level, "message": message}
            for level, messages in (("error", result.errors), ("warning", result.warnings))
            for message in messages
        )
        if isinstance(outcome, InterfacePruningOutcome) and isinstance(result, InterfacePruningOutcome):
            outcome.removed_interfaces.update(result.removed_interfaces)
            outcome.removed_addresses.update(result.removed_addresses)
    outcome.errors = list(dict.fromkeys(outcome.errors))
    outcome.warnings = list(dict.fromkeys(outcome.warnings))
    return outcome


def merge_simulation_results(
    results: Iterable[tuple[str, SimulationAdaptationOutcome]],
) -> SimulationAdaptationOutcome:
    outcome = SimulationAdaptationOutcome()
    for rule_id, result in results:
        for action in ("added", "replaced", "removed"):
            for category, count in getattr(result, action).items():
                outcome.record(action, category, count)
        if result.total:
            outcome.rule_changes[rule_id] = result.total
    return outcome
