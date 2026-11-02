"""按行解析 IOS XR 配置，并显式维护 group 和 banner 状态。"""

import re

from ...common.config_ast import SourceLocation
from .ast import CiscoNode


class ConfigurationParseError(ValueError):
    """报告错误配置结构及其原始位置。

    当输入的结构解释不可信时，调用方必须停止后续适配。
    """


class CiscoParser:
    """直接根据配置文本行构造可变配置树。

    Parser 处理 CLI 结构边界，但不解释未知命令体。
    """

    def validate_header(self, node, header):
        """拒绝会改变结构边界的命令文本。

        header 编辑不能引入额外语句，也不能静默改变节点的结构角色。
        """
        if (
            header.lower().startswith("group ")
            and node.parent is not None
            and node.parent.kind == "root"
            and node.kind != "group"
        ):
            raise ConfigurationParseError(
                "Use subtree replacement to create a group"
            )
        if node.kind == "group" and not header.lower().startswith("group "):
            raise ConfigurationParseError(
                "Group headers must retain the group keyword"
            )
        if node.kind == "opaque":
            raise ConfigurationParseError(
                "Use replace_subtree to edit a banner"
            )
        if header.lower() == "end-group":
            raise ValueError("end-group is a structural delimiter")

    def parse(self, text):
        """解析缩进、分隔符、group 和不透明 banner。

        返回的根节点拥有全部节点；分隔符错误或未闭合的不透明区间会在返回树前失败。
        """
        root = CiscoNode("<root>", [], kind="root")
        stack, group, base = [(-1, root)], None, None
        lines = iter(
            enumerate(text.lstrip("\ufeff").splitlines(keepends=True), 1)
        )
        for number, raw in lines:
            expanded, header = raw.expandtabs(8), self._header(raw)
            if not header.strip():
                continue
            indent = len(expanded) - len(expanded.lstrip())
            if header.lower() == "end-group":
                self._require_group(group, number)
                stack, group, base = [(-1, root)], None, None
                continue
            if group is not None:
                indent, base = self._group_indent(indent, base, header)
            if header.startswith("!") and header != "!":
                node = self._node(
                    header, number, lines, len(raw) - len(raw.lstrip()) + 1
                )
                parent = self._comment_parent(stack, indent)
                parent.children.append(node)
                node.parent = parent
                continue
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            node = self._node(
                header, number, lines, len(raw) - len(raw.lstrip()) + 1
            )
            parent = stack[-1][1]
            self._attach(parent, node)
            if node.kind not in {"comment", "separator", "opaque"}:
                stack.append((indent, node))
            if header.lower().startswith("group ") and parent is root:
                node.kind, group, base = "group", node, None
        if group is not None:
            raise ConfigurationParseError(
                f"行 {group.location.line}: unclosed group"
            )
        return root

    @staticmethod
    def _header(raw):
        """规范化命令空白，同时保留 banner 正文中的空格。

        尚未结束的 banner 第一行正文可能包含有意义的行尾空白。
        """
        header = raw.rstrip("\r\n").lstrip()
        return (
            header if re.match(r"banner\s", header, re.I) else header.rstrip()
        )

    @staticmethod
    def _require_group(group, number):
        """拒绝 group 之外的结束分隔符。

        孤立的 ``end-group`` 会改变包含关系，不能当作普通命令处理。
        """
        if group is None:
            raise ConfigurationParseError(f"行 {number}: unexpected end-group")

    @staticmethod
    def _attach(parent, node):
        """挂接命令，并把普通父节点提升为块。

        提升后仍能区分空块和没有后代的普通语句。
        """
        parent.children.append(node)
        node.parent = parent
        if parent.kind == "statement":
            parent.kind = "block"

    @staticmethod
    def _group_indent(indent, base, header):
        """相对于 group 的第一条命令规范化未缩进内容。

        IOS XR group 中的命令即使位于第零列，也可能属于该 group。
        """
        if base is None and not header.startswith("!"):
            base = indent
        return max(1, indent - (base or 0) + 1), base

    @staticmethod
    def _comment_parent(stack, indent):
        """选择注释所属父节点，且不改变命令栈。

        注释保留当前作用域，但不会影响下一条可执行命令的父节点。
        """
        for level, parent in reversed(stack):
            if level < indent:
                return parent
        return stack[0][1]

    def _node(self, header, number, lines, column):
        """创建一个节点，并在需要时读取完整 banner。

        命令文本保持通用，只有结构类别在这里获得特殊处理。
        """
        kind, comment = "statement", None
        if header == "!":
            kind = "separator"
        elif header.startswith("!"):
            kind, comment, header = "comment", header, ""
        elif re.match(r"banner\s", header, re.I):
            header, kind = self._banner(header, number, lines), "opaque"
        elif re.match(
            r"^(interface|router|vrf|l2vpn|mpls|username|line|segment-routing|telemetry)\b",
            header,
            re.I,
        ):
            kind = "block"
        return CiscoNode(
            header,
            kind=kind,
            comment=comment,
            location=SourceLocation(number, column),
            declared_in_source=True,
        )

    def _banner(self, header, number, lines):
        """读取不透明 banner 文本，直到声明的结束分隔符。

        banner 内容可能看起来像可执行命令，因此必须排除在语义遍历之外。
        """
        match = re.match(r"banner\s+\S+\s+(\^C|\S)(.*)$", header, re.I)
        if not match:
            raise ConfigurationParseError(
                f"行 {number}: missing banner delimiter"
            )
        delimiter, body = match.groups()
        parts = [header]
        if delimiter in body:
            return header
        for _, raw in lines:
            parts.append(raw.rstrip("\r\n"))
            if delimiter in raw:
                return "\n".join(parts)
        raise ConfigurationParseError(
            f"行 {number}: banner 缺少结束分隔符 {delimiter}"
        )
