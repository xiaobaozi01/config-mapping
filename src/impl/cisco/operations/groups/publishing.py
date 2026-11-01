"""Cisco Group 工作语句树与正式 AST 的发布边界。"""

from __future__ import annotations

import copy
from collections.abc import Iterable

from impl.common.domain.outcomes import GroupExpansionOutcome

from ...syntax.rewrite_helpers import append_top_level
from .models import _GroupStatement


def from_context(context, origin="explicit"):
    """把 Cisco 解析上下文复制成 group 工作语句。

    保持 header、块类型、注释和来源，使后续合并可以操作独立副本而不污染正式 AST。
    """
    return _GroupStatement(
        context.header,
        [from_context(child, origin) for child in context.nodes],
        context.is_block,
        origin=origin,
        comment=context.comment,
    )


def clone_statements(
    nodes: Iterable[_GroupStatement],
    *,
    include_formatting: bool = False,
    include_comments: bool = False,
    origin: str | None = None,
) -> list[_GroupStatement]:
    """深拷贝节点列表，并可过滤格式节点或统一重设来源。

    文档保真渲染需要格式行，但 group 语义合并只需要命令节点；集中克隆可以
    在两个场景间安全转换，同时确保对工作树的修改不会污染持久化文档 AST。
    """
    result: list[_GroupStatement] = []
    for node in nodes:
        if (
            node.is_formatting
            and not include_formatting
            and not (include_comments and node.comment)
        ):
            continue
        clone = copy.copy(node)
        if origin is not None:
            clone.origin = origin
        clone.statements = clone_statements(
            node.statements,
            include_formatting=include_formatting,
            include_comments=include_comments,
            origin=origin,
        )
        result.append(clone)
    return result


def render_statements(
    nodes: Iterable[_GroupStatement], depth: int = 1
) -> list[str]:
    """把 group 工作语句渲染为带缩进的配置行。

    该渲染仅服务候选事务，保留格式和注释后再交给Editor 作为新片段解析。
    """
    lines = []
    for node in nodes:
        if node.is_formatting:
            lines.append(
                " " * depth + (node.comment or node.header)
                if node.header or node.comment
                else ""
            )
        else:
            line = " " * depth + node.header
            if node.comment:
                line += " " + node.comment
            lines.append(line)
            lines.extend(render_statements(node.statements, depth + 1))
    return lines


def _signature(statement):
    """生成工作语句的结构签名。

    签名忽略无语义格式节点但保留注释，用于判断展开结果是否真的改变正式配置。
    """
    return (
        statement.header,
        statement.comment,
        tuple(
            _signature(child)
            for child in statement.statements
            if not child.is_formatting or child.comment
        ),
    )


def publish_expansion(document, statements):
    """把展开结果中真正变化的块发布到正式文档。

    复用原上下文删除或替换并追加新块，可保留未受影响显式配置的原始格式。
    """
    from collections import defaultdict, deque

    candidates = defaultdict(deque)
    for statement in statements:
        if not statement.is_formatting:
            candidates[statement.header].append(statement)
    transaction = document.edit()
    consumed = set()
    for context in document.root.nodes:
        if context.is_formatting:
            continue
        choices = candidates[context.header]
        if not choices:
            transaction.delete(context, "移除已展开的 group 定义或控制语句")
            continue
        statement = choices.popleft()
        consumed.add(id(statement))
        if _signature(from_context(context)) != _signature(statement):
            text = "\n".join(render_statements([statement], 0)) + "\n"
            transaction.replace_subtree(context, text, "物化 group 继承配置")
    for statement in statements:
        if not statement.is_formatting and id(statement) not in consumed:
            text = "\n".join(render_statements([statement], 0)) + "\n"
            append_top_level(
                transaction, document, text, "新增 group 继承配置块"
            )
    transaction.apply()


def _rollback_expansion(outcome: GroupExpansionOutcome) -> None:
    """记录整体回滚并清除未提交的冲突明细。

    失败时统一重置成功标记，防止调用方把部分分析结果当成已发布配置。
    """
    outcome.events.append("IOS XR group 展开未完整解析，已整体回滚并保留原配置")
    outcome.conflicts.clear()
    outcome.success = False
