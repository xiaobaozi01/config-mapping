"""跨文本提交保留块语义；只标记生成的 Context，不维护额外配置树。"""

from bisect import bisect_right
from itertools import accumulate


def retain_block_modes(original, candidate, operations, header_operations):
    events, ranges = {}, []
    for order, (start, stop, text, _, context, _) in enumerate(operations):
        begin = original.tokens.tokens[start].start
        end = original.tokens.tokens[stop].stop + 1 if start <= stop else begin
        events[end] = events.get(end, 0) + len(text) - (end - begin)
        if start <= stop:
            ranges.append((begin, end, order, context))
    positions = sorted(events)
    deltas = list(accumulate(events[position] for position in positions))
    ranges.sort(key=lambda item: item[0])
    starts = [item[0] for item in ranges]
    contexts = {context.start.start: context for context in candidate.root.walk(include_formatting=True)}
    for context in original.root.walk(include_formatting=True):
        if not context.is_block:
            continue
        offset = context.start.start
        selected = bisect_right(starts, offset) - 1
        if selected >= 0:
            begin, end, order, target = ranges[selected]
            if begin <= offset < end and not (order in header_operations and target is context):
                continue
        event = bisect_right(positions, offset) - 1
        updated = contexts.get(offset + (deltas[event] if event >= 0 else 0))
        if updated is not None:
            updated._was_block = True
