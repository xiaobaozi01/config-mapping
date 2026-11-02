"""Juniper Junos 模拟参数适配能力门面。"""

from ..document import JunosDocument
from ..rules.simulation import adapt_to_simulation


class JunosSimulationOperations:
    """提供 Junos 模拟参数适配能力。

    模拟规则位于 ``rules.simulation``，本类只提供稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: JunosDocument):
        """绑定模拟能力所属文档。

        构造阶段不执行任何规则。
        """
        self._document = document

    def adapt(self, policy, data_interfaces):
        """按镜像策略适配已映射逻辑接口。

        返回现有模拟规则产生的分类结果。
        """
        return adapt_to_simulation(self._document, policy, data_interfaces)
