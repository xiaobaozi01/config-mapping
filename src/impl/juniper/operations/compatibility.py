"""Juniper Junos 目标接口兼容能力门面。"""

from ..document import JunosDocument
from ..rules import adapt_interfaces


class JunosCompatibilityOperations:
    """提供 Junos 目标接口兼容适配能力。

    规则实现位于 ``rules``，本类只提供稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: JunosDocument):
        """绑定兼容能力所属文档。

        构造阶段不执行规则。
        """
        self._document = document

    def adapt_interfaces(self, target_interfaces):
        """适配最终目标接口配置。

        返回统一接口适配结果。
        """
        return adapt_interfaces(self._document, target_interfaces)
