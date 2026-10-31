"""接口分析结果的文档级缓存，兼容调用方直接编辑可修改 AST。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar, cast

from .interface import InterfaceSpec


class _AnalysisNode(Protocol):
    """描述接口分析缓存所需的最小节点读取协议。

    缓存只依赖 header、活动状态、注释和子节点，避免绑定 Cisco 或 Junos 具体节点类型。
    """

    @property
    def header(self) -> str:
        """返回参与签名比较的原始节点头。

        头文本变化必须使缓存失效，因此该字段直接进入树签名。
        """
        ...

    @property
    def active(self) -> bool:
        """返回节点是否仍参与当前配置逻辑。

        活动状态变化会改变业务接口集合，因此缓存必须读取它。
        """
        ...

    @property
    def comment(self) -> str | None:
        """返回节点的独立注释状态。

        接口分析只关心注释是否存在，注释内容不会改变分类结果。
        """
        ...

    @property
    def children(self) -> Sequence[_AnalysisNode] | None:
        """返回子节点序列或表示叶子的 ``None``。

        子节点数量和层级同时构成树签名，以捕获直接修改 AST 后的结构变化。
        """
        ...


_TreeSignature = tuple[tuple[str, bool, bool, int], ...]
_Result = TypeVar("_Result")


def _tree_signature(root: _AnalysisNode) -> _TreeSignature:
    """只读取原始字段；子节点数量同时保留树形结构及叶子/空块区别。

    配置节点及 children 列表允许被任意模块直接修改，因此不能只在 Document
    修改方法中清理缓存。每次读取用线性扫描校验，避免重新解析、渲染及关联分析。
    注释仅需记录是否存在，接口分析只用它识别独立注释节点。
    """
    parts: list[tuple[str, bool, bool, int]] = []
    pending = [root]
    while pending:
        node = pending.pop()
        children = node.children
        parts.append(
            (
                node.header,
                node.active,
                node.comment is not None,
                -1 if children is None else len(children),
            )
        )
        if children:
            pending.extend(reversed(children))
    return tuple(parts)


def copy_interface_specs(specs: list[InterfaceSpec]) -> list[InterfaceSpec]:
    """复制接口规格列表及其中的不可变规格对象。

    调用方可能对返回列表排序或替换元素，独立容器可保护缓存中的原始结果。
    """
    return [
        InterfaceSpec(
            spec.name,
            spec.parent,
            spec.unit,
            spec.vlan,
            spec.kind,
            spec.inner_vlan,
        )
        for spec in specs
    ]


class InterfaceAnalysisCache:
    """每个文档只保存当前树的查询结果，不保留历史树或其他文档。

    查询期间 AST 应保持不变；该缓存不提供并发修改配置树的事务隔离。
    """

    def __init__(self) -> None:
        """创建空签名和空查询结果缓存。

        首次读取会根据当前树重新计算全部结果，避免构造阶段触发昂贵的接口分析。
        """
        self._signature: _TreeSignature | None = None
        self._results: dict[str, object] = {}

    def __deepcopy__(self, _memo: dict[int, object]) -> InterfaceAnalysisCache:
        """为复制后的文档创建空接口分析缓存。

        复制后的树可能继续独立修改，复用旧缓存会返回与当前结构不一致的结果。
        """
        return type(self)()

    def read(
        self,
        root: _AnalysisNode,
        query: str,
        collect: Callable[[], _Result],
        copy_result: Callable[[_Result], _Result],
    ) -> _Result:
        """返回指定查询的结果，并在树结构变化时自动失效缓存。

        每次读取先比较轻量树签名，使直接修改 AST 的调用方也能得到当前修订的数据。
        """
        signature = _tree_signature(root)
        if signature != self._signature:
            self._signature = signature
            self._results.clear()
        if query not in self._results:
            self._results[query] = collect()
        return copy_result(cast(_Result, self._results[query]))
