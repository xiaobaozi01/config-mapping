"""跨重解析传播源码块来源和生成块元数据。"""

from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate


def retain_metadata(original, candidate, operations):
    """按编辑坐标把源码块标记映射到候选 Context。

    重解析会创建全新 Context，必须复制来源语义并标记新插入容器。
    """
    positions, deltas, ranges, inserted = _projection(operations)
    _retain_existing(original, candidate, ranges, positions, deltas)
    _mark_generated(candidate, inserted)


def _projection(operations):
    """把编辑映射为偏移增量、替换区间和候选插入区间。

    同一套投影同时用于旧节点位置换算和新节点来源识别，避免两套坐标漂移。
    """
    events, ranges, inserted = {}, [], []
    delta = 0
    for order, (start, stop, text, _, context, _) in enumerate(operations):
        events[stop] = events.get(stop, 0) + len(text) - (stop - start)
        if start <= stop:
            ranges.append((start, stop, order, context))
            delta += len(text) - (stop - start)
        else:
            inserted.append((start + delta, start + delta + len(text)))
            delta += len(text)
    positions = sorted(events)
    return (
        positions,
        list(accumulate(events[position] for position in positions)),
        sorted(ranges),
        inserted,
    )


def _retain_existing(original, candidate, ranges, positions, deltas):
    """把未编辑源码块的来源标记复制到新修订。

    只有未被替换的块可以继承来源标记，被修改区域的语义由操作上下文决定。
    """
    starts = [item[0] for item in ranges]
    by_position = {
        context.start.start: context
        for context in candidate.root.walk(include_formatting=True)
    }
    for context in original.root.walk(include_formatting=True):
        if not context.is_block:
            continue
        offset = context.start.start
        selected = bisect_right(starts, offset) - 1
        if selected >= 0:
            begin, end, _, target = ranges[selected]
            if begin <= offset < end and target is context:
                continue
        event = bisect_right(positions, offset) - 1
        updated = by_position.get(offset + (deltas[event] if event >= 0 else 0))
        if updated is not None:
            updated._declared_in_source = context.declared_in_source


def _mark_generated(candidate, inserted):
    """把插入文本内新建的空块标记为生成内容。

    生成块不能触发源码空块输出策略，否则内部临时容器会出现在最终配置中。
    """
    for context in candidate.root.walk(include_formatting=True):
        if not context.is_block:
            continue
        if any(begin <= context.start.start < end for begin, end in inserted):
            context._declared_in_source = False
