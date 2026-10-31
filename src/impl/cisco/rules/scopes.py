"""从当前解析快照选择 Visitor 的访问根，不缓存 Context。"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from impl.common.interface import InterfaceKind, interface_parent


def document_scope(document, context):
    return (document.root,)


def select_interfaces(document, targets: Iterable[str], *, include_subinterfaces: bool, kinds: frozenset[InterfaceKind]):
    from ..interfaces import interface_nodes
    parents = {interface_parent(document.resolve_interface(name)) for name in targets}
    return tuple(
        block for block in interface_nodes(document)
        if (name := block.interface_name) and interface_parent(name) in parents
        and (include_subinterfaces or name == interface_parent(name))
        and document.interface_kind(name) in kinds
    )


@dataclass(frozen=True, slots=True)
class InterfaceScope:
    """显式声明目标来源、子接口闭包和接口类型。"""

    field: Literal["target_interfaces", "data_interfaces"]
    include_subinterfaces: bool
    kinds: frozenset[InterfaceKind]

    def __call__(self, document, context):
        return select_interfaces(
            document, getattr(context, self.field),
            include_subinterfaces=self.include_subinterfaces, kinds=self.kinds,
        )


def compatibility_interfaces_scope(document, context):
    """沿用旧校验范围：目标父口闭包，以及文档中的全部 BVI。"""
    from ..interfaces import interface_nodes
    parents = {interface_parent(document.resolve_interface(name)) for name in context.target_interfaces}
    return tuple(
        block for block in interface_nodes(document)
        if (name := block.interface_name) and (
            interface_parent(name) in parents or document.interface_kind(name) == InterfaceKind.GATEWAY
        )
    )
