"""生成 Context 的只读查询能力；不保存第二份配置树或 Document 引用。"""

import re

from antlr4 import ParserRuleContext


class CiscoContext(ParserRuleContext):
    """在 ANTLR 解析上下文上提供配置树查询视图。

    查询直接读取解析 token 和父子关系，避免复制第二份树或持有额外文档引用。
    """

    @property
    def snapshot(self):
        """返回当前上下文所属的解析快照。

        从根上下文向上查找可保证整棵树共享同一修订标识和原始文本。
        """
        root = self
        while root.parentCtx is not None:
            root = root.parentCtx
        return root._snapshot

    @property
    def nodes(self):
        """返回当前节点的直接 Cisco 子上下文。

        过滤 ANTLR 内部非配置子节点，使业务遍历只需处理配置结构。
        """
        return tuple(
            child
            for child in self.getChildren()
            if isinstance(child, CiscoContext)
        )

    @property
    def raw_header(self):
        """返回当前节点第一行的原始文本。

        保留未清理文本供缩进、注释和接口解析使用，根节点使用固定占位文本。
        """
        return (
            self.start.text.splitlines()[0]
            if self.parentCtx is not None
            else "<root>"
        )

    @property
    def header(self):
        """返回去除首尾空白后的可执行配置头。

        独立注释行不参与命令匹配，因此作为格式节点时返回空字符串。
        """
        raw = self.raw_header.strip()
        return "" if raw.startswith("!") and raw != "!" else raw

    @property
    def comment(self):
        """返回独立注释内容或空值。

        只有以叹号开头且不是纯分隔符的行才视为注释，避免把配置结束行当成说明。
        """
        raw = self.raw_header.strip()
        return raw if raw.startswith("!") and raw != "!" else None

    @property
    def active(self):
        """返回节点是否参与当前配置逻辑。

        ANTLR 上下文本身不承载逻辑删除状态，因此默认始终为活动节点。
        """
        return True

    @property
    def is_formatting(self):
        """返回节点是否仅用于空行或分隔符格式。

        格式节点不参与命令匹配和业务迁移，统一判断可避免各处理器重复过滤。
        """
        return self.header in {"", "!"}

    @property
    def is_block(self):
        """判断节点是否为可递归遍历的配置块。

        除实际子节点外，已知顶层命令即使为空也保留块语义，使 group 能进入该路径。
        """
        if getattr(self, "_was_block", False) or any(
            not child.is_formatting or child.header == "!"
            for child in self.nodes
        ):
            return True
        top_level = (
            self.parentCtx is not None and self.parentCtx.parentCtx is None
        )
        return top_level and bool(
            re.match(
                r"^(?:interface|router|vrf|l2vpn|mpls|username|line|"
                r"segment-routing|telemetry)\b",
                self.header,
                re.I,
            )
        )

    @property
    def interface_name(self):
        """返回当前节点头对应的规范化接口名称。

        复用文档层的接口名解析，确保上下文查询与正式接口分类使用同一规则。
        """
        from ..document import _interface_name_from_header

        return _interface_name_from_header(self.header)

    @property
    def is_preconfigured_interface(self):
        """返回当前节点是否是尚未实例化的预配置接口。

        该标记用于在业务分析前识别候选配置，避免其进入真实接口迁移。
        """
        return bool(
            re.match(r"^interface\s+preconfigure(?:\s|$)", self.header, re.I)
        )

    @property
    def l2transport(self):
        """返回当前接口头是否声明了二层传输模式。

        只看行内后缀可保持判断轻量，同时与接口分类使用的配置语法一致。
        """
        return bool(
            re.match(r"interface\s+.+\s+l2transport\s*$", self.header, re.I)
        )

    @property
    def indent(self):
        """返回原始节点头展开制表符后的缩进宽度。

        将制表符转换为固定宽度后，ANTLR 生成的虚拟缩进标记才能稳定表达层级。
        """
        raw = self.raw_header.expandtabs(8)
        return len(raw) - len(raw.lstrip())

    @property
    def text(self):
        """返回当前上下文覆盖的原始配置文本。

        文本由快照 token 区间切片得到，确保移动和克隆片段时注释与空格保持不变。
        """
        if self.parentCtx is None:
            return self.snapshot.text
        return self.snapshot.text[self.start.start : self.stop.stop + 1]

    @property
    def path(self):
        """返回从根到当前节点父级的配置路径。

        父级路径用于规则作用域和报告定位，不包含当前节点自身的 header。
        """
        ancestors = []
        parent = self.parentCtx
        while parent is not None and parent.parentCtx is not None:
            ancestors.append(parent.header)
            parent = parent.parentCtx
        return tuple(reversed(ancestors))

    def walk(self, *, include_self=False, include_formatting=False):
        """按深度优先顺序递归遍历当前上下文子树。

        默认跳过格式节点并返回后代，调用方可通过参数显式包含自身或空行分隔符。
        """
        pending = [self] if include_self else list(reversed(self.nodes))
        while pending:
            context = pending.pop()
            if include_formatting or not context.is_formatting:
                yield context
            pending.extend(reversed(context.nodes))
