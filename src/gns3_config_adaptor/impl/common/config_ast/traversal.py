"""配置树使用的轻量 Visitor 和 Listener 协议。"""


class ConfigVisitor:
    """由业务 Visitor 决定是否以及如何继续向下遍历。

    子类重写节点回调，并在需要继续遍历时显式调用 ``visit_children``。
    """

    def visit(self, node):
        """在选择范围允许时访问根节点或普通节点。

        根节点回调与普通命令分开，使全文档分析无需检查特殊 header。
        """
        if not self.should_visit(node):
            return None
        if node.kind == "root":
            return self.visit_configuration(node)
        return self.visit_node(node)

    def should_visit(self, node):
        """允许子类跳过整棵子树。

        返回 ``False`` 时，当前节点及其全部后代都不参与遍历。
        """
        return True

    def visit_configuration(self, node):
        """访问虚拟文档根节点。

        默认行为使用与普通节点相同的有序子节点遍历逻辑。
        """
        return self.visit_children(node)

    def visit_node(self, node):
        """访问普通配置节点。

        重写该回调时可以不调用 ``visit_children``，从而停在当前命令。
        """
        return self.visit_children(node)

    def visit_children(self, node):
        """按顺序访问直接子节点，子类可以提前停止。

        每个子节点都通过 ``accept`` 分派，确保 ``should_visit`` 和重写回调继续生效。
        """
        for child in node.nodes:
            child.accept(self)


class ConfigListener:
    """接收遍历事件，但不负责控制遍历过程。

    Listener 只观察遍历；结构修改应先单独收集，再在遍历结束后统一应用。
    """

    def enter_node(self, node):
        """处理进入节点事件。

        Walker 在访问子节点前调用该钩子，Listener 可据此压入作用域信息。
        """

    def exit_node(self, node):
        """处理离开节点事件。

        对应的退出钩子让 Listener 在后代处理完成后清理作用域信息。
        """


class ConfigTreeWalker:
    """使用显式遍历栈发送成对的进入和退出事件。

    延迟退出事件保证每个已进入节点都会在后代处理完成后收到退出事件。
    """

    def walk(self, listener, node):
        """按源码顺序遍历树，并包含根节点。

        显式栈避免递归深度限制，并通过反向压入待处理子节点保持源码顺序。
        """
        pending = [(node, False)]
        while pending:
            current, exiting = pending.pop()
            if exiting:
                listener.exit_node(current)
                continue
            listener.enter_node(current)
            pending.append((current, True))
            pending.extend((child, False) for child in reversed(current.nodes))
