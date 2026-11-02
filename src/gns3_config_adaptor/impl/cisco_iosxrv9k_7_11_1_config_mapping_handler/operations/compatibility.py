"""Cisco IOS XR 目标接口兼容能力门面。"""

from ...common.domain.outcomes import InterfaceAdaptationOutcome
from ..document import CiscoDocument
from ..rules import adapt_interfaces


class CiscoCompatibilityOperations:
    """提供 IOS XR 目标接口兼容适配能力。

    规则实现位于 ``rules.compatibility``，本类只提供稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定兼容能力所属文档。

        构造阶段不读取或修改配置。
        """
        self._document = document

    def adapt_interfaces(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """适配最终目标接口配置。

        返回现有规则产生的修改、告警和错误。
        """
        return adapt_interfaces(self._document, target_interfaces)
