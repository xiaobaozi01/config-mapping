"""只读配置校验能力协议。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..outcomes import DeferredInterfaceReference, InterfaceAdaptationOutcome


class ValidationOperations(Protocol):
    """定义源配置与最终产物校验能力。

    所有方法必须保持只读，不得创建编辑事务或改变配置树。
    """

    def validate_nni_clone(
        self, source: str, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """检查源接口能否安全复制到全部目标。

        校验必须先于任何克隆修改执行。
        """
        ...

    def validate_source_semantics(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """校验映射前的源配置语义。

        结果不得包含目标引用完整性或最终镜像兼容检查。
        """
        ...

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

    def validate_interfaces(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references: Iterable[
            DeferredInterfaceReference
        ] = (),
        *,
        strict_input_validation: bool = True,
    ) -> InterfaceAdaptationOutcome:
        """执行完整接口约束和引用校验。

        校验失败只返回结构化结果，不在验证过程中自动修复配置。
        """
        ...
