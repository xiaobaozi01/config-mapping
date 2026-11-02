"""Juniper Junos 只读校验能力门面。"""

from ..document import JunosDocument
from ..rules import validate_final_integrity


class JunosValidationOperations:
    """提供 Junos 只读接口校验能力。

    规则实现保持只读，本类只组合稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: JunosDocument):
        """绑定校验能力所属文档。

        构造阶段不执行规则或修改配置。
        """
        self._document = document

    def validate_final_integrity(
        self,
        target_interfaces,
        removed_interfaces,
        deferred_interface_references=(),
    ):
        """校验最终配置的引用完整性。

        源配置语义不会在这里重复校验。
        """
        return validate_final_integrity(
            self._document,
            target_interfaces,
            removed_interfaces,
            deferred_interface_references,
        )
