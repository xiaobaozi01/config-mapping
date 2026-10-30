"""保留 Junos ae/unit 和成员绑定，按模拟成员数调整门限。"""
import re
from ..common.outcomes import InterfaceAdaptationOutcome


def _walk(document, node):
    for child in node.children or []:
        if document._parser.is_effective(child):
            yield child
            yield from _walk(document, child)


def bundle_mode(document, member):
    node = document._find_interface_node(member)
    if node is None:
        return "unknown"
    bindings = []
    for child in _walk(document, node):
        if not document._parser.is_effective(child):
            continue
        base = document._parser.base_header(child.header).rstrip(";")
        if re.match(r"^(?:unit|family|address)\b", base):
            return "unknown"
        if base.startswith("802.3ad "):
            bindings.append(base.split()[1])
    if len(set(bindings)) != 1:
        return "unknown"
    aggregate = document._find_interface_node(bindings[0])
    if aggregate is None:
        return "on"
    for options in aggregate.children or []:
        if document._parser.is_effective(options) and document._parser.base_header(options.header) == "aggregated-ether-options":
            for lacp in options.children or []:
                if document._parser.is_effective(lacp) and document._parser.base_header(lacp.header).rstrip(";") == "lacp":
                    values = {document._parser.base_header(child.header).rstrip(";") for child in lacp.children or [] if document._parser.is_effective(child)}
                    if {"active", "passive"} <= values:
                        return "unknown"
                    return "active" if "active" in values else "passive"
    return "on"


def adapt_bundle(document, name):
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
                result.changes.append({"interface": name, "action": "replace", "reason": "缩减成员后最小活动成员数调整为1"})
        elif re.match(r"^minimum-bandwidth\b", base):
            child.active = False
            result.changes.append({"interface": name, "action": "remove", "reason": "删除不适用于模拟成员带宽的生产聚合门限"})
    return result
