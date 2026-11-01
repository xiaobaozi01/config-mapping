"""对厂商配置树执行有序的声明式清洗。"""

from collections.abc import Sequence

from impl.common.rules.cleaning import (
    CleaningAction,
    CleaningOperation,
    compile_cleaning_rules,
)

from ..syntax.ast import JuniperNode


def apply_cleaning_rules(
    document, operations: Sequence[CleaningOperation]
) -> list[int]:
    """按删除、替换顺序执行批量清洗并返回原输入顺序的命中数。

    每轮从最新树快照开始且每个节点只执行首条规则，避免同一轮重复改写和顺序歧义。
    """
    phases = compile_cleaning_rules(operations)
    hits = [0] * len(operations)
    parser = document._parser
    with document.edit().bulk() as working_root:
        for phase in phases:
            for block in working_root.children or []:
                _walk_cleaning_phase(
                    block,
                    (),
                    None,
                    phase,
                    hits,
                    parser,
                )
    return hits


def _walk_cleaning_phase(
    node: JuniperNode,
    ancestors: tuple[str, ...],
    interface: str | None,
    phase,
    hits: list[int],
    parser,
) -> None:
    """在单个清洗阶段中递归遍历节点并执行匹配操作。

    子接口上下文从 interfaces 层向下传递，使规则可以按完整接口名限制作用范围。
    """
    if not parser.is_effective(node):
        return
    header = parser.base_header(node.header).rstrip(";").strip()
    if ancestors == ("interfaces",):
        interface = header
    operations = phase.matching_operations(ancestors, header)
    if _apply_cleaning_phase(
        node,
        phase,
        operations,
        hits,
        header,
        interface,
    ):
        return
    header = parser.base_header(node.header).rstrip(";").strip()
    for child in node.children or []:
        _walk_cleaning_phase(
            child,
            (*ancestors, header),
            interface,
            phase,
            hits,
            parser,
        )


def _apply_cleaning_phase(
    node: JuniperNode,
    phase,
    operations,
    hits: list[int],
    header: str,
    interface: str | None,
) -> bool:
    """执行节点上的首条匹配清洗操作。

    删除后立即停止子树遍历，替换则继续使用更新后的 header 定位子节点。
    """
    for operation in operations:
        if (
            operation.interface_names is not None
            and interface not in operation.interface_names
        ):
            continue
        if not operation.pattern.search(header):
            continue
        hits[operation.index] += 1
        if phase.action == CleaningAction.DELETE:
            node.active = False
            return True
        node.header = operation.pattern.sub(operation.value or "", header) + (
            "" if node.is_block else ";"
        )
        return False
    return False
