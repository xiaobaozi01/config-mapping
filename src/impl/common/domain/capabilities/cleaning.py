"""配置清洗能力协议。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ...rules.cleaning import CleaningAction, CleaningOperation
from ..outcomes import CleanupOutcome


class CleaningOperations(Protocol):
    """定义管理认证与声明式配置清洗能力。

    所有修改必须通过 Document 的编辑事务提交，并保持审计顺序。
    """

    def clean_management_access(self) -> CleanupOutcome:
        """删除生产管理访问配置。

        返回结果只包含分类计数，不得暴露被删除的秘密值。
        """
        ...

    def add_lab_account(self) -> None:
        """添加隔离实验环境账号。

        认证替换必须保证清理后仍存在可用的登录入口。
        """
        ...

    def apply_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: CleaningAction,
        value: str | None,
    ) -> int:
        """执行一条声明式清洗规则。

        命中数按该规则单独返回，便于报告和审计。
        """
        ...

    def apply_rules(self, operations: Sequence[CleaningOperation]) -> list[int]:
        """批量执行声明式清洗规则。

        返回值顺序必须与输入规则顺序一致。
        """
        ...
