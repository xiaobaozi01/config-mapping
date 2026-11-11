"""IOS XR 聚合属性及 UNI 换型专属命令的共享分类。"""

import re

_BUNDLE_COMMANDS = r"bundle\b|lacp\b"
_AGGREGATION_COMMANDS = rf"{_BUNDLE_COMMANDS}|aggregated-"

AGGREGATION_ATTRIBUTES = re.compile(
    rf"^(?:no\s+)?(?:{_AGGREGATION_COMMANDS})", re.I
)
STRIPPABLE_AGGREGATION_ATTRIBUTES = re.compile(
    rf"^(?:{_AGGREGATION_COMMANDS})", re.I
)
UNI_AGGREGATION_ATTRIBUTES = re.compile(
    rf"^(?:no\s+)?(?:{_BUNDLE_COMMANDS})", re.I
)


def is_uni_type_attribute(command: str) -> bool:
    """识别 UNI 扁平化后失效的聚合属性。

    UNI 的既有匹配范围不包含 aggregated- 别名，普通兼容阶段保留该别名支持。
    """
    return bool(UNI_AGGREGATION_ATTRIBUTES.match(command))
