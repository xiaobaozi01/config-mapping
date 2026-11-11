"""Cisco IOS XR 接口树修改。"""

from __future__ import annotations

from ....common.config_ast.editor import format_fragment
from ....common.domain.outcomes import InterfaceAdaptationOutcome
from ...document import CiscoDocument, CiscoNode
from ...support.interface_commands import STRIPPABLE_AGGREGATION_ATTRIBUTES
from ...syntax.rewrite_helpers import append_top_level, delete_matching
from ...syntax.writer import CiscoWriter
from .access import (
    find_interface_node,
    interface_tree_nodes,
    named_interface_nodes,
)


def node_identity(node: CiscoNode) -> tuple[object, ...]:
    """返回节点及语义子树的稳定哈希结构。

    格式节点不参与比较，避免 ``!`` 位置差异造成重复配置。
    """
    return (
        node.header,
        node.is_block,
        tuple(
            node_identity(child)
            for child in node.nodes
            if not child.is_formatting
        ),
    )


def remove_interface(
    document: CiscoDocument, name: str, include_children: bool = False
) -> None:
    """逻辑删除指定接口及其可选子接口。

    统一事务保持分隔符和原始顺序稳定。
    """
    canonical = document._parser.canonical_interface(name)
    transaction = document.edit()
    nodes = (
        interface_tree_nodes(document, canonical)
        if include_children
        else named_interface_nodes(document, canonical)
    )
    for block in nodes:
        transaction.delete(block, "删除接口及其业务子树")
    transaction.apply()


def rename_interface_tree(document, source, target, strip_bundle=False):
    """把源接口及其子接口改名到目标父接口。

    父口和子口在同一事务中修改，移出聚合时可删除成员属性。
    """
    source, target = document._parser.canonical_interface(
        source
    ), document._parser.canonical_interface(target)
    transaction = document.edit()
    renamed = []
    for block in interface_tree_nodes(document, source):
        current = block.interface_name
        if current != source and not (
            current and current.startswith(source + ".")
        ):
            continue
        new_name = target + current[len(source) :]
        renamed.append(new_name)
        transaction.replace_header(
            block,
            f"interface {new_name}"
            + (" l2transport" if block.l2transport else ""),
            "迁移接口名称",
        )
        if strip_bundle:
            delete_matching(
                transaction,
                block.nodes,
                STRIPPABLE_AGGREGATION_ATTRIBUTES,
            )
    transaction.apply()
    for name in renamed:
        merge_duplicate_interface(document, find_interface_node(document, name))


def clone_fragment(original, name, strip_bundle):
    """生成重命名且可选剥离聚合属性的接口片段。

    先复制完整子树再格式化，保留注释和未知语句。
    """
    candidate = CiscoDocument(CiscoWriter().write(original))
    root = candidate.root.nodes[0]
    transaction = candidate.edit()
    transaction.replace_header(
        root,
        f"interface {name}" + (" l2transport" if original.l2transport else ""),
        "克隆接口",
    )
    if strip_bundle:
        delete_matching(
            transaction,
            root.nodes,
            STRIPPABLE_AGGREGATION_ATTRIBUTES,
        )
    transaction.apply()
    return candidate.source + "!\n"


def clone_interface_tree(document, source, target, strip_bundle=False):
    """把源接口树复制到目标接口树。

    源接口树保持可用，目标重名定义随后合并。
    """
    source, target = document._parser.canonical_interface(
        source
    ), document._parser.canonical_interface(target)
    originals = interface_tree_nodes(document, source)
    if not originals:
        ensure_parent_interface(document, target)
        return
    fragments, names = [], []
    for original in originals:
        name = target + original.interface_name[len(source) :]
        names.append(name)
        fragments.append(clone_fragment(original, name, strip_bundle))
    transaction = document.edit()
    transaction.insert_after(originals[-1], "".join(fragments), "克隆接口树")
    transaction.apply()
    for name in names:
        merge_duplicate_interface(document, find_interface_node(document, name))


def merge_duplicate_interface(document, preferred):
    """把同名接口块的唯一子命令合并到首选块。

    语义身份去重保留双方不同配置，并停用冗余块。
    """
    if preferred is None:
        return
    name = preferred.interface_name
    duplicates = named_interface_nodes(document, name)
    if len(duplicates) < 2:
        return
    seen, fragments = set(), []
    for block in duplicates:
        for node in block.nodes:
            if node.is_formatting:
                continue
            identity = node_identity(node)
            if identity not in seen:
                seen.add(identity)
                fragments.append(
                    format_fragment(document.writer.subtree(node), 1)
                )
    transaction = document.edit()
    transaction.replace_subtree(
        preferred,
        preferred.header + "\n" + "".join(fragments),
        "合并同名接口块",
    )
    for block in duplicates:
        if block is not preferred:
            transaction.delete(block, "移除已合并的重复接口")
    transaction.apply()


def insert_top_level_before_end(document, text):
    """在配置终止标记前插入新的顶层接口块。

    终止命令之后的内容不属于活动配置。
    """
    transaction = document.edit()
    append_top_level(transaction, document, text, "创建目标接口")
    transaction.apply()


def ensure_parent_interface(document: CiscoDocument, name: str) -> None:
    """确保目标接口父定义存在。

    缺失时创建最小启用父口，不伪造业务配置。
    """
    canonical = document._parser.canonical_interface(name)
    if find_interface_node(document, canonical) is None:
        insert_top_level_before_end(
            document, f"interface {canonical}\n no shutdown\n!\n"
        )


class CiscoInterfaceTransform:
    """提供 IOS XR 接口树修改能力。

    每个修改方法自行管理局部事务。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定一次调用使用的文档。

        能力对象不缓存配置树。
        """
        self._document = document

    def remove(self, name: str, include_children: bool = False) -> None:
        """移除接口及其可选子接口。

        删除通过逻辑停用完成。
        """
        with self._document.edit().bulk():
            remove_interface(self._document, name, include_children)

    def rename(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """重命名整棵接口树。

        可同时剥离不再适用的聚合属性。
        """
        with self._document.edit().bulk():
            rename_interface_tree(self._document, source, target, strip_bundle)

    def clone(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """复制接口树到目标接口。

        源接口树在操作后仍可继续使用。
        """
        with self._document.edit().bulk():
            clone_interface_tree(self._document, source, target, strip_bundle)

    def ensure_parent(self, name: str) -> None:
        """确保目标父接口存在。

        只在缺失时创建最小父节点。
        """
        with self._document.edit().bulk():
            ensure_parent_interface(self._document, name)

    def map_uni(
        self, source: str, parent: str, vlan: int, inner_vlan: int | None
    ) -> InterfaceAdaptationOutcome:
        """按目标子接口类型迁移 UNI，并返回命令级兼容处理结果。

        迁移实现与普通接口改名分离，避免将源物理口配置原样带入子接口。
        """
        from .uni import map_uni

        with self._document.edit().bulk():
            return map_uni(self._document, source, parent, vlan, inner_vlan)

    def finalize_uni_source(
        self, source: str, check_parent: bool
    ) -> InterfaceAdaptationOutcome:
        """清洗完成后检查并移除原 UNI 暂存树。

        把父级剩余属性的判断推迟到现有清洗之后，避免重复维护硬件命令定义。
        """
        from .uni import finalize_uni_source

        with self._document.edit().bulk():
            return finalize_uni_source(self._document, source, check_parent)
