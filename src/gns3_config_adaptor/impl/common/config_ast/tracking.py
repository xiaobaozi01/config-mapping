"""AST 的增量所有权索引、修改序号与可逆编辑日志。

节点以弱引用指回本对象；日志只在存在活动回滚边界时保留，或保留到下一次显式的 tree_changed 通知为止。
"""

from collections import Counter
from contextlib import contextmanager
from operator import index as integer_index
from weakref import WeakSet, ref

_MISSING = object()


class TrackedChildren(list):
    """拦截对子节点列表的常规改动。

    列表读取保持原有行为；写入在暴露新内容之前，先记录成可逆的区间替换。
    """

    __slots__ = ("_owner",)

    def __init__(self, owner, values=()):
        """把子节点序列绑定到其所属节点。

        持有者使用弱引用，避免在 AST 上再增加一条所有权环。
        """
        super().__init__(values)
        self._owner = ref(owner)

    def _splice(self, start, stop, values):
        """替换一个连续区间，并记录其逆操作。

        先把待写入的值物化，既支持生成器，也支持向自身扩展，同时避免把未完成的改动部分暴露出去。
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
        """处理单元素、切片和扩展切片三种赋值。

        索引校验先于日志更新，因此失败的列表操作不会留下任何树状态改动。
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
        """通过可逆的区间替换删除单个元素或切片。

        扩展切片先在局部重建，以保留 Python 列表原有的顺序和索引语义。
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
        """追加一个子节点。

        只记录新插入的这条边，不复制整段兄弟序列。
        """
        self._splice(len(self), len(self), [value])

    def extend(self, values):
        """追加一组子节点。

        更新所有权之前先物化传入的可迭代对象，源就是本列表时同样成立。
        """
        self._splice(len(self), len(self), values)

    def insert(self, index, value):
        """按列表常规的索引截断规则插入子节点。

        规范化后的插入位置会写进撤销日志，回滚时据此还原。
        """
        index = integer_index(index)
        index = (
            max(0, len(self) + index) if index < 0 else min(index, len(self))
        )
        self._splice(index, index, [value])

    def pop(self, index=-1):
        """删除并返回指定位置的子节点。

        先读取再删除，从而保留空列表和非法索引下标准的报错行为。
        """
        value = self[index]
        del self[index]
        return value

    def remove(self, value):
        """删除第一个匹配的子节点。

        查找沿用列表语义，删除动作同样参与事务回滚。
        """
        del self[self.index(value)]

    def clear(self):
        """删除全部子节点。

        被删除的区间只在活动回滚边界仍然需要时才留在日志中。
        """
        self._splice(0, len(self), [])

    def reverse(self):
        """反转子节点顺序。

        顺序属于配置语义的一部分，因此这里按整段替换记录，而不是原地交换。
        """
        self._splice(0, len(self), reversed(self))

    def sort(self, *, key=None, reverse=False):
        """按给定的键函数和方向排序子节点。

        先对临时列表排序，使键函数抛出异常时不会把活动树改成一半。
        """
        self._splice(0, len(self), sorted(self, key=key, reverse=reverse))

    def __iadd__(self, values):
        """就地扩展本列表。

        返回同一个容器，既保留别名关系，也支持对 node.children 使用增量赋值。
        """
        self.extend(values)
        return self

    def __imul__(self, count):
        """就地重复或清空本列表。

        重复出现的共享子节点会在事务同步边界被所有权校验拒绝。
        """
        self._splice(0, len(self), list(self) * count)
        return self


def tracker_for(node):
    """解析节点当前关联的追踪器。

    弱引用使被保留的节点可以在文档销毁后继续存在，而不会连带保住文档的分析缓存。
    """
    weak = getattr(node, "_tracking", None)
    tracker = weak() if weak is not None else None
    return tracker if tracker is not None and node in tracker.edges else None


def cache_token(root):
    """返回根节点身份和即时修改序号。

    未被追踪的根每次拿到一个新令牌，使游离的可变树不会复用陈旧的分析结果。
    """
    tracker = tracker_for(root)
    return (root, tracker.epoch) if tracker is not None else object()


class TreeTracker:
    """维护增量所有权索引和可逆修改。

    边索引属于单个文档，节点以弱引用指回它；因此成功的规则不需要整树快照。
    """

    def __init__(self, root):
        """对已挂载的配置树做一次索引和校验。

        后续同步只访问发生变化的关系，不必为每条规则重建整个索引。
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
        self.listeners = WeakSet()
        self.adopt(root)
        self.dirty.update(self.edges)
        self.repair()
        self.clear()

    def notify(self, kind, node=None, name=None, change=None):
        """通知弱引用的元数据观察者，同时不保留编辑历史。

        观察者只负责排队失效，绝不会去查询写到一半的 AST。
        """
        for listener in tuple(self.listeners):
            listener.changed(self, kind, node, name, change)

    def adopt(self, root):
        """只索引此前未见过的子树。

        跨文档共享在发布前就被拒绝；入边与已访问节点分开记录，用来发现环。
        """
        self._adopt_many([root])

    def _adopt_many(self, roots):
        """在发布任何所有权变更之前，先校验待接入的子树。

        即使不在编辑作用域内，直接改列表失败时索引也必须保持不变；后续某个子节点属于其他文档时同样如此。
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
        """调整一条入边，单父情况下使用紧凑存储。

        存在多个父节点时改用计数映射，直到同步阶段拒绝共享，或后续操作完成一次临时搬移。
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
        """在指定阶段用调用方给定的异常临时拒绝实时写入。

        回调抛出异常时，嵌套的各个阶段仍会恢复之前的限制状态。
        """
        previous = self.write_error
        self.write_error = (error, message)
        try:
            yield
        finally:
            self.write_error = previous

    def check_write(self):
        """强制执行当前规则回调的写入限制。

        在发布之前检查，可以阻止 Visitor 和结果映射器通过另一个 Editor 提交改动。
        """
        if self.write_error is not None:
            error, message = self.write_error
            raise error(message)

    def belongs(self, node):
        """检查按父边索引能否走到当前根节点。

        沿祖先链上溯既避免线性搜索兄弟节点，也能拒绝游离、已退役、成环和一子多父的节点。
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
        """在字段改动可见之前先记录下来。

        替换子节点容器要同步更新边索引，而标量写入只推进即时修改序号。
        """
        self.check_write()
        if name == "children":
            self._change_edges(node, old, new)
        self.journal.append(("field", node, name, old, new))
        self.epoch += 1
        if name == "parent":
            self.dirty.add(node)
        if self.listeners:
            self.notify("field", node, name)

    def _change_edges(self, node, old, new):
        """计算并应用新旧子节点区间之间的边差异。

        用重数计数保留重复子节点的检出能力，未变化的边则完全不需要遍历子树。
        """
        old_counts, new_counts = Counter(old), Counter(new)
        removed, added = old_counts - new_counts, new_counts - old_counts
        self._adopt_many(added)
        for child, count in removed.items():
            self._incoming(child, node, -count)
        for child, count in added.items():
            self._incoming(child, node, count)

    def splice(self, node, children, index, old, new):
        """记录子节点容器区间替换的逆操作。

        已退役的列表别名仍可逆，但不再贡献边，也不会让当前配置失效。
        """
        active = node.children is children
        if active:
            self.check_write()
            self._change_edges(node, old, new)
            self.epoch += 1
            if self.listeners:
                self.notify("splice", node, "children", (index, old, new))
        self.journal.append(("splice", children, index, old, new))

    def replace_root(self, root, *, restoring=False):
        """挂载新根节点并记录其前任。

        回滚在内部可以绕过回调限制，把这次替换也记进日志才能保住外层事务的检查点。
        """
        if not restoring:
            self.check_write()
        self.adopt(root)
        self.journal.append(("root", self, "root", self.root, root))
        self.detached.add(self.root)
        self.root = root
        self.dirty.add(root)
        self.epoch += 1
        self.notify("root")

    def repair(self):
        """校验发生变化的入边，并重新连接受影响的节点。

        祖先检查用来发现环，而直接修复 parent 字段可避免递归触发修改钩子。
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
        """只比较日志检查点之后被触及的值。

        对比时先在局部反向重放列表替换，这样不必对整个树做哈希，也能识别出改动相互抵消的批次。
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
        """修复待处理的边，并报告是否存在净配置变化。

        同步游标确保内层批次已发布的工作不会被外层作用域退出时重复执行。
        """
        self.repair()
        changed = self.changed_since(self.sync_index)
        self.sync_index = len(self.journal)
        if not self.scopes:
            self.clear()
        return changed

    def undo(self, index):
        """按相反顺序还原日志中记录的取值。

        这条失败路径会依据还原后的树重建所有权；根节点替换同样被反向执行，使外层记录继续可用。
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
        """撤销到一个边界，并换上一棵独立所有权的回滚副本。

        复制过程本身已保证子节点唯一、父链正确，因此只需一次索引就能接管，不必把新副本再校验两遍；根替换写入日志后，外层回滚记录仍然可用。
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
        self.notify("root")

    def clear(self):
        # 只回收本批次影响到的、已断开连接的组件。保留所有退役
        # 子树会把索引变成无界缓存。
        """释放已提交的日志条目和断开连接的索引组件。

        只访问受影响的组件，使反复成功的规则无法累积退役树，也不会去扫描未变化的节点。
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
