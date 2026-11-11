"""Juniper Junos 接口树修改。"""

from __future__ import annotations

from ....common.domain.outcomes import InterfaceAdaptationOutcome
from ....common.support.errors import require_invariant
from ...support.interface_commands import BUNDLE_OPTION_NAMES
from ...syntax.ast import JuniperNode
from .access import (
    find_interface_node,
    interface_name,
    interfaces_block,
    named_interface_nodes,
)


def remove_interface(
    document, name: str, include_children: bool = False
) -> None:
    """逻辑停用指定接口所在的 Junos 父节点。

    unit 属于父节点子树，因此会一起停用。
    """
    node = find_interface_node(document, name)
    if node:
        node.active = False


def strip_bundle_options(document, children: list[JuniperNode]):
    """移除脱离 ae 后不再成立的聚合 options。

    迁移到普通目标口后不应保留旧成员关系。
    """
    parser = document._parser
    return [
        child
        for child in children
        if not (
            parser.is_effective(child)
            and child.is_block
            and parser.base_header(child.header) in BUNDLE_OPTION_NAMES
        )
    ]


def rename_interface_tree(
    document,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    """重命名整个 Junos 接口节点。

    可移除聚合 options，并在目标已存在时合并重复定义。
    """
    node = find_interface_node(document, source)
    if not node:
        return
    node.header = document._parser.canonical_interface(target)
    if strip_bundle and node.is_block:
        node.children = strip_bundle_options(document, node.children)
    merge_duplicate_interface(document, node)


def clone_interface_tree(
    document,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    """深拷贝源 Junos 接口树到目标节点。

    源不存在时仍创建空目标节点，便于后续迁移落位。
    """
    source_node = find_interface_node(document, source)
    interfaces = interfaces_block(document, create=True)
    require_invariant(
        interfaces is not None and interfaces.is_block,
        "Junos interfaces 块在 create=True 后必须存在且可包含子节点",
    )
    if source_node is None:
        clone = JuniperNode(document._parser.canonical_interface(target), [])
    else:
        clone = source_node.clone()
        clone.header = document._parser.canonical_interface(target)
        clone.active = True
        if strip_bundle and clone.is_block:
            clone.children = strip_bundle_options(document, clone.children)
    interfaces.children.append(clone)
    merge_duplicate_interface(document, clone)


def merge_duplicate_interface(document, preferred: JuniperNode) -> None:
    """合并同名接口节点的非重复子树。

    保留双方不同配置，并停用首选节点之外的副本。
    """
    name = interface_name(document, preferred)
    duplicates = named_interface_nodes(document, name)
    if len(duplicates) < 2:
        return
    require_invariant(
        preferred.is_block,
        "Junos 重复接口合并的首选节点必须是块节点",
    )
    existing = {document._render_node(child, 0) for child in preferred.children}
    for node in duplicates:
        if node is preferred or not node.is_block:
            continue
        for child in node.children:
            rendered = document._render_node(child, 0)
            if rendered not in existing:
                preferred.children.append(child)
                existing.add(rendered)
        node.active = False


def ensure_parent_interface(document, name: str) -> None:
    """确保父接口存在而不重建业务子树。

    缺失时只创建空父节点。
    """
    if find_interface_node(document, name) is not None:
        return
    interfaces = interfaces_block(document, create=True)
    require_invariant(
        interfaces is not None and interfaces.is_block,
        "Junos interfaces 块必须存在且可包含子节点",
    )
    interfaces.children.append(JuniperNode(name, []))


class JunosInterfaceTransform:
    """提供 Junos 接口树修改能力。

    每个修改方法自行创建局部批量编辑事务。
    """

    __slots__ = ("_document",)

    def __init__(self, document):
        """绑定一次调用使用的文档。

        能力对象不拥有或缓存配置树。
        """
        self._document = document

    def remove(self, name: str, include_children: bool = False) -> None:
        """移除接口及其全部 unit。

        Junos unit 属于父接口子树。
        """
        with self._document.edit().bulk():
            remove_interface(self._document, name, include_children)

    def rename(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """重命名整个接口节点。

        可移除不再适用的聚合 options。
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

        只在缺失时创建空父节点。
        """
        with self._document.edit().bulk():
            ensure_parent_interface(self._document, name)

    def map_uni(
        self,
        source: str,
        parent: str,
        vlan: int,
        inner_vlan: int | None,
        *,
        original_source: str | None = None,
    ) -> InterfaceAdaptationOutcome:
        """将 UNI 源 unit 迁移为目标单层或双层业务接口。

        批量编辑统一维护缓存失效，所有后续分析读取最新接口树。
        """
        from .uni import map_uni

        with self._document.edit().bulk():
            return map_uni(
                self._document,
                source,
                parent,
                vlan,
                inner_vlan,
                original_source=original_source,
            )

    def finalize_uni_source(
        self, source: str, check_parent: bool
    ) -> InterfaceAdaptationOutcome:
        """在正常清洗完成后移除 UNI 源树，并检查父级业务是否遗漏。

        使用原有批量事务维护缓存，不改变最终网关或桥域的关系。
        """
        from .uni import finalize_uni_source

        with self._document.edit().bulk():
            return finalize_uni_source(self._document, source, check_parent)
