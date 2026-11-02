"""只读配置校验能力协议。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..outcomes import DeferredInterfaceReference, InterfaceAdaptationOutcome


class ValidationOperations(Protocol):
    """定义转换前置约束与最终产物校验能力。

    所有方法必须保持只读，不得创建编辑事务或改变配置树。
    """

    def validate_final_integrity(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references: Iterable[
            DeferredInterfaceReference
        ] = (),
    ) -> InterfaceAdaptationOutcome:
        """校验最终配置的目标兼容和引用完整性。

        不得重复执行源配置语义检查。
        """
        ...
