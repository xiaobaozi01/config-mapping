"""Incremental interface metadata indexes, independent of edit journal retention.

Document ownership is weak; budget eviction and task teardown release all indexed
nodes. Ordinary payload edits do not rebuild a document's interface catalog.
"""

from bisect import insort
from weakref import WeakMethod, ref

from ..domain.interface import interface_parent, interface_unit
from .optimization_cache import (
    CACHE_MISS,
    LOCAL_CACHE_BUDGET,
    BudgetCache,
    current_cache,
)


class InterfaceState:
    """Store names, order and logical membership for one interface container.

    Existing AST objects are referenced but never charged as newly allocated data.
    """

    def __init__(self, document):
        """Build one initial container snapshot using existing vendor semantics.

        Junos selects only the first effective interfaces block, matching top_block.
        """
        self.cisco = document.vendor == "cisco_iosxr"
        self.parser = document._parser
        self.root = document.root
        self.container = (
            self.root
            if self.cisco
            else next(
                (
                    node
                    for node in self.root.children
                    if node.effective
                    and node.is_block
                    and node.base_header == "interfaces"
                ),
                None,
            )
        )
        children = self.container.children if self.container is not None else ()
        self.order = {node: index for index, node in enumerate(children)}
        self.last_position = len(children)
        if len(self.order) != len(children):
            raise ValueError("Shared interface child")
        self.entries, self.by_name, self.by_parent, self.by_tree = (
            {},
            {},
            {},
            {},
        )
        self.ordered = None
        self.cost = 512 + 128 * len(self.order)
        for node in children:
            self.refresh(node)

    def describe(self, node):
        """Read the active canonical name and logical interface names of one node.

        Scalar configuration beneath units is intentionally irrelevant to this view.
        """
        parser = self.parser
        if self.cisco:
            name = parser.interface_name(node.header) if node.active else None
            return (name, (name,)) if name else None
        if not node.is_block or not parser.is_effective(node):
            return None
        name = parser.canonical_interface(
            parser.base_header(node.header).split()[0]
        )
        units = tuple(
            f"{name}.{parser.unit_number(child.header)}"
            for child in node.children
            if parser.is_effective(child) and parser.is_unit(child.header)
        )
        return name, units or (name,)

    def groups(self, entry):
        """Enumerate lookup buckets without losing nested dotted-name behavior.

        Tree prefixes match the original exact-name or source-dot-prefix scans.
        """
        name, _ = entry
        yield self.by_name, name
        yield self.by_parent, interface_parent(name) if self.cisco else name
        parts = name.split(".")
        for count in range(1, len(parts) + 1):
            yield self.by_tree, ".".join(parts[:count])

    @staticmethod
    def charge(entry):
        """Conservatively charge newly allocated names, buckets and references.

        This estimate is updated only for changed nodes, never by scanning on hits.
        """
        prefixes = 1 + entry[0].count(".")
        return (
            512
            + len(entry[0]) * 4
            + prefixes * (160 + 4 * len(entry[0]))
            + sum(96 + len(name) * 4 for name in entry[1])
        )

    def refresh(self, node):
        """Update only the buckets belonging to one changed interface.

        Original source positions keep duplicate-name and first-match semantics.
        """
        before = self.entries.get(node)
        after = self.describe(node)
        if before == after:
            return
        if before is not None:
            for mapping, key in self.groups(before):
                bucket = mapping[key]
                bucket.remove(node)
                if not bucket:
                    del mapping[key]
            self.cost -= self.charge(before)
            del self.entries[node]
        if after is not None:
            self.entries[node] = after
            for mapping, key in self.groups(after):
                bucket = mapping.setdefault(key, [])
                if not bucket or self.order[bucket[-1]] < self.order[node]:
                    bucket.append(node)
                else:
                    insort(bucket, node, key=self.order.__getitem__)
            self.cost += self.charge(after)
        self.ordered = None

    def remove(self, node):
        """Remove one detached candidate without renumbering unaffected positions.

        Relative source order is unchanged by deletion, including duplicate buckets.
        """
        before = self.entries.pop(node, None)
        if before is not None:
            for mapping, key in self.groups(before):
                bucket = mapping[key]
                bucket.remove(node)
                if not bucket:
                    del mapping[key]
            self.cost -= self.charge(before)
        if node in self.order:
            del self.order[node]
            self.cost -= 128
        self.ordered = None

    def append(self, node):
        """Index a new tail candidate using a monotonically increasing position.

        A still-shared node falls back to the scan rather than losing multiplicity.
        """
        if node in self.order:
            raise ValueError("Shared interface child")
        self.order[node] = self.last_position
        self.last_position += 1
        self.cost += 128
        self.refresh(node)

    def all_nodes(self):
        """Return a caller-owned ordered list of current interface nodes.

        Repeated reads reuse the ordered view until interface membership changes.
        """
        if self.ordered is None:
            self.ordered = sorted(self.entries, key=self.order.__getitem__)
        return list(self.ordered)

    def matching(self, name, tree=False):
        """Return independent exact-name or interface-tree results.

        Duplicate definitions remain separate nodes in source order.
        """
        return list((self.by_tree if tree else self.by_name).get(name, ()))

    def logical_names(self, parent):
        """Collect logical names only from the requested parent's entries.

        Sorting and deduplication retain the interface_specs-based query behavior.
        """
        names = {
            name
            for node in self.by_parent.get(parent, ())
            for name in self.entries[node][1]
        }
        return sorted(
            names, key=lambda value: (interface_unit(value) is not None, value)
        )


