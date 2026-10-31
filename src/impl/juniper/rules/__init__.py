"""Juniper 规则包：显式阶段注册表与执行入口。"""

from impl.common.outcomes import InterfaceAdaptationOutcome, InterfacePruningOutcome, SimulationAdaptationOutcome
from impl.common.rules import (
    AdaptContext, FunctionRule, PruneContext, SimulationContext, ValidationContext,
    execute_rules, execute_prune_rules, merge_interface_results, merge_simulation_results,
)
from . import compatibility, pruning, simulation

PRUNE_RULES = (
    FunctionRule("juniper.prune.non_nni", lambda doc, ctx: pruning._JunosPruner(doc, ctx.protected_parents).run()),
)
ADAPT_RULES = ()
COMPATIBILITY_VALIDATION_RULES = (
    FunctionRule("juniper.validate.reference_bindings", compatibility.validate_reference_bindings),
    FunctionRule("juniper.validate.units", compatibility.validate_units),
)
VALIDATION_RULES = COMPATIBILITY_VALIDATION_RULES
SIMULATION_RULES = (
    FunctionRule("juniper.simulate.empty_options", simulation.remove_empty_interface_options,
                 lambda ctx: ctx.policy.mode != "off" and ctx.policy.remove_physical_interface_knobs),
    FunctionRule("juniper.simulate.enable_interfaces", simulation.enable_data_interfaces,
                 lambda ctx: ctx.policy.mode != "off" and ctx.policy.ensure_data_interfaces_enabled),
    FunctionRule("juniper.simulate.bfd", simulation.clamp_bfd_timers,
                 lambda ctx: ctx.policy.mode == "stable"),
)


def prune_non_nni(document, protected_parents):
    context = PruneContext(frozenset(protected_parents))
    return merge_interface_results(execute_prune_rules(PRUNE_RULES, document, context), InterfacePruningOutcome())


def adapt_interfaces(document, target_interfaces):
    context = AdaptContext(frozenset(target_interfaces))
    return merge_interface_results(execute_rules(ADAPT_RULES, document, context), InterfaceAdaptationOutcome())


def validate_interfaces(document, target_interfaces, removed_interfaces):
    context = ValidationContext(frozenset(target_interfaces), frozenset(removed_interfaces))
    results = execute_rules(VALIDATION_RULES, document, context)
    return merge_interface_results(results, InterfaceAdaptationOutcome())


def adapt_to_simulation(document, policy, data_interfaces):
    if policy.mode == "off":
        return SimulationAdaptationOutcome()
    context = SimulationContext(policy, frozenset(data_interfaces))
    return merge_simulation_results(execute_rules(SIMULATION_RULES, document, context))
