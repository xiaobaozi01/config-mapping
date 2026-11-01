"""Stateful scanner and stack parser for Junos configuration syntax."""

import re

from impl.common.config_ast import SourceLocation

from .ast import JuniperNode


class JunosConfigurationParseError(ValueError):
    """Report a structurally invalid Junos configuration.

    The original line and column identify the input problem before any configuration edits are attempted.
    """


_TOKEN = re.compile(
    r"""(?P<space>\s+)|(?P<line>\#[^\r\n]*)|(?P<block>/\*[\s\S]*?\*/)|(?P<quote>"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*')|(?P<delimiter>[{};])|(?P<word>[^\s{};"'\#/]+|/(?!\*))"""
)


class JuniperParser:
    """Parse quoted strings and comments before interpreting punctuation.

    Quoted strings and comments are consumed before their punctuation can affect block nesting.
    """

    def validate_header(self, node, header):
        """Reject command text that would change structural boundaries.

        A header edit must not introduce additional statements or silently change the node’s structural role.
        """
        suffix = " {}" if node.is_block else ";"
        parsed = self.parse(header + suffix)
        if len(parsed.children) != 1 or parsed.children[0].header != header:
            raise ValueError("Header contains structural delimiters")

    def parse(self, text):
        """Build a tree using delimiter tokens and an explicit block stack.

        The resulting root owns all nodes; malformed delimiters or unfinished opaque regions fail before returning a tree.
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
        from impl.common.config_ast import bind_parents

        return bind_parents(root)

    def _delimiter(self, delimiter, buffer, stack, location):
        """Finish a statement or change the current block nesting.

        Only scanner-recognized delimiters reach this method, so punctuation inside strings does not change the stack.
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