class InterfaceLookup:
    """Observe mutations without retaining a document or depending on its journal.

    Dirty metadata survives nested commits; root replacement drops all old nodes.
    """

    def __init__(self, document):
        """Attach a weak observer and a lazily allocated standalone cache.

        A weak document callback releases task-owned index payloads immediately.
        """
        weak = ref(self)

        def released(_):
            """Drop budget-owned data when the weak document owner dies.

            Capturing only a weak holder avoids a callback ownership cycle.
            """
            holder = weak()
            if holder is not None:
                holder.discard()

        self.document = ref(document, released)
        self.key = ("interfaces", object())
        self.pool = self.local = None
        self.dirty = set()
        self.removed, self.added = set(), []
        self.structural = True
        self.rejected = False
        self.budget_rejected = False
        document._tracker.listeners.add(self)

    def reset(self):
        """Forget queued nodes after eviction or a structural reset.

        No AST references remain in this holder after task teardown.
        """
        self.dirty.clear()
        self.removed.clear()
        self.added.clear()
        self.structural = True
        self.rejected = False
        self.budget_rejected = False

    def discard(self):
        """Remove this index from its current pool when its document retires.

        Budget callbacks are weak, so neither side retains the other owner.
        """
        if self.pool is not None:
            self.pool.discard(self.key)
        self.reset()

    def changed(self, tracker, kind, node, name, change=None):
        """Queue only catalog-relevant mutations before a write becomes visible.

        Parent repair and ordinary payload edits do not invalidate all interfaces.
        """
        if kind == "root":
            self.discard()
            return
        state = (
            self.pool.peek(self.key) if self.pool is not None else CACHE_MISS
        )
        if state is CACHE_MISS:
            if not self.budget_rejected or name == "children":
                self.rejected = False
            return
        if name not in {
            "header",
            "_header",
            "active",
            "kind",
            "comment",
            "children",
        }:
            return
        if name == "children":
            self.children_changed(state, kind, node, change)
            return
        link = tracker.parents.get(node)
        parent = link[0] if isinstance(link, tuple) else None
        if not state.cisco and parent is tracker.root:
            self.structural = True
        elif node in state.order:
            self.dirty.add(node)
        elif not state.cisco and parent in state.entries:
            self.dirty.add(parent)

    def children_changed(self, state, kind, node, change):
        """Coalesce removals and appends; rebuild only for genuine reordering.

        Large queues collapse to a single rebuild marker rather than retaining
        unlimited mutation history outside the cache budget.
        """
        if node is state.root or node is state.container:
            if not state.cisco and node is state.root:
                self.structural = True
                self.removed.clear()
                self.added.clear()
                return
            if kind != "splice" or self.structural:
                self.structural = True
                return
            index, old, new = change
            if not new:
                for child in old:
                    if child in self.added:
                        self.added.remove(child)
                    else:
                        self.removed.add(child)
            elif not old and index == len(node.children):
                self.added.extend(new)
            else:
                self.structural = True
            if self.structural or len(self.added) + len(self.removed) > 4096:
                self.structural = True
                self.removed.clear()
                self.added.clear()
        elif not state.cisco and node in state.order:
            self.dirty.add(node)

    def refresh(self, state, pool):
        """Apply queued deltas without scanning unrelated interface definitions.

        Notifications are consumed separately from rollback journal checkpoints.
        """
        dirty, removed, added = self.dirty, self.removed, self.added
        self.dirty, self.removed, self.added = set(), set(), []
        for node in removed:
            state.remove(node)
        for node in added:
            state.append(node)
        for node in dirty:
            if node in state.order:
                state.refresh(node)
        pool.note(
            "interfaces", "updates", len(dirty) + len(removed) + len(added)
        )
        pool.resize(self.key, state.cost)

    def selected_pool(self):
        """Choose the active task or a bounded document-local pool.

        Crossing task boundaries never reuses input-derived index payloads.
        """
        pool = current_cache()
        if pool is None:
            if self.local is None:
                self.local = BudgetCache(LOCAL_CACHE_BUDGET)
            pool = self.local
        if pool is not self.pool:
            self.discard()
            self.pool = pool
        return pool

    def get(self):
        """Build or incrementally refresh an optional current index.

        Uncacheable or malformed views fall back to the original scan at its
        original call boundary rather than changing error or result semantics.
        """
        pool = self.selected_pool()
        if pool.closed or not pool.budget or self.rejected:
            return None
        state = pool.get(self.key, "interfaces")
        try:
            if state is CACHE_MISS or self.structural:
                state = self.build(pool)
            elif self.dirty or self.removed or self.added:
                self.refresh(state, pool)
            return state
        except (IndexError, ValueError, AttributeError, TypeError):
            self.discard()
            self.rejected = True
            pool.note("interfaces", "fallbacks")
            return None

    def build(self, pool):
        """Build a container index and admit it only if the budget allows.

        Rebuilding after real reordering is distinct from scalar bucket updates.
        """
        document = self.document()
        if document is None:
            return None
        pool.discard(self.key)
        if 512 + 128 * len(document.root.children) > pool.budget:
            self.rejected = True
            self.budget_rejected = True
            pool.note("interfaces", "bypasses")
            return None
        state = InterfaceState(document)
        pool.note("interfaces", "builds")
        if not pool.put(
            self.key, state, state.cost, "interfaces", WeakMethod(self.reset)
        ):
            self.rejected = True
            self.budget_rejected = True
            return None
        self.structural = False
        self.dirty.clear()
        return state


def interface_lookup(document):
    """Return an optional indexed view while preserving a zero-budget scan path.

    Holders are created lazily and removed from deep-copied document state.
    """
    pool = current_cache()
    if pool is not None and (pool.closed or not pool.budget):
        return None
    holder = vars(document).get("_interface_lookup")
    if holder is None:
        holder = InterfaceLookup(document)
        document._interface_lookup = holder
    return holder.get()
