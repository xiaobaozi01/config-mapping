"""Junos Group 展开的内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from impl.common.domain.outcomes import GroupExpansionOutcome
from impl.common.domain.policies import WashingPolicy
from impl.common.semantic_identity import (
    SemanticDecision,
)

from ...document import (
    JuniperNode,
)


@dataclass(frozen=True, slots=True)
class _GroupApplication:
    """保存一次 group 应用及其继承优先级和传递引用链。

    不可变值对象可用于集合去重，并让嵌套引用继承和比较顺序保持稳定。
    """

    name: str
    precedence: tuple[int, ...]
    dependency_chain: tuple[str, ...]


@dataclass(slots=True)
class _ResolvedGroupInheritance:
    """保存当前路径最终求得的 group 继承集合。

    候选、有效和排除集合同时保留，使递归可以向子节点传递完整求值依据。
    """

    candidate_applications: list[_GroupApplication]
    effective_applications: list[_GroupApplication]
    excluded_group_names: set[str]


@dataclass(slots=True)
class _ExpansionState:
    """保存一次 group 展开共享的定义、依赖和诊断状态。

    状态集中传递可避免递归方法反复构造索引，并让失败回滚看到同一份求值记录。
    """

    groups_container: JuniperNode
    group_definitions: dict[str, JuniperNode]
    group_dependencies: dict[str, list[str]]
    outcome: GroupExpansionOutcome
    washing_policy: WashingPolicy
    reachable_group_names: set[str] = field(default_factory=set)
    encountered_group_names: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _PreparedExpansion:
    """保存事务开始后已完成预处理的工作树和状态。

    预处理标记区分原配置是否变化，使无实际展开的调用不必替换文档根节点。
    """

    working_root: JuniperNode
    state: _ExpansionState
    preprocessing_changed: bool
    pruned_group_names: tuple[str, ...]


_ResolvedStatement = tuple[JuniperNode, SemanticDecision]
