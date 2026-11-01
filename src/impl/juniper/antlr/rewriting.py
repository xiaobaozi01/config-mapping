"""Junos 原文保留的文本重写事务。"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from impl.common.outcomes import SimulationChangeAction

from .metadata import retain_metadata
from .parsing import JunosSnapshot


class RewriteConflictError(ValueError):
    """表示事务或 Context 已失效，或编辑区间发生重叠。

    在写盘前拒绝冲突可防止旧节点覆盖新修订，并保持所有失败操作原子回滚。
    """


@dataclass(frozen=True)
class RewriteResult:
    """保存一次成功提交对应的实际文本变更。

    结果只来自发布后的操作集合，调用方可以据此报告真实修改而不是计划动作。
    """

    changes: tuple[dict[str, object], ...] = ()

    @property
    def total(self):
        """返回实际变更数量。

        统一计数口径便于规则执行器判断幂等性和汇总跨规则变更。
        """
        return len(self.changes)


def format_fragment(text: str, indent: int, newline: str = "\n") -> str:
    """把新增或移动片段格式化为目标缩进。

    只重排片段自身并统一换行，未编辑的源码区域仍由事务原样保留。
    """
    lines = text.splitlines()
    if not lines:
        return ""
    first = next((line.expandtabs(8) for line in lines if line.strip()), "")
    base = len(first) - len(first.lstrip())
    rendered = [
        " " * indent + line.expandtabs(8)[base:] if line.strip() else ""
        for line in lines
    ]
    return newline.join(rendered) + newline


class _EditIndex:
    """用有序区间检测事务内的重叠编辑。

    编辑必须先完成冲突检查再入队，保证候选文本不会组合相互覆盖的操作。
    """

    def __init__(self) -> None:
        """初始化空的编辑区间索引。

        分别保存有序起点和对应区间，使冲突检测无需扫描全部历史操作。
        """
        self._starts: list[int] = []
        self._ranges: list[tuple[int, int, object]] = []

    def check_and_add(self, start: int, stop: int, context: object) -> None:
        """校验字符区间不与已声明修改重叠。

        相邻的删除和替换允许首尾相接，但祖先与后代或同一上下文的重叠必须拒绝。
        """
        position = bisect_right(self._starts, start)
        if position:
            old_start, old_stop, old_context = self._ranges[position - 1]
            if old_stop > old_start and (
                old_stop > start
                or (old_start == start and old_context is context)
            ):
                raise RewriteConflictError(
                    "重叠修改：不能同时修改同一区间或祖先与后代"
                )
        if position < len(self._ranges):
            next_start, _, _ = self._ranges[position]
            if stop > next_start:
                raise RewriteConflictError(
                    "重叠修改：不能同时修改同一区间或祖先与后代"
                )
        self._starts.insert(position, start)
        self._ranges.insert(position, (start, stop, context))


class JunosRewriteTransaction:
    """收集 Junos 文本编辑，成功重解析后原子发布。

    所有操作绑定创建时的快照，提交失败不会暴露部分修改或使旧 Context 继续可用。
    """

    def __init__(self, document):
        """绑定当前文档快照并初始化操作集合。

        事务后续只使用固定字符坐标，避免文档修订变化时编辑落到错误位置。
        """
        self._document = document
        self.snapshot = document.snapshot
        self._operations = []
        self._edit_index = _EditIndex()
        self._finished = False
        self._child_insertions = set()

    @property
    def newline(self):
        """返回当前文档使用的换行风格。

        新增内容沿用原文件风格，可避免 Windows 配置在局部修改后混入 LF。
        """
        return "\r\n" if "\r\n" in self.snapshot.text else "\n"

    def _check(self, context):
        """校验事务和 Context 属于同一个当前修订。

        过期 Context 或重复提交必须立即失败，不能把旧坐标用于新源码。
        """
        if self._finished or self._document.snapshot is not self.snapshot:
            raise RewriteConflictError(
                "事务已提交或文档已变化，请重新查询 Context"
            )
        if context.snapshot is not self.snapshot:
            raise RewriteConflictError("Context 不属于当前文档修订")

    def _bounds(self, context):
        """返回包含独占行缩进和换行的节点删除范围。

        扩展纯空白边界能移除完整配置行，同时不会误删同一行后续的有效命令。
        """
        begin = context.start.start
        line_start = self.snapshot.text.rfind("\n", 0, begin) + 1
        if not self.snapshot.text[line_start:begin].strip():
            begin = line_start
        stop = context.stop.stop + 1
        line_end = self.snapshot.text.find("\n", stop)
        if line_end < 0:
            line_end = len(self.snapshot.text)
        if not self.snapshot.text[stop:line_end].strip():
            stop = min(line_end + 1, len(self.snapshot.text))
        return begin, stop

    def _indent(self, context):
        """返回新增或替换片段应使用的结构缩进。

        内联配置缺少可见行缩进，因此同时参考业务路径深度以得到稳定层级。
        """
        return max(context.indent, len(context.path) * 4)

    def _add(
        self, start: int, stop: int, text: str, reason: str, context, details
    ):
        """记录一个字符区间修改并拒绝空原因或审计字段覆盖。

        在入队前完成校验和冲突检测，使失败事务不会留下部分底层操作。
        """
        self._check(context)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("每项修改必须提供非空原因")
        if set(details) & {
            "rule_id",
            "action",
            "command",
            "value",
            "reason",
            "path",
        }:
            raise ValueError("不能覆盖审计字段")
        original = self.snapshot.text[start:stop]
        if original == text:
            return
        self._edit_index.check_and_add(start, stop, context)
        self._operations.append(
            (start, stop, text, reason, context, dict(details))
        )

    def _editable(self, context):
        """拒绝直接替换虚拟根。

        根节点没有单一源码范围，新增顶层内容必须通过 append_child 明确插入。
        """
        self._check(context)
        if context is self.snapshot.root:
            raise RewriteConflictError("请使用 append_child 编辑虚拟根")

    def delete(self, context, reason, **details):
        """删除完整语句或块子树。

        删除范围覆盖节点独占行的缩进和换行，以维持其余配置的合法结构。
        """
        self._editable(context)
        start, stop = self._bounds(context)
        self._add(start, stop, "", reason, context, details)

    def replace_header(self, context, header, reason, **details):
        """替换语句头的命令文本并保留块及原始格式。

        只修改 header token 区间，可让子树、注释和未触及的空白保持原字节。
        """
        self._editable(context)
        if "\n" in header or "\r" in header:
            raise ValueError("命令头不能包含换行，请使用 replace_subtree")
        command = context.command()
        replacement = header.rstrip()
        if not context.is_block and replacement.endswith(";"):
            replacement = replacement[:-1].rstrip()
        self._add(
            command.start.start,
            command.stop.stop + 1,
            replacement,
            reason,
            context,
            details,
        )

    def replace_subtree(self, context, text, reason, **details):
        """替换完整子树并按目标层级格式化新片段。

        新片段需要重新缩进以适配目标位置，原始片段则直接复用其完整源码范围。
        """
        self._editable(context)
        start, _ = self._bounds(context)
        payload = (
            text
            if text == context.text
            else format_fragment(text, self._indent(context), self.newline)
        )
        self._add(
            start,
            context.stop.stop + 1,
            payload.rstrip("\r\n"),
            reason,
            context,
            details,
        )

    def _insert(self, position, text, reason, context, details):
        """记录字符位置插入。

        插入不改写原区间，零长度坐标可在同一位置与其他插入按声明顺序合并。
        """
        self._add(position, position - 1, text, reason, context, details)

    def insert_before(self, context, text, reason, **details):
        """在目标节点前插入同级片段。

        使用目标节点的结构缩进可保持同级关系并避免破坏原有子树边界。
        """
        self._editable(context)
        start, _ = self._bounds(context)
        self._insert(
            start,
            format_fragment(text, self._indent(context), self.newline),
            reason,
            context,
            details,
        )

    def insert_after(self, context, text, reason, **details):
        """在目标节点后插入同级片段。

        插入点取完整节点之后，确保新内容不会落入被替换节点的子树。
        """
        self._editable(context)
        _, stop = self._bounds(context)
        self._insert(
            stop,
            format_fragment(text, self._indent(context), self.newline),
            reason,
            context,
            details,
        )

    def append_child(self, context, text, reason, **details):
        """向块末尾或虚拟根追加子配置。

        插入点位于右大括号之前并按现有子级缩进，避免新配置逃出目标块。
        """
        self._check(context)
        if context is self.snapshot.root:
            position, indent = len(self.snapshot.text), 0
        else:
            if not context.is_block:
                raise ValueError("只能向 Junos 配置块追加子节点")
            position = context.RBRACE().symbol.start
            indent = next(
                (
                    max(child.indent, self._indent(context) + 4)
                    for child in context.nodes
                    if not child.is_formatting
                ),
                self._indent(context) + 4,
            )
        prefix = ""
        if position and self.snapshot.text[position - 1] not in "\r\n":
            prefix = self.newline
        operation_index = len(self._operations)
        self._insert(
            position,
            prefix + format_fragment(text, indent, self.newline),
            reason,
            context,
            details,
        )
        if len(self._operations) > operation_index:
            self._child_insertions.add(operation_index)

    def _candidate(self):
        """生成不发布、不解析的候选文本。

        插入和替换按源码坐标一次性拼接，便于提交前统一执行语法校验。
        """
        inserts = {}
        replacements = []
        for order, (start, stop, text, _, _, _) in enumerate(self._operations):
            if start > stop:
                priority = 0 if order in self._child_insertions else 1
                inserts.setdefault(start, []).append((priority, order, text))
            else:
                replacements.append((start, stop, order, text))
        events = [
            (
                position,
                0,
                fragments[0][1],
                position,
                "".join(text for _, _, text in sorted(fragments)),
            )
            for position, fragments in inserts.items()
        ]
        events.extend(
            (start, 1, order, stop, text)
            for start, stop, order, text in replacements
        )
        parts = []
        cursor = 0
        for start, kind, _, stop, text in sorted(events):
            parts.append(self.snapshot.text[cursor:start])
            parts.append(text)
            cursor = stop if kind else start
        parts.append(self.snapshot.text[cursor:])
        return "".join(parts)

    def preview(self):
        """返回候选文本但不修改当前快照。

        预览用于诊断和空块渲染，发布仍必须经过 commit 的重新解析流程。
        """
        self._check(self.snapshot.root)
        return self._candidate()

    def commit(self):
        """重解析候选文本，成功后发布新快照。

        只有完整语法通过后才替换文档，失败时旧源码和旧 Context 保持不变。
        """
        self._check(self.snapshot.root)
        candidate = self._candidate()
        if candidate == self.snapshot.text:
            self._finished = True
            return RewriteResult()
        snapshot = JunosSnapshot(candidate, self.snapshot.revision + 1)
        retain_metadata(self.snapshot, snapshot, self._operations)
        changes = tuple(
            dict(
                details,
                action=SimulationChangeAction.REMOVED.value
                if not text
                else SimulationChangeAction.ADDED.value
                if start > stop
                else SimulationChangeAction.REPLACED.value,
                command=context.header,
                value=text,
                reason=reason,
                path=context.path,
            )
            for start, stop, text, reason, context, details in self._operations
        )
        self._document._publish(snapshot)
        self._finished = True
        return RewriteResult(changes)
