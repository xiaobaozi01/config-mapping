"""Cisco 配置树事务的通用编辑辅助函数。"""

from __future__ import annotations


def append_top_level(transaction, document, text: str, reason: str) -> None:
    """在终止命令前追加顶层配置，或在没有终止命令时直接追加根部。

    Editor 事务负责统一登记修改，使调用方可以在完整编辑成功后再一次性提交。
    """
    terminal = next(
        (
            node
            for node in document.root.nodes
            if node.header.lower() in {"end", "commit"}
        ),
        None,
    )
    if terminal is None:
        transaction.append_child(document.root, text, reason)
    else:
        transaction.insert_before(terminal, text, reason)


def delete_matching(
    transaction,
    nodes,
    pattern,
    *,
    match_anywhere: bool = False,
    reason: str = "删除匹配命令",
) -> int:
    """递归删除匹配正则的配置节点并返回删除对象数量。

    注释和分隔符不参与匹配，命中父节点时其活动子树会作为同一删除操作处理。
    """
    test = pattern.search if match_anywhere else pattern.match
    removed = 0
    for node in nodes:
        if node.is_formatting:
            continue
        if test(node.header):
            removed += sum(1 for _ in node.walk(include_self=True))
            transaction.delete(node, reason)
        else:
            removed += delete_matching(
                transaction,
                node.nodes,
                pattern,
                match_anywhere=match_anywhere,
                reason=reason,
            )
    return removed


__all__ = ["append_top_level", "delete_matching"]
