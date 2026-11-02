"""面向 Junos 配置语法的有状态扫描器和栈式 Parser。"""

import re

from ...common.config_ast import SourceLocation
from .ast import JuniperNode


class JunosConfigurationParseError(ValueError):
    """报告结构无效的 Junos 配置。

    原始行列号在发生任何配置编辑之前定位输入问题。
    """


_TOKEN = re.compile(
    r"""(?P<space>\s+)|(?P<line>\#[^\r\n]*)|(?P<block>/\*[\s\S]*?\*/)|(?P<quote>"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*')|(?P<delimiter>[{};])|(?P<word>[^\s{};"'\#/]+|/(?!\*))"""
)


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
        """使用定界符 token 和显式块栈构造配置树。

        返回的根节点拥有全部节点；定界符错误或未闭合区域会在返回树前失败。
        """
        root = JuniperNode("<root>", [], kind="root")
        stack, buffer, position, line, column = [root], [], 0, 1, 1
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
            if kind in {"line", "block"} and not buffer:
                if previous is not None and previous_line == line:
                    previous.comment = (
                        previous.comment + " " if previous.comment else ""
                    ) + value
                else:
                    stack[-1].children.append(
                        JuniperNode(
                            "", kind="comment", comment=value, location=location
                        )
                    )
            elif kind == "delimiter":
                previous = self._delimiter(
                    value, buffer, stack, start or location
                )
                previous_line, buffer, start = line, [], None
            elif kind != "space" or buffer:
                if start is None:
                    start = location
                buffer.append(value)
            position = match.end()
            line += value.count("\n")
            column = (
                len(value.rsplit("\n", 1)[-1]) + 1
                if "\n" in value
                else column + len(value)
            )
        if "".join(buffer).strip() or len(stack) != 1:
            raise JunosConfigurationParseError(
                f"行 {line} 列 {column}: 缺少分号或大括号不平衡"
            )
        from ...common.config_ast import bind_parents

        return bind_parents(root)

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
