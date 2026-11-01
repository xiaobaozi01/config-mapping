"""Cisco IOS XR 清洗能力门面。"""

from collections.abc import Sequence

from impl.common.domain.outcomes import CleanupOutcome
from impl.common.rules.cleaning import CleaningAction, CleaningOperation

from ..document import CiscoDocument
from .declarative_cleaning import apply_cleaning_rules
from .management_cleaning import add_lab_account, clean_management_access


class CiscoCleaningOperations:
    """组合 IOS XR 管理面清洗和声明式规则执行能力。

    能力对象只转发到两个专用模块，不缓存配置状态。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定清洗能力所属文档。

        构造阶段不读取或修改配置。
        """
        self._document = document

    def clean_management_access(self) -> CleanupOutcome:
        """删除生产管理面配置。

        返回结果只包含分类统计。
        """
        return clean_management_access(self._document)

    def add_lab_account(self) -> None:
        """添加统一实验账号。

        账号写入通过 Document 编辑事务完成。
        """
        add_lab_account(self._document)

    def apply_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: CleaningAction,
        value: str | None,
    ) -> int:
        """执行一条声明式清洗规则。

        返回值是该规则的命中数量。
        """
        action = CleaningAction(action)
        if action == CleaningAction.REPLACE and value is None:
            value = ""
        return self.apply_rules(
            [CleaningOperation(path, match, action, value)]
        )[0]

    def apply_rules(self, operations: Sequence[CleaningOperation]) -> list[int]:
        """批量执行声明式清洗规则。

        返回顺序与输入规则顺序一致。
        """
        return apply_cleaning_rules(self._document, operations)
