"""Junos 一对多 NNI 迁移的地址及单值引用约束。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from impl.common.interface import referenced_interface_names
from impl.common.outcomes import InterfaceAdaptationOutcome

if TYPE_CHECKING:
    from .document import JunosDocument, JunosNode


def _walk(document: JunosDocument, node: JunosNode, path: tuple[str, ...] = ()):
    """递归遍历有效配置节点及其相对路径。

    路径与节点同时返回，使约束错误能够说明具体地址或引用所在的配置层级。
    """
    for child in node.children or []:
        if not document._parser.is_effective(child):
            continue
        header = document._parser.base_header(child.header).rstrip(";")
        current = (*path, header)
        yield child, current
        yield from _walk(document, child, current)


def validate_nni_clone(
    document: JunosDocument,
    source: str,
    target_interfaces: set[str],
) -> InterfaceAdaptationOutcome:
    """校验源接口能否安全复制到多个目标接口。

    空 family 和二层 unit 可以重复展开，但带地址或单值引用的三层配置会产生歧义，
    因此必须在克隆前拒绝。
    """
    outcome = InterfaceAdaptationOutcome()
    if len(target_interfaces) < 2:
        return outcome
    source = document.resolve_interface(source)
    sources = {source, *document.logical_names_under(source)}
    parent = document._find_interface_node(source)
    if parent is not None:
        for _, path in _walk(document, parent):
            if re.match(r"^(?:address|unnumbered-address)\b", path[-1]) and any(
                family in {"family inet", "family inet6"}
                for family in path[:-1]
            ):
                outcome.errors.append(
                    f"源接口 {source} 的 {' / '.join(path)} 含三层地址或借用地址配置，"
                    "不能复制到多个独立 NNI 目标口"
                )
    for _, path in _walk(document, document.root):
        bgp_source = "bgp" in path[:-1] and re.match(
            r"^(?:local-interface|local-address)\s", path[-1]
        )
        unnumbered = path[0] == "interfaces" and re.match(
            r"^unnumbered-address\s", path[-1]
        )
        if not (bgp_source or unnumbered):
            continue
        refs = referenced_interface_names(path[-1], sources)
        if refs:
            outcome.errors.append(
                f"单值接口引用不能展开为多个目标: {' / '.join(path)}; "
                f"源接口 {', '.join(sorted(refs))}"
            )
    return outcome
