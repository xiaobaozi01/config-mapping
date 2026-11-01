"""Cisco IOS XR 接口裁剪能力门面。"""

from impl.common.domain.outcomes import InterfacePruningOutcome

from ..document import CiscoDocument
from ..rules.pruning import (
    prune_interface_dependencies,
    prune_non_nni,
)


class CiscoPruningOperations:
    """提供 IOS XR 非 NNI 和成员依赖裁剪能力。

    规则实现位于 ``rules.pruning``，本类只提供稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定裁剪能力所属文档。

        构造阶段不执行任何规则。
        """
        self._document = document

    def prune_non_nni(
        self,
        protected_parents: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """删除 NNI 保留闭包之外的接口业务。

        Cisco 当前规则不接受严格输入开关，但入口保持统一协议。
        """
        del strict_input_validation
        return prune_non_nni(self._document, protected_parents)

    def prune_interface_dependencies(
        self,
        removed_interfaces: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """清理已删除成员接口的依赖。

        Cisco 当前规则不接受严格输入开关，但入口保持统一协议。
        """
        del strict_input_validation
        return prune_interface_dependencies(self._document, removed_interfaces)
