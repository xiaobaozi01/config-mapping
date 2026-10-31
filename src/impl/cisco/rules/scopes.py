"""从当前解析快照选择 Visitor 的访问根，不缓存 Context。"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from impl.common.interface import InterfaceKind, interface_parent


def document_scope(document, context):
    """选择整份 Cisco 文档作为规则访问根。

    全局校验和剪枝需要看到接口之外的协议引用，因此不能只返回接口块。
    """
    return (document.root,)


def select_interfaces(
    document,
    targets: Iterable[str],
    *,
    include_subinterfaces: bool,
    kinds: frozenset[InterfaceKind],
):
    """按目标父口、子接口策略和接口类型选择访问根。

    先统一父口再比较类型，使接口别名不会绕过规则范围，同时避免修改无关接口。
    """
    from ..interfaces import interface_nodes

    parents = {
        interface_parent(document.resolve_interface(name)) for name in targets
    }
    return tuple(
        block
        for block in interface_nodes(document)
        if (name := block.interface_name)
        and interface_parent(name) in parents
        and (include_subinterfaces or name == interface_parent(name))
        and document.interface_kind(name) in kinds
    )


@dataclass(frozen=True, slots=True)
class InterfaceScope:
    """声明规则从上下文字段读取的目标及接口筛选条件。

    可调用对象直接作为选择器，使注册表无需为不同数据来源增加分支。
    """

    field: Literal["target_interfaces", "data_interfaces"]
    include_subinterfaces: bool
    kinds: frozenset[InterfaceKind]

    def __call__(self, document, context):
        """用当前规则上下文选择匹配的接口访问根。

        字段名和数据集合由作用域对象固定，调用时只读取上下文，不修改选择策略。
        """
        return select_interfaces(
            document,
            getattr(context, self.field),
            include_subinterfaces=self.include_subinterfaces,
            kinds=self.kinds,
        )


def compatibility_interfaces_scope(document, context):
    """选择目标父口闭包以及文档中的全部 BVI。

    网关引用可能位于目标接口之外，额外纳入 BVI 可保证兼容性校验覆盖完整业务链。
    """
    from ..interfaces import interface_nodes

    parents = {
        interface_parent(document.resolve_interface(name))
        for name in context.target_interfaces
    }
    return tuple(
        block
        for block in interface_nodes(document)
        if (name := block.interface_name)
        and (
            interface_parent(name) in parents
            or document.interface_kind(name) == InterfaceKind.GATEWAY
        )
    )
