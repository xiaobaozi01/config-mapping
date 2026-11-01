"""Juniper Junos 接口裁剪能力门面。"""

from impl.common.domain.outcomes import InterfacePruningOutcome

from ..document import JunosDocument
from ..rules.pruning import (
    prune_interface_dependencies,
    prune_non_nni,
)


class JunosPruningOperations:
    """提供 Junos 非 NNI 和成员依赖裁剪能力。

    规则实现位于 ``rules.pruning``，本类只提供稳定能力入口。
    """

    __slots__ = ("_document",)

    def __init__(self, document: JunosDocument):
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

        严格输入策略继续传递给现有裁剪器。
        """
        return prune_non_nni(
            self._document,
            protected_parents,
            strict_input_validation=strict_input_validation,
        )

    def prune_interface_dependencies(
        self,
        removed_interfaces: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """清理已删除成员接口的依赖。

        修改通过局部批量编辑事务提交。
        """
        with self._document.edit().bulk():
            return prune_interface_dependencies(
                self._document,
                removed_interfaces,
                strict_input_validation=strict_input_validation,
            )
