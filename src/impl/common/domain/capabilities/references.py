"""接口引用改写能力协议。"""

from __future__ import annotations

from typing import Protocol


class ReferenceOperations(Protocol):
    """定义接口引用改写能力。

    能力只更新接口定义之外的引用，不负责接口定义改名。
    """

    def replace(self, replacements: dict[str, list[str]]) -> None:
        """按映射表重写接口引用。

        一对多映射必须保持确定顺序，并避免级联替换新目标名称。
        """
        ...
