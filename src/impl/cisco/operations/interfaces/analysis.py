"""Cisco IOS XR 接口规格与只读分析。"""

from __future__ import annotations

import re

from impl.common.domain.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    interface_unit,
)
from impl.common.support.errors import require_invariant
from impl.common.support.interface_cache import copy_interface_specs

from ...document import CiscoDocument
from .access import interface_nodes, resolve_interface
from .business import (
    bundle_binding_is_ambiguous,
    gateway_inner_vlan_hints,
    interface_has_business,
)


def interface_specs(document: CiscoDocument) -> list[InterfaceSpec]:
    """提取接口名、父子关系、类型及 dot1q/QinQ 标签。

    BVI 业务 VLAN 只从无歧义的 bridge-domain 绑定推导。
    """
    parser = document._parser
    result: list[InterfaceSpec] = []
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        require_invariant(
            name is not None,
            "Cisco 接口节点集合中的节点必须包含接口名",
        )
        vlan = None
        inner_vlan = None
        for node in block.walk():
            match = re.match(
                r"encapsulation\s+dot1q\s+(\d+)"
                r"(?:\s+second-dot1q\s+(\d+))?",
                node.header,
                re.IGNORECASE,
            )
            if match:
                vlan = int(match.group(1))
                inner_vlan = int(match.group(2)) if match.group(2) else None
                break
        result.append(
            InterfaceSpec(
                name=name,
                parent=interface_parent(name),
                unit=interface_unit(name),
                vlan=vlan,
                kind=parser.interface_kind(name),
                inner_vlan=inner_vlan,
            )
        )

    gateway_hints = (
        gateway_inner_vlan_hints(document, result)
        if any(spec.kind == InterfaceKind.GATEWAY for spec in result)
        else {}
    )
    for spec in result:
        if spec.kind == InterfaceKind.GATEWAY:
            spec.inner_vlan = gateway_hints.get(spec.name)
    return result


def interface_kind(document: CiscoDocument, name: str) -> InterfaceKind:
    """按 IOS XR 名称规则返回接口类别。

    分类只依赖名称和文档解析缓存。
    """
    return document._parser.interface_kind(name)


def bundle_members(document: CiscoDocument) -> dict[str, str]:
    """返回物理父接口到 ``Bundle-Ether`` 的映射。

    只读取物理父口上的唯一 ``bundle id``。
    """
    parser = document._parser
    result: dict[str, str] = {}
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        require_invariant(
            name is not None,
            "Cisco 接口节点集合中的节点必须包含接口名",
        )
        if (
            parser.interface_kind(name) != InterfaceKind.PHYSICAL
            or interface_unit(name) is not None
        ):
            continue
        for node in block.walk():
            match = re.match(
                r"bundle\s+id\s+(\d+)\b", node.header, re.IGNORECASE
            )
            if match:
                result[name] = f"Bundle-Ether{match.group(1)}"
                break
    return result


def logical_names_under(document: CiscoDocument, parent: str) -> list[str]:
    """列出父接口自身及全部已配置逻辑单元。

    父口排在前面，其余名称保持稳定排序。
    """
    parent = document._parser.canonical_interface(parent)
    return sorted(
        {
            spec.name
            for spec in document.interfaces.analysis.interface_specs()
            if spec.parent == parent
        },
        key=lambda value: (interface_unit(value) is not None, value),
    )


class CiscoInterfaceAnalysis:
    """提供 IOS XR 只读接口查询能力。

    分析结果通过 Document 缓存读取，能力对象不保存树状态。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
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
