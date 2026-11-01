"""Deterministic Cisco configuration serialization."""

from impl.common.empty_blocks import EmptyBlockAction


def _omit_empty_block(node, ancestors, policy):
    """判断节点是否应按镜像策略省略整个空块。

    只有块自身和全部后代都为空时才省略，避免删除仍含注释或业务内容的容器。
    """
    if node.is_formatting:
        return True
    path = (*ancestors, node.header.strip())
    children_empty = all(
        _omit_empty_block(child, path, policy) for child in node.nodes
    )
    return (
        node.is_block
        and children_empty
        and policy.action_for(path) == EmptyBlockAction.OMIT
    )


class CiscoWriter:
    """Write tree structure with one space per nesting level.

    Indentation follows tree depth and group closing markers are generated from explicit group nodes.
    """

    def render(self, root, policy):
        """Apply empty-block policy while serializing the live tree read-only.

        Output filtering does not create editors, copies, or document revisions.
        """
        return self.write(root, policy=policy)

    def subtree(self, node):
        """Serialize a subtree at its current canonical indentation.

        Nodes remain independent from output services; callers select the writer.
        """
        return self.write(node, depth=len(node.path))

    def write(self, root, *, depth=0, policy=None):
        """Serialize a document or subtree without mutating it.

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
            if isinstance(node, str):
                lines.append(" " * level + node)
                continue
            if (
                policy is not None
                and not node.is_formatting
                and _omit_empty_block(node, node.path, policy)
            ):
                continue
            if not node.active:
                continue
            value = node.comment if node.is_comment_only else node.header
            line = " " * level + value
            if node.kind == "separator" and lines and lines[-1] == line:
                continue
            lines.append(line)
            if node.kind == "group":
                pending.append(("end-group", level))
            pending.extend((child, level + 1) for child in reversed(node.nodes))
        return "\n".join(lines) + ("\n" if lines else "")
