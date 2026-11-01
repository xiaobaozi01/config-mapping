"""配置语义规则的最小协议；顺序由厂商注册表决定，不读取配置文件。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Generic, Protocol, TypeVar

from .outcomes import (
    DeferredInterfaceReference,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
    SimulationChangeAction,
)
from .policies import SimulationAdaptationPolicy

DocumentT = TypeVar("DocumentT")
ContextT = TypeVar("ContextT")
OutcomeT = TypeVar("OutcomeT")
RuleDocumentT = TypeVar("RuleDocumentT", contravariant=True)
RuleContextT = TypeVar("RuleContextT", contravariant=True)
RuleOutcomeT = TypeVar("RuleOutcomeT", covariant=True)
InterfaceOutcomeT = TypeVar(
    "InterfaceOutcomeT", bound=InterfaceAdaptationOutcome
)


@dataclass(frozen=True, slots=True)
class PruneContext:
    """保存接口裁剪规则所需的保护范围和累计删除集合。

    上下文为不可变快照，规则只能返回新增结果，从而避免不同规则互相覆盖删除状态。
    """

    protected_parents: frozenset[str]
    removed_interfaces: frozenset[str] = frozenset()
    removed_addresses: frozenset[str] = frozenset()
    remove_non_nni: bool = True


@dataclass(frozen=True, slots=True)
class AdaptContext:
    """保存接口适配规则需要处理的目标接口集合。

    适配范围在阶段开始时固定，规则不能扩大目标集合，以保证改动可预测和可审计。
    """

    target_interfaces: frozenset[str]


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """保存最终校验需要的目标接口和已删除接口集合。

    只读上下文让验证规则能够检查残留引用，同时禁止它们通过上下文修改转换状态。
    """

    target_interfaces: frozenset[str]
    removed_interfaces: frozenset[str] = frozenset()
    deferred_interface_references: frozenset[DeferredInterfaceReference] = (
        frozenset()
    )


@dataclass(frozen=True, slots=True)
class SimulationContext:
    """保存模拟适配策略及其允许处理的数据接口。

    接口范围和策略一并传递，避免各规则自行读取 profile 而采用不一致的门限。
    """

    policy: SimulationAdaptationPolicy
    data_interfaces: frozenset[str]


class ConfigurationRule(Protocol[RuleDocumentT, RuleContextT, RuleOutcomeT]):
    """定义配置规则的标识、适用条件和执行入口。

    统一协议允许 YAML 规则、函数规则和 Visitor 规则进入同一注册表，而不要求共同基类。
    """

    @property
    def rule_id(self) -> str:
        """返回规则在报告中的全局唯一标识。

        稳定 ID 是去重、定位和聚合诊断的依据，因此注册前必须校验非空且唯一。
        """
        ...

    def applies(self, context: RuleContextT) -> bool:
        """判断当前上下文是否满足规则执行条件。

        条件判断与具体执行分离，使注册表能够跳过不适用规则而不产生副作用。
        """
        ...

    def apply(
        self, document: RuleDocumentT, context: RuleContextT
    ) -> RuleOutcomeT:
        """在文档和上下文上执行规则并返回结构化结果。

        规则不得自行提交事务，统一执行器负责校验快照、提交或回滚。
        """
        ...


@dataclass(frozen=True, slots=True)
class FunctionRule(Generic[DocumentT, ContextT, OutcomeT]):
    """把内聚业务函数包装成配置规则。

    简单规则无需额外类层级即可复用统一注册和审计协议，同时保留可选条件判断。
    """

    rule_id: str
    action: Callable[[DocumentT, ContextT], OutcomeT]
    condition: Callable[[ContextT], bool] = field(default=lambda context: True)

    def applies(self, context: ContextT) -> bool:
        """调用规则条件函数判断是否执行。

        延迟到执行阶段求值，使条件可以依赖当前转换上下文。
        """
        return self.condition(context)

    def apply(self, document: DocumentT, context: ContextT) -> OutcomeT:
        """调用规则动作并返回其结构化结果。

        包装层只负责协议适配，业务行为完全由注册的函数决定。
        """
        return self.action(document, context)


def execute_rules(
    rules: Iterable[ConfigurationRule[DocumentT, ContextT, OutcomeT]],
    document: DocumentT,
    context: ContextT,
) -> list[tuple[str, OutcomeT]]:
    """执行全部适用规则并按注册顺序返回规则结果。

    注册表在执行前统一校验，避免重复 ID 或空规则导致不完整诊断；阶段间是否短路由
    上层流水线决定。
    """
    return [
        (rule.rule_id, rule.apply(document, context))
        for rule in validate_rule_registry(rules)
        if rule.applies(context)
    ]


def execute_prune_rules(
    rules: Iterable[
        ConfigurationRule[DocumentT, PruneContext, InterfacePruningOutcome]
    ],
    document: DocumentT,
    context: PruneContext,
) -> list[tuple[str, InterfacePruningOutcome]]:
    """按顺序执行裁剪规则并累积删除结果。

    每条规则接收新替换的不可变上下文，使后续规则能看到先前删除对象，同时不能修改
    既有快照。
    """
    results = []
    for rule in validate_rule_registry(rules):
        if not rule.applies(context):
            continue
        outcome = rule.apply(document, context)
        results.append((rule.rule_id, outcome))
        context = replace(
            context,
            removed_interfaces=context.removed_interfaces
            | frozenset(outcome.removed_interfaces),
            removed_addresses=context.removed_addresses
            | frozenset(outcome.removed_addresses),
        )
    return results


def validate_rule_registry(
    rules: Iterable[ConfigurationRule[DocumentT, ContextT, OutcomeT]],
) -> tuple[ConfigurationRule[DocumentT, ContextT, OutcomeT], ...]:
    """校验规则集合的 ID 并返回稳定执行序列。

    在任何文档修改前完成校验，可避免中途发现重复规则时留下部分执行结果。
    """
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
    results: Iterable[tuple[str, InterfaceAdaptationOutcome]],
    outcome: InterfaceOutcomeT,
) -> InterfaceOutcomeT:
    """聚合多个接口规则的变更、诊断和删除结果。

    规则 ID 在此写回每条变更，使报告可以追溯来源，并统一去重错误和告警。
    """
    for rule_id, result in results:
        outcome.changes.extend(
            dict(change, rule_id=rule_id) for change in result.changes
        )
        outcome.errors.extend(result.errors)
        outcome.warnings.extend(result.warnings)
        outcome.diagnostics.extend(
            {"rule_id": rule_id, "level": level, "message": message}
            for level, messages in (
                ("error", result.errors),
                ("warning", result.warnings),
            )
            for message in messages
        )
        if isinstance(outcome, InterfacePruningOutcome) and isinstance(
            result, InterfacePruningOutcome
        ):
            outcome.removed_interfaces.update(result.removed_interfaces)
            outcome.removed_addresses.update(result.removed_addresses)
            outcome.deferred_interface_references.update(
                replace(reference, rule_id=rule_id)
                for reference in result.deferred_interface_references
            )
    outcome.errors = list(dict.fromkeys(outcome.errors))
    outcome.warnings = list(dict.fromkeys(outcome.warnings))
    return outcome


def merge_simulation_results(
    results: Iterable[tuple[str, SimulationAdaptationOutcome]],
) -> SimulationAdaptationOutcome:
    """合并多个模拟规则的分类计数和规则命中总数。

    汇总保留每条有变更规则的来源，使最终报告既能显示类别总量，也能定位贡献规则。
    """
    outcome = SimulationAdaptationOutcome()
    for rule_id, result in results:
        for action in SimulationChangeAction:
            for category, count in getattr(result, action).items():
                outcome.record(action, category, count)
        if result.total:
            outcome.rule_changes[rule_id] = result.total
    return outcome
