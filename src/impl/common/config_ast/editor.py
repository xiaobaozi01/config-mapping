"""可变 AST 的受校验结构编辑和审计记录。"""

from contextlib import contextmanager
from dataclasses import dataclass

from .model import ConfigNode, bind_parents


class EditConflictError(ValueError):
    """拒绝使用游离节点或无效树关系的编辑。

    批量编辑必须失败，不能把操作应用到无关或无效的树位置。
    """


@dataclass(frozen=True)
class EditResult:
    """暴露已应用编辑批次的审计记录。

    只有成功应用的操作会进入结果映射器读取的 ``changes`` 元组。
    """

    changes: tuple = ()

    @property
    def total(self):
        """统计已完成的编辑数量。

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        return len(self.changes)


def format_fragment(text, indent=0, newline="\n"):
    """在解析为节点前规范化片段缩进。

    该辅助函数只供构造片段的调用方使用；完整文档输出由 Writer 负责。
    """
    lines = text.splitlines()
    first = next((line.expandtabs(8) for line in lines if line.strip()), "")
    base = len(first) - len(first.lstrip())
    return newline.join(
        " " * indent + line.expandtabs(8)[base:] for line in lines
    ) + (newline if lines else "")


class ConfigEditor:
    """收集经过校验的节点编辑，并在遍历后统一应用。

    操作基于节点身份而非源偏移；输入片段会先解析，再挂接到配置树。
    """

    def __init__(self, document):
        """把编辑批次绑定到当前根节点身份。

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        self.document, self.root = document, document.root
        self.operations, self.finished = [], False
        self._after = {}

    @property
    def newline(self):
        """返回规范输出换行符。

        无论输入配置使用何种换行符，两个厂商的 Writer 都输出 LF。
        """
        return "\n"

    def _check(self, node):
        """拒绝游离节点、外部节点和已回滚节点的引用。

        根节点替换会使旧 Editor 失效，游离节点也不能继续作为编辑目标。
        """
        if self.finished or self.root is not self.document.root:
            raise EditConflictError("Editor is no longer current")
        if not self.document._tracker.belongs(node) or not node.active:
            raise EditConflictError("Node does not belong to this document")

    def _record(self, action, node, payload, reason, details):
        """在记录操作前校验节点归属和审计字段。

        保留审计字段归框架管理；调用方可以增加明细字段，但不能覆盖这些值。
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
        """在改变文档状态前解析并分离新节点。

        新节点会清除源位置，因为其坐标不属于原始输入文档。
        """
        nodes = self.document.parse_fragment(text).children
        for node in nodes:
            node.parent = None
            for child in node.walk(include_self=True, include_formatting=True):
                child.location = None
                child.declared_in_source = False
        return nodes

    def delete(self, node, reason, **details):
        """登记删除一整棵子树。

        删除父节点会同时移除后代，因此调用方不应重复登记子节点删除。
        """
        self._record("delete", node, None, reason, details)

    def replace_header(self, node, header, reason, **details):
        """校验新命令头，同时保留原有子节点。

        替换命令头后，节点身份和父子关系保持不变。
        """
        self._check(node)
        if "\n" in header or "\r" in header:
            raise ValueError("Command header cannot contain newlines")
        header = self.document.normalize_header(header)
        self.document.validate_header(node, header)
        if header != node.header:
            self._record("replace_header", node, header, reason, details)

    def replace_subtree(self, node, text, reason, **details):
        """使用解析后的配置片段替换子树。

        替换片段可以包含多个兄弟节点，它们都会挂接到原位置。
        """
        self._check(node)
        if text != self.document.writer.subtree(node):
            self._record(
                "replace_subtree", node, self._fragment(text), reason, details
            )

    def insert_before(self, node, text, reason, **details):
        """在指定节点之前插入解析后的兄弟节点。

        目标节点保持原位，所有插入节点都成为同一父节点的子节点。
        """
        self._record(
            "insert_before", node, self._fragment(text), reason, details
        )

    def insert_after(self, node, text, reason, **details):
        """在指定节点之后插入解析后的兄弟节点。

        连续插入保持声明顺序，不会反转已收集的片段。
        """
        self._record(
            "insert_after", node, self._fragment(text), reason, details
        )

    def append_child(self, node, text, reason, **details):
        """追加解析后的子节点，并把叶子提升为块。

        目标节点保持身份不变；叶子节点在增加子节点后变成块。
        """
        self._record(
            "append_child", node, self._fragment(text), reason, details
        )

    def move(self, node, parent, reason, **details):
        """移动子树，并在应用编辑前拒绝循环关系。

        源节点保持身份和元数据，只改变父节点及所在路径。
        """
        self._check(parent)
        self._check_move(node, parent)
        self._record("move", node, parent, reason, details)

    def _check_move(self, node, parent):
        """在先前移动操作排入队列后再次校验当前目标。

        排队操作可能改变祖先关系，因此真正移动前必须重新校验。
        """
        ancestor = parent
        while ancestor is not None:
            if ancestor is node:
                raise EditConflictError("Move would create a cycle")
            ancestor = ancestor.parent

    def clone(self, node, parent, reason, **details):
        """向目标位置追加子树的独立副本。

        副本拥有独立子节点列表，并止步于当前子树边界。
        """
        self._check(node)
        cloned = node.clone()
        for child in cloned.walk(include_self=True, include_formatting=True):
            child.location = None
        self._record("append_child", parent, [cloned], reason, details)
        return cloned

    def _execute(self, action, node, payload):
        """执行一个已经校验过的结构操作。

        执行时同步更新父节点链接；文档级最终校验会重连批量修改并刷新缓存。
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
        """按现有审计报告结构序列化插入的节点。

        虽然编辑基于树节点执行，报告仍保留插入的配置文本。
        """
        if isinstance(payload, str):
            return payload
        if isinstance(payload, list):
            return "".join(self.document.writer.write(node) for node in payload)
        return ""

    def scope(self, *, independent=False):
        """创建回滚边界，并在嵌套编辑中复用已激活边界。

        规则执行器申请独立边界；同一 Editor 的批量编辑共享该边界。
        """
        return EditScope(self.document, independent=independent)

    @contextmanager
    def bulk(self):
        """管理直接修改多个 AST 节点的厂商算法。

        Editor 会校验父节点归属，并在批量修改失败时回滚。
        """
        self._check(self.root)
        with self.scope():
            yield self.document.root

    def replace_root(self, root):
        """通过 Editor 安装独立构造的结果树。

        Group 展开会先校验候选树，再把其中的节点发布到当前文档。
        """
        self._check(self.root)
        if root is self.root or root.parent is not None or root.kind != "root":
            raise EditConflictError("Replacement root must be detached")
        bind_parents(root)
        with self.scope():
            self.document.root = root
        self.finished = True

    def apply(self):
        """在当前回滚边界内应用已收集的编辑。

        独立批次自行持有日志检查点；受管规则复用活动边界。
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
        """逐个应用操作并收集其原始语义位置。

        只有整个批次成功后才会返回审计记录。
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
        """渲染编辑后的副本，但不改变当前活动树。

        活动树及其缓存保持不变，因此同一编辑批次可以重复预览。
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
    """为规则或独立结构编辑持有可撤销日志检查点。

    嵌套 Editor 批次共享活动作用域；显式规则边界则独立嵌套。
    """

    def __init__(self, document, *, independent=False):
        """记录所需回滚边界，但暂不复制配置树。

        进入上下文时决定是否复用外层日志检查点。
        """
        self.document, self.independent = document, independent
        self.failed = self.rolled_back = False

    def __enter__(self):
        """为边界所有者记录日志位置，不复制配置树。

        返回边界所有者，使嵌套算法可以请求回滚共享修改。
        """
        self.previous = getattr(self.document, "_edit_scope", None)
        self.owner = (
            self if self.independent or self.previous is None else self.previous
        )
        if self.owner is self:
            self.checkpoint = len(self.document._tracker.journal)
            self.document._tracker.scopes += 1
            self.revision = self.document.revision
            self.document._edit_scope = self
        elif self.owner.failed or self.owner.rolled_back:
            raise EditConflictError("The enclosing edit scope has failed")
        return self.owner

    def rollback(self):
        """恢复边界创建时的原树，并使旧引用失效。

        业务校验失败时可以直接请求回滚，而无需抛出异常。
        """
        if not self.rolled_back:
            tracker = self.document._tracker
            tracker.undo(self.checkpoint)
            # Only failure paths copy; the inverse root swap lets outer scopes
            # continue to undo records that reference the retired tree.
            restored = tracker.root
            tracker.edges = set()
            tracker.parents = {}
            tracker.detached = set()
            tracker.dirty = set()
            tracker.replace_root(restored.clone(), restoring=True)
            tracker.repair()
            tracker.sync_index = len(tracker.journal)
            self.document._revision = self.revision
            self.document._interface_analysis_cache = type(
                self.document._interface_analysis_cache
            )()
            self.rolled_back = True

    def synchronize(self):
        """在发布缓存和修订号变化前校验树结构。

        Writer 和后续规则只会看到父子关系一致的配置树。
        """
        self.document.tree_changed()

    def __exit__(self, exc_type, exc, traceback):
        """出错时恢复原树，并且只释放当前上下文拥有的作用域。

        即使嵌套异常被内部吞掉，外层批次也会中止，避免发布部分编辑。
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
                tracker = self.document._tracker
                tracker.scopes -= 1
                if not tracker.scopes:
                    tracker.clear()
            self.owner = None
        return False
