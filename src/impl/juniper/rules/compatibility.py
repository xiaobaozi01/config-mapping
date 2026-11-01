"""Junos 接口引用、网关与逻辑单元兼容校验。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from impl.common.domain.interface import (
    interface_parent,
    referenced_interface_names,
)
from impl.common.domain.outcomes import (
    DeferredInterfaceReference,
    InterfacePruningOutcome,
)

from ..operations.interfaces import (
    interface_name,
    interface_nodes,
    unit_nodes,
    unit_number,
)

if TYPE_CHECKING:
    from ..document import JuniperNode, JunosDocument


def _walk(
    document: JunosDocument, node: JuniperNode, path: tuple[str, ...] = ()
):
    """递归遍历活动节点并保留厂商语法路径。

    报告需要定位到原始层级，因此这里不把路径转换成跨厂商名称。
    """
    for child in node.nodes:
        if not document._parser.is_effective(child):
            continue
        header = document._parser.base_header(child.header).rstrip(";")
        child_path = (*path, header)
        yield child, child_path
        yield from _walk(document, child, child_path)


def _record_input_semantic(outcome, strict, message):
    """按运行策略记录 Junos 源配置语义问题。

    引用悬空等映射完整性错误不经过此入口。
    """
    destination = outcome.errors if strict else outcome.warnings
    destination.append(message)


def _validate_gateway_reference(
    header,
    path,
    actual_names,
    result,
    strict,
    *,
    check_role=True,
    check_missing=True,
):
    """校验一个网关引用形态及其定义是否存在。

    非法 IRB 类型属于源语义问题，缺失定义仍属于引用完整性错误。
    """
    gateway = re.fullmatch(
        r"(?:routing-interface|l3-interface)\s+(\S+)", header.rstrip(";")
    )
    if not gateway:
        return
    name = gateway.group(1)
    if check_role and not re.fullmatch(r"irb\.\d+", name):
        _record_input_semantic(
            result,
            strict,
            f"非法网关引用：{' / '.join(path)} 必须引用原生 irb 逻辑接口",
        )
    elif check_missing and name not in actual_names:
        result.errors.append(
            f"网关接口缺失：{' / '.join(path)} 引用的 {name} 没有有效定义"
        )


def _validation_strict(context, explicit):
    """解析校验上下文与显式覆盖参数中的严格模式。

    上下文优先，缺省时保持低层校验默认严格。
    """
    if context is not None:
        return context.strict_input_validation
    return True if explicit is None else explicit


def validate_pruned_references(
    document: JunosDocument,
    removed: set[str],
    deferred_interface_references=(),
) -> InterfacePruningOutcome:
    """只读复核最终配置中的已删除接口引用和网关定义。

    已存在的新目标接口可复用源接口名称，因此先从删除集合中排除当前实际名称。
    """
    actual_names = _actual_interface_names(document)
    actual_names.update(
        interface_name(document, node) for node in interface_nodes(document)
    )
    return _validate_pruned_references(
        document,
        removed - actual_names,
        deferred_interface_references,
    )


def _validate_reference_bindings(
    document: JunosDocument,
    removed: set[str],
    deferred_interface_references=(),
    *,
    defer_unknown=False,
    strict_input_validation=True,
) -> InterfacePruningOutcome:
    """使用给定删除集合检查引用绑定和网关角色。

    原始剪枝阶段调用严格的内部版本，最终阶段先排除重用名称再进入同一检查逻辑。
    """
    result = InterfacePruningOutcome()
    actual_names = _actual_interface_names(document)
    deferred = {
        (reference.path, reference.interface)
        for reference in deferred_interface_references
    }
    for node, path in _walk(document, document.root):
        header = document._parser.base_header(node.header)
        if header.startswith("description "):
            continue
        # 全局 interfaces 容器本身不是引用，已移除定义不会出现在有效遍历中。
        refs = referenced_interface_names(header, removed)
        unexpected = {name for name in refs if (path, name) not in deferred}
        if unexpected:
            if defer_unknown:
                result.warnings.append(
                    f"接口引用无法确定，已保留原配置等待人工确认："
                    f"{' / '.join(path)} 引用了已删除接口 "
                    f"{', '.join(sorted(unexpected))}"
                )
                result.deferred_interface_references.update(
                    DeferredInterfaceReference(
                        path=path,
                        interface=name,
                        reason="未知 Junos 接口引用，保留原配置",
                    )
                    for name in unexpected
                )
            else:
                result.errors.append(
                    f"未知活动接口引用：{' / '.join(path)} 引用了已删除接口 "
                    f"{', '.join(sorted(unexpected))}"
                )
        _validate_gateway_reference(
            header,
            path,
            actual_names,
            result,
            strict_input_validation,
            check_role=False,
        )
    return result


def _actual_interface_names(document: JunosDocument) -> set[str]:
    """返回当前树中实际存在的接口名称。

    最终校验允许新目标复用被删除源名称，因此需要先排除当前名称。
    """
    return {
        spec.name for spec in document.interfaces.analysis.interface_specs()
    }


def validate_units(document, context, *, strict_input_validation=None):
    """校验逻辑单元编号唯一性及二三层模式互斥。

    重复 unit 或混合 bridge 与 inet family 属于源配置语义问题，默认告警；
    严格输入校验时恢复为阻断错误。
    """
    result = InterfacePruningOutcome()
    strict = _validation_strict(context, strict_input_validation)
    targeted_parents = {
        interface_parent(document.interfaces.analysis.resolve_interface(name))
        for name in getattr(context, "target_interfaces", ())
    }
    for parent in interface_nodes(document):
        parent_name = interface_name(document, parent)
        if (
            targeted_parents
            and interface_parent(parent_name) not in targeted_parents
        ):
            continue
        seen = set()
        for unit in unit_nodes(document, parent):
            number = unit_number(document, unit)
            path = (
                f"interfaces / {interface_name(document, parent)} / "
                f"unit {number}"
            )
            if number in seen:
                _record_input_semantic(
                    result,
                    strict,
                    f"重复逻辑接口：{path}",
                )
            seen.add(number)
            families = {
                document._parser.base_header(child.header)
                .rstrip(";")
                .removeprefix("family ")
                for child in unit.nodes
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
                _record_input_semantic(
                    result,
                    strict,
                    f"逻辑接口二三层模式冲突：{path}",
                )
    return result


def validate_gateway_role(
    document,
    context,
    *,
    strict_input_validation=None,
):
    """只校验网关引用是否使用原生 IRB 接口。

    缺失定义属于引用完整性，由引用绑定校验单独阻断。
    """
    result = InterfacePruningOutcome()
    strict = _validation_strict(context, strict_input_validation)
    actual_names = {
        spec.name for spec in document.interfaces.analysis.interface_specs()
    }
    for node, path in _walk(document, document.root):
        header = document._parser.base_header(node.header)
        _validate_gateway_reference(
            header,
            path,
            actual_names,
            result,
            strict,
            check_missing=False,
        )
    return result


def _validate_pruned_references(
    document,
    removed,
    deferred_interface_references=(),
    *,
    defer_unknown=False,
    strict_input_validation=True,
):
    """合并引用绑定和逻辑单元检查。

    两类问题都属于最终配置完整性，集中返回可让裁剪流程一次报告全部错误。
    """
    result = _validate_reference_bindings(
        document,
        removed,
        deferred_interface_references,
        defer_unknown=defer_unknown,
        strict_input_validation=strict_input_validation,
    )
    gateway_roles = validate_gateway_role(
        document,
        None,
        strict_input_validation=strict_input_validation,
    )
    result.errors.extend(gateway_roles.errors)
    result.warnings.extend(gateway_roles.warnings)
    units = validate_units(
        document,
        None,
        strict_input_validation=strict_input_validation,
    )
    result.errors.extend(units.errors)
    result.warnings.extend(units.warnings)
    return result


def validate_reference_bindings(document, context):
    """按只读校验上下文检查删除接口引用。

    当前仍存在的目标名称会从删除集合中剔除，保证名称复用不被误报为悬空引用。
    """
    actual = {
        spec.name for spec in document.interfaces.analysis.interface_specs()
    }
    actual.update(
        interface_name(document, node) for node in interface_nodes(document)
    )
    return _validate_reference_bindings(
        document,
        set(context.removed_interfaces) - actual,
        context.deferred_interface_references,
        strict_input_validation=context.strict_input_validation,
    )
