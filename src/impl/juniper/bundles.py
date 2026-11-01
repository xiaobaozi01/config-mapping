"""保留 Junos ae/unit 和成员绑定，按模拟成员数调整门限。"""

import re

from impl.common.interface import BundleMode
from impl.common.outcomes import InterfaceAdaptationOutcome


def _walk(document, node):
    """递归遍历节点下的有效子节点。

    只暴露仍然生效的节点，使聚合分析不会读取已逻辑删除的绑定或业务语句。
    """
    for child in node.children or []:
        if document._parser.is_effective(child):
            yield child
            yield from _walk(document, child)


def bundle_mode(document, member) -> BundleMode:
    """返回聚合成员的绑定与协商模式。

    模式决定跨设备聚合能否安全迁移，缺失或冲突信息统一返回未知而不做乐观推断。
    """
    node = document._find_interface_node(member)
    if node is None:
        return BundleMode.UNKNOWN
    bundle_name = _member_bundle(document, node)
    if bundle_name is None:
        return BundleMode.UNKNOWN
    aggregate = document._find_interface_node(bundle_name)
    if aggregate is None:
        return BundleMode.ON
    return _aggregate_lacp_mode(document, aggregate) or BundleMode.ON


def _member_bundle(document, node) -> str | None:
    """返回成员唯一绑定的 ae 名称。

    含业务语句或出现多个绑定时返回 ``None``，避免把不明确归属复制到新的聚合接口。
    """
    bindings = []
    for child in _walk(document, node):
        base = document._parser.base_header(child.header).rstrip(";")
        if re.match(r"^(?:unit|family|address)\b", base):
            return None
        if base.startswith("802.3ad "):
            bindings.append(base.split()[1])
    if len(set(bindings)) != 1:
        return None
    return bindings[0]


def _aggregate_lacp_mode(document, aggregate) -> BundleMode | None:
    """从聚合主口读取显式 LACP 模式。

    active 与 passive 同时出现说明配置不可判定，因此返回未知而不是任选一种模式。
    """
    for options in aggregate.children or []:
        if (
            document._parser.is_effective(options)
            and document._parser.base_header(options.header)
            == "aggregated-ether-options"
        ):
            for lacp in options.children or []:
                if (
                    document._parser.is_effective(lacp)
                    and document._parser.base_header(lacp.header).rstrip(";")
                    == "lacp"
                ):
                    values = {
                        document._parser.base_header(child.header).rstrip(";")
                        for child in lacp.children or []
                        if document._parser.is_effective(child)
                    }
                    if {"active", "passive"} <= values:
                        return BundleMode.UNKNOWN
                    return (
                        BundleMode.ACTIVE
                        if "active" in values
                        else BundleMode.PASSIVE
                    )
    return None


def adapt_bundle(document, name):
    """调整聚合接口的最小成员数和不适用的带宽门限。

    只修改与模拟成员数量直接相关的门限，保留业务主口及其他配置不动。
    """
    result = InterfaceAdaptationOutcome()
    node = document._find_interface_node(name)
    if node is None:
        document.ensure_parent_interface(name)
        return result
    for child in _walk(document, node):
        if not document._parser.is_effective(child):
            continue
        base = document._parser.base_header(child.header).rstrip(";")
        if re.match(r"^minimum-links\s+\d+$", base):
            if base != "minimum-links 1":
                child.header = "minimum-links 1;"
                result.changes.append(
                    {
                        "interface": name,
                        "action": "replace",
                        "reason": "缩减成员后最小活动成员数调整为1",
                    }
                )
        elif re.match(r"^minimum-bandwidth\b", base):
            child.active = False
            result.changes.append(
                {
                    "interface": name,
                    "action": "remove",
                    "reason": "删除不适用于模拟成员带宽的生产聚合门限",
                }
            )
    return result
