"""Juniper 规则包：显式阶段注册表与执行入口。"""

from impl.common.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationMode
from impl.common.rules import (
    AdaptContext,
    PruneContext,
    SimulationContext,
    ValidationContext,
    execute_prune_rules,
    execute_rules,
    merge_interface_results,
    merge_simulation_results,
)

from . import compatibility, pruning, simulation
from .runtime import JunosVisitorRule

PRUNE_RULES = (
    JunosVisitorRule(
        "juniper.prune.non_nni",
        document_action=lambda doc, ctx: pruning._JunosPruner(
            doc,
            ctx.protected_parents,
            strict_input_validation=ctx.strict_input_validation,
        ).run(),
    ),
)
ADAPT_RULES = ()
COMPATIBILITY_VALIDATION_RULES = (
    JunosVisitorRule(
        "juniper.validate.reference_bindings",
        document_action=compatibility.validate_reference_bindings,
        readonly=True,
    ),
    JunosVisitorRule(
        "juniper.validate.gateway_role",
        document_action=compatibility.validate_gateway_role,
        readonly=True,
    ),
    JunosVisitorRule(
        "juniper.validate.units",
        document_action=compatibility.validate_units,
        readonly=True,
    ),
)
SOURCE_SEMANTIC_VALIDATION_RULES = (
    JunosVisitorRule(
        "juniper.validate.gateway_role",
        document_action=compatibility.validate_gateway_role,
        readonly=True,
    ),
    JunosVisitorRule(
        "juniper.validate.units",
        document_action=compatibility.validate_units,
        readonly=True,
    ),
)
VALIDATION_RULES = COMPATIBILITY_VALIDATION_RULES
FINAL_INTEGRITY_VALIDATION_RULES = (
    JunosVisitorRule(
        "juniper.validate.reference_bindings",
        document_action=compatibility.validate_reference_bindings,
        readonly=True,
    ),
)
SIMULATION_RULES = (
    JunosVisitorRule(
        "juniper.simulate.empty_options",
        document_action=simulation.remove_empty_interface_options,
        condition=lambda ctx: (
            ctx.policy.mode != SimulationMode.OFF
            and ctx.policy.remove_physical_interface_knobs
        ),
    ),
    JunosVisitorRule(
        "juniper.simulate.enable_interfaces",
        document_action=simulation.enable_data_interfaces,
        condition=lambda ctx: (
            ctx.policy.mode != SimulationMode.OFF
            and ctx.policy.ensure_data_interfaces_enabled
        ),
    ),
    JunosVisitorRule(
        "juniper.simulate.bfd",
        document_action=simulation.clamp_bfd_timers,
        condition=lambda ctx: ctx.policy.mode == SimulationMode.STABLE,
    ),
)


def prune_non_nni(document, protected_parents, *, strict_input_validation=True):
    """执行 Junos 非 NNI 接口裁剪规则。

    保护集合来自原始拓扑，使接口迁移不会改变裁剪边界。
    """
    context = PruneContext(
        frozenset(protected_parents),
        strict_input_validation=strict_input_validation,
    )
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


def validate_source_semantics(document, target_interfaces):
    """在映射前执行 Junos 源配置语义校验。

    网关定义缺失和删除引用完整性不进入该入口。
    """
    context = ValidationContext(
        frozenset(target_interfaces),
        strict_input_validation=True,
    )
    results = execute_rules(SOURCE_SEMANTIC_VALIDATION_RULES, document, context)
    return merge_interface_results(results, InterfaceAdaptationOutcome())


def validate_interfaces(
    document,
    target_interfaces,
    removed_interfaces,
    deferred_interface_references=(),
    *,
    strict_input_validation=True,
):
    """执行 Junos 接口和删除引用校验。

    目标与删除集合共同构成只读校验上下文，避免验证阶段修改配置。
    """
    context = ValidationContext(
        frozenset(target_interfaces),
        frozenset(removed_interfaces),
        frozenset(deferred_interface_references),
        strict_input_validation,
    )
    results = execute_rules(VALIDATION_RULES, document, context)
    return merge_interface_results(results, InterfaceAdaptationOutcome())


def validate_final_integrity(
    document,
    target_interfaces,
    removed_interfaces,
    deferred_interface_references=(),
):
    """校验适配产物的引用完整性。

    unit 和网关角色等源配置语义已在前置阶段处理，这里只检查删除后引用。
    """
    context = ValidationContext(
        frozenset(target_interfaces),
        frozenset(removed_interfaces),
        frozenset(deferred_interface_references),
    )
    results = execute_rules(FINAL_INTEGRITY_VALIDATION_RULES, document, context)
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
