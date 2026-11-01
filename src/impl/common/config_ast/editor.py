"""Validated structural edits and audit records for mutable ASTs."""

from contextlib import contextmanager
from dataclasses import dataclass

from .model import ConfigNode, bind_parents


class EditConflictError(ValueError):
    """Reject edits using detached nodes or invalid tree relationships.

    The batch must fail instead of applying an operation to an unrelated or invalid tree position.
    """


@dataclass(frozen=True)
class EditResult:
    """Expose the audit records of an applied edit batch.

    Only successfully applied operations appear in the changes tuple consumed by rule result mappers.
    """

    changes: tuple = ()

    @property
    def total(self):
        """Count completed edits.

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        return len(self.changes)


def format_fragment(text, indent=0, newline="\n"):
    """Normalize fragment indentation before parsing it into nodes.

    Only callers constructing fragments use this helper; full-document output is the responsibility of writers.
    """
    lines = text.splitlines()
    first = next((line.expandtabs(8) for line in lines if line.strip()), "")
    base = len(first) - len(first.lstrip())
    return newline.join(
        " " * indent + line.expandtabs(8)[base:] for line in lines
    ) + (newline if lines else "")


class ConfigEditor:
    """Collect validated node edits and apply them after traversal.

    Operations use node identities rather than source offsets, and input fragments are parsed before attachment.
    """

    def __init__(self, document):
        """Bind an edit batch to the current root identity.

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        self.document, self.root = document, document.root
        self.operations, self.finished = [], False
        self._after = {}

    @property
    def newline(self):
        """Return the canonical output line ending.

        Both vendor writers emit LF regardless of the line ending used by the input configuration.
        """
        return "\n"

    def _check(self, node):
        """Reject detached, foreign, and rolled-back references.

        A root replacement invalidates earlier editors, and detached nodes cannot be reused as edit targets.
        """
        if self.finished or self.root is not self.document.root:
            raise EditConflictError("Editor is no longer current")
        current = node
        while current.parent is not None:
            if current not in current.parent.children:
                raise EditConflictError("Detached node")
            current = current.parent
        if current is not self.root or not node.active:
            raise EditConflictError("Node does not belong to this document")

    def _record(self, action, node, payload, reason, details):
        """Validate ownership and audit fields before recording an operation.

        Reserved audit fields belong to the framework; callers can add detail fields without replacing those values.
        """
        self._check(node)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Every edit requires a reason")
        if set(details) & {
            "rule_id",
            "action",
            "command",
            "value",
            "reason",
            "path",
        }:
            raise ValueError("Cannot override audit fields")
        if node is self.root and action != "append_child":
            raise EditConflictError("Cannot replace or remove the virtual root")
        self.operations.append((action, node, payload, reason, details))

    def _fragment(self, text):
        """Parse and detach new nodes before changing any document state.

        New nodes lose source coordinates because their positions do not refer to the original input document.
        """
        nodes = self.document.parse_fragment(text).children
        for node in nodes:
            node.parent = None
            for child in node.walk(include_self=True, include_formatting=True):
                child.location = None
                child.declared_in_source = False
        return nodes

    def delete(self, node, reason, **details):
        """Schedule removal of a complete subtree.

        Removing the parent also removes its descendants, so callers should avoid scheduling redundant child deletions.
        """
        self._record("delete", node, None, reason, details)

    def replace_header(self, node, header, reason, **details):
        """Validate a new command head without replacing its children.

        The existing node identity and child relationships survive a command-head replacement.
        """
        self._check(node)
        if "\n" in header or "\r" in header:
            raise ValueError("Command header cannot contain newlines")
        header = self.document.normalize_header(header)
        self.document.validate_header(node, header)
        if header != node.header:
            self._record("replace_header", node, header, reason, details)

    def replace_subtree(self, node, text, reason, **details):
        """Replace a subtree with a parsed configuration fragment.

        The parsed replacement may contain several sibling nodes, all attached at the original position.
        """
        self._check(node)
        if text != self.document.writer.subtree(node):
            self._record(
                "replace_subtree", node, self._fragment(text), reason, details
            )

    def insert_before(self, node, text, reason, **details):
        """Insert parsed siblings before a node.

        The target remains in place and every inserted node becomes a child of the same parent.
        """
        self._record(
            "insert_before", node, self._fragment(text), reason, details
        )

    def insert_after(self, node, text, reason, **details):
        """Insert parsed siblings after a node.

        Repeated insertions use declaration order rather than reversing the collected fragments.
        """
        self._record(
            "insert_after", node, self._fragment(text), reason, details
        )

    def append_child(self, node, text, reason, **details):
        """Append parsed children, promoting a leaf to a block.

        The target retains its identity; leaf nodes become blocks when children are added.
        """
        self._record(
            "append_child", node, self._fragment(text), reason, details
        )

    def move(self, node, parent, reason, **details):
        """Move a subtree, rejecting cycles before applying edits.

        The source node keeps its identity and metadata while its parent and containing path change.
        """
        self._check(parent)
        self._check_move(node, parent)
        self._record("move", node, parent, reason, details)

    def _check_move(self, node, parent):
        """Validate the current destination after earlier queued moves.

        Queued moves can change ancestry, so validation runs again immediately before moving a node.
        """
        ancestor = parent
        while ancestor is not None:
            if ancestor is node:
                raise EditConflictError("Move would create a cycle")
            ancestor = ancestor.parent

    def clone(self, node, parent, reason, **details):
        """Append an independent copy of a subtree.

        The copy owns independent child lists and stops at the subtree boundary rather than copying ancestors.
        """
        self._check(node)
        cloned = node.clone()
        for child in cloned.walk(include_self=True, include_formatting=True):
            child.location = None
        self._record("append_child", parent, [cloned], reason, details)
        return cloned

    def _execute(self, action, node, payload):
        """Apply one already validated structural operation.

        Parent links are updated with child lists; final document validation reconnects bulk changes and refreshes caches.
        """
        if action == "replace_header":
            node.header = payload
            return
        if action == "append_child":
            node.children.extend(payload)
            if not node.is_block:
                node.kind = "block"
            for child in payload:
                child.parent = node
            return
        parent = node.parent
        index = parent.children.index(node)
        if action == "move":
            self._check(payload)
            self._check_move(node, payload)
            parent.children.remove(node)
            payload.children.append(node)
            payload.kind = "block" if payload.kind != "root" else "root"
            node.parent = payload
            return
        if action in {"delete", "replace_subtree"}:
            parent.children.pop(index)
            node.parent = None
        elif action == "insert_after":
            anchor = self._after.get(id(node), node)
            index = parent.children.index(anchor) + 1
            if payload:
                self._after[id(node)] = payload[-1]
        for child in reversed(payload or []):
            parent.children.insert(index, child)
            child.parent = parent

    def _audit_value(self, payload):
        """Serialize inserted nodes for the existing audit report schema.

        Reports retain the inserted configuration text even though edits now operate on tree nodes.
        """
        if isinstance(payload, str):
            return payload
        if isinstance(payload, list):
            return "".join(self.document.writer.write(node) for node in payload)
        return ""

    def scope(self, *, independent=False):
        """Create a rollback boundary, reusing an active boundary for nested edits.

        Rule executors request independent boundaries; editor batches share them.
        """
        return EditScope(self.document, independent=independent)

    @contextmanager
    def bulk(self):
        """Manage a vendor algorithm that mutates several AST nodes directly.

        The editor validates parent ownership and rolls back failed bulk changes.
        """
        self._check(self.root)
        with self.scope():
            yield self.document.root

    def replace_root(self, root):
        """Install an independently constructed result tree through the editor.

        Group expansion validates a detached candidate before publishing its nodes.
        """
        self._check(self.root)
        if root is self.root or root.parent is not None or root.kind != "root":
            raise EditConflictError("Replacement root must be detached")
        bind_parents(root)
        with self.scope():
            self.document.root = root
        self.finished = True

    def apply(self):
        """Apply collected edits within the current rollback boundary.

        Standalone batches own a backup; managed rules supply their existing backup.
        """
        self._check(self.root)
        if not self.operations:
            self.finished = True
            return EditResult()
        with self.scope():
            changes = self._apply_operations()
        self.finished = True
        return EditResult(tuple(changes))

    def _apply_operations(self):
        """Apply each operation and collect its original semantic location.

        Audit records are returned only after the complete batch succeeds.
        """
        changes = []
        for action, node, payload, reason, details in self.operations:
            self._check(node)
            changes.append(
                dict(
                    details,
                    action="removed"
                    if action == "delete"
                    else "replaced"
                    if action.startswith("replace")
                    else "added",
                    command=node.header,
                    value=self._audit_value(payload),
                    reason=reason,
                    path=node.path,
                )
            )
            self._execute(action, node, payload)
        return changes

    def preview(self):
        """Render edited copies without changing the live tree.

        The live tree and its caches remain untouched, allowing repeated previews of the same edit batch.
        """
        from copy import deepcopy

        cloned = deepcopy(self.document)
        pairs = zip(
            self.root.walk(include_self=True, include_formatting=True),
            cloned.root.walk(include_self=True, include_formatting=True),
            strict=True,
        )
        mapping = {id(old): new for old, new in pairs}
        editor = ConfigEditor(cloned)
        for action, node, payload, reason, details in self.operations:
            if isinstance(payload, list):
                payload = [child.clone() for child in payload]
            elif isinstance(payload, ConfigNode):
                payload = mapping[id(payload)]
            editor.operations.append(
                (action, mapping[id(node)], payload, reason, details)
            )
        editor.apply()
        return cloned.source


