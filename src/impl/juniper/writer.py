"""Canonical Junos serialization preserving comments and quoted contents."""

from impl.common.empty_blocks import EmptyBlockAction

from .ast import JuniperNode


class JuniperWriter:
    """Write explicit blocks and statements with four-space indentation.

    Block and statement punctuation is generated from node kind, preserving explicit empty blocks in source output.
    """

    def subtree(self, node):
        """Serialize a subtree at its current canonical indentation.

        Nodes remain independent from output services; callers select the writer.
        """
        return self.write(node, depth=len(node.path))

    def write(self, root, *, depth=0):
        """Serialize a tree without modifying its nodes.

        The explicit pending stack emits children in source order and never updates node fields or source locations.
        """
        pending = (
            [(root, depth)]
            if root.kind != "root"
            else [(node, depth) for node in reversed(root.nodes)]
        )
        lines = []
        while pending:
            node, level = pending.pop()
            prefix = "    " * level
            if isinstance(node, str):
                lines.append(prefix + node)
                continue
            if not node.active:
                continue
            if node.is_comment_only:
                lines.append(prefix + node.comment)
                continue
            comment = " " + node.comment if node.comment else ""
            if node.is_block:
                lines.append(prefix + node.header + " {" + comment)
                pending.append(("}", level))
                pending.extend(
                    (child, level + 1) for child in reversed(node.nodes)
                )
            elif node.header:
                lines.append(prefix + node.header + ";" + comment)
        return "\n".join(lines) + ("\n" if lines else "")

    def render(self, root, parser, policy):
        """Render deployment output without changing the configuration tree.

        Empty-block and state-prefix policies apply only to this final view.
        """
        renderer = _PolicyWriter(parser, policy)
        lines = [renderer._render_node(node, 0) for node in root.nodes]
        return "\n".join(line for line in lines if line) + "\n"

    def effective(self, node: JuniperNode, parser, depth: int = 0) -> str:
        """渲染仅含当前生效节点的语义视图。

        该视图供分析使用且不触发空块策略，避免分析结果改变最终配置输出。
        """
        if not parser.is_effective(node):
            return ""
        indent = "    " * depth
        if not node.is_block:
            return indent + parser.output_header(node.header) + ";"
        lines = [indent + parser.output_header(node.header) + " {"]
        for child in node.nodes:
            rendered = self.effective(child, parser, depth + 1)
            if rendered:
                lines.append(rendered)
        lines.append(indent + "}")
        return "\n".join(lines)


class _PolicyWriter:
    """Apply deployment policies while formatting a Junos tree.

    The helper owns only read-only policy dependencies, never a document tree.
    """

    def __init__(self, parser, policy):
        """Bind shared header semantics and the selected empty-block policy.

        Keeping these inputs explicit avoids mutating node fields during output.
        """
        self._parser, self.policy = parser, policy

    def _render_node(
        self, node: JuniperNode, depth: int, ancestors: tuple[str, ...] = ()
    ) -> str:
        """按深度递归渲染一个活动 Junos 节点及其子树。

        叶子直接输出 header，块节点补齐缩进和大括号；原配置中的空层级和
        由叶子升级的空块默认输出为分号语句，YAML 可指定保留大括号或省略；
        新建的空容器不输出；omit 路径上的注释不单独使空块保留。
        逻辑停用节点跳过。
        """
        if not node.active:
            return ""
        indent = "    " * depth
        if not node.is_block:
            return self._render_leaf(node, indent)
        path = (*ancestors, self._parser.base_header(node.header))
        opening = indent + self._parser.output_header(node.header) + " {"
        if node.comment:
            opening += " " + node.comment
        children, has_statement, standalone_comments = self._render_children(
            node, depth, path
        )
        if not has_statement:
            return self._render_empty_block(
                node,
                indent,
                opening,
                path,
                standalone_comments,
            )
        return "\n".join([opening, *children, indent + "}"])

    def _render_leaf(self, node: JuniperNode, indent: str) -> str:
        """渲染叶子节点及其行内注释。

        protect 前缀在输出时移除，inactive 状态则继续保留以维持原节点是否生效。
        """
        header = self._parser.output_header(node.header)
        if header:
            header += ";"
        if node.comment:
            header = f"{header} {node.comment}" if header else node.comment
        return indent + header

    def _render_children(
        self,
        node: JuniperNode,
        depth: int,
        path: tuple[str, ...],
    ) -> tuple[list[str], bool, list[str]]:
        """渲染子节点并统计有效语句和独立注释。

        分开记录两种内容，使空块策略只由真实语句决定，而注释仍可随块输出。
        """
        print_sections: list[str] = []
        has_statement = False
        standalone_comments: list[str] = []
        for child in node.children or []:
            rendered = self._render_node(child, depth + 1, path)
            if not rendered:
                continue
            print_sections.append(rendered)
            if child.is_block or child.header.strip():
                has_statement = True
            elif child.is_comment_only:
                standalone_comments.append(child.comment)
        return print_sections, has_statement, standalone_comments

    def _render_empty_block(
        self,
        node: JuniperNode,
        indent: str,
        opening: str,
        path: tuple[str, ...],
        standalone_comments: list[str],
    ) -> str:
        """按路径策略渲染源码中声明的空块。

        只有源码中原本存在或由叶子升级的块才按镜像策略输出，避免转换过程新建的
        空容器污染结果；独立注释仍随所选语法保留，以免删除用户信息。
        """
        if not node.declared_in_source:
            return ""
        action = self.policy.action_for(path)
        if action == EmptyBlockAction.OMIT:
            return ""
        if action == EmptyBlockAction.SEMICOLON:
            statement = indent + self._parser.output_header(node.header) + ";"
            if node.comment:
                statement += " " + node.comment
            return "\n".join(
                [
                    *(indent + comment for comment in standalone_comments),
                    statement,
                ]
            )
        return "\n".join(
            [
                opening,
                *(indent + "    " + comment for comment in standalone_comments),
                indent + "}",
            ]
        )
