"""模拟规则声明动作与统计类别，生命周期由 ANTLR 规则执行器管理。"""

import re
from impl.common.interface import InterfaceKind, interface_unit
from impl.common.outcomes import SimulationAdaptationOutcome
from .support import CiscoVisitorRule, CiscoRuleVisitor, InterfaceScope, simulation_result

_SHUTDOWN = re.compile(r"^shutdown$", re.I)
_NO_SHUTDOWN = re.compile(r"^no\s+shutdown$", re.I)
_BFD_INTERVAL = re.compile(r"^(bfd\s+(?:minimum-interval|minimum-receive-interval)\s+)(\d+)(\s*)$", re.I)
_BFD_MULTIPLIER = re.compile(r"^(bfd\s+multiplier\s+)(\d+)(\s*)$", re.I)
_DATA_INTERFACES = InterfaceScope("data_interfaces", True, frozenset(InterfaceKind))


def enable_interfaces_condition(context):
    return context.policy.mode != "off" and context.policy.ensure_data_interfaces_enabled


def stable_condition(context):
    return context.policy.mode == "stable"


class EnableInterfacesVisitor(CiscoRuleVisitor):
    def visitNode(self, node):
        if node.parentCtx is self.session.document.root:
            if interface_unit(node.interface_name) is None and not any(_NO_SHUTDOWN.fullmatch(child.header) for child in node.walk()):
                self.session.edits.append_child(
                    node, "no shutdown\n", "启用数据口", category="interface-no-shutdown",
                )
        elif _SHUTDOWN.fullmatch(node.header):
            count = sum(1 for _ in node.walk(include_self=True))
            self.session.edits.delete(node, "删除数据口 shutdown", category="interface-shutdown", count=count)
            return None
        return self.visitChildren(node)


class BfdTimersVisitor(CiscoRuleVisitor):
    def visitNode(self, node):
        if node.parentCtx is not self.session.document.root:
            policy = self.session.context.policy
            for pattern, minimum, category in (
                (_BFD_INTERVAL, policy.bfd_minimum_interval_ms, "bfd-minimum-interval"),
                (_BFD_MULTIPLIER, policy.bfd_minimum_multiplier, "bfd-multiplier"),
            ):
                match = pattern.fullmatch(node.header)
                if match and int(match.group(2)) < minimum:
                    updated = f"{match.group(1)}{minimum}{match.group(3)}"
                    self.session.edits.replace_header(node, updated, "调整 BFD 定时器", category=category)
        return self.visitChildren(node)


ENABLE_INTERFACES_RULE = CiscoVisitorRule(
    "cisco.simulate.enable_interfaces", EnableInterfacesVisitor,
    selector=_DATA_INTERFACES, condition=enable_interfaces_condition,
    outcome_factory=SimulationAdaptationOutcome, result_mapper=simulation_result,
)
BFD_RULE = CiscoVisitorRule(
    "cisco.simulate.bfd", BfdTimersVisitor, condition=stable_condition,
    outcome_factory=SimulationAdaptationOutcome, result_mapper=simulation_result,
)


def adapt_to_simulation(document, policy, data_interfaces):
    from . import adapt_to_simulation as execute
    return execute(document, policy, data_interfaces)


def enable_data_interfaces(document, context):
    return ENABLE_INTERFACES_RULE.apply(document, context)


def clamp_bfd_timers(document, context):
    return BFD_RULE.apply(document, context)
