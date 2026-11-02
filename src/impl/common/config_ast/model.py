"""可变的配置节点及源位置诊断。"""

from dataclasses import dataclass

from .tracking import _MISSING, TrackedChildren, tracker_for


@dataclass(frozen=True)
class SourceLocation:
    """记录原始输入位置，且不随后续编辑变化。

    行号和列号始终指向输入文本；编辑过程不会把它们重新解释为输出坐标。
    """

    line: int
    column: int = 1


class ConfigNode:
    """保存配置语法和层级关系，不依赖特定解析器运行时。

    子节点持有父节点引用；``kind`` 独立记录空块语义，不以子节点数量作为判断依据。
    """

    _TRACKED_FIELDS = frozenset(
        {
            "header",
            "_header",
            "kind",
            "comment",
            "active",
            "origin",
            "rank",
            "declared_in_source",
            "location",
            "children",
            "parent",
        }
    )

    def _tracker(self):
        return tracker_for(self)

    def __setattr__(self, name, value):
        if name not in self._TRACKED_FIELDS:
            object.__setattr__(self, name, value)
            return
        # Juniper's descriptor normalizes header and writes _header itself.
        if name == "header" and isinstance(
            getattr(type(self), name, None), property
        ):
            object.__setattr__(self, name, value)
            return
        old = vars(self).get(name, _MISSING)
        if old is value or (old is not _MISSING and old == value):
            return
        if name == "children":
            value = TrackedChildren(self, value)
        tracker = self._tracker()
        if tracker is not None:
            tracker.field(self, name, old, value)
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        old = getattr(self, name)
        tracker = self._tracker()
        if name in self._TRACKED_FIELDS and tracker is not None:
            tracker.field(self, name, old, _MISSING)
        object.__delattr__(self, name)

    def __init__(
        self,
        header="",
        children=None,
        *,
        kind=None,
        comment=None,
        location=None,
        active=True,
        origin="explicit",
        rank=(1_000_000, 0),
        declared_in_source=False,
    ):
        """创建一个游离节点；空块通过显式 ``kind`` 表示。

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        self.header = header
        self.kind = kind or ("block" if children is not None else "statement")
        self.children = list(children or [])
        self.parent = None
        self.comment = comment
        self.location = location
        self.active = active
        self.origin = origin
        self.rank = rank
        self.declared_in_source = declared_in_source
        for child in self.children:
            child.parent = self

    @property
    def nodes(self):
        """返回用于遍历的稳定直接子节点视图。

        元组避免调用方收集编辑时结构发生变化；逻辑上已删除的子节点不会出现。
        """
        return tuple(child for child in self.children if child.active)

    @property
    def is_block(self):
        """区分空块和叶子语句。

        根节点和 group 节点即使没有子节点也属于可承载子配置的节点。
        """
        return self.kind in {"root", "block", "group"}

    @property
    def is_formatting(self):
        """识别不参与语义查询的注释和分隔符。

        格式化节点仍可供 Writer 输出，但默认不会进入语义遍历。
        """
        return self.kind in {"comment", "separator"} or not self.header

    @property
    def is_comment_only(self):
        """识别独立注释节点。

        命令末尾的注释只表示行尾注释，不会把该命令节点变成纯注释节点。
        """
        return not self.header and self.comment is not None

    @property
    def path(self):
        """返回祖先命令头，不包含虚拟根节点。

        当前节点也排除在外，使选择器可以匹配该节点所处的配置作用域。
        """
        result, parent = [], self.parent
        while parent is not None and parent.kind != "root":
            result.append(parent.header)
            parent = parent.parent
        return tuple(reversed(result))

    def accept(self, visitor):
        """把当前节点分派给 Visitor，但不控制其子节点遍历。

        遍历策略由 Visitor 决定，调用方可以据此跳过或聚合整棵子树。
        """
        return visitor.visit(self)

    def walk(self, *, include_self=False, include_formatting=False):
        """按源码顺序迭代后代节点，不使用递归调用。

        显式栈避免递归深度限制，并通过反向压入待处理子节点保持源码顺序。
        """
        pending = (
            [self]
            if include_self
            else [child for child in reversed(self.children) if child.active]
        )
        while pending:
            node = pending.pop()
            if include_formatting or not node.is_formatting:
                yield node
            pending.extend(
                child for child in reversed(node.children) if child.active
            )

    def clone(self):
        """复制子树，但不沿着父节点把文档其他部分一并复制。

        副本拥有独立的子节点列表，并止步于当前子树边界。
        """

        def shell(original, parent):
            cloned = object.__new__(type(original))
            cloned.__dict__.update(
                {
                    key: value
                    for key, value in vars(original).items()
                    if key not in {"_tracking", "children", "parent"}
                }
            )
            object.__setattr__(cloned, "parent", parent)
            object.__setattr__(cloned, "children", TrackedChildren(cloned))
            return cloned

        root = shell(self, None)
        pending = [(self, root)]
        while pending:
            original, target = pending.pop()
            for child in original.children:
                cloned = shell(child, target)
                list.append(target.children, cloned)
                pending.append((child, cloned))
        return root

    def __deepcopy__(self, memo):
        """为规则回滚提供非递归的树复制能力。

        父节点关系和派生缓存不得继续引用原文档中的节点。
        """
        cloned = self.clone()
        memo[id(self)] = cloned
        return cloned


def bind_parents(root):
    """在批量结构编辑后校验并重新连接父子关系。

    重复出现的节点身份会被拒绝，因为一个配置节点只能有一个所有者。
    """
    root.parent = None
    seen, pending = {id(root)}, [root]
    while pending:
        node = pending.pop()
        for child in node.children:
            if id(child) in seen:
                raise ValueError("AST contains a cycle or shared child")
            seen.add(id(child))
            child.parent = node
            pending.append(child)
    return root


def tree_signature(root):
    """记录可编辑值和树结构，用于只读规则检查。

    签名同时包含可编辑元数据和子节点数量，因此能够发现结构变化。
    """
    pending, result, seen = [(root, None)], [], set()
    while pending:
        node, expected_parent = pending.pop()
        if id(node) in seen:
            raise ValueError("AST contains a cycle or shared child")
        seen.add(id(node))
        result.append(
            (
                node.header,
                node.kind,
                node.comment,
                node.active,
                node.origin,
                node.rank,
                node.declared_in_source,
                node.location,
                len(node.children),
                node.parent is expected_parent,
            )
        )
        pending.extend((child, node) for child in reversed(node.children))
    return tuple(result)
