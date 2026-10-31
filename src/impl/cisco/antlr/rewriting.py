"""薄事务封装：ANTLR TokenStreamRewriter 负责重写，成功重解析后才发布。"""

import re
from dataclasses import dataclass
from antlr4.TokenStreamRewriter import TokenStreamRewriter
from .parsing import CiscoSnapshot
from .generated.CiscoLexer import CiscoLexer
from .metadata import retain_block_modes


class RewriteConflictError(ValueError):
    pass


@dataclass(frozen=True)
class RewriteResult:
    changes: tuple[dict[str, object], ...] = ()

    @property
    def total(self):
        return len(self.changes)


def format_fragment(text: str, indent: int, newline: str = "\n") -> str:
    """仅格式化新增或移动片段，未编辑源码不经过此函数。"""
    lines = text.splitlines()
    if not lines:
        return ""
    first = next((line.expandtabs(8) for line in lines if line.strip()), "")
    base = len(first) - len(first.lstrip())
    rendered = [" " * indent + line.expandtabs(8)[base:] if line.strip() else "" for line in lines]
    return newline.join(rendered) + newline


class RewriteTransaction:
    def __init__(self, document):
        self._document = document
        self.snapshot = document.snapshot
        self._operations = []
        self._finished = False
        self._child_insertions = set()
        self._header_operations = set()

    @property
    def newline(self):
        return "\r\n" if "\r\n" in self.snapshot.text else "\n"

    def _check(self, context):
        if self._finished or self._document.snapshot is not self.snapshot:
            raise RewriteConflictError("事务已提交或文档已变化，请重新查询 Context")
        if context.snapshot is not self.snapshot:
            raise RewriteConflictError("Context 不属于当前文档修订")

    def _add(self, start, stop, text, reason, context, details):
        self._check(context)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("每项修改必须提供非空原因")
        if set(details) & {"rule_id", "action", "command", "value", "reason", "path"}:
            raise ValueError("不能覆盖审计字段")
        original = self.snapshot.tokens.getText(start, stop) if stop >= start else ""
        if original == text:
            return
        for old_start, old_stop, _, _, old_context, _ in self._operations:
            if start <= stop and old_start <= old_stop and max(start, old_start) <= min(stop, old_stop):
                raise RewriteConflictError("重叠修改：不能同时修改同一区间或祖先与后代")
            if start > stop and old_start <= old_stop and (old_start < start <= old_stop or (start == old_start and old_context is context)):
                raise RewriteConflictError("插入位置位于被删除或替换的区间内")
            if old_start > old_stop and start <= stop and (start < old_start <= stop or (start == old_start and old_context is context)):
                raise RewriteConflictError("修改区间包含已声明的插入位置")
        self._operations.append((start, stop, text, reason, context, dict(details)))

    def _editable(self, context):
        self._check(context)
        if context is self.snapshot.root:
            raise RewriteConflictError("请使用 replace_document 或 append_child 编辑虚拟根")

    def delete(self, context, reason, **details):
        self._editable(context)
        stop = context.stop.tokenIndex
        siblings = context.parentCtx.nodes
        position = siblings.index(context)
        if 0 < position < len(siblings) - 1:
            previous, following = siblings[position - 1], siblings[position + 1]
            if previous.header == following.header == "!" and previous.indent == following.indent:
                stop = following.stop.tokenIndex
        self._add(context.start.tokenIndex, stop, "", reason, context, details)

    def replace_header(self, context, header, reason, **details):
        self._editable(context)
        if "\n" in header or "\r" in header:
            raise ValueError("命令头不能包含换行，请使用 replace_subtree")
        raw = context.start.text
        first = raw.splitlines(keepends=True)[0]
        prefix = re.match(r"[ \t]*", first).group()
        ending = first[len(first.rstrip("\r\n")):]
        replacement = raw if header == context.header else prefix + header + ending + raw[len(first):]
        operation_index = len(self._operations)
        self._add(context.start.tokenIndex, context.start.tokenIndex, replacement, reason, context, details)
        if len(self._operations) > operation_index:
            self._header_operations.add(operation_index)

    def replace_subtree(self, context, text, reason, **details):
        self._editable(context)
        payload = text if text == context.text else format_fragment(text, context.indent, self.newline)
        self._add(context.start.tokenIndex, context.stop.tokenIndex, payload, reason, context, details)

    def _insert(self, index, text, reason, context, details):
        position = self.snapshot.tokens.tokens[index].start
        existing = any(start == index and stop < start for start, stop, *_ in self._operations)
        if text and not existing and position and self.snapshot.text[position - 1:position] not in {"\r", "\n"}:
            text = self.newline + text
        self._add(index, index - 1, text, reason, context, details)

    def insert_before(self, context, text, reason, **details):
        self._editable(context)
        self._insert(context.start.tokenIndex, format_fragment(text, context.indent, self.newline), reason, context, details)

    def insert_after(self, context, text, reason, **details):
        self._editable(context)
        self._insert(context.stop.tokenIndex + 1, format_fragment(text, context.indent, self.newline), reason, context, details)

    def append_child(self, context, text, reason, **details):
        self._check(context)
        if context is self.snapshot.root:
            index, indent = len(self.snapshot.tokens.tokens) - 1, 0
        else:
            index = context.stop.tokenIndex + (context.stop.type != CiscoLexer.DEDENT)
            if context.start.type == CiscoLexer.GROUP:
                index = context.stop.tokenIndex
                if self.snapshot.tokens.tokens[index - 1].type == CiscoLexer.DEDENT:
                    index -= 1
            indent = next((child.indent for child in context.nodes if not child.is_formatting), context.indent + 1)
        operation_index = len(self._operations)
        self._insert(index, format_fragment(text, indent, self.newline), reason, context, details)
        if context is not self.snapshot.root and len(self._operations) > operation_index:
            self._child_insertions.add(operation_index)

    def replace_document(self, text, reason):
        self._add(0, len(self.snapshot.tokens.tokens) - 2, text, reason, self.snapshot.root, {})

    def _candidate(self):
        rewriter = TokenStreamRewriter(self.snapshot.tokens)
        inserts = {}
        for order, (start, stop, text, _, _, _) in enumerate(self._operations):
            if start > stop:
                priority = 0 if order in self._child_insertions else 1
                inserts.setdefault(start, []).append((priority, order, text))
            else:
                rewriter.replaceRange(start, stop, text)
        for index, fragments in inserts.items():
            rewriter.insertBeforeIndex(index, "".join(text for _, _, text in sorted(fragments)))
        return rewriter.getDefaultText()

    def preview(self):
        self._check(self.snapshot.root)
        return self._candidate()

    def commit(self):
        self._check(self.snapshot.root)
        candidate = self._candidate()
        if candidate == self.snapshot.text:
            self._finished = True
            return RewriteResult()
        snapshot = CiscoSnapshot(candidate, self.snapshot.revision + 1)
        retain_block_modes(self.snapshot, snapshot, self._operations, self._header_operations)
        changes = tuple(dict(
            details, action="removed" if not text else "added" if start > stop else "replaced",
            command=context.header, value=text, reason=reason, path=context.path,
        ) for start, stop, text, reason, context, details in self._operations)
        self._document._publish(snapshot)
        self._finished = True
        return RewriteResult(changes)
