"""Small visitor and listener protocols for configuration trees."""


class ConfigVisitor:
    """Let business visitors decide whether and how to descend.

    Subclasses override node callbacks and explicitly call visit_children when they want to descend.
    """

    def visit(self, node):
        """Visit a root or ordinary node when the selection permits it.

        Root callbacks are distinct from ordinary commands so document-wide analyses need no sentinel-header checks.
        """
        if not self.should_visit(node):
            return None
        if node.kind == "root":
            return self.visit_configuration(node)
        return self.visit_node(node)

    def should_visit(self, node):
        """Allow subclasses to prune a complete subtree.

        Returning false excludes both the selected node and all of its descendants.
        """
        return True

    def visit_configuration(self, node):
        """Visit a virtual document root.

        The default root behavior delegates to the same ordered child traversal used by ordinary nodes.
        """
        return self.visit_children(node)

    def visit_node(self, node):
        """Visit an ordinary configuration node.

        An overriding callback can omit visit_children to stop at the current command.
        """
        return self.visit_children(node)

    def visit_children(self, node):
        """Visit direct children in order; subclasses may stop descent.

        Each child is dispatched through accept so should_visit and overridden callbacks remain effective.
        """
        for child in node.nodes:
            child.accept(self)


class ConfigListener:
    """Receive traversal events without managing traversal.

    Listeners observe traversal; structural modifications should be collected separately and applied afterward.
    """

    def enter_node(self, node):
        """Handle entry into a node.

        The walker invokes this hook before visiting children, allowing a listener to push scope information.
        """

    def exit_node(self, node):
        """Handle exit after all children have been visited.

        The matching exit hook allows listeners to discard scope information after descendants complete.
        """


class ConfigTreeWalker:
    """Deliver balanced events using an explicit traversal stack.

    Pending exit events ensure every entered node receives an exit event after its descendants.
    """

    def walk(self, listener, node):
        """Walk a tree in source order, including its root.

        The explicit stack avoids recursion limits and reverses pending children to preserve source order.
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
