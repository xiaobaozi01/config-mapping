"""供节点和文档查询共享的纯 Cisco 命令语义。"""

import re


def _is_preconfigured_interface_header(header: str) -> bool:
    """判断接口头是否声明为 ``preconfigure`` 候选。

    该模式表示硬件尚未实例化，必须在接口分类前识别并排除。
    """
    return bool(
        re.match(r"^interface\s+preconfigure(?:\s|$)", header, re.IGNORECASE)
    )


def _is_l2transport_header(header: str) -> bool:
    """判断接口头是否以 ``l2transport`` 结尾。

    只匹配行内模式可快速区分二层接口，同时不把子命令中的同名文本视为接口模式。
    """
    return bool(
        re.match(r"interface\s+.+\s+l2transport\s*$", header, re.IGNORECASE)
    )


def _interface_name_from_header(header: str) -> str | None:
    """从不可变接口头提取规范化接口名。

    统一处理预配置和 l2transport 后缀，使缓存键不受等价书写差异影响。
    """
    if not header.lower().startswith("interface"):
        return None
    match = re.match(r"^interface\s+(.+?)\s*$", header, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if _is_preconfigured_interface_header(header):
        preconfigured = re.match(
            r"^interface\s+preconfigure(?:\s+(.+?))?\s*$",
            header,
            re.IGNORECASE,
        )
        value = preconfigured.group(1) if preconfigured else None
        if not value:
            return None
    value = re.sub(r"\s+l2transport\s*$", "", value, flags=re.IGNORECASE)
    return canonical_cisco_interface(value)


# 拓扑中经常使用接口缩写，统一展开后才能与配置块可靠匹配。
_CISCO_PREFIXES = {
    "gi": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "te": "TenGigE",
    "tengige": "TenGigE",
    "hu": "HundredGigE",
    "hundredgige": "HundredGigE",
    "fo": "FortyGigE",
    "fortygige": "FortyGigE",
    "tf": "TwentyFiveGigE",
    "twentyfivegige": "TwentyFiveGigE",
    "be": "Bundle-Ether",
    "bundle-ether": "Bundle-Ether",
    "lo": "Loopback",
    "loopback": "Loopback",
    "mgmteth": "MgmtEth",
}

_CISCO_INTERFACE_TOKEN = re.compile(
    r"(?<![\w./-])(?:[A-Za-z][A-Za-z-]*\s*\d[\d/]*(?:\.\d+)?)(?![\w./-])"
)


def canonical_cisco_interface(value: str) -> str:
    """清理接口名空白、移除头部模式后缀并展开常见 IOS XR 缩写。

    拓扑表可能使用 ``Gi``、``Te`` 或 ``BE``，配置中则通常使用完整名称；统一成
    同一种表示后，端点校验、聚合关系查询和接口树改名才能可靠匹配同一接口。
    """
    value = re.sub(
        r"\s+l2transport\s*$", "", value.strip(), flags=re.IGNORECASE
    )
    compact = re.sub(r"\s+", "", value.strip())
    match = re.match(r"([A-Za-z-]+)(.*)", compact)
    if not match:
        return compact
    prefix, suffix = match.groups()
    expanded = _CISCO_PREFIXES.get(prefix.lower(), prefix)
    return f"{expanded}{suffix}"
