"""确定性的 Cisco 配置序列化。"""

from ...common.rules.empty_blocks import EmptyBlockAction


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
    """按每层一个空格输出 Cisco 配置树。

    缩进由树深度决定，group 的结束标记由显式 group 节点生成。
    """

    def render(self, root, policy):
        """以只读方式序列化活动树，并应用空块策略。

        输出过滤不会创建 Editor、树副本或文档修订。
        """
        return self.write(root, policy=policy)

    def subtree(self, node):
        """按当前规范缩进序列化子树。

        节点不依赖输出服务，由调用方选择具体 Writer。
        """
        return self.write(node, depth=len(node.path))

    def write(self, root, *, depth=0, policy=None):
        """序列化文档或子树，且不修改节点。

        显式待处理栈按源码顺序输出子节点，不会更新节点字段或源位置。
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
