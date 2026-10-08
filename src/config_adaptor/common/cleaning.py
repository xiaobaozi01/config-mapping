"""清洗规则在不同厂商配置树之间共享的路径匹配原语。"""

from __future__ import annotations

import re
from functools import lru_cache


def matches_cleaning_path(ancestors: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """判断完整父级路径是否命中规则路径。

    普通元素以不区分大小写的正则匹配一级父节点，``*`` 恰好消费一级，``**``
    消费零级或多级。匹配必须同时用完整条规则路径和完整祖先路径，因而空路径只会
    命中顶层节点。
    """

    @lru_cache(maxsize=None)
    def match(rule_index: int, ancestor_index: int) -> bool:
        if rule_index == len(path):
            return ancestor_index == len(ancestors)
        component = path[rule_index]
        if component == "**":
            return match(rule_index + 1, ancestor_index) or (
                ancestor_index < len(ancestors)
                and match(rule_index, ancestor_index + 1)
            )
        if ancestor_index == len(ancestors):
            return False
        if component == "*" or re.search(
            component,
            ancestors[ancestor_index],
            re.IGNORECASE,
        ):
            return match(rule_index + 1, ancestor_index + 1)
        return False

    return match(0, 0)
