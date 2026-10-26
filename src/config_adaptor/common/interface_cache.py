"""接口分析结果的文档级缓存，兼容调用方直接编辑可修改 AST。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar, cast

from .interface import InterfaceSpec


class _AnalysisNode(Protocol):
    @property
    def header(self) -> str: ...

    @property
    def active(self) -> bool: ...

    @property
    def comment(self) -> str | None: ...

    @property
    def children(self) -> Sequence[_AnalysisNode] | None: ...


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
        parts.append((
            node.header,
            node.active,
            node.comment is not None,
            -1 if children is None else len(children),
        ))
        if children:
            pending.extend(reversed(children))
    return tuple(parts)


def copy_interface_specs(specs: list[InterfaceSpec]) -> list[InterfaceSpec]:
    """复制规格及列表，避免调用方修改返回值时污染缓存。"""
    return [
        InterfaceSpec(spec.name, spec.parent, spec.unit, spec.vlan, spec.kind, spec.inner_vlan)
        for spec in specs
    ]


class InterfaceAnalysisCache:
    """每个文档只保存当前树的查询结果，不保留历史树或其他文档。

    查询期间 AST 应保持不变；该缓存不提供并发修改配置树的事务隔离。
    """

    def __init__(self) -> None:
        self._signature: _TreeSignature | None = None
        self._results: dict[str, object] = {}

    def __deepcopy__(self, memo: dict[int, object]) -> InterfaceAnalysisCache:
        """复制文档时重新按其树计算结果，不复制原文档的缓存状态。"""
        return type(self)()

    def read(
        self,
        root: _AnalysisNode,
        query: str,
        collect: Callable[[], _Result],
        copy_result: Callable[[_Result], _Result],
    ) -> _Result:
        signature = _tree_signature(root)
        if signature != self._signature:
            self._signature = signature
            self._results.clear()
        if query not in self._results:
            self._results[query] = collect()
        return copy_result(cast(_Result, self._results[query]))
