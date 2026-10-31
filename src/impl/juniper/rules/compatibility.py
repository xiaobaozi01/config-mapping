"""Junos 接口引用、网关与逻辑单元兼容校验。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from impl.common.interface import referenced_interface_names
from impl.common.outcomes import InterfacePruningOutcome

if TYPE_CHECKING:
    from ..document import JunosDocument, JunosNode


def _walk(document: JunosDocument, node: JunosNode, path: tuple[str, ...] = ()):
    """递归遍历活动节点并保留厂商语法路径。

    报告需要定位到原始层级，因此这里不把路径转换成跨厂商名称。
    """
    for child in node.children or []:
        if not document._parser.is_effective(child):
            continue
        header = document._parser.base_header(child.header).rstrip(";")
        child_path = (*path, header)
        yield child, child_path
        yield from _walk(document, child, child_path)


def validate_pruned_references(
    document: JunosDocument, removed: set[str]
) -> InterfacePruningOutcome:
    """只读复核最终配置中的已删除接口引用和网关定义。

    已存在的新目标接口可复用源接口名称，因此先从删除集合中排除当前实际名称。
    """
    actual_names = {spec.name for spec in document.interface_specs()}
    actual_names.update(
        document._interface_name(node) for node in document._interface_nodes()
    )
    return _validate_pruned_references(document, removed - actual_names)


def _validate_reference_bindings(
    document: JunosDocument, removed: set[str]
) -> InterfacePruningOutcome:
    """使用给定删除集合检查引用绑定和网关角色。

    原始剪枝阶段调用严格的内部版本，最终阶段先排除重用名称再进入同一检查逻辑。
    """
    result = InterfacePruningOutcome()
    actual_names = {spec.name for spec in document.interface_specs()}
    for node, path in _walk(document, document.root):
        header = document._parser.base_header(node.header)
        if header.startswith("description "):
            continue
        # 全局 interfaces 容器本身不是引用，已移除定义不会出现在有效遍历中。
        refs = referenced_interface_names(header, removed)
        if refs:
            result.errors.append(
                f"未知活动接口引用：{' / '.join(path)} 引用了已删除接口 "
                f"{', '.join(sorted(refs))}"
            )
        gateway = re.fullmatch(
            r"(?:routing-interface|l3-interface)\s+(\S+)", header.rstrip(";")
        )
        if gateway:
            name = gateway.group(1)
            if not re.fullmatch(r"irb\.\d+", name):
                result.errors.append(
                    f"非法网关引用：{' / '.join(path)} 必须引用原生 irb 逻辑接口"
                )
            elif name not in actual_names:
                result.errors.append(
                    f"网关接口缺失：{' / '.join(path)} 引用的 {name} 没有有效定义"
                )
    return result


def validate_units(document, context):
    """校验逻辑单元编号唯一性及二三层模式互斥。

    重复 unit 或混合 bridge 与 inet family 会产生不可预测的配置语义，必须阻止输出。
    """
    result = InterfacePruningOutcome()
    for parent in document._interface_nodes():
        seen = set()
        for unit in document._unit_nodes(parent):
            number = document._unit_number(unit)
            path = (
                f"interfaces / {document._interface_name(parent)} / "
                f"unit {number}"
            )
            if number in seen:
                result.errors.append(f"重复逻辑接口：{path}")
            seen.add(number)
            families = {
                document._parser.base_header(child.header)
                .rstrip(";")
                .removeprefix("family ")
                for child in unit.children or []
                if document._parser.is_effective(child)
                and document._parser.base_header(child.header).startswith(
                    "family "
                )
            }
            if families & {
                "bridge",
                "ethernet-switching",
                "ccc",
                "vpls",
            } and families & {"inet", "inet6"}:
                result.errors.append(f"逻辑接口二三层模式冲突：{path}")
    return result


def _validate_pruned_references(document, removed):
    """合并引用绑定和逻辑单元检查。

    两类问题都属于最终配置完整性，集中返回可让裁剪流程一次报告全部错误。
    """
    result = _validate_reference_bindings(document, removed)
    result.errors.extend(validate_units(document, None).errors)
    return result


def validate_reference_bindings(document, context):
    """按只读校验上下文检查删除接口引用。

    当前仍存在的目标名称会从删除集合中剔除，保证名称复用不被误报为悬空引用。
    """
    actual = {spec.name for spec in document.interface_specs()}
    actual.update(
        document._interface_name(node) for node in document._interface_nodes()
    )
    return _validate_reference_bindings(
        document, set(context.removed_interfaces) - actual
    )
