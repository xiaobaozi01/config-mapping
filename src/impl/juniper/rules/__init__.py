"""Juniper 规则包：显式阶段注册表与执行入口。"""

from impl.common.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationMode
from impl.common.rules import (
    AdaptContext,
    FunctionRule,
    PruneContext,
    SimulationContext,
    ValidationContext,
    execute_prune_rules,
    execute_rules,
    merge_interface_results,
    merge_simulation_results,
)

from . import compatibility, pruning, simulation

PRUNE_RULES = (
    FunctionRule(
        "juniper.prune.non_nni",
        lambda doc, ctx: pruning._JunosPruner(doc, ctx.protected_parents).run(),
    ),
)
ADAPT_RULES = ()
COMPATIBILITY_VALIDATION_RULES = (
    FunctionRule(
        "juniper.validate.reference_bindings",
        compatibility.validate_reference_bindings,
    ),
    FunctionRule("juniper.validate.units", compatibility.validate_units),
)
VALIDATION_RULES = COMPATIBILITY_VALIDATION_RULES
SIMULATION_RULES = (
    FunctionRule(
        "juniper.simulate.empty_options",
        simulation.remove_empty_interface_options,
        lambda ctx: ctx.policy.mode != SimulationMode.OFF
        and ctx.policy.remove_physical_interface_knobs,
    ),
    FunctionRule(
        "juniper.simulate.enable_interfaces",
        simulation.enable_data_interfaces,
        lambda ctx: ctx.policy.mode != SimulationMode.OFF
        and ctx.policy.ensure_data_interfaces_enabled,
    ),
    FunctionRule(
        "juniper.simulate.bfd",
        simulation.clamp_bfd_timers,
        lambda ctx: ctx.policy.mode == SimulationMode.STABLE,
    ),
)


def prune_non_nni(document, protected_parents):
    """执行 Junos 非 NNI 接口裁剪规则。

    保护集合来自原始拓扑，使接口迁移不会改变裁剪边界。
    """
    context = PruneContext(frozenset(protected_parents))
    return merge_interface_results(
        execute_prune_rules(PRUNE_RULES, document, context),
        InterfacePruningOutcome(),
    )


def adapt_interfaces(document, target_interfaces):
    """执行已注册的 Junos 接口适配规则。

    当前规则集为空时仍返回统一结果，方便后续按同一入口扩展而不修改调用方。
    """
    context = AdaptContext(frozenset(target_interfaces))
    return merge_interface_results(
        execute_rules(ADAPT_RULES, document, context),
        InterfaceAdaptationOutcome(),
    )


def validate_interfaces(document, target_interfaces, removed_interfaces):
    """执行 Junos 接口和删除引用校验。

    目标与删除集合共同构成只读校验上下文，避免验证阶段修改配置。
    """
    context = ValidationContext(
        frozenset(target_interfaces), frozenset(removed_interfaces)
    )
    results = execute_rules(VALIDATION_RULES, document, context)
    return merge_interface_results(results, InterfaceAdaptationOutcome())


def adapt_to_simulation(document, policy, data_interfaces):
    """按镜像策略执行 Junos 模拟环境适配。

    关闭模式直接返回空结果，避免为不使用模拟参数的镜像创建无意义规则上下文。
    """
    if policy.mode == SimulationMode.OFF:
        return SimulationAdaptationOutcome()
    context = SimulationContext(policy, frozenset(data_interfaces))
    return merge_simulation_results(
        execute_rules(SIMULATION_RULES, document, context)
    )
