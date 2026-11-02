"""Juniper Junos 接口树只读访问。"""

from ....common.domain.interface import interface_parent
from ....common.support.interface_lookup import interface_lookup
from ...document import JuniperNode, JunosDocument
from ...syntax.ast import top_block


def interfaces_block(
    document: JunosDocument, create: bool = False
) -> JuniperNode | None:
    """返回顶层 ``interfaces`` 块，并可按需创建。

    辅助函数只操作当前树，不自行提交编辑事务。
    """
    return top_block(document.root, "interfaces", create=create)


def interface_nodes(document: JunosDocument) -> list[JuniperNode]:
    """返回活动且可承载子配置的接口节点。

    非块、注释和 inactive 接口不会进入分析。
    """
    indexed = interface_lookup(document)
    if indexed is not None:
        return indexed.all_nodes()
    block = interfaces_block(document)
    if not block or not block.is_block:
        return []
    return [
        node
        for node in block.nodes
        if document._parser.is_effective(node) and node.is_block
    ]


def interface_name(document: JunosDocument, node: JuniperNode) -> str:
    """返回接口节点的规范父接口名称。

    状态前缀和空白在匹配前统一移除。
    """
    return document._parser.canonical_interface(
        document._parser.base_header(node.header).split()[0]
    )


def unit_nodes(
    document: JunosDocument, interface: JuniperNode
) -> list[JuniperNode]:
    """返回接口节点下的有效 unit。

    空叶子 unit 仍代表逻辑接口，必须保留。
    """
    if not interface.is_block:
        return []
    return [
        node
        for node in interface.nodes
        if document._parser.is_effective(node)
        and document._parser.is_unit(node.header)
    ]


def unit_number(document: JunosDocument, node: JuniperNode) -> str:
    """从 unit 节点提取原始逻辑编号。

    字符串形式保留前导零等原始信息。
    """
    return document._parser.unit_number(node.header)


def find_vlan(document: JunosDocument, unit: JuniperNode) -> int | None:
    """返回 unit 的单层或 QinQ 外层 VLAN。

    未配置 VLAN 时返回空值。
    """
    if not unit.is_block:
        return None
    for child in unit.nodes:
        if not document._parser.is_effective(child):
            continue
        vlan = document._parser.vlan(child.header)
        if vlan is not None:
            return vlan
    return None


def find_inner_vlan(document: JunosDocument, unit: JuniperNode) -> int | None:
    """返回 unit 的 QinQ 内层 VLAN。

    单层或无 VLAN unit 返回空值。
    """
    if not unit.is_block:
        return None
    for child in unit.nodes:
        if not document._parser.is_effective(child):
            continue
        vlan = document._parser.inner_vlan(child.header)
        if vlan is not None:
            return vlan
    return None


def walk_effective_nodes(document: JunosDocument, node: JuniperNode):
    """遍历节点下的有效后代。

    inactive 和注释子节点不会进入结果。
    """
    for child in node.nodes:
        if document._parser.is_effective(child):
            yield child
            yield from walk_effective_nodes(document, child)


def resolve_interface(document: JunosDocument, value: str) -> str:
    """把外部 Junos 接口名转换为解析器使用的规范形式。

    拓扑和配置必须经过同一规范化过程。
    """
    return document._parser.canonical_interface(value)


def find_interface_node(
    document: JunosDocument, name: str
) -> JuniperNode | None:
    """按规范父接口名查找第一个有效接口节点。

    unit 名会先归一到父接口，因为 Junos unit 位于父节点内部。
    """
    canonical = document._parser.canonical_interface(interface_parent(name))
    indexed = interface_lookup(document)
    if indexed is not None:
        nodes = indexed.matching(canonical)
        return nodes[0] if nodes else None
    return next(
        (
            node
            for node in interface_nodes(document)
            if interface_name(document, node) == canonical
        ),
        None,
    )


def named_interface_nodes(document, name):
    """返回重复的父接口定义，且不扫描无关接口。

    名称规范化仍由调用方负责，与合并逻辑保持一致，避免两处各自规范化导致结果不一致。
    """
    indexed = interface_lookup(document)
    if indexed is not None:
        return indexed.matching(name)
    return [
        node
        for node in interface_nodes(document)
        if interface_name(document, node) == name
    ]
