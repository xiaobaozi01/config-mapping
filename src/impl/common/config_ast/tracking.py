"""Incremental AST ownership, mutation epochs and reversible edit journals.

Nodes keep weak references to this object. Journals are retained only for active
rollback boundaries or until the next explicit tree_changed notification.
"""

from collections import Counter
from contextlib import contextmanager
from operator import index as integer_index
from weakref import ref

_MISSING = object()


class TrackedChildren(list):
    """Intercept ordinary child-list mutations.

    List reads retain their usual behavior; writes describe reversible splices before exposing the new contents.
    """

    __slots__ = ("_owner",)

    def __init__(self, owner, values=()):
        """Bind a child sequence to its owning node.

        A weak owner reference avoids adding another ownership cycle to the AST.
        """
        super().__init__(values)
        self._owner = ref(owner)

    def _splice(self, start, stop, values):
        """Replace one contiguous range and record its inverse.

        Materializing incoming values first supports generators and self-extension without partially publishing a mutation.
        """
        values = list(values)
        old = self[start:stop]
        if old == values:
            return
        owner = self._owner()
        tracker = owner._tracker() if owner is not None else None
        if tracker is not None:
            tracker.splice(owner, self, start, old, values)
        list.__setitem__(self, slice(start, stop), values)

    def __setitem__(self, key, value):
        """Apply item, slice or extended-slice assignment.

        Index validation precedes journal updates so failed list operations leave tree state unchanged.
        """
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            if step == 1:
                self._splice(start, max(start, stop), value)
            else:
                rebuilt = list(self)
                rebuilt[key] = value
                self._splice(0, len(self), rebuilt)
        else:
            index = key.__index__()
            if index < 0:
                index += len(self)
            if not 0 <= index < len(self):
                raise IndexError("list assignment index out of range")
            self._splice(index, index + 1, [value])

    def __delitem__(self, key):
        """Remove an item or slice through a reversible splice.

        Extended slices are rebuilt locally to preserve Python list ordering and index semantics.
        """
        if isinstance(key, slice):
            rebuilt = list(self)
            del rebuilt[key]
            self._splice(0, len(self), rebuilt)
        else:
            index = key.__index__()
            if index < 0:
                index += len(self)
            if not 0 <= index < len(self):
                raise IndexError("list assignment index out of range")
            self._splice(index, index + 1, [])

    def append(self, value):
        """Append one child.

        The splice records only the inserted edge rather than copying the whole sibling sequence.
        """
        self._splice(len(self), len(self), [value])

    def extend(self, values):
        """Append an iterable of children.

        The iterable is materialized before updating ownership, including when the source is this list.
        """
        self._splice(len(self), len(self), values)

    def insert(self, index, value):
        """Insert a child using normal list index clamping.

        The normalized insertion offset is stored in the undo journal.
        """
        index = integer_index(index)
        index = (
            max(0, len(self) + index) if index < 0 else min(index, len(self))
        )
        self._splice(index, index, [value])

    def pop(self, index=-1):
        """Remove and return the child at an index.

        Reading first preserves the standard error behavior for empty lists and invalid indices.
        """
        value = self[index]
        del self[index]
        return value

    def remove(self, value):
        """Remove the first matching child.

        Lookup uses list semantics and the resulting removal participates in transaction rollback.
        """
        del self[self.index(value)]

    def clear(self):
        """Remove all children.

        The removed range remains in the journal only as long as an active rollback boundary needs it.
        """
        self._splice(0, len(self), [])

    def reverse(self):
        """Reverse the child order.

        The replacement is tracked because source ordering is part of configuration semantics.
        """
        self._splice(0, len(self), reversed(self))

    def sort(self, *, key=None, reverse=False):
        """Sort children with the supplied key and direction.

        Sorting a temporary list prevents a failing key function from partially changing the live tree.
        """
        self._splice(0, len(self), sorted(self, key=key, reverse=reverse))

    def __iadd__(self, values):
        """Extend this list in place.

        Returning the same container preserves aliases and supports augmented assignment to node.children.
        """
        self.extend(values)
        return self

    def __imul__(self, count):
        """Repeat or empty this list in place.

        Ownership validation rejects repeated shared children at the transaction synchronization boundary.
        """
        self._splice(0, len(self), list(self) * count)
        return self


def tracker_for(node):
    """Resolve the live tracker for a node.

    Weak references allow retained nodes to outlive their document without keeping its analysis caches alive.
    """
    weak = vars(node).get("_tracking")
    tracker = weak() if weak is not None else None
    return tracker if tracker is not None and node in tracker.edges else None


