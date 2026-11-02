"""Cisco IOS XR 接口树只读访问。"""

from impl.common.support.interface_lookup import interface_lookup

from ...document import CiscoDocument, CiscoNode


def interface_nodes(document: CiscoDocument) -> list[CiscoNode]:
    """返回仍有效且具有接口名的 IOS XR 配置块。

    后续分析和修改统一从这里取数，避免处理逻辑删除节点或普通顶层配置。
    """
    indexed = interface_lookup(document)
    if indexed is not None:
        return indexed.all_nodes()
    parser = document._parser
    return [
        block
        for block in document.root.nodes
        if block.active and parser.interface_name(block.header)
    ]


def resolve_interface(document: CiscoDocument, value: str) -> str:
    """把外部接口名转换为文档解析器使用的规范名称。

    拓扑和配置必须经过同一规范化过程才能可靠匹配。
    """
    return document._parser.canonical_interface(value)


def find_interface_node(document: CiscoDocument, name: str) -> CiscoNode | None:
    """按规范名称查找第一个活动接口块。

    非活动块不会成为后续修改目标。
    """
    parser = document._parser
    canonical = parser.canonical_interface(name)
    indexed = interface_lookup(document)
    if indexed is not None:
        nodes = indexed.matching(canonical)
        return nodes[0] if nodes else None
    return next(
        (
            block
            for block in interface_nodes(document)
            if parser.interface_name(block.header) == canonical
        ),
        None,
    )


def interface_tree_nodes(document, name):
    """按源码顺序选取规范接口及其点号子接口。

    回退路径保留精确前缀语义，即使接口名本身嵌套了多级点号也能正确匹配。
    """
    indexed = interface_lookup(document)
    if indexed is not None:
        return indexed.matching(name, tree=True)
    return [
        node
        for node in interface_nodes(document)
        if node.interface_name == name
        or node.interface_name.startswith(name + ".")
    ]


def named_interface_nodes(document, name):
    """返回某个规范接口名的全部活动定义。

    重复定义合并不能把这些节点压成一个字典值，否则会丢失各自的源码位置和原始语句。
    """
    indexed = interface_lookup(document)
    if indexed is not None:
        return indexed.matching(name)
    return [
        node
        for node in interface_nodes(document)
        if node.interface_name == name
    ]
