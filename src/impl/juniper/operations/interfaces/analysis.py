"""Juniper Junos 接口规格与只读分析。"""

from __future__ import annotations

import re

from impl.common.domain.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_unit,
)
from impl.common.support.interface_cache import copy_interface_specs
from impl.common.support.interface_lookup import interface_lookup

from ...document import JunosDocument
from .access import (
    find_inner_vlan,
    find_vlan,
    interface_name,
    interface_nodes,
    resolve_interface,
    unit_nodes,
    unit_number,
)
from .business import (
    MAX_VLAN_ID,
    bundle_binding_is_ambiguous,
    interface_has_business,
)


def interface_specs(document: JunosDocument) -> list[InterfaceSpec]:
    """提取 Junos 接口或 unit 的父子关系、类型和 VLAN。

    有 unit 时每个 unit 是独立业务对象，无 unit 时父口本身是对象。
    """
    result: list[InterfaceSpec] = []
    for node in interface_nodes(document):
        parent = interface_name(document, node)
        units = unit_nodes(document, node)
        if not units:
            result.append(
                InterfaceSpec(
                    parent,
                    parent,
                    None,
                    None,
                    document._parser.interface_kind(parent),
                )
            )
            continue
        for unit in units:
            number = unit_number(document, unit)
            vlan = find_vlan(document, unit)
            if (
                vlan is None
                and parent.lower() == "irb"
                and number.isdigit()
                and 1 <= int(number) <= MAX_VLAN_ID
            ):
                vlan = int(number)
            result.append(
                InterfaceSpec(
                    name=f"{parent}.{number}",
                    parent=parent,
                    unit=number,
                    vlan=vlan,
                    kind=document._parser.interface_kind(parent),
                    inner_vlan=find_inner_vlan(document, unit),
                )
            )
    return result


def interface_kind(document: JunosDocument, name: str) -> InterfaceKind:
    """按 Junos 名称规则返回接口类别。

    分类只依赖名称和文档解析缓存。
    """
    return document._parser.interface_kind(name)


def bundle_members(document: JunosDocument) -> dict[str, str]:
    """返回物理接口到 ``ae`` 聚合接口的映射。

    只识别物理接口中的 ``802.3ad`` 绑定。
    """
    result: dict[str, str] = {}
    for node in interface_nodes(document):
        parent = interface_name(document, node)
        if document._parser.interface_kind(parent) != InterfaceKind.PHYSICAL:
            continue
        rendered = document.writer.effective(node, document._parser)
        match = re.search(r"\b802\.3ad\s+(ae\d+)\s*;", rendered)
        if match:
            result[parent] = match.group(1)
    return result


def logical_names_under(document: JunosDocument, parent: str) -> list[str]:
    """列出父接口对应的全部已配置业务名称。

    有 unit 时只返回 unit，无 unit 时返回父口。
    """
    parent = document._parser.canonical_interface(parent)
    indexed = interface_lookup(document)
    if indexed is not None:
        return indexed.logical_names(parent)
    return sorted(
        {
            spec.name
            for spec in document.interfaces.analysis.interface_specs()
            if spec.parent == parent
        },
        key=lambda value: (interface_unit(value) is not None, value),
    )


class JunosInterfaceAnalysis:
    """提供 Junos 只读接口查询能力。

    分析结果通过 Document 缓存读取，能力对象不保存树状态。
    """

    __slots__ = ("_document",)

    def __init__(self, document: JunosDocument):
        """绑定一次调用使用的文档。

        构造函数不读取配置或创建缓存。
        """
        self._document = document

    def interface_specs(self) -> list[InterfaceSpec]:
        """返回当前文档的接口规格。

        结果由 Document 缓存失效并复制后返回。
        """
        return self._document._read_interface_analysis(
            "interface_specs",
            lambda: interface_specs(self._document),
            copy_interface_specs,
        )

    def interface_kind(self, name: str) -> InterfaceKind:
        """返回接口的业务类别。

        分类使用厂商解析缓存。
        """
        return interface_kind(self._document, name)

    def bundle_members(self) -> dict[str, str]:
        """返回聚合成员映射。

        结果由 Document 缓存失效并复制后返回。
        """
        return self._document._read_interface_analysis(
            "bundle_members",
            lambda: bundle_members(self._document),
            dict,
        )

    def interface_has_business(self, name: str) -> bool:
        """判断成员接口是否承载独立业务。

        查询只读取当前树。
        """
        return interface_has_business(self._document, name)

    def bundle_binding_is_ambiguous(self, name: str) -> bool:
        """判断成员是否存在多个聚合绑定。

        不唯一绑定应在迁移前阻断。
        """
        return bundle_binding_is_ambiguous(self._document, name)

    def resolve_interface(self, value: str) -> str:
        """返回规范接口名称。

        规范化规则与配置树内部查找一致。
        """
        return resolve_interface(self._document, value)

    def logical_names_under(self, parent: str) -> list[str]:
        """返回父接口及其逻辑单元名称。

        输出顺序稳定，可直接用于映射。
        """
        return logical_names_under(self._document, parent)

    def business_interface_names(self) -> set[str]:
        """返回承载有效业务的接口集合。

        结果由 Document 缓存失效并复制后返回。
        """
        from .business import business_interface_names

        return self._document._read_interface_analysis(
            "business_interface_names",
            lambda: business_interface_names(self._document),
            set,
        )