def cache_token(root):
    """Return the root identity and immediate mutation epoch.

    Untracked roots receive a fresh token so detached mutable trees cannot reuse stale analysis results.
    """
    tracker = root._tracker()
    return (root, tracker.epoch) if tracker is not None else object()


class TreeTracker:
    """Maintain incremental ownership and reversible mutations.

    The edge index belongs to one document; nodes reference it weakly and successful rules never need whole-tree snapshots.
    """

    def __init__(self, root):
        """Index and validate an installed configuration tree once.

        Subsequent synchronization visits changed relationships instead of rebuilding this index for each rule.
        """
        self.root = root
        self.epoch = 0
        self.journal = []
        self.sync_index = 0
        self.scopes = 0
        self.edges = set()
        self.parents = {}
        self.detached = set()
        self.write_error = None
        self.dirty = set()
        self.adopt(root)
        self.dirty.update(self.edges)
        self.repair()
        self.clear()

    def adopt(self, root):
        """Index only previously unseen subtrees.

        Cross-document sharing is rejected before publication; incoming edges are tracked separately from visited nodes to detect cycles.
        """
        self._adopt_many([root])

    def _adopt_many(self, roots):
        """Validate incoming subtrees before publishing any ownership changes.

        A failed direct list write must leave the index unchanged even outside
        an edit scope, including when a later child belongs to another document.
        """
        pending, prepared = list(roots), {}
        while pending:
            node = pending.pop()
            if node in self.edges or node in prepared:
                continue
            other = node._tracker()
            if other is not None and other is not self:
                raise ValueError("AST contains a foreign or shared child")
            prepared[node] = tuple(node.children)
            pending.extend(node.children)
        for node in prepared:
            object.__setattr__(node, "_tracking", ref(self))
            self.edges.add(node)
        for node, children in prepared.items():
            for child in children:
                self._incoming(child, node, 1)

    def _incoming(self, child, parent, delta):
        """Adjust one incoming edge using compact storage for a single parent.

        Multiple parents retain a count mapping until synchronization rejects
        sharing or a later operation resolves a temporary move.
        """
        link = self.parents.get(child)
        parents = (
            {link[0]: link[1]} if isinstance(link, tuple) else dict(link or {})
        )
        count = parents.get(parent, 0) + delta
        if count:
            parents[parent] = count
        else:
            parents.pop(parent, None)
        if len(parents) == 1:
            self.parents[child] = next(iter(parents.items()))
        elif parents:
            self.parents[child] = parents
        else:
            self.parents.pop(child, None)
        self.dirty.add(child)
        self.detached.add(child)

    @contextmanager
    def readonly(self, error, message):
        """Temporarily reject live writes with a caller-selected exception.

        Nested callback phases restore the previous restriction even when a callback raises.
        """
        previous = self.write_error
        self.write_error = (error, message)
        try:
            yield
        finally:
            self.write_error = previous

    def check_write(self):
        """Enforce the active rule callback restriction.

        Checking before publication prevents visitors and result mappers from committing through another Editor.
        """
        if self.write_error is not None:
            error, message = self.write_error
            raise error(message)

    def belongs(self, node):
        """Check whether indexed parent edges reach the current root.

        Walking ancestors avoids linear sibling searches and rejects detached, retired, cyclic and multiply owned nodes.
        """
        seen = set()
        while node is not self.root:
            if node in seen:
                return False
            seen.add(node)
            link = self.parents.get(node)
            if not isinstance(link, tuple):
                return False
            node, count = link
            if count != 1:
                return False
        return True

    def field(self, node, name, old, new):
        """Record a field mutation before it becomes visible.

        Child-container replacement updates the edge index while scalar writes only advance the immediate epoch.
        """
        self.check_write()
        if name == "children":
            self._change_edges(node, old, new)
        self.journal.append(("field", node, name, old, new))
        self.epoch += 1
        if name == "parent":
            self.dirty.add(node)

    def _change_edges(self, node, old, new):
        """Apply the edge delta between old and new child ranges.

        Multiplicity counts retain detection of duplicate children while unchanged edges require no subtree traversal.
        """
        old_counts, new_counts = Counter(old), Counter(new)
        removed, added = old_counts - new_counts, new_counts - old_counts
        self._adopt_many(added)
        for child, count in removed.items():
            self._incoming(child, node, -count)
        for child, count in added.items():
            self._incoming(child, node, count)

    def splice(self, node, children, index, old, new):
        """Record the inverse of a child-container splice.

        A retired list alias remains reversible but does not contribute edges or invalidate the current configuration.
        """
        active = node.children is children
        if active:
            self.check_write()
            self._change_edges(node, old, new)
            self.epoch += 1
        self.journal.append(("splice", children, index, old, new))

    def replace_root(self, root, *, restoring=False):
        """Install a root and journal its predecessor.

        Rollback can bypass callback restrictions internally; recording that swap preserves enclosing transaction checkpoints.
        """
        if not restoring:
            self.check_write()
        self.adopt(root)
        self.journal.append(("root", self, "root", self.root, root))
        self.detached.add(self.root)
        self.root = root
        self.dirty.add(root)
        self.epoch += 1

    def repair(self):
        """Validate changed incoming edges and reconnect affected nodes.

        Ancestor checks detect cycles while raw parent repair avoids recursively invoking the mutation hooks.
        """
        dirty, self.dirty = self.dirty, set()
        for node in dirty:
            link = self.parents.get(node)
            if isinstance(link, dict) or (link is not None and link[1] != 1):
                raise ValueError("AST contains a cycle or shared child")
            if node is self.root and link is not None:
                raise ValueError("AST contains a cycle or shared child")
            parent = link[0] if link is not None else None
            seen, ancestor = {node}, parent
            while ancestor is not None:
                if ancestor in seen:
                    raise ValueError("AST contains a cycle or shared child")
                seen.add(ancestor)
                incoming = self.parents.get(ancestor)
                ancestor = incoming[0] if isinstance(incoming, tuple) else None
            if node.parent is not parent:
                old = node.parent
                object.__setattr__(node, "parent", parent)
                self.journal.append(("field", node, "parent", old, parent))
                self.epoch += 1

    def changed_since(self, index):
        """Compare only values touched since a journal checkpoint.

        Reversing local list splices for comparison recognizes batches whose mutations cancel out without hashing the whole tree.
        """
        fields, lists = {}, {}
        for kind, target, key, old, new in self.journal[index:]:
            if kind == "splice":
                lists.setdefault(id(target), (target, []))[1].append(
                    (key, old, new)
                )
            else:
                fields.setdefault((target, key), old)
        if any(
            getattr(target, key, _MISSING) != old
            for (target, key), old in fields.items()
        ):
            return True
        for target, operations in lists.values():
            owner = target._owner()
            if owner is None or owner.children is not target:
                continue
            before = list(target)
            for key, old, new in reversed(operations):
                before[key : key + len(new)] = old
            if before != target:
                return True
        return False

    def synchronize(self):
        """Repair pending edges and report a net configuration change.

        The synchronization cursor prevents nested scope exits from repeating work already published by an inner batch.
        """
        self.repair()
        changed = self.changed_since(self.sync_index)
        self.sync_index = len(self.journal)
        if not self.scopes:
            self.clear()
        return changed

    def undo(self, index):
        """Restore journaled values in reverse order.

        Ownership is rebuilt from the restored tree on this failure path; inverse root swaps keep outer records usable.
        """
        for kind, target, key, old, new in reversed(self.journal[index:]):
            if kind == "splice":
                list.__setitem__(target, slice(key, key + len(new)), old)
            elif old is _MISSING:
                object.__delattr__(target, key)
            else:
                object.__setattr__(target, key, old)
        del self.journal[index:]
        self.sync_index = min(self.sync_index, index)

    def restore_checkpoint(self, index):
        """Undo a boundary and install an independently owned rollback clone.

        The clone builder already establishes unique children and correct parent
        links. A single indexing pass avoids validating that fresh clone twice;
        the journaled root swap keeps enclosing rollback records usable.
        """
        self.undo(index)
        original = self.root
        cloned = original.clone()
        edges, parents, pending = set(), {}, [cloned]
        tracking = ref(self)
        while pending:
            node = pending.pop()
            edges.add(node)
            object.__setattr__(node, "_tracking", tracking)
            for child in node.children:
                parents[child] = (node, 1)
                pending.append(child)
        self.journal.append(("root", self, "root", original, cloned))
        self.root = cloned
        self.edges, self.parents = edges, parents
        self.dirty.clear()
        self.detached.clear()
        self.epoch += 1
        self.sync_index = len(self.journal)

    def clear(self):
        # Remove only disconnected components affected by this batch. Keeping
        # every retired subtree would turn the index into an unbounded cache.
        """Release committed journal entries and disconnected index components.

        Only affected components are visited so repeated successful rules cannot accumulate retired trees or scan unchanged nodes.
        """
        pending = [
            node
            for node in self.detached
            if node in self.edges and not self.belongs(node)
        ]
        seen = set()
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            if not self.belongs(node):
                self.edges.discard(node)
                self.parents.pop(node, None)
                pending.extend(node.children)
                if node._tracker() is self:
                    object.__setattr__(node, "_tracking", None)
        self.detached.clear()
        self.journal.clear()
        self.sync_index = 0
