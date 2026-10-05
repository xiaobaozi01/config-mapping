"""厂商操作向配置自适应流程返回的统一结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class GroupExpansionOutcome:
    """一次 group 展开的事件、告警、冲突和成败状态。"""

    events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    ambiguities: list[dict[str, str]] = field(default_factory=list)
    identity_rule_hits: int = 0
    identity_fallbacks: int = 0
    success: bool = True


@dataclass(slots=True)
class CleanupOutcome:
    """按类别统计配置清洗结果，报告中只记录数量而不泄露内容。"""

    removed: dict[str, int] = field(default_factory=dict)

    def record(self, category: str, count: int = 1) -> None:
        if count > 0:
            self.removed[category] = self.removed.get(category, 0) + count

    def merge(self, other: "CleanupOutcome") -> None:
        for category, count in other.removed.items():
            self.record(category, count)

    @property
    def total(self) -> int:
        return sum(self.removed.values())


AuthenticationCleanupOutcome = CleanupOutcome


@dataclass(slots=True)
class SimulationAdaptationOutcome:
    """模拟环境参数适配的分类统计。"""

    added: dict[str, int] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)

    def record(self, action: str, category: str, count: int = 1) -> None:
        if count <= 0:
            return
        buckets = {
            "added": self.added,
            "replaced": self.replaced,
            "removed": self.removed,
        }
        bucket = buckets[action]
        bucket[category] = bucket.get(category, 0) + count

    @property
    def total(self) -> int:
        return sum(map(sum, (self.added.values(), self.replaced.values(), self.removed.values())))


ParameterAdjustmentOutcome = SimulationAdaptationOutcome
