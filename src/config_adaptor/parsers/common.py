"""厂商解析器共享的数据结构和无厂商语义的小工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class InterfaceKind(StrEnum):
    """接口的厂商无关类别；未知类型必须保守处理。"""

    PHYSICAL = "physical"
    BUNDLE = "bundle"
    GATEWAY = "gateway"
    LOOPBACK = "loopback"
    MANAGEMENT = "management"
    VIRTUAL = "virtual"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class InterfaceSpec:
    """从原配置提取出的一个接口或逻辑单元。"""
    name: str
    parent: str
    unit: str | None
    vlan: int | None
    kind: InterfaceKind
    # QinQ 源配置中可单独识别的内层客户 VLAN。
    inner_vlan: int | None = None


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
        """累加某类删除项；忽略零和负数。"""
        if count > 0:
            self.removed[category] = self.removed.get(category, 0) + count

    def merge(self, other: "CleanupOutcome") -> None:
        """合并另一次清洗结果，供兼容组合入口使用。"""
        for category, count in other.removed.items():
            self.record(category, count)

    @property
    def total(self) -> int:
        """返回所有类别的删除总数。"""
        return sum(self.removed.values())


# 兼容已有导入路径；新代码使用语义更通用的 CleanupOutcome。
AuthenticationCleanupOutcome = CleanupOutcome


@dataclass(slots=True)
class SimulationAdaptationOutcome:
    """模拟环境参数适配的分类统计。"""

    added: dict[str, int] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)

    def record(self, action: str, category: str, count: int = 1) -> None:
        """按动作和类别累计修改数量。"""
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
        """返回新增、替换和删除的总数。"""
        return sum(map(sum, (self.added.values(), self.replaced.values(), self.removed.values())))


# 兼容旧的 Python 导入名；新代码使用 SimulationAdaptationOutcome。
ParameterAdjustmentOutcome = SimulationAdaptationOutcome


def normalized_command(value: str) -> str:
    """归一化命令空白与大小写，用于语义冲突比较。"""
    return " ".join(value.strip().rstrip(";").split()).lower()


def interface_parent(name: str) -> str:
    """从子接口名中提取物理父接口名。"""
    return name.rsplit(".", 1)[0] if "." in name else name


def interface_unit(name: str) -> str | None:
    """返回点号后的逻辑单元编号；物理接口返回 ``None``。"""
    return name.rsplit(".", 1)[1] if "." in name else None
