"""模拟规则声明动作与统计类别，生命周期由 AST 规则执行器管理。"""

import re

from impl.common.interface import InterfaceKind, interface_unit
from impl.common.outcomes import SimulationAdaptationOutcome
from impl.common.policies import SimulationMode

from .support import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    InterfaceScope,
    simulation_result,
)

_SHUTDOWN = re.compile(r"^shutdown$", re.I)
_NO_SHUTDOWN = re.compile(r"^no\s+shutdown$", re.I)
_BFD_INTERVAL = re.compile(
    r"^(bfd\s+(?:minimum-interval|minimum-receive-interval)\s+)(\d+)(\s*)$",
    re.I,
)
_BFD_MULTIPLIER = re.compile(r"^(bfd\s+multiplier\s+)(\d+)(\s*)$", re.I)
_DATA_INTERFACES = InterfaceScope(
    "data_interfaces", True, frozenset(InterfaceKind)
)


def enable_interfaces_condition(context):
    """判断当前策略是否要求启用数据接口。

    只有非关闭模式且显式开启该能力时才执行，避免无关模式产生额外配置。
    """
    return (
        context.policy.mode != SimulationMode.OFF
        and context.policy.ensure_data_interfaces_enabled
    )


def stable_condition(context):
    """判断策略是否处于稳定模式。

    BFD 门限调整只用于稳定模式，兼容模式不得改变已有业务定时器。
    """
    return context.policy.mode == SimulationMode.STABLE


class EnableInterfacesVisitor(CiscoRuleVisitor):
    """为选中的数据接口移除 shutdown 或补写 no shutdown。

    该行为只在已映射接口上执行，防止模拟环境误开启管理口或未参与迁移的接口。
    """

    def visit_node(self, node):
        """处理单个接口块中的启停配置。

        顶层接口缺少显式启用语句时追加 no shutdown，子命令中的 shutdown 则整段删除。
        """
        if node.parent is self.session.document.root:
            if interface_unit(node.interface_name) is None and not any(
                _NO_SHUTDOWN.fullmatch(child.header) for child in node.walk()
            ):
                self.session.edits.append_child(
                    node,
                    "no shutdown\n",
                    "启用数据口",
                    category="interface-no-shutdown",
                )
        elif _SHUTDOWN.fullmatch(node.header):
            count = sum(1 for _ in node.walk(include_self=True))
            self.session.edits.delete(
                node,
                "删除数据口 shutdown",
                category="interface-shutdown",
                count=count,
            )
            return None
        return self.visit_children(node)


class BfdTimersVisitor(CiscoRuleVisitor):
    """把低于策略下限的 Cisco BFD 定时器提升到安全值。

    只上修小于阈值的已有参数，不新增 BFD 或降低已经较高的配置。
    """

    def visit_node(self, node):
        """检查接口子树下的 BFD interval 和 multiplier 命令。

        对每个匹配语句单独记录类别，使报告能区分两类门限调整。
        """
        if node.parent is not self.session.document.root:
            policy = self.session.context.policy
            for pattern, minimum, category in (
                (
                    _BFD_INTERVAL,
                    policy.bfd_minimum_interval_ms,
                    "bfd-minimum-interval",
                ),
                (
                    _BFD_MULTIPLIER,
                    policy.bfd_minimum_multiplier,
                    "bfd-multiplier",
                ),
            ):
                match = pattern.fullmatch(node.header)
                if match and int(match.group(2)) < minimum:
                    updated = f"{match.group(1)}{minimum}{match.group(3)}"
                    self.session.edits.replace_header(
                        node, updated, "调整 BFD 定时器", category=category
                    )
        return self.visit_children(node)


ENABLE_INTERFACES_RULE = CiscoVisitorRule(
    "cisco.simulate.enable_interfaces",
    EnableInterfacesVisitor,
    selector=_DATA_INTERFACES,
    condition=enable_interfaces_condition,
    outcome_factory=SimulationAdaptationOutcome,
    result_mapper=simulation_result,
)
BFD_RULE = CiscoVisitorRule(
    "cisco.simulate.bfd",
    BfdTimersVisitor,
    condition=stable_condition,
    outcome_factory=SimulationAdaptationOutcome,
    result_mapper=simulation_result,
)


def adapt_to_simulation(document, policy, data_interfaces):
    """通过注册表执行 Cisco 模拟适配规则。

    入口只构造稳定调用路径，具体启停和 BFD 行为由规则对象提供。
    """
    from . import adapt_to_simulation as execute

    return execute(document, policy, data_interfaces)


def enable_data_interfaces(document, context):
    """执行数据接口启用规则。

    函数入口供规则表直接引用，保持动作实现与注册声明分离。
    """
    return ENABLE_INTERFACES_RULE.apply(document, context)


def clamp_bfd_timers(document, context):
    """执行 BFD 定时器下限规则。

    委托已注册规则可复用条件判断、事务和结果映射。
    """
    return BFD_RULE.apply(document, context)
