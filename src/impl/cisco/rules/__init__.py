"""Cisco 规则包：显式阶段注册表与执行入口；业务模块和镜像 YAML 位于同包。"""

from collections.abc import Iterable
from typing import TypeAlias

from impl.common.outcomes import InterfaceAdaptationOutcome, InterfacePruningOutcome, SimulationAdaptationOutcome
from impl.common.policies import SimulationAdaptationPolicy
from impl.common.rules import (
    AdaptContext, ConfigurationRule, PruneContext, SimulationContext, ValidationContext,
    execute_rules, execute_prune_rules, merge_interface_results, merge_simulation_results,
)
from ..document import CiscoDocument
from . import compatibility, pruning, simulation

CiscoPruneRule: TypeAlias = ConfigurationRule[CiscoDocument, PruneContext, InterfacePruningOutcome]
CiscoAdaptRule: TypeAlias = ConfigurationRule[CiscoDocument, AdaptContext, InterfaceAdaptationOutcome]
CiscoValidationRule: TypeAlias = ConfigurationRule[CiscoDocument, ValidationContext, InterfaceAdaptationOutcome]
CiscoSimulationRule: TypeAlias = ConfigurationRule[CiscoDocument, SimulationContext, SimulationAdaptationOutcome]

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
VALIDATION_RULES: tuple[CiscoValidationRule, ...] = COMPATIBILITY_VALIDATION_RULES + (
    compatibility.REMOVED_REFERENCES_RULE,
)
SIMULATION_RULES: tuple[CiscoSimulationRule, ...] = (
    simulation.ENABLE_INTERFACES_RULE,
    simulation.BFD_RULE,
)


def prune_non_nni(document: CiscoDocument, protected_parents: Iterable[str]) -> InterfacePruningOutcome:
    context = PruneContext(frozenset(protected_parents))
    return merge_interface_results(execute_prune_rules(PRUNE_RULES, document, context), InterfacePruningOutcome())


def prune_member_dependencies(document: CiscoDocument, removed_interfaces: Iterable[str]) -> InterfacePruningOutcome:
    context = PruneContext(
        frozenset(), frozenset(document.resolve_interface(name) for name in removed_interfaces),
        remove_non_nni=False,
    )
    outcome = InterfacePruningOutcome(removed_interfaces=set(context.removed_interfaces))
    return merge_interface_results(execute_prune_rules(PRUNE_RULES, document, context), outcome)


def adapt_interfaces(document: CiscoDocument, target_interfaces: Iterable[str]) -> InterfaceAdaptationOutcome:
    context = AdaptContext(frozenset(target_interfaces))
    return merge_interface_results(execute_rules(ADAPT_RULES, document, context), InterfaceAdaptationOutcome())


def validate_compatibility(document: CiscoDocument, target_interfaces: Iterable[str]) -> InterfaceAdaptationOutcome:
    context = ValidationContext(frozenset(target_interfaces))
    results = execute_rules(COMPATIBILITY_VALIDATION_RULES, document, context)
    return merge_interface_results(results, InterfaceAdaptationOutcome())


def validate_interfaces(
    document: CiscoDocument, target_interfaces: Iterable[str], removed_interfaces: Iterable[str],
) -> InterfaceAdaptationOutcome:
    context = ValidationContext(frozenset(target_interfaces), frozenset(removed_interfaces))
    return merge_interface_results(execute_rules(VALIDATION_RULES, document, context), InterfaceAdaptationOutcome())


def adapt_to_simulation(
    document: CiscoDocument, policy: SimulationAdaptationPolicy, data_interfaces: Iterable[str],
) -> SimulationAdaptationOutcome:
    if policy.mode == "off":
        return SimulationAdaptationOutcome()
    context = SimulationContext(policy, frozenset(data_interfaces))
    return merge_simulation_results(execute_rules(SIMULATION_RULES, document, context))
