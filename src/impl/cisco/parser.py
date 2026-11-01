"""Line-oriented IOS XR parser with explicit group and banner states."""

import re

from impl.common.config_ast import SourceLocation

from .ast import CiscoNode


class ConfigurationParseError(ValueError):
    """Report malformed configuration structure and its original position.

    Callers stop adaptation when the structural interpretation of the input cannot be trusted.
    """


class CiscoParser:
    """Build a mutable tree directly from configuration lines.

    The parser handles structural CLI boundaries while leaving unknown command bodies uninterpreted.
    """

    def validate_header(self, node, header):
        """Reject command text that would change structural boundaries.

        A header edit must not introduce additional statements or silently change the node’s structural role.
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
        """Parse indentation, separators, groups, and opaque banners.

        The resulting root owns all nodes; malformed delimiters or unfinished opaque regions fail before returning a tree.
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
        """Normalize command whitespace while preserving banner body spaces.

        Trailing whitespace can be part of an unfinished banner's first body line.
        """
        header = raw.rstrip("\r\n").lstrip()
        return (
            header if re.match(r"banner\s", header, re.I) else header.rstrip()
        )

    @staticmethod
    def _require_group(group, number):
        """Reject a closing group delimiter outside a group body.

        An unmatched end-group cannot be treated as an ordinary command because it changes containment.
        """
        if group is None:
            raise ConfigurationParseError(f"行 {number}: unexpected end-group")

    @staticmethod
    def _attach(parent, node):
        """Attach a command and promote an ordinary parent to a block.

        Promotion preserves the distinction between an empty block and a statement with no descendants.
        """
        parent.children.append(node)
        node.parent = parent
        if parent.kind == "statement":
            parent.kind = "block"

    @staticmethod
    def _group_indent(indent, base, header):
        """Normalize unindented group contents relative to their first command.

        IOS XR groups may contain commands at column zero even though those commands belong inside the group.
        """
        if base is None and not header.startswith("!"):
            base = indent
        return max(1, indent - (base or 0) + 1), base

    @staticmethod
    def _comment_parent(stack, indent):
        """Choose comment ownership without changing the command stack.

        Comments retain their containing scope without changing the parent of the next executable command.
        """
        for level, parent in reversed(stack):
            if level < indent:
                return parent
        return stack[0][1]

    def _node(self, header, number, lines, column):
        """Create one node, consuming a complete banner when necessary.

        Command text stays generic; only structural categories receive special handling here.
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
        """Consume opaque banner text up to its declared delimiter.

        Banner contents can look like executable commands and therefore must remain outside semantic traversal.
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
