"""接口裁剪能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..outcomes import InterfacePruningOutcome


class PruningOperations(Protocol):
    """定义非 NNI 接口和依赖裁剪能力。

    保护集合来自原始拓扑，裁剪结果必须保留完整审计信息。
    """

    def prune_non_nni(
        self,
        protected_parents: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """删除 NNI 保留闭包之外的接口业务。

        删除范围必须基于原始接口身份计算。
        """
        ...

    def prune_interface_dependencies(
        self,
        removed_interfaces: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """清理指定已删除接口的可识别依赖。

        不得重新扩大到完整非 NNI 裁剪。
        """
        ...
