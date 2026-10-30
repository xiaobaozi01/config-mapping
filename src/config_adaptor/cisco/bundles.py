"""保留 IOS XR 聚合业务层，调整模拟成员门限。"""
import re
from ..common.outcomes import InterfaceAdaptationOutcome
from .interfaces import find_interface_node, ensure_parent_interface


def bundle_mode(document, member):
    block = find_interface_node(document, member)
    if block is None:
        return "unknown"
    bindings = []
    for node in block.walk():
        if re.match(r"^(?:ipv[46]\s+(?:address|unnumbered)|vrf|l2transport|xconnect)\b", node.header, re.I):
            return "unknown"
        if re.match(r"^bundle\s+id\b", node.header, re.I):
            match = re.fullmatch(r"bundle\s+id\s+(\d+)(?:\s+mode\s+(on|active|passive))?", node.header, re.I)
            if not match:
                return "unknown"
            bindings.append((match.group(1), (match.group(2) or "on").lower()))
    return bindings[0][1] if len(set(bindings)) == 1 else "unknown"


def adapt_bundle(document, name):
    result = InterfaceAdaptationOutcome()
    ensure_parent_interface(document, name)
    block = find_interface_node(document, name)
    if not any(node.active and re.match(r"^mac(?:-|\s+)address\s+", node.header, re.I) for node in block.children):
        result.warnings.append(f"聚合 {name} 未配置显式 MAC；XRv9000 7.11.1 需实机核验 Bundle MAC 与聚合 up 状态，不自动生成 MAC 或修改全局 LACP MAC")
    for node in block.children:
        if not node.active:
            continue
        if re.match(r"^bundle\s+minimum-active\s+links\s+\d+$", node.header, re.I):
            old = node.header
            node.header = "bundle minimum-active links 1"
            if old != node.header:
                result.changes.append({"interface": name, "action": "replace", "reason": "缩减成员后最小活动成员数调整为1"})
        elif re.match(r"^bundle\s+minimum-active\s+bandwidth\b", node.header, re.I):
            node.active = False
            result.changes.append({"interface": name, "action": "remove", "reason": "删除不适用于模拟成员带宽的生产聚合门限"})
    return result
