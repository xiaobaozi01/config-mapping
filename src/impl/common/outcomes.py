"""厂商操作向配置自适应流程返回的统一结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SimulationChangeAction(StrEnum):
    """定义模拟适配结果的增删改分类。"""

    ADDED = "added"
    REPLACED = "replaced"
    REMOVED = "removed"


@dataclass(slots=True)
class GroupExpansionOutcome:
    """保存一次 group 展开的事件、冲突和成败状态。

    厂商展开器只回填结构化结果，由调度层统一决定何时记录或回滚，避免报告职责散落。
    """

    events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    ambiguities: list[dict[str, str]] = field(default_factory=list)
    identity_rule_hits: int = 0
    identity_fallbacks: int = 0
    success: bool = True


@dataclass(slots=True)
class CleanupOutcome:
    """按类别统计配置清洗结果。

    报告只需要数量而非被删除文本，因此该模型天然避免把认证秘密写入诊断输出。
    """

    removed: dict[str, int] = field(default_factory=dict)

    def record(self, category: str, count: int = 1) -> None:
        """累加一个清洗类别的命中数量。

        非正数计数没有审计意义，直接忽略可让调用方无需重复判断零值。
        """
        if count > 0:
            self.removed[category] = self.removed.get(category, 0) + count

    @property
    def total(self) -> int:
        """返回所有清洗类别的命中总数。

        汇总在结果对象中完成，避免报告层自行遍历并遗漏新增分类。
        """
        return sum(self.removed.values())


@dataclass(slots=True)
class SimulationAdaptationOutcome:
    """保存模拟参数适配的增删改分类结果。

    分类计数既支持汇总，也能让报告说明改动来源，而不需要暴露具体配置内容。
    """

    added: dict[str, int] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)

    rule_changes: dict[str, int] = field(default_factory=dict)

    def record(self, action: str, category: str, count: int = 1) -> None:
        """把一次模拟适配动作累加到对应类别。

        未知动作会通过字典查找直接失败，从而暴露规则映射错误而不是静默丢失统计。
        """
        if count <= 0:
            return
        try:
            normalized_action = SimulationChangeAction(action)
        except ValueError:
            raise KeyError(action) from None
        buckets = {
            SimulationChangeAction.ADDED: self.added,
            SimulationChangeAction.REPLACED: self.replaced,
            SimulationChangeAction.REMOVED: self.removed,
        }
        bucket = buckets[normalized_action]
        bucket[category] = bucket.get(category, 0) + count

    @property
    def total(self) -> int:
        """返回新增、替换和删除操作的总数。

        统一的合计口径让规则幂等性和报告汇总使用同一判断。
        """
        return sum(
            map(
                sum,
                (
                    self.added.values(),
                    self.replaced.values(),
                    self.removed.values(),
                ),
            )
        )


@dataclass(slots=True)
class InterfaceAdaptationOutcome:
    """保存接口迁移约束的变更、错误和告警。

    结果不携带厂商 AST，使规则运行时和报告层可以跨厂商复用同一审计结构。
    """

    changes: list[dict[str, object]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DeferredInterfaceReference:
    """记录因无法安全推断而保留的已删除接口引用。

    精确保存配置路径和接口名，使最终校验只豁免 prune 已明确告警的对象，
    不会按接口名全局放行其他残留引用。
    """

    path: tuple[str, ...]
    interface: str
    reason: str
    rule_id: str = ""


@dataclass(slots=True)
class InterfacePruningOutcome(InterfaceAdaptationOutcome):
    """保存接口裁剪阶段删除的接口和地址。

    删除集合供最终引用检查使用，确保接口定义消失后遗留的依赖也能被发现。
    """

    removed_interfaces: set[str] = field(default_factory=set)
    removed_addresses: set[str] = field(default_factory=set)
    deferred_interface_references: set[DeferredInterfaceReference] = field(
        default_factory=set
    )
