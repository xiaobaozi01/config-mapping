"""四类托管 Visitor 模板；合成 RULE_DEMO 标记不在默认注册表中启用。"""

from impl.common.interface import InterfaceKind
from impl.common.outcomes import InterfacePruningOutcome, SimulationAdaptationOutcome
from .support import CiscoVisitorRule, CiscoRuleVisitor, InterfaceScope, document_scope, simulation_result

_PHYSICAL = frozenset({InterfaceKind.PHYSICAL})
_TARGETS = InterfaceScope("target_interfaces", True, _PHYSICAL)
_DATA_PARENTS = InterfaceScope("data_interfaces", False, _PHYSICAL)


class DemoDependencyVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.removed = {session.document.resolve_interface(name) for name in session.context.removed_interfaces}

    def visitNode(self, node):
        prefix = "description RULE_DEMO_DEPENDS_ON "
        if node.header.startswith(prefix):
            source = self.session.document.resolve_interface(node.header[len(prefix):])
            if source in self.removed:
                self.session.edits.delete(node, "示例依赖的接口已删除", source_interface=source)
                return None
        return self.visitChildren(node)


class DemoDescriptionVisitor(CiscoRuleVisitor):
    def visitNode(self, block):
        descriptions = [node for node in block.nodes if node.header.startswith("description ")]
        old = [node for node in descriptions if node.header == "description RULE_DEMO_OLD"]
        if old and len(descriptions) > 1:
            self.session.result.errors.append(f"接口 {block.interface_name} 示例描述存在冲突")
        else:
            for node in old:
                self.session.edits.replace_header(
                    node, "description RULE_DEMO_NEW", "示例描述适配", interface=block.interface_name,
                )
        return None


class DemoEnableVisitor(CiscoRuleVisitor):
    def visitNode(self, block):
        for node in block.nodes:
            if node.header.lower() == "shutdown":
                self.session.edits.replace_header(
                    node, "no shutdown", "示例启用数据口", category="demo-enable", interface=block.interface_name,
                )
        return None


class DemoValidationVisitor(CiscoRuleVisitor):
    def visitNode(self, node):
        if node.header == "description RULE_DEMO_INVALID":
            self.session.result.errors.append(f"示例非法描述位于 {' / '.join(node.path)}")
        return self.visitChildren(node)


def demo_enable_condition(context):
    return context.policy.mode != "off" and context.policy.ensure_data_interfaces_enabled


PRUNE_DEMO_RULE = CiscoVisitorRule(
    "cisco.prune.demo", DemoDependencyVisitor, selector=document_scope, outcome_factory=InterfacePruningOutcome,
)
ADAPT_DEMO_RULE = CiscoVisitorRule("cisco.adapt.demo", DemoDescriptionVisitor, selector=_TARGETS)
SIMULATE_DEMO_RULE = CiscoVisitorRule(
    "cisco.simulate.demo", DemoEnableVisitor, selector=_DATA_PARENTS, condition=demo_enable_condition,
    outcome_factory=SimulationAdaptationOutcome, result_mapper=simulation_result,
)
VALIDATE_DEMO_RULE = CiscoVisitorRule(
    "cisco.validate.demo", DemoValidationVisitor, selector=_TARGETS, readonly=True,
)


# 保留上一版示例函数入口；新规则在注册表直接登记上面的规则对象。
def prune_demo_dependencies(document, context):
    return PRUNE_DEMO_RULE.apply(document, context)


def adapt_demo_description(document, context):
    return ADAPT_DEMO_RULE.apply(document, context)


def simulate_demo_enable(document, context):
    if not SIMULATE_DEMO_RULE.applies(context):
        return SimulationAdaptationOutcome()
    return SIMULATE_DEMO_RULE.apply(document, context)


def validate_demo_description(document, context):
    return VALIDATE_DEMO_RULE.apply(document, context)
