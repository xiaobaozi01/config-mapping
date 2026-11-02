"""Task-scoped, byte-budgeted LRU storage for optional optimization data.

Only conservative estimates are charged here; the existing AST and process RSS
remain separate metrics. No cache key or input value is exported in statistics.
"""

import sys
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass

DEFAULT_CACHE_BUDGET = 2 * 1024**3
LOCAL_CACHE_BUDGET = 16 * 1024**2
CACHE_MISS = object()
_CURRENT = ContextVar("conversion_optimization_cache", default=None)


def estimate_size(*values):
    """Estimate immutable cache payloads once when inserting an entry.

    AST nodes are never recursively traversed; interface indexes provide their
    own metadata-only estimates instead of charging the existing tree twice.
    """
    seen, pending, total = set(), list(values), 128
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        total += sys.getsizeof(value)
        if isinstance(value, (tuple, list)):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif is_dataclass(value) and not isinstance(value, type):
            pending.extend(
                getattr(value, field.name) for field in fields(value)
            )
    return total


def current_cache():
    """Return the optional active conversion cache.

    ContextVar isolation also restores the enclosing cache after nested calls.
    """
    return _CURRENT.get()


class BudgetCache:
    """Share a bounded LRU across independent optimization namespaces.

    Entry callbacks are weak methods so a task never retains a Document merely
    to invalidate an evicted interface index.
    """

    def __init__(self, budget_bytes):
        """Create an empty pool without reserving the advertised capacity.

        Zero disables caching; invalid budgets fail before conversion work starts.
        """
        if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int):
            raise TypeError("cache_budget_bytes must be an integer")
        if budget_bytes < 0:
            raise ValueError("cache_budget_bytes must be nonnegative")
        self.budget = budget_bytes
        self.entries = OrderedDict()
        self.bytes = self.peak = 0
        self.stats = {}
        self.closed = False

    def note(self, namespace, event, count=1):
        """Increment an aggregate counter without storing the request key.

        Namespaces are fixed code labels, not device or configuration identifiers.
        """
        counters = self.stats.setdefault(namespace, {})
        counters[event] = counters.get(event, 0) + count

    def get(self, key, namespace):
        """Retrieve and promote an entry in constant expected time.

        Misses use an explicit sentinel so empty and negative results are cacheable.
        """
        entry = self.entries.get(key)
        if entry is None:
            self.note(namespace, "misses")
            return CACHE_MISS
        self.entries.move_to_end(key)
        self.note(namespace, "hits")
        return entry[0]

    def peek(self, key):
        """Inspect a live index during invalidation without changing LRU order.

        Mutation notifications must not masquerade as query hits or trigger builds.
        """
        entry = self.entries.get(key)
        return entry[0] if entry is not None else CACHE_MISS

    def put(self, key, value, size, namespace, callback=None):
        """Admit a new value after evicting only the necessary cold entries.

        Oversized values and closed or disabled pools fall back to uncached work.
        """
        self.discard(key, "replacements")
        charge = max(0, size) + 128
        if self.closed or not self.budget or charge > self.budget:
            self.note(namespace, "bypasses")
            return False
        while self.bytes + charge > self.budget:
            self.discard(next(iter(self.entries)), "evictions")
        self.entries[key] = (value, charge, namespace, callback)
        self.bytes += charge
        self.peak = max(self.peak, self.bytes)
        self.note(namespace, "inserts")
        return True

    def resize(self, key, size):
        """Update the incremental charge of an existing mutable index.

        Growth can evict this index too; a caller's current result stays valid
        while its next lookup transparently rebuilds or uses the scan fallback.
        """
        entry = self.entries.get(key)
        if entry is None:
            return
        value, charge, namespace, callback = entry
        updated = max(0, size) + 128
        self.bytes += updated - charge
        self.entries[key] = (value, updated, namespace, callback)
        while self.bytes > self.budget:
            self.discard(next(iter(self.entries)), "evictions")
        self.peak = max(self.peak, self.bytes)

    def discard(self, key, reason="invalidations"):
        """Release one value and notify its optional weak owner.

        Removing entries before callbacks makes recursive invalidation harmless.
        """
        entry = self.entries.pop(key, None)
        if entry is None:
            return
        _, charge, namespace, callback = entry
        self.bytes -= charge
        self.note(namespace, reason)
        method = callback() if callback is not None else None
        if method is not None:
            method()

    def close(self):
        """Release all payloads after success, exception or cancellation.

        Aggregate counters survive for the final performance report only.
        """
        if not self.closed:
            self.closed = True
            while self.entries:
                self.discard(next(iter(self.entries)), "releases")

    def snapshot(self):
        """Copy budget and counters without exposing cached inputs.

        Estimated peak is bounded by the pool limit and is not a process RSS cap.
        """
        return {
            "budget_bytes": self.budget,
            "estimated_bytes": self.bytes,
            "estimated_peak_bytes": self.peak,
            "entries": len(self.entries),
            "closed": self.closed,
            "namespaces": {
                name: dict(counts) for name, counts in self.stats.items()
            },
        }


@contextmanager
def optimization_scope(cache_budget_bytes=DEFAULT_CACHE_BUDGET):
    """Own one pool for a conversion and restore the previous task on exit.

    No parallel execution is introduced; finally cleanup includes BaseException.
    """
    cache = BudgetCache(cache_budget_bytes)
    token = _CURRENT.set(cache)
    try:
        yield cache
    finally:
        cache.close()
        _CURRENT.reset(token)


def memoized(namespace, key, collect, *, local=None):
    """Compute a pure immutable result with the active or instance-local pool.

    Failed computations are never inserted and mutable AST objects are excluded.
    """
    cache = current_cache() or local
    if cache is None or cache.closed or not cache.budget:
        return collect()
    full_key = (namespace, key)
    value = cache.get(full_key, namespace)
    if value is CACHE_MISS:
        value = collect()
        cache.put(full_key, value, estimate_size(full_key, value), namespace)
    return value
