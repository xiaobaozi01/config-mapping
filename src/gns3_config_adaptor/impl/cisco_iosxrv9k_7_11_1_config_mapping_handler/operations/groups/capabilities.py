"""Cisco Group 能力门面。"""

from __future__ import annotations

from collections.abc import Iterable

from ....common.domain.outcomes import GroupExpansionOutcome
from ....common.domain.policies import WashingPolicy
from ...document import CiscoDocument
from .expander import CiscoGroupExpander


class CiscoGroupOperations:
    """提供 IOS XR Group 展开能力。

    能力对象只组合现有展开器，不持有额外配置状态。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定 Group 展开所属文档。

        构造阶段不解析或展开任何 Group。
        """
        self._document = document

    def expand(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """展开当前配置引用的全部 Group。

        返回结果保留既有事务回滚和冲突诊断语义。
        """
        return CiscoGroupExpander(self._document).expand_groups(
            known_interfaces, policy
        )
