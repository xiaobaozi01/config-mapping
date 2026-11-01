"""保留 IOS XR 聚合业务层，调整模拟成员门限。"""

import re

from impl.common.domain.interface import BundleMode
from impl.common.domain.outcomes import InterfaceAdaptationOutcome

from .interfaces import ensure_parent_interface, find_interface_node


def bundle_mode(document, member) -> BundleMode:
    """读取聚合成员唯一绑定的 LACP 模式。

    绑定语法冲突时返回未知；地址、VRF 和 l2transport 等业务由独立检查保留，
    不影响模式解析。
    """
    block = find_interface_node(document, member)
    if block is None:
        return BundleMode.UNKNOWN
    bindings = []
    for node in block.walk():
        if re.match(r"^bundle\s+id\b", node.header, re.I):
            match = re.fullmatch(
                r"bundle\s+id\s+(\d+)(?:\s+mode\s+(on|active|passive))?",
                node.header,
                re.I,
            )
            if not match:
                return BundleMode.UNKNOWN
            bindings.append(
                (
                    match.group(1),
                    BundleMode((match.group(2) or BundleMode.ON).lower()),
                )
            )
    return bindings[0][1] if len(set(bindings)) == 1 else BundleMode.UNKNOWN


def adapt_bundle(document, name):
    """调整聚合主口的模拟成员数量和带宽门限。

    只修改已有聚合门限并保留业务子树，可避免模拟环境把生产规模约束当成必须满足的
    硬件条件。
    """
    result = InterfaceAdaptationOutcome()
    ensure_parent_interface(document, name)
    block = find_interface_node(document, name)
    if not any(
        node.active and re.match(r"^mac(?:-|\s+)address\s+", node.header, re.I)
        for node in block.nodes
    ):
        result.warnings.append(
            f"聚合 {name} 未配置显式 MAC；XRv9000 7.11.1 需实机核验 "
            "Bundle MAC 与聚合 up 状态，不自动生成 MAC 或修改全局 LACP MAC"
        )
    transaction = document.edit()
    for node in block.nodes:
        if not node.active:
            continue
        if re.match(
            r"^bundle\s+minimum-active\s+links\s+\d+$", node.header, re.I
        ):
            old = node.header
            updated = "bundle minimum-active links 1"
            transaction.replace_header(node, updated, "调整聚合门限")
            if old != updated:
                result.changes.append(
                    {
                        "interface": name,
                        "action": "replace",
                        "reason": "缩减成员后最小活动成员数调整为1",
                    }
                )
        elif re.match(
            r"^bundle\s+minimum-active\s+bandwidth\b", node.header, re.I
        ):
            transaction.delete(node, "删除生产聚合带宽门限")
            result.changes.append(
                {
                    "interface": name,
                    "action": "remove",
                    "reason": "删除不适用于模拟成员带宽的生产聚合门限",
                }
            )
    transaction.apply()
    return result
