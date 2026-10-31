"""Cisco 规则包：显式阶段注册表与执行入口；业务模块和镜像 YAML 位于同包。"""

from collections.abc import Iterable
from typing import TypeAlias

from impl.common.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationAdaptationPolicy
from impl.common.rules import (
    AdaptContext,
    ConfigurationRule,
    PruneContext,
    SimulationContext,
    ValidationContext,
    execute_prune_rules,
    execute_rules,
    merge_interface_results,
    merge_simulation_results,
)

from ..document import CiscoDocument
from . import compatibility, pruning, simulation

CiscoPruneRule: TypeAlias = ConfigurationRule[
    CiscoDocument, PruneContext, InterfacePruningOutcome
]
CiscoAdaptRule: TypeAlias = ConfigurationRule[
    CiscoDocument, AdaptContext, InterfaceAdaptationOutcome
]
CiscoValidationRule: TypeAlias = ConfigurationRule[
    CiscoDocument, ValidationContext, InterfaceAdaptationOutcome
]
CiscoSimulationRule: TypeAlias = ConfigurationRule[
    CiscoDocument, SimulationContext, SimulationAdaptationOutcome
]

PRUNE_RULES: tuple[CiscoPruneRule, ...] = (
    pruning.NON_NNI_RULE,
    pruning.MEMBER_SOURCES_RULE,
    pruning.TRACK_DEPENDENCIES_RULE,
    pruning.INTERFACE_BINDINGS_RULE,
)
ADAPT_RULES: tuple[CiscoAdaptRule, ...] = (
    compatibility.AGGREGATION_ATTRIBUTES_RULE,
    compatibility.ETHERTYPE_RULE,
)
COMPATIBILITY_VALIDATION_RULES: tuple[CiscoValidationRule, ...] = (
    compatibility.END_RULE,
    compatibility.SCALAR_VALUES_RULE,
    compatibility.INTERFACE_TYPES_RULE,
    compatibility.COMPATIBLE_COMMANDS_RULE,
    compatibility.L2VPN_RULE,
)
VALIDATION_RULES: tuple[CiscoValidationRule, ...] = (
    *COMPATIBILITY_VALIDATION_RULES,
    compatibility.REMOVED_REFERENCES_RULE,
)
SIMULATION_RULES: tuple[CiscoSimulationRule, ...] = (
    simulation.ENABLE_INTERFACES_RULE,
    simulation.BFD_RULE,
)


def prune_non_nni(
    document: CiscoDocument, protected_parents: Iterable[str]
) -> InterfacePruningOutcome:
    """执行完整非 NNI 清理并返回聚合结果。

    所有剪枝规则共享不可变保护范围和累计删除上下文，以保持规则间依赖一致。
    """
    context = PruneContext(frozenset(protected_parents))
    return merge_interface_results(
        execute_prune_rules(PRUNE_RULES, document, context),
        InterfacePruningOutcome(),
    )


def prune_member_dependencies(
    document: CiscoDocument, removed_interfaces: Iterable[str]
) -> InterfacePruningOutcome:
    """清理聚合成员删除后遗留的业务引用。

    该入口关闭完整非 NNI 清理，只针对指定已删除成员传播依赖删除。
    """
    context = PruneContext(
        frozenset(),
        frozenset(
            document.resolve_interface(name) for name in removed_interfaces
        ),
        remove_non_nni=False,
    )
    outcome = InterfacePruningOutcome(
        removed_interfaces=set(context.removed_interfaces)
    )
    return merge_interface_results(
        execute_prune_rules(PRUNE_RULES, document, context), outcome
    )


def adapt_interfaces(
    document: CiscoDocument, target_interfaces: Iterable[str]
) -> InterfaceAdaptationOutcome:
    """执行全部接口兼容适配规则。

    目标集合在开始时固定，规则只能处理这些接口及其合法子接口。
    """
    context = AdaptContext(frozenset(target_interfaces))
    return merge_interface_results(
        execute_rules(ADAPT_RULES, document, context),
        InterfaceAdaptationOutcome(),
    )


def validate_compatibility(
    document: CiscoDocument, target_interfaces: Iterable[str]
) -> InterfaceAdaptationOutcome:
    """执行接口类型兼容校验规则。

    该入口不包含删除引用校验，用于单独验证目标接口的最终语法类型。
    """
    context = ValidationContext(frozenset(target_interfaces))
    results = execute_rules(COMPATIBILITY_VALIDATION_RULES, document, context)
    return merge_interface_results(results, InterfaceAdaptationOutcome())


def validate_interfaces(
    document: CiscoDocument,
    target_interfaces: Iterable[str],
    removed_interfaces: Iterable[str],
) -> InterfaceAdaptationOutcome:
    """执行完整接口和目标引用校验。

    同时传入目标与已删除集合，使最终规则能识别残留引用和接口类型冲突。
    """
    context = ValidationContext(
        frozenset(target_interfaces), frozenset(removed_interfaces)
    )
    return merge_interface_results(
        execute_rules(VALIDATION_RULES, document, context),
        InterfaceAdaptationOutcome(),
    )


def adapt_to_simulation(
    document: CiscoDocument,
    policy: SimulationAdaptationPolicy,
    data_interfaces: Iterable[str],
) -> SimulationAdaptationOutcome:
    """按策略执行数据接口和 BFD 模拟适配。

    关闭模式直接返回空结果，避免在不允许适配时构造或执行模拟规则。
    """
    if policy.mode == "off":
        return SimulationAdaptationOutcome()
    context = SimulationContext(policy, frozenset(data_interfaces))
    return merge_simulation_results(
        execute_rules(SIMULATION_RULES, document, context)
    )