class EditScope:
    """Own one rollback copy for a rule or standalone structural edit.

    Nested editor batches share the active scope; explicit rule boundaries nest independently.
    """

    def __init__(self, document, *, independent=False):
        """Record the requested boundary without copying the tree yet.

        Entering the context decides whether an enclosing scope can be reused.
        """
        self.document, self.independent = document, independent
        self.failed = self.rolled_back = False

    def __enter__(self):
        """Acquire a backup only when this scope owns the rollback boundary.

        Returning the owner lets nested algorithms request rollback of their shared work.
        """
        self.previous = getattr(self.document, "_edit_scope", None)
        self.owner = (
            self if self.independent or self.previous is None else self.previous
        )
        if self.owner is self:
            self.backup = self.document.root.clone()
            self.revision = self.document.revision
            self.document._edit_scope = self
        elif self.owner.failed or self.owner.rolled_back:
            raise EditConflictError("The enclosing edit scope has failed")
        return self.owner

    def rollback(self):
        """Restore the boundary's original tree and invalidate old references.

        A business validation failure can request rollback without raising an exception.
        """
        if not self.rolled_back:
            self.document.restore_tree(self.backup, self.revision)
            self.rolled_back = True

    def synchronize(self):
        """Validate structure before publishing cache and revision changes.

        Writers and subsequent rules only observe trees with consistent parent links.
        """
        bind_parents(self.document.root)
        self.document.tree_changed()

    def __exit__(self, exc_type, exc, traceback):
        """Restore on error and release only scopes owned by this context.

        A swallowed nested failure still aborts its enclosing batch rather than publishing partial edits.
        """
        try:
            if exc_type is not None:
                self.owner.failed = True
                self.owner.rollback()
            elif self.owner.failed:
                raise EditConflictError("The enclosing edit scope has failed")
            elif not self.owner.rolled_back:
                try:
                    self.owner.synchronize()
                except Exception:
                    self.owner.failed = True
                    self.owner.rollback()
                    raise
        finally:
            if self.owner is self:
                self.document._edit_scope = self.previous
            self.owner = None
        return False
