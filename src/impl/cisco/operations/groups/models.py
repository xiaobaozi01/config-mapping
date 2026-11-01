"""Cisco Group 展开的内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from impl.common.domain.outcomes import GroupExpansionOutcome
from impl.common.domain.policies import WashingPolicy


@dataclass
class _GroupStatement:
    """表示 group 展开期间使用的可变配置语句节点。

    该轻量节点保留来源和继承优先级，使合并、渲染和工作树回滚独立于正式 Cisco AST。
    """

    header: str
    statements: list[_GroupStatement] = field(default_factory=list)
    is_block: bool = False
    origin: str = "explicit"
    rank: tuple[int, int] = (1_000_000, 0)
    comment: str | None = None

    @property
    def active(self):
        """返回工作语句是否需要参与展开。

        工作树节点在创建后保持活动，逻辑删除通过从列表移除而非状态标记完成。
        """
        return True

    @property
    def is_formatting(self):
        """返回语句是否只是空行或叹号分隔符。

        格式节点不参与 identity 合并，但仍可能在最终重写中保留。
        """
        return self.header in {"", "!"}

    def walk(self, *, include_self=False):
        """按深度优先顺序遍历非格式工作语句。

        跳过格式节点让 group 规则只处理实际命令，同时可选择包含当前节点。
        """
        pending = [self] if include_self else list(reversed(self.statements))
        while pending:
            item = pending.pop()
            if not item.is_formatting:
                yield item
            pending.extend(reversed(item.statements))


@dataclass(slots=True)
class _GroupDefinition:
    """保存一个 IOS XR group 的可物化节点和静态能力标记。

    在遍历前识别运行时变量和嵌套 apply 可避免使用无法安全展开的定义。
    """

    body_nodes: list[_GroupStatement]
    has_runtime_variables: bool
    has_nested_apply: bool


@dataclass(frozen=True, slots=True)
class _GroupApplication:
    """记录一个 group 引用及其继承优先级。

    优先级由路径深度和同级顺序组成，用于确定同名语句的覆盖方向。
    """

    name: str
    precedence: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _GroupControls:
    """保存一个配置层级直接声明的 apply 和 exclude 名称。

    将两类控制一起解析，后续可以同时抑制本地引用和从父层继承的引用。
    """

    applied_group_names: tuple[str, ...]
    excluded_group_names: frozenset[str]


@dataclass(slots=True)
class _ExpansionState:
    """保存一次 group 展开共享的索引、结果和失败状态。

    递归阶段共享状态，避免重复加载定义或通过长参数列表传递可变结果。
    """

    group_definitions: dict[str, _GroupDefinition]
    referenced_group_names: set[str]
    outcome: GroupExpansionOutcome
    washing_policy: WashingPolicy
    encountered_group_names: set[str] = field(default_factory=set)
    has_unresolvable_reference: bool = False
