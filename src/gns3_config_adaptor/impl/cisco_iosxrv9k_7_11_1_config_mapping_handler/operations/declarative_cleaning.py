"""对厂商配置树执行有序的声明式清洗。"""

from collections.abc import Sequence

from ...common.rules.cleaning import (
    CleaningAction,
    CleaningOperation,
    compile_cleaning_rules,
)


def apply_cleaning_rules(
    document, operations: Sequence[CleaningOperation]
) -> list[int]:
    """按删除和替换轮次执行批量清洗操作。

    两轮顺序在单次树遍历中模拟，最终通过 Editor 统一应用结构修改。
    """
    hits = [0] * len(operations)
    phases = {
        phase.action: phase for phase in compile_cleaning_rules(operations)
    }
    transaction = document.edit()
    for block in document.root.nodes:
        _clean_phases(
            transaction,
            block,
            (),
            block.interface_name,
            phases,
            hits,
        )
    transaction.apply()
    return hits


def _clean_phases(
    transaction, node, ancestors, interface, phases, hits
) -> None:
    """在单次遍历中模拟先删除、后替换的两个清洗轮次。

    删除节点后不访问其子树；替换后的 header 立即作为子节点的祖先路径。
    """
    if node.is_formatting:
        return
    header = node.header
    if _apply_clean_phase(
        transaction,
        node,
        ancestors,
        interface,
        phases.get(CleaningAction.DELETE),
        hits,
        header,
    ):
        return
    replacement = phases.get(CleaningAction.REPLACE)
    if replacement is not None:
        header = _replace_cleaning_header(
            transaction,
            node,
            ancestors,
            interface,
            replacement,
            hits,
            header,
        )
    for child in node.nodes:
        _clean_phases(
            transaction,
            child,
            (*ancestors, header),
            interface,
            phases,
            hits,
        )


def _apply_clean_phase(
    transaction, node, ancestors, interface, phase, hits, header
) -> bool:
    """应用删除轮次的首条命中并返回是否删除当前子树。

    规则顺序和首条命中行为属于现有清洗契约，结构编辑不能改变这些业务约束。
    """
    if phase is None:
        return False
    for operation in phase.matching_operations(ancestors, header):
        if (
            operation.interface_names is not None
            and interface not in operation.interface_names
        ):
            continue
        if not operation.pattern.search(header):
            continue
        hits[operation.index] += 1
        transaction.delete(node, "声明式清洗删除")
        return True
    return False


def _replace_cleaning_header(
    transaction, node, ancestors, interface, phase, hits, header
) -> str:
    """应用替换轮次首条命中并返回子节点应使用的新 header。

    规则顺序和首条命中行为属于现有清洗契约，结构编辑不能改变这些业务约束。
    """
    for operation in phase.matching_operations(ancestors, header):
        if (
            operation.interface_names is not None
            and interface not in operation.interface_names
        ):
            continue
        if not operation.pattern.search(header):
            continue
        hits[operation.index] += 1
        header = operation.pattern.sub(operation.value or "", header)
        transaction.replace_header(node, header, "声明式清洗替换")
        break
    return header
