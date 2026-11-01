"""Cisco interface-reference expansion over configuration nodes."""

from .semantics import _CISCO_INTERFACE_TOKEN


def replace_references(document, replacements: dict[str, list[str]]) -> None:
    """改写配置中指向迁移源接口的引用。

    展开基于原始树状态并跳过接口定义 header，避免新目标再次作为源产生级联替换。
    """
    canonical = {
        document.resolve_interface(source): list(dict.fromkeys(targets))
        for source, targets in replacements.items()
        if targets and targets != [source]
    }
    if not canonical:
        return
    source_keys = {name.lower(): name for name in canonical}

    def expand(value):
        """把一个命令文本展开为所有合法目标组合。

        多源同时出现时笛卡尔展开并去重，可保留一对多映射的完整语义。
        """
        matches = [
            match
            for match in _CISCO_INTERFACE_TOKEN.finditer(value)
            if document.resolve_interface(match.group()).lower() in source_keys
        ]
        variants, position = [""], 0
        for match in matches:
            targets = canonical[
                source_keys[document.resolve_interface(match.group()).lower()]
            ]
            variants = [
                current + value[position : match.start()] + target
                for current in variants
                for target in targets
            ]
            position = match.end()
        return list(
            dict.fromkeys(current + value[position:] for current in variants)
        )

    transaction = document.edit()
    for node in document.root.nodes:
        _rewrite_reference_node(
            transaction,
            node,
            expand,
            skip_header=node.interface_name is not None,
        )
    transaction.apply()


def _expanded_reference_text(node, expand, header, writer):
    """递归渲染使用指定 header 变体的节点子树。

    子节点也逐层应用同一映射，使父命令展开后内部引用保持对应目标。
    """
    lines = [" " * len(node.path) + header]
    for child in node.nodes:
        if child.is_formatting:
            lines.extend(writer.subtree(child).rstrip("\r\n").splitlines())
        else:
            for variant in expand(child.header):
                lines.extend(
                    _expanded_reference_text(child, expand, variant, writer)
                    .rstrip("\r\n")
                    .splitlines()
                )
    return "\n".join(lines) + "\n"


def _rewrite_reference_node(transaction, node, expand, *, skip_header=False):
    """递归登记节点及其子树的引用替换操作。

    多目标替换改为整棵子树展开，单目标只改 header，确保编辑区间不重叠。
    """
    if node.is_formatting:
        return
    headers = [node.header] if skip_header else expand(node.header)
    if len(headers) > 1:
        text = "".join(
            _expanded_reference_text(
                node, expand, header, transaction.document.writer
            )
            for header in headers
        )
        transaction.replace_subtree(node, text, "展开一对多接口引用")
        return
    if headers[0] != node.header:
        transaction.replace_header(node, headers[0], "改写接口引用")
    for child in node.nodes:
        _rewrite_reference_node(transaction, child, expand)
