"""四类托管 Visitor 模板；合成 RULE_DEMO 标记不在默认注册表中启用。"""

from impl.common.interface import InterfaceKind
from impl.common.outcomes import (
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationMode

from .support import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    InterfaceScope,
    document_scope,
    simulation_result,
)

_PHYSICAL = frozenset({InterfaceKind.PHYSICAL})
_TARGETS = InterfaceScope("target_interfaces", True, _PHYSICAL)
_DATA_PARENTS = InterfaceScope("data_interfaces", False, _PHYSICAL)


class DemoDependencyVisitor(CiscoRuleVisitor):
    """演示规则：删除依赖已移除接口的描述行。

    示例展示规则如何读取删除上下文并通过托管编辑收集动作，而不直接修改文档。
    """

    def __init__(self, session):
        """保存演示规则所需的已删除接口集合。

        规范化一次后供每个描述行比较，避免重复解析接口别名。
        """
        super().__init__(session)
        self.removed = {
            session.document.resolve_interface(name)
            for name in session.context.removed_interfaces
        }

    def visitNode(self, node):
        """检查描述行是否引用已删除接口并登记删除。

        仅识别固定示例前缀，避免误伤普通描述文本。
        """
        prefix = "description RULE_DEMO_DEPENDS_ON "
        if node.header.startswith(prefix):
            source = self.session.document.resolve_interface(
                node.header[len(prefix) :]
            )
            if source in self.removed:
                self.session.edits.delete(
                    node, "示例依赖的接口已删除", source_interface=source
                )
                return None
        return self.visitChildren(node)


class DemoDescriptionVisitor(CiscoRuleVisitor):
    """演示规则：替换旧示例描述并报告重复描述冲突。

    多条描述无法确定唯一目标时只报错，示例展示了规则的事务回滚行为。
    """

    def visitNode(self, block):
        """处理一个接口块中的示例描述命令。

        只在旧描述唯一存在时替换，防止覆盖其他业务描述。
        """
        descriptions = [
            node
            for node in block.nodes
            if node.header.startswith("description ")
        ]
        old = [
            node
            for node in descriptions
            if node.header == "description RULE_DEMO_OLD"
        ]
        if old and len(descriptions) > 1:
            self.session.result.errors.append(
                f"接口 {block.interface_name} 示例描述存在冲突"
            )
        else:
            for node in old:
                self.session.edits.replace_header(
                    node,
                    "description RULE_DEMO_NEW",
                    "示例描述适配",
                    interface=block.interface_name,
                )
        return None


class DemoEnableVisitor(CiscoRuleVisitor):
    """演示规则：把示例接口的 shutdown 改为 no shutdown。

    该行为模拟稳定模式下的接口启用，不影响接口之外的主体配置。
    """

    def visitNode(self, block):
        """替换接口块中的直接 shutdown 子命令。

        只处理当前接口块的一级命令，避免误改嵌套协议配置。
        """
        for node in block.nodes:
            if node.header.lower() == "shutdown":
                self.session.edits.replace_header(
                    node,
                    "no shutdown",
                    "示例启用数据口",
                    category="demo-enable",
                    interface=block.interface_name,
                )
        return None


class DemoValidationVisitor(CiscoRuleVisitor):
    """演示规则：拒绝包含固定非法描述的配置。

    只读 Visitor 展示校验规则可以报告错误而不能申请编辑。
    """

    def visitNode(self, node):
        """检查节点是否包含示例非法描述。

        命中时写入结构化错误并继续遍历，使一次运行可以报告全部问题。
        """
        if node.header == "description RULE_DEMO_INVALID":
            self.session.result.errors.append(
                f"示例非法描述位于 {' / '.join(node.path)}"
            )
        return self.visitChildren(node)


def demo_enable_condition(context):
    """判断示例启用规则是否应在当前策略下执行。

    关闭模式或禁用接口启用能力时不运行示例，保持与其他模拟规则一致。
    """
    return (
        context.policy.mode != SimulationMode.OFF
        and context.policy.ensure_data_interfaces_enabled
    )


PRUNE_DEMO_RULE = CiscoVisitorRule(
    "cisco.prune.demo",
    DemoDependencyVisitor,
    selector=document_scope,
    outcome_factory=InterfacePruningOutcome,
)
ADAPT_DEMO_RULE = CiscoVisitorRule(
    "cisco.adapt.demo", DemoDescriptionVisitor, selector=_TARGETS
)
SIMULATE_DEMO_RULE = CiscoVisitorRule(
    "cisco.simulate.demo",
    DemoEnableVisitor,
    selector=_DATA_PARENTS,
    condition=demo_enable_condition,
    outcome_factory=SimulationAdaptationOutcome,
    result_mapper=simulation_result,
)
VALIDATE_DEMO_RULE = CiscoVisitorRule(
    "cisco.validate.demo",
    DemoValidationVisitor,
    selector=_TARGETS,
    readonly=True,
)
