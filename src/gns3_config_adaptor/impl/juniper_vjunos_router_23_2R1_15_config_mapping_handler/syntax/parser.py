"""面向 Junos 配置语法的有状态扫描器和栈式 Parser。"""

import re

from ...common.config_ast import SourceLocation
from .ast import JuniperNode


class JunosConfigurationParseError(ValueError):
    """报告结构无效的 Junos 配置。

    原始行列号在发生任何配置编辑之前定位输入问题。
    """


_TOKEN = re.compile(
    r"""(?P<space>\s+)|(?P<line>\#[^\r\n]*)|(?P<block>/\*[\s\S]*?\*/)|(?P<quote>"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*')|(?P<delimiter>[{};])|(?P<bracket>[\[\]])|(?P<word>[^\s{};\[\]"'\#/]+|/(?!\*))"""
)


def _continues(text, position):
    """查看空白、注释之后是否为显式续行标点。

    只在遇到行尾时向前查看，不缓存整份配置的 token。
    """
    match = _TOKEN.match(text, position)
    while match and match.lastgroup in {"space", "line", "block"}:
        match = _TOKEN.match(text, match.end())
    return match is not None and match.group() in {"{", ";"}


def _line_end(kind, value, brackets, text, position):
    """识别列表和字符串之外的隐式语句边界。

    下一有效字符是 { 或 ; 时继续当前语句，注释中的换行不参与判断。
    """
    if brackets:
        return False
    if kind == "delimiter":
        return value == "}"
    return (
        kind == "line" or (kind == "space" and "\n" in value)
    ) and not _continues(text, position)


class JuniperParser:
    """先处理引号和注释，再解释结构标点。

    引号和注释会先被完整读取，避免其中标点影响块嵌套。
    """

    def validate_header(self, node, header):
        """拒绝会改变结构边界的命令文本。

        header 编辑不能引入额外语句，也不能静默改变节点的结构角色。
        """
        suffix = " {}" if node.is_block else ";"
        parsed = self.parse(header + suffix)
        if len(parsed.children) != 1 or parsed.children[0].header != header:
            raise ValueError("Header contains structural delimiters")

    def parse(self, text):
        """在原有扫描循环中处理显式标点和省略分号的行尾。

        缩进不决定层级；引号、列表和后接 { 或 ; 的语句允许跨行。
        """
        root = JuniperNode("<root>", [], kind="root")
        stack, buffer, comments = [root], [], []
        position, line, column, brackets = 0, 1, 1, 0
        start, previous, previous_line = None, None, 0
        text = text.lstrip("\ufeff")
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None:
                raise JunosConfigurationParseError(
                    f"行 {line} 列 {column}: unclosed quote or comment"
                )
            kind, value = match.lastgroup, match.group()
            location = SourceLocation(line, column)
            if buffer and _line_end(kind, value, brackets, text, match.end()):
                previous = self._finish(";", buffer, comments, stack, start)
                previous_line, start = line, None
            if kind in {"line", "block"}:
                if buffer:
                    comments.append((value, location))
                    buffer.append(" ")
                else:
                    inline = previous if previous_line == line else None
                    self._comment(stack[-1], inline, value, location)
            elif kind == "delimiter":
                source = start or location
                previous = self._finish(value, buffer, comments, stack, source)
                previous_line, start = line, None
            elif kind != "space" or buffer:
                start = start or location
                buffer.append(value)
            brackets += (
                (value == "[") - (value == "]") if kind == "bracket" else 0
            )
            position = match.end()
            line += value.count("\n")
            column = (
                len(value.rsplit("\n", 1)[-1]) + 1
                if "\n" in value
                else column + len(value)
            )
        start = start or SourceLocation(line, column)
        self._finish(";", buffer, comments, stack, start)
        if len(stack) != 1 or brackets:
            raise JunosConfigurationParseError(
                f"行 {line} 列 {column}: {'方括号' if brackets else '大括号'}不平衡"
            )
        from ...common.config_ast import bind_parents

        return bind_parents(root)

    def _finish(self, delimiter, buffer, comments, stack, location):
        """结束语句并复用已有节点保存注释。

        第一条注释作为行尾注释，其余保留为独立节点，沿用现有 Writer。
        """
        node = self._delimiter(delimiter, buffer, stack, location)
        for value, source in comments:
            self._comment(stack[-1], node, value, source)
        buffer.clear()
        comments.clear()
        return node

    def _comment(self, parent, previous, value, location):
        """保留行尾或独立注释。

        每个节点只附一条注释，多余注释独立保存，避免 # 吞掉后续内容。
        """
        if previous is not None and not previous.comment:
            previous.comment = value
        else:
            parent.children.append(
                JuniperNode(
                    "", kind="comment", comment=value, location=location
                )
            )

    def _delimiter(self, delimiter, buffer, stack, location):
        """结束语句或改变当前块嵌套层级。

        只有扫描器识别出的定界符会进入该方法，字符串内部标点不会改变栈。
        """
        header = "".join(buffer).strip()
        if delimiter == "}":
            if header or len(stack) == 1:
                raise JunosConfigurationParseError(
                    f"Junos 配置第 {location.line} 行出现多余右大括号"
                )
            return stack.pop()
        if delimiter == ";" and not header:
            return None
        if not header:
            raise JunosConfigurationParseError(
                f"行 {location.line}: missing command"
            )
        node = JuniperNode(
            header,
            kind="block" if delimiter == "{" else "statement",
            location=location,
            declared_in_source=True,
        )
        stack[-1].children.append(node)
        if delimiter == "{":
            stack.append(node)
        return node
