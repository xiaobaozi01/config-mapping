"""拒绝无法等价表达的一对多 NNI 地址与单值引用。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..common.interface import interface_parent
from ..common.outcomes import InterfaceAdaptationOutcome
from .document import _CISCO_INTERFACE_TOKEN

if TYPE_CHECKING:
    from .document import CiscoDocument, CiscoNode


def _walk(nodes: list[CiscoNode], path: tuple[str, ...] = ()):
    for node in nodes:
        if not node.active or node.is_formatting:
            continue
        current = (*path, node.header)
        yield node, current
        yield from _walk(node.children, current)


def validate_nni_clone(
    document: CiscoDocument, source: str, target_interfaces: set[str],
) -> InterfaceAdaptationOutcome:
    """允许可复制的 L2 配置，拒绝地址复制及协议本地源的歧义。"""
    outcome = InterfaceAdaptationOutcome()
    if len(target_interfaces) < 2:
        return outcome
    source = document.resolve_interface(source)
    sources = {source, *document.logical_names_under(source)}
    indexed = {name.lower(): name for name in sources}
    for block in document._interface_nodes():
        name = block.interface_name
        if name is None or interface_parent(name) != source:
            continue
        if any(re.match(r"^ipv[46]\s+(?:address|unnumbered)\b", node.header, re.I) for node in block.walk()):
            outcome.errors.append(
                f"源接口 {name} 含三层地址或借用地址配置，不能复制到多个独立 NNI 目标口"
            )
    for node, path in _walk(document.root.children):
        bgp_source = (
            re.match(r"^router\s+bgp\b", path[0], re.I)
            and re.match(r"^(?:update-source|local-address)\s", node.header, re.I)
        )
        ldp_source = (
            re.match(r"^mpls\s+ldp\b", path[0], re.I)
            and re.match(r"^router-id\s", node.header, re.I)
        )
        unnumbered = (
            re.match(r"^interface\s", path[0], re.I)
            and re.match(r"^ipv[46]\s+unnumbered\s", node.header, re.I)
        )
        if not (bgp_source or ldp_source or unnumbered):
            continue
        refs = {
            indexed[document.resolve_interface(match.group()).lower()]
            for match in _CISCO_INTERFACE_TOKEN.finditer(node.header)
            if document.resolve_interface(match.group()).lower() in indexed
        }
        if refs:
            outcome.errors.append(
                f"单值接口引用不能展开为多个目标: {' / '.join(path)}; 源接口 {', '.join(sorted(refs))}"
            )
    return outcome
