"""Cisco IOS XR 只读校验能力门面。"""

from impl.common.domain.outcomes import InterfaceAdaptationOutcome

from ..document import CiscoDocument
from ..rules import (
    validate_final_integrity,
    validate_interfaces,
    validate_source_semantics,
)
from ..support.clone_constraints import validate_nni_clone


class CiscoValidationOperations:
    """提供 IOS XR 只读接口校验能力。

    所有规则实现保持只读，本类只组合稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定校验能力所属文档。

        构造阶段不执行规则或修改配置。
        """
        self._document = document

    def validate_nni_clone(
        self, source: str, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """检查源接口能否复制到全部目标。

        校验先于克隆执行。
        """
        return validate_nni_clone(self._document, source, target_interfaces)

    def validate_source_semantics(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """校验映射前的源配置语义。

        该入口不执行目标镜像和最终引用检查。
        """
        return validate_source_semantics(self._document, target_interfaces)

    def validate_final_integrity(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references=(),
    ) -> InterfaceAdaptationOutcome:
        """校验最终配置的完整性和目标兼容性。

        源配置语义不会在这里重复校验。
        """
        return validate_final_integrity(
            self._document,
            target_interfaces,
            removed_interfaces,
            deferred_interface_references,
        )

    def validate_interfaces(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references=(),
        *,
        strict_input_validation: bool = True,
    ) -> InterfaceAdaptationOutcome:
        """执行完整接口和引用校验。

        校验保持只读，结果由调用方写入报告。
        """
        return validate_interfaces(
            self._document,
            target_interfaces,
            removed_interfaces,
            deferred_interface_references,
            strict_input_validation=strict_input_validation,
        )
