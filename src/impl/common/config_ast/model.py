"""Mutable configuration nodes and source diagnostics."""

from copy import copy
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceLocation:
    """Record the original position, independent of subsequent edits.

    Line and column refer to the input text; edits never reinterpret them as output coordinates.
    """

    line: int
    column: int = 1


class ConfigNode:
    """Store syntax and hierarchy without depending on a parser runtime.

    Children carry parent links, while kind records empty block semantics independently of child count.
    """

    def __init__(
        self,
        header="",
        children=None,
        *,
        kind=None,
        comment=None,
        location=None,
        active=True,
        origin="explicit",
        rank=(1_000_000, 0),
        declared_in_source=False,
    ):
        """Create a detached node; empty blocks have an explicit kind.

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        self.header = header
        self.kind = kind or ("block" if children is not None else "statement")
        self.children = list(children or [])
        self.parent = None
        self.comment = comment
        self.location = location
        self.active = active
        self.origin = origin
        self.rank = rank
        self.declared_in_source = declared_in_source
        for child in self.children:
            child.parent = self

    @property
    def nodes(self):
        """Return a stable view of direct children for traversal.

        A tuple keeps traversal stable while callers collect edits; logically removed children are excluded.
        """
        return tuple(child for child in self.children if child.active)

    @property
    def is_block(self):
        """Distinguish empty blocks from leaf statements.

        Root and group nodes also accept children, even when their child lists are empty.
        """
        return self.kind in {"root", "block", "group"}

    @property
    def is_formatting(self):
        """Identify comments and separators excluded from semantic queries.

        Formatting nodes remain available to writers but are omitted by default from semantic walks.
        """
        return self.kind in {"comment", "separator"} or not self.header

    @property
    def is_comment_only(self):
        """Identify a standalone comment.

        A trailing comment on a command does not make that command a comment-only node.
        """
        return not self.header and self.comment is not None

    @property
    def path(self):
        """Return ancestor command headers, excluding the virtual root.

        The current node is excluded so selectors can match the containing configuration scope.
        """
        result, parent = [], self.parent
        while parent is not None and parent.kind != "root":
            result.append(parent.header)
            parent = parent.parent
        return tuple(reversed(result))

    def accept(self, visitor):
        """Dispatch to a visitor without controlling its child traversal.

        Traversal policy belongs to the visitor, allowing callers to prune or aggregate a subtree.
        """
        return visitor.visit(self)

    def walk(self, *, include_self=False, include_formatting=False):
        """Iterate descendants in source order without recursive calls.

        The explicit stack avoids recursion limits and reverses pending children to preserve source order.
        """
        pending = [self] if include_self else list(reversed(self.nodes))
        while pending:
            node = pending.pop()
            if include_formatting or not node.is_formatting:
                yield node
            pending.extend(reversed(node.nodes))

    def clone(self):
        """Copy a subtree without following its parent into the document.

        The copy owns independent child lists and stops at the subtree boundary rather than copying ancestors.
        """
        root = copy(self)
        root.parent, root.children = None, []
        pending = [(self, root)]
        while pending:
            original, target = pending.pop()
            for child in original.children:
                cloned = copy(child)
                cloned.parent, cloned.children = target, []
                target.children.append(cloned)
                pending.append((child, cloned))
        return root

    def __deepcopy__(self, memo):
        """Provide an iterative tree copy for rule rollback.

        Parent relationships and derived caches must not retain references to the original document.
        """
        cloned = self.clone()
        memo[id(self)] = cloned
        return cloned


def bind_parents(root):
    """Validate and reconnect a tree after a bulk structural edit.

    Repeated node identities are rejected because a configuration node may have only one owner.
    """
    root.parent = None
    seen, pending = {id(root)}, [root]
    while pending:
        node = pending.pop()
        for child in node.children:
            if id(child) in seen:
                raise ValueError("AST contains a cycle or shared child")
            seen.add(id(child))
            child.parent = node
            pending.append(child)
    return root


def tree_signature(root):
    """Capture editable values and structure for read-only enforcement.

    The signature includes editable metadata as well as child counts, so read-only checks detect structural changes.
    """
    pending, result, seen = [(root, None)], [], set()
    while pending:
        node, expected_parent = pending.pop()
        if id(node) in seen:
            raise ValueError("AST contains a cycle or shared child")
        seen.add(id(node))
        result.append(
            (
                node.header,
                node.kind,
                node.comment,
                node.active,
                node.origin,
                node.rank,
                node.declared_in_source,
                node.location,
                len(node.children),
                node.parent is expected_parent,
            )
        )
        pending.extend((child, node) for child in reversed(node.children))
    return tuple(result)
