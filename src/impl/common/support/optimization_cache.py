"""面向可选优化数据的任务级、按字节计预算的 LRU 存储。

这里只对保守估算值计费，既有 AST 和进程 RSS 仍属彼此独立的指标；统计信息既不导出缓存键，也不导出任何输入值。
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
    """在插入条目时，对不可变缓存载荷估算一次体积。

    永不递归遍历 AST 节点；接口索引自行提供只计元数据的估算，避免把既有树重复计费。
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
    """返回当前可选的转换缓存。

    ContextVar 的隔离性还能在嵌套调用结束后自动恢复外层缓存。
    """
    return _CURRENT.get()


class BudgetCache:
    """在彼此独立的优化命名空间之间共享一份有界 LRU。

    条目回调使用弱方法，使任务不会仅仅为了失效一个已被逐出的接口索引而继续保活文档。
    """

    def __init__(self, budget_bytes):
        """创建一个空池，但不预留所宣称的容量。

        预算为零即关闭缓存；非法预算在转换工作开始之前就报错。
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
        """累加聚合计数，同时不保存请求键。

        命名空间是固定的代码标签，不是设备或配置标识。
        """
        counters = self.stats.setdefault(namespace, {})
        counters[event] = counters.get(event, 0) + count

    def get(self, key, namespace):
        """以常数期望时间取出条目并提升其近期程度。

        未命中改用显式哨兵表示，使空结果和假值结果同样可以被缓存。
        """
        entry = self.entries.get(key)
        if entry is None:
            self.note(namespace, "misses")
            return CACHE_MISS
        self.entries.move_to_end(key)
        self.note(namespace, "hits")
        return entry[0]

    def peek(self, key):
        """在失效期间查看仍在索引中的条目，且不改变 LRU 顺序。

        修改通知不得冒充查询命中，也不得触发重建。
        """
        entry = self.entries.get(key)
        return entry[0] if entry is not None else CACHE_MISS

    def put(self, key, value, size, namespace, callback=None):
        """只逐出必要的冷条目，然后接纳新值。

        超大值以及已关闭或已禁用的池都退化为不做缓存。
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
        """更新现有可变索引的增量计费。

        体积增长也可能把该索引自身逐出；调用方当前持有的结果仍然有效，下一次查询会透明地重建或走扫描回退。
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
        """释放一个值，并通知其可选的弱引用持有者。

        先摘除条目再执行回调，使递归失效不会造成死循环。
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
        """无论成功、异常还是取消，都在结束时释放全部载荷。

        聚合计数只为最终的性能报告保留。
        """
        if not self.closed:
            self.closed = True
            releases, callbacks = {}, []
            for _, _, namespace, callback in self.entries.values():
                releases[namespace] = releases.get(namespace, 0) + 1
                if callback is not None:
                    callbacks.append(callback)
            self.entries.clear()
            self.bytes = 0
            for namespace, count in releases.items():
                self.note(namespace, "releases", count)
            for callback in callbacks:
                method = callback()
                if method is not None:
                    method()

    def snapshot(self):
        """复制预算与计数，但不暴露被缓存的输入。

        估算峰值受池上限约束，它并不等于进程 RSS 的上限。
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
    """为一次转换持有一个池，并在退出时恢复上一个任务的状态。

    这里不引入并行执行；finally 中的清理同样覆盖 BaseException。
    """
    cache = BudgetCache(cache_budget_bytes)
    token = _CURRENT.set(cache)
    try:
        yield cache
    finally:
        cache.close()
        _CURRENT.reset(token)


def memoized(namespace, key, collect, *, local=None, charge=None):
    """用活动池或实例局部池计算纯不可变结果。

    计算失败的结果永不写入，可变 AST 对象也被排除在缓存之外。
    """
    cache = current_cache() or local
    if cache is None or cache.closed or not cache.budget:
        return collect()
    full_key = (namespace, key)
    value = cache.get(full_key, namespace)
    if value is CACHE_MISS:
        value = collect()
        size = (
            charge(value)
            if charge is not None
            else estimate_size(full_key, value)
        )
        cache.put(full_key, value, size, namespace)
    return value
