"""薄事务封装：ANTLR TokenStreamRewriter 负责重写，成功重解析后才发布。"""

import re
from dataclasses import dataclass

from antlr4.TokenStreamRewriter import TokenStreamRewriter

from .generated.CiscoLexer import CiscoLexer
from .metadata import retain_block_modes
from .parsing import CiscoSnapshot


class RewriteConflictError(ValueError):
    """表示文本修改与当前事务或文档修订发生冲突。

    冲突必须在写盘前暴露，避免旧上下文覆盖新配置或两个区间产生不可预测的交叠。
    """

    pass


@dataclass(frozen=True)
class RewriteResult:
    """保存一次成功提交产生的结构化变更记录。

    变更只在候选文本重新解析成功后返回，使调用方看到的记录与已发布快照一致。
    """

    changes: tuple[dict[str, object], ...] = ()

    @property
    def total(self):
        """返回本次事务记录的变更总数。

        简单合计供规则并发控制和报告使用，不依赖具体变更类型。
        """
        return len(self.changes)


def format_fragment(text: str, indent: int, newline: str = "\n") -> str:
    """把新增或移动片段调整为统一换行和目标缩进。

    只处理新文本可保留未编辑区域的原始空格、CRLF 与注释；统一基准缩进后，嵌套片段
    插入任意配置层时不会累积原位置偏移。
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


class RewriteTransaction:
    """收集一次文档修订上的文本操作，并在验证后原子发布。

    操作延迟到 commit 才应用，使重叠检测、候选解析和块语义传播都能在修改文档前完成。
    """

    def __init__(self, document):
        """绑定当前文档快照并初始化空操作集合。

        后续 Context 必须属于同一快照，才能在稳定 token 坐标上执行修改。
        """
        self._document = document
        self.snapshot = document.snapshot
        self._operations = []
        self._finished = False
        self._child_insertions = set()
        self._header_operations = set()

    @property
    def newline(self):
        """返回当前快照使用的换行符。

        改写片段沿用原文档换行风格，避免插入内容与周围配置混用 LF 和 CRLF。
        """
        return "\r\n" if "\r\n" in self.snapshot.text else "\n"

    def _check(self, context):
        """校验事务和 Context 仍属于当前文档修订。

        事务提交或快照发布后旧 Context 已失效，提前拒绝可防止写入错误位置。
        """
        if self._finished or self._document.snapshot is not self.snapshot:
            raise RewriteConflictError(
                "事务已提交或文档已变化，请重新查询 Context"
            )
        if context.snapshot is not self.snapshot:
            raise RewriteConflictError("Context 不属于当前文档修订")

    def _add(self, start, stop, text, reason, context, details):
        """记录一个文本区间操作并拒绝重叠或无效审计字段。

        所有修改先集中检测冲突，使失败事务不会产生部分操作；调用方也不能覆盖统一
        审计字段。
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
        original = (
            self.snapshot.tokens.getText(start, stop) if stop >= start else ""
        )
        if original == text:
            return
        for old_start, old_stop, _, _, old_context, _ in self._operations:
            if (
                start <= stop
                and old_start <= old_stop
                and max(start, old_start) <= min(stop, old_stop)
            ):
                raise RewriteConflictError(
                    "重叠修改：不能同时修改同一区间或祖先与后代"
                )
            if (
                start > stop
                and old_start <= old_stop
                and (
                    old_start < start <= old_stop
                    or (start == old_start and old_context is context)
                )
            ):
                raise RewriteConflictError("插入位置位于被删除或替换的区间内")
            if (
                old_start > old_stop
                and start <= stop
                and (
                    start < old_start <= stop
                    or (start == old_start and old_context is context)
                )
            ):
                raise RewriteConflictError("修改区间包含已声明的插入位置")
        self._operations.append(
            (start, stop, text, reason, context, dict(details))
        )

    def _editable(self, context):
        """校验 Context 允许直接编辑，并阻止替换虚拟根。

        虚拟根只用于整树发布，直接修改会绕过正常块边界和子节点管理。
        """
        self._check(context)
        if context is self.snapshot.root:
            raise RewriteConflictError("请使用 append_child 编辑虚拟根")

    def delete(self, context, reason, **details):
        """记录删除完整节点区间的操作。

        相邻分隔符在安全条件下合并删除，避免输出保留重复叹号分隔行。
        """
        self._editable(context)
        stop = context.stop.tokenIndex
        siblings = context.parentCtx.nodes
        position = siblings.index(context)
        if 0 < position < len(siblings) - 1:
            previous, following = siblings[position - 1], siblings[position + 1]
            if (
                previous.header == following.header == "!"
                and previous.indent == following.indent
            ):
                stop = following.stop.tokenIndex
        self._add(context.start.tokenIndex, stop, "", reason, context, details)

    def replace_header(self, context, header, reason, **details):
        """记录节点首行 header 的替换操作。

        保留原缩进和换行，并把后续子树原样拼接，使局部命令替换不改变节点层级。
        """
        self._editable(context)
        if "\n" in header or "\r" in header:
            raise ValueError("命令头不能包含换行，请使用 replace_subtree")
        raw = context.start.text
        first = raw.splitlines(keepends=True)[0]
        prefix = re.match(r"[ \t]*", first).group()
        ending = first[len(first.rstrip("\r\n")) :]
        replacement = (
            raw
            if header == context.header
            else prefix + header + ending + raw[len(first) :]
        )
        operation_index = len(self._operations)
        self._add(
            context.start.tokenIndex,
            context.start.tokenIndex,
            replacement,
            reason,
            context,
            details,
        )
        if len(self._operations) > operation_index:
            self._header_operations.add(operation_index)

    def replace_subtree(self, context, text, reason, **details):
        """记录完整子树文本的替换操作。

        新片段先按目标上下文缩进格式化，避免复制到不同层级后保留错误的视觉结构。
        """
        self._editable(context)
        payload = (
            text
            if text == context.text
            else format_fragment(text, context.indent, self.newline)
        )
        self._add(
            context.start.tokenIndex,
            context.stop.tokenIndex,
            payload,
            reason,
            context,
            details,
        )

    def _insert(self, index, text, reason, context, details):
        """记录 token 插入，并在位置不处于行首时补写换行。

        插入点由 token 坐标决定，自动换行可保证新配置独占一行而不破坏原命令边界。
        """
        position = self.snapshot.tokens.tokens[index].start
        existing = any(
            start == index and stop < start
            for start, stop, *_ in self._operations
        )
        if (
            text
            and not existing
            and position
            and self.snapshot.text[position - 1 : position] not in {"\r", "\n"}
        ):
            text = self.newline + text
        self._add(index, index - 1, text, reason, context, details)

    def insert_before(self, context, text, reason, **details):
        """在指定节点前插入格式化后的同级片段。

        使用目标节点的缩进保持同级关系，同时保持原节点位置不变。
        """
        self._editable(context)
        self._insert(
            context.start.tokenIndex,
            format_fragment(text, context.indent, self.newline),
            reason,
            context,
            details,
        )

    def insert_after(self, context, text, reason, **details):
        """在指定节点后插入格式化后的同级片段。

        插入位置取原节点结束 token 之后，避免新片段进入原节点的子块。
        """
        self._editable(context)
        self._insert(
            context.stop.tokenIndex + 1,
            format_fragment(text, context.indent, self.newline),
            reason,
            context,
            details,
        )

    def append_child(self, context, text, reason, **details):
        """向配置块末尾追加子片段，或向虚拟根追加顶层配置。

        插入点需要避让 DEDENT 和已有子插入，确保子配置位于正确大括号层级。
        """
        self._check(context)
        if context is self.snapshot.root:
            index, indent = len(self.snapshot.tokens.tokens) - 1, 0
        else:
            index = context.stop.tokenIndex + (
                context.stop.type != CiscoLexer.DEDENT
            )
            if context.start.type == CiscoLexer.GROUP:
                index = context.stop.tokenIndex
                if (
                    self.snapshot.tokens.tokens[index - 1].type
                    == CiscoLexer.DEDENT
                ):
                    index -= 1
            indent = next(
                (
                    child.indent
                    for child in context.nodes
                    if not child.is_formatting
                ),
                context.indent + 1,
            )
        operation_index = len(self._operations)
        self._insert(
            index,
            format_fragment(text, indent, self.newline),
            reason,
            context,
            details,
        )
        if (
            context is not self.snapshot.root
            and len(self._operations) > operation_index
        ):
            self._child_insertions.add(operation_index)

    def _candidate(self):
        """生成尚未解析和发布的候选配置文本。

        同一位置的插入按子节点优先和声明顺序合并，避免多次修改产生不确定顺序。
        """
        rewriter = TokenStreamRewriter(self.snapshot.tokens)
        inserts = {}
        for order, (start, stop, text, _, _, _) in enumerate(self._operations):
            if start > stop:
                priority = 0 if order in self._child_insertions else 1
                inserts.setdefault(start, []).append((priority, order, text))
            else:
                rewriter.replaceRange(start, stop, text)
        for index, fragments in inserts.items():
            rewriter.insertBeforeIndex(
                index, "".join(text for _, _, text in sorted(fragments))
            )
        return rewriter.getDefaultText()

    def preview(self):
        """返回候选配置文本，但不修改文档快照。

        该方法用于调用前检查，不能替代 commit 的语法验证和发布流程。
        """
        self._check(self.snapshot.root)
        return self._candidate()

    def commit(self):
        """重解析候选文本，成功后发布新快照并返回变更记录。

        只有完整解析成功才替换当前文档，使失败事务保持原快照和原接口分析状态。
        """
        self._check(self.snapshot.root)
        candidate = self._candidate()
        if candidate == self.snapshot.text:
            self._finished = True
            return RewriteResult()
        snapshot = CiscoSnapshot(candidate, self.snapshot.revision + 1)
        retain_block_modes(
            self.snapshot, snapshot, self._operations, self._header_operations
        )
        changes = tuple(
            dict(
                details,
                action="removed"
                if not text
                else "added"
                if start > stop
                else "replaced",
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
