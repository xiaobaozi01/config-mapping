"""Junos 聚合 options 与 UNI 换型专属属性的共享分类。"""

BUNDLE_OPTION_NAMES = frozenset(
    {"aggregated-ether-options", "gigether-options", "ether-options"}
)
UNI_OPTION_NAMES = BUNDLE_OPTION_NAMES | {"fastether-options"}


def is_uni_type_attribute(command: str) -> bool:
    """识别 UNI 源接口上不再保留的聚合及成员 options。

    聚合改名只使用原有三个 options，UNI 扁平化另外处理 fastether 容器。
    """
    return command in UNI_OPTION_NAMES
