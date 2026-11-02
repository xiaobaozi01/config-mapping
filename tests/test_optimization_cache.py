"""Budget, LRU order, ownership and task teardown are correctness properties."""

import gc
import weakref

import pytest

from impl.common.support.optimization_cache import (
    CACHE_MISS,
    BudgetCache,
    current_cache,
    estimate_size,
    memoized,
    optimization_scope,
)


def test_lru_evicts_only_cold_entries_and_handles_negative_results():
    cache = BudgetCache(512)
    assert cache.put("a", (), 128, "test")
    assert cache.put("b", False, 128, "test")
    assert cache.get("a", "test") == ()
    assert cache.put("c", None, 128, "test")
    assert cache.get("b", "test") is CACHE_MISS
    assert cache.get("a", "test") == ()
    assert cache.get("c", "test") is None
    assert cache.snapshot()["estimated_peak_bytes"] <= 512
    assert cache.stats["test"]["evictions"] == 1


@pytest.mark.parametrize("budget", [-1, 1.2, True])
def test_invalid_budgets_rejected(budget):
    with pytest.raises((ValueError, TypeError)):
        BudgetCache(budget)


def test_disabled_oversize_resize_and_cleanup():
    cache = BudgetCache(256)
    assert not cache.put("big", object(), 1000, "test")
    assert cache.put("small", 1, 1, "test")
    cache.resize("small", 500)
    assert cache.peek("small") is CACHE_MISS
    cache.close()
    assert not cache.put("closed", 1, 1, "test")
    with optimization_scope(0) as disabled:
        assert memoized("test", "key", lambda: 42) == 42
        assert not disabled.entries


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_nested_scope_restores_outer_and_releases_payloads(exception):
    class Payload:
        pass

    with optimization_scope(1024) as outer:
        value = Payload()
        weak = weakref.ref(value)
        outer.put("key", value, 1, "test")
        del value
        with pytest.raises(exception):
            with optimization_scope(256) as inner:
                assert current_cache() is inner
                raise exception()
        assert inner.closed
        assert current_cache() is outer
        assert weak() is not None
    assert current_cache() is None
    assert outer.closed and not outer.entries
    assert weak() is None


def test_weak_eviction_callback_does_not_retain_owner():
    class Owner:

        def evicted(self):
            raise AssertionError("owner should have been released")

    owner = Owner()
    weak = weakref.ref(owner)
    cache = BudgetCache(1024)
    cache.put("key", "value", 1, "test", weakref.WeakMethod(owner.evicted))
    del owner
    gc.collect()
    assert weak() is None
    cache.close()


def test_memoization_is_isolated_and_does_not_cache_exceptions():
    calls = []

    def collect():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError()
        return "value"

    with optimization_scope(4096) as first:
        with pytest.raises(ValueError):
            memoized("test", "key", collect)
        assert memoized("test", "key", collect) == "value"
        assert memoized("test", "key", collect) == "value"
        assert len(calls) == 2
    with optimization_scope(4096) as second:
        assert memoized("test", "key", collect) == "value"
    assert len(calls) == 3
    assert "value" not in repr(first.snapshot())
    assert first.closed and second.closed
    assert estimate_size(("key", "value")) > 0
