"""Junos ANTLR Context 的只读查询视图。"""

from __future__ import annotations

from antlr4 import ParserRuleContext, TerminalNode

_STATE_PREFIXES = ("inactive:", "protect:")


def base_header(header: str) -> str:
    """移除 Junos 状态前缀并返回用于业务匹配的语句头。

    业务身份不应因为节点被停用或保护而改变，因此这里统一剥离开关前缀。
    """
    result = header.strip()
    while True:
        for prefix in _STATE_PREFIXES:
            if result.startswith(prefix):
                result = result[len(prefix) :].strip()
                break
        else:
            return result


class JunosContext(ParserRuleContext):
    """在 ANTLR 解析上下文上提供只读配置树视图。

    查询直接读取 token 和父子关系，避免复制第二棵树或持有可变文档状态。
    """

    def __deepcopy__(self, memo):
        """返回当前不可变 Context 并忽略复制记录。

        Context 与 token 流按修订隔离且不会原地修改，共享实例可避免昂贵的深拷贝。
        """
        del memo
        return self

    @property
    def snapshot(self):
        """返回整棵 Context 树所属的不可变解析快照。

        所有节点共享同一修订身份，才能可靠拒绝跨快照读取和过期事务。
        """
        root = self
        while root.parentCtx is not None:
            root = root.parentCtx
        return root._snapshot

    @property
    def nodes(self):
        """返回当前节点的直接 Junos 语句或注释上下文。

        过滤生成的包装节点和终结符，使业务遍历只看到稳定语义节点。
        """
        return tuple(
            _unwrap_element(child)
            for child in self.getChildren()
            if _is_configuration_child(child)
        )

    @property
    def header(self):
        """返回不带块分隔符的原始语句头。

        命令内容保持原样以兼容未知 CLI 语法；叶子额外保留分号供既有规则匹配。
        """
        if self.parentCtx is None:
            return "<root>"
        command = getattr(self, "command", None)
        if callable(command):
            value = command()
            if value is not None:
                result = self.snapshot.tokens.getText(
                    value.start.tokenIndex,
                    value.stop.tokenIndex,
                ).strip()
                brace = getattr(self, "LBRACE", None)
                semicolon = getattr(self, "SEMI", None)
                if (
                    callable(semicolon)
                    and semicolon() is not None
                    and not (callable(brace) and brace() is not None)
                ):
                    result += ";"
                return result
        if getattr(self, "LINE_COMMENT", None):
            return ""
        if getattr(self, "BLOCK_COMMENT", None):
            return ""
        statement = getattr(self, "statement", None)
        if callable(statement):
            value = statement()
            if value is not None:
                return value.header
        return self.snapshot.tokens.getText(
            self.start.tokenIndex,
            self.stop.tokenIndex,
        ).strip()

    @property
    def base_header(self):
        """返回去除 inactive/protect 状态前缀的语句头。

        节点身份和路径匹配必须忽略编辑状态，原始 header 仍用于源码输出。
        """
        return base_header(self.header)

    @property
    def comment(self):
        """返回当前语句同一行附着的注释文本。

        注释决定删除范围和人读层级，必须来自直接 token，不能混入子块注释。
        """
        for child in self.getChildren() or []:
            if not isinstance(child, TerminalNode):
                continue
            text = child.getText()
            if text.startswith(("#", "/*")):
                return text
        return None

    @property
    def active(self):
        """报告 Context 是否仍在当前快照中生效。

        Context 不承载逻辑删除状态，所有实例都对应实际存在的源码节点。
        """
        return True

    @property
    def is_formatting(self):
        """返回节点是否仅用于保留独立注释。

        格式节点不参与业务分析，但原文输出和注释保留仍需要看到它们。
        """
        return not self.header and self.comment is not None

    @property
    def is_comment_only(self):
        """返回节点是否只承载独立注释。

        独立注释没有命令头，业务规则据此跳过而不是误判为空语句。
        """
        return self.is_formatting

    @property
    def is_block(self):
        """返回节点是否由大括号声明为配置块。

        直接检查生成 Context 的括号 token，避免依赖缩进或业务命令白名单。
        """
        brace = getattr(self, "LBRACE", None)
        if callable(brace) and brace() is not None:
            return True
        statement = getattr(self, "statement", None)
        return bool(
            callable(statement) and statement() and statement().is_block
        )

    @property
    def configured_inactive(self):
        """返回原始 header 是否携带 ``inactive:`` 状态。

        停用状态影响继承和业务求值，但必须与转换过程中的逻辑删除区分。
        """
        return self.header.startswith("inactive:")

    @property
    def effective(self):
        """返回节点是否参与继承、接口和业务语义分析。

        配置停用和独立注释不参与求值，普通 Context 则默认有效。
        """
        return not self.configured_inactive and not self.is_comment_only

    @property
    def declared_in_source(self):
        """返回块是否来自源码而不是转换过程新建的容器。

        最终空块策略只能影响源配置，新生成的空容器应保持省略以免污染输出。
        """
        return getattr(self, "_declared_in_source", True)

    @property
    def indent(self):
        """返回语句起点所在行的展开 Tab 后缩进宽度。

        新片段需要复用原层级，因此制表符按固定宽度换算以得到稳定视觉缩进。
        """
        position = self.start.start
        line_start = self.snapshot.text.rfind("\n", 0, position) + 1
        prefix = self.snapshot.text[line_start:position]
        if prefix.strip():
            return 0
        return len(prefix.expandtabs(8))

    @property
    def text(self):
        """返回当前节点覆盖的原始源码区间。

        区间包含独占行缩进和换行，使移动、克隆和删除能保留周围格式。
        """
        if self.parentCtx is None:
            return self.snapshot.text
        begin = self.start.start
        line_start = self.snapshot.text.rfind("\n", 0, begin) + 1
        if not self.snapshot.text[line_start:begin].strip():
            begin = line_start
        stop = self.stop.stop + 1
        line_end = self.snapshot.text.find("\n", stop)
        if line_end < 0:
            line_end = len(self.snapshot.text)
        if not self.snapshot.text[stop:line_end].strip():
            stop = min(line_end + 1, len(self.snapshot.text))
        return self.snapshot.text[begin:stop]

    @property
    def path(self):
        """返回从根到当前节点父级的语义路径。

        路径跳过语法包装和格式节点，供清洗规则稳定定位业务层级。
        """
        ancestors = []
        parent = self.parentCtx
        seen = set()
        while parent is not None:
            semantic = _unwrap_element(parent)
            if (
                semantic is not self
                and id(semantic) not in seen
                and semantic.parentCtx is not None
                and semantic.header
                and semantic.header != "<root>"
                and not semantic.is_formatting
            ):
                ancestors.append(semantic.header)
                seen.add(id(semantic))
            parent = parent.parentCtx
        return tuple(reversed(ancestors))

    def walk(self, *, include_self=False, include_formatting=False):
        """按深度优先顺序遍历当前子树。

        默认仅返回业务节点，避免分析代码重复处理终结符和独立注释。
        """
        pending = [self] if include_self else list(reversed(self.nodes))
        while pending:
            context = pending.pop()
            if include_formatting or not context.is_formatting:
                yield context
            pending.extend(reversed(context.nodes))


def _is_configuration_child(child) -> bool:
    """判断 ANTLR 子节点是否属于 Junos 语义树。

    grammar 包装层会混入终结符，统一过滤后才能暴露稳定的业务节点集合。
    """
    return isinstance(child, JunosContext) and (
        callable(getattr(child, "statement", None))
        or callable(getattr(child, "LINE_COMMENT", None))
        or callable(getattr(child, "BLOCK_COMMENT", None))
    )


def _unwrap_element(context: JunosContext) -> JunosContext:
    """把 grammar 包装节点折叠为业务语句或注释 Context。

    包装层只服务解析结构，业务查询需要在保持父级语义的同时隐藏重复层级。
    """
    statement = getattr(context, "statement", None)
    if callable(statement):
        nested = statement()
        if nested is not None:
            return nested
    return context
