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
    """A normal list whose mutating operations record reversible splices."""

    def __init__(self, owner, values=()):
        super().__init__(values)
        self._owner = ref(owner)

    def _splice(self, start, stop, values):
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
        self._splice(len(self), len(self), [value])

    def extend(self, values):
        self._splice(len(self), len(self), values)

    def insert(self, index, value):
        index = integer_index(index)
        index = (
            max(0, len(self) + index) if index < 0 else min(index, len(self))
        )
        self._splice(index, index, [value])

    def pop(self, index=-1):
        value = self[index]
        del self[index]
        return value

    def remove(self, value):
        del self[self.index(value)]

    def clear(self):
        self._splice(0, len(self), [])

    def reverse(self):
        self._splice(0, len(self), reversed(self))

    def sort(self, *, key=None, reverse=False):
        self._splice(0, len(self), sorted(self, key=key, reverse=reverse))

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        self._splice(0, len(self), list(self) * count)
        return self


def tracker_for(node):
    """Return the live tracker without retaining a document from a node."""
    weak = vars(node).get("_tracking")
    return weak() if weak is not None else None


def cache_token(root):
    """O(1) token for tracked trees; detached roots have no cached analysis."""
    tracker = root._tracker()
    return (root, tracker.epoch) if tracker is not None else object()


class TreeTracker:
    """Own an edge index and inverse journal independently of a Document."""

    def __init__(self, root):
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
        """Index only previously unseen subtrees, rejecting foreign ownership."""
        pending = [root]
        while pending:
            node = pending.pop()
            if node in self.edges:
                continue
            other = node._tracker()
            if other is not None and other is not self:
                raise ValueError("AST contains a foreign or shared child")
            object.__setattr__(node, "_tracking", ref(self))
            self.edges.add(node)
            for child in node.children:
                if child not in self.edges:
                    pending.append(child)
                # adopt() must not mark an unvisited child as visited.
                self._incoming(child, node, 1)

    def _incoming(self, child, parent, delta):
        # Incoming entries are separate from the visited/adopted marker.
        parents = self.parents.setdefault(child, {})
        count = parents.get(parent, 0) + delta
        if count:
            parents[parent] = count
        else:
            parents.pop(parent, None)
        self.dirty.add(child)
        self.detached.add(child)

    @contextmanager
    def readonly(self, error, message):
        previous = self.write_error
        self.write_error = (error, message)
        try:
            yield
        finally:
            self.write_error = previous

    def check_write(self):
        if self.write_error is not None:
            error, message = self.write_error
            raise error(message)

    def belongs(self, node):
        """Check reachability through indexed edges, without scanning siblings."""
        seen = set()
        while node is not self.root:
            if node in seen:
                return False
            seen.add(node)
            parents = self.parents.get(node, {})
            if len(parents) != 1:
                return False
            node, count = next(iter(parents.items()))
            if count != 1:
                return False
        return True

    def field(self, node, name, old, new):
        """Record an attribute write before exposing it to readers."""
        self.check_write()
        if name == "children":
            self._change_edges(node, old, new)
        self.journal.append(("field", node, name, old, new))
        self.epoch += 1
        if name == "parent":
            self.dirty.add(node)

    def _change_edges(self, node, old, new):
        removed, added = Counter(old), Counter(new)
        for child, count in (removed - added).items():
            self._incoming(child, node, -count)
        for child, count in (added - removed).items():
            self.adopt(child)
            self._incoming(child, node, count)

    def splice(self, node, children, index, old, new):
        self.check_write()
        self._change_edges(node, old, new)
        self.journal.append(("splice", children, index, old, new))
        self.epoch += 1

    def replace_root(self, root, *, restoring=False):
        if not restoring:
            self.check_write()
        self.adopt(root)
        self.journal.append(("root", self, "root", self.root, root))
        self.detached.add(self.root)
        self.root = root
        self.dirty.add(root)
        self.epoch += 1

    def repair(self):
        """Validate changed incoming edges and reconnect only affected nodes."""
        dirty, self.dirty = self.dirty, set()
        for node in dirty:
            parents = self.parents.get(node, {})
            if len(parents) > 1 or any(
                count != 1 for count in parents.values()
            ):
                raise ValueError("AST contains a cycle or shared child")
            if node is self.root and parents:
                raise ValueError("AST contains a cycle or shared child")
            parent = next(iter(parents), None)
            seen, ancestor = {node}, parent
            while ancestor is not None:
                if ancestor in seen:
                    raise ValueError("AST contains a cycle or shared child")
                seen.add(ancestor)
                incoming = self.parents.get(ancestor, {})
                ancestor = next(iter(incoming), None)
            if node.parent is not parent:
                old = node.parent
                object.__setattr__(node, "parent", parent)
                self.journal.append(("field", node, "parent", old, parent))
                self.epoch += 1

    def changed_since(self, index):
        """Compare touched values only, including batches that cancel out."""
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
            before = list(target)
            for key, old, new in reversed(operations):
                before[key : key + len(new)] = old
            if before != target:
                return True
        return False

    def synchronize(self):
        self.repair()
        changed = self.changed_since(self.sync_index)
        self.sync_index = len(self.journal)
        if not self.scopes:
            self.clear()
        return changed

    def undo(self, index):
        """Restore raw values; the caller rebuilds ownership on the cold path."""
        for kind, target, key, old, new in reversed(self.journal[index:]):
            if kind == "splice":
                list.__setitem__(target, slice(key, key + len(new)), old)
            elif old is _MISSING:
                object.__delattr__(target, key)
            else:
                object.__setattr__(target, key, old)
        del self.journal[index:]
        self.sync_index = min(self.sync_index, index)

    def clear(self):
        # Remove only disconnected components affected by this batch. Keeping
        # every retired subtree would turn the index into an unbounded cache.
        pending = [node for node in self.detached if not self.belongs(node)]
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
