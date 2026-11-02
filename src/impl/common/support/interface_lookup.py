"""接口元数据的增量索引，不依赖编辑日志是否仍然保留。

文档以弱引用持有；预算逐出和任务结束都会释放全部被索引的节点；普通载荷编辑不会重建整个接口目录。
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
    """保存单个接口容器的名称、顺序和逻辑归属关系。

    这里只是引用既有 AST 对象，不把它们当作新分配的数据计费。
    """

    def __init__(self, document):
        """按厂商既有的语义建立一份初始容器快照。

        Junos 只取第一个生效的 interfaces 块，与 top_block 的行为保持一致。
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
        """读取单个节点的活动规范名和逻辑接口名。

        unit 之下的标量配置刻意不进入这份视图，否则单位级参数会污染接口目录。
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
        """枚举该条目所属的各个查找桶，同时不丢失嵌套点号名的既有行为。

        树前缀桶对应原实现的精确名匹配和源码点号前缀扫描。
        """
        name, _ = entry
        yield self.by_name, name
        yield self.by_parent, interface_parent(name) if self.cisco else name
        parts = name.split(".")
        for count in range(1, len(parts) + 1):
            yield self.by_tree, ".".join(parts[:count])

    @staticmethod
    def charge(entry):
        """对新分配的名称、桶和引用做保守计费。

        该估算只在节点变化时更新，命中查询永远不会触发扫描。
        """
        prefixes = 1 + entry[0].count(".")
        return (
            512
            + len(entry[0]) * 4
            + prefixes * (160 + 4 * len(entry[0]))
            + sum(96 + len(name) * 4 for name in entry[1])
        )

    def refresh(self, node):
        """只更新属于某个已变化接口的那些桶。

        原始源码位置保持不变，从而维持重复定义和首次匹配的语义。
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
        """移除一个已脱离的候选节点，且不重排未受影响的位置。

        删除不会改变其余节点的相对源码顺序，重复定义所在的桶同样如此。
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
        """用单调递增的位置为新的尾部候选建立索引。

        仍被共享的节点会触发异常，由上层回退到扫描路径，而不是悄悄丢掉重数信息。
        """
        if node in self.order:
            raise ValueError("Shared interface child")
        self.order[node] = self.last_position
        self.last_position += 1
        self.cost += 128
        self.refresh(node)

    def all_nodes(self):
        """返回由调用方持有的、按序排列的当前接口节点列表。

        在接口成员发生变化之前，重复读取复用同一份有序视图。
        """
        if self.ordered is None:
            self.ordered = sorted(self.entries, key=self.order.__getitem__)
        return list(self.ordered)

    def matching(self, name, tree=False):
        """返回按精确名或按接口树匹配的独立结果。

        重复定义仍是彼此独立的节点，并保持源码顺序。
        """
        return list((self.by_tree if tree else self.by_name).get(name, ()))

    def logical_names(self, parent):
        """只从指定父接口的条目中收集逻辑名。

        排序和去重沿用原先基于 interface_specs 的查询行为。
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
    """观察修改，但不持有文档，也不依赖其日志。

    待刷新的元数据可以跨嵌套提交存活；根节点被替换时丢弃全部旧节点。
    """

    def __init__(self, document):
        """挂上弱引用观察者，并按需分配一份独立的局部缓存。

        文档销毁时的弱回调会立即释放由任务持有的索引载荷。
        """
        weak = ref(self)

        def released(_):
            """弱引用的文档所有者销毁时，丢弃由预算池持有的数据。

            回调只捕获弱引用持有者，避免形成所有权环。
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
        """在逐出或结构性重置之后，忘掉已排队的节点。

        任务结束后，这个持有者中不再残留任何 AST 引用。
        """
        self.dirty.clear()
        self.removed.clear()
        self.added.clear()
        self.structural = True
        self.rejected = False
        self.budget_rejected = False

    def discard(self):
        """文档退役时，把该索引从当前池中移除。

        预算回调是弱引用，因此双方都不会保活对方。
        """
        if self.pool is not None:
            self.pool.discard(self.key)
        self.reset()

    def changed(self, tracker, kind, node, name, change=None):
        """在写入可见之前，只排队与目录相关的改动。

        父链修复和普通载荷编辑都不会让整个接口目录失效。
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
        """合并删除与追加；只有真正重排时才整体重建。

        队列过大时收敛成一个重建标记，避免在缓存预算之外保留无限长度的修改历史。
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
        """应用队列中的增量，且不扫描无关的接口定义。

        通知的消费与回滚日志检查点彼此独立，两者不共享游标。
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
        """选择活动任务池或文档局部的有界池。

        跨任务边界时绝不复用由输入派生的索引载荷。
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
        """构建或增量刷新当前可选的索引。

        无法缓存或视图异常时，在原先的调用边界回退到扫描路径，而不是改变报错或结果语义。
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
        """构建容器索引，并且只在预算允许时才接纳它。

        真正重排之后的整体重建，与标量改动引起的桶更新是两条不同的路径。
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
    """返回可选的索引视图，同时保留预算为零时的扫描路径。

    持有者按需创建，并在文档深复制后从副本状态中剔除。
    """
    pool = current_cache()
    if pool is not None and (pool.closed or not pool.budget):
        return None
    holder = vars(document).get("_interface_lookup")
    if holder is None:
        holder = InterfaceLookup(document)
        document._interface_lookup = holder
    return holder.get()
