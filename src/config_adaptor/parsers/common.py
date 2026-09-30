"""厂商解析器共享的数据结构和无厂商语义的小工具。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class InterfaceSpec:
    """从原配置提取出的一个物理接口或逻辑单元。"""
    name: str
    parent: str
    unit: str | None
    vlan: int | None
    kind: str
    # QinQ 源配置中可单独识别的内层客户 VLAN。
    inner_vlan: int | None = None


@dataclass(slots=True)
class GroupExpansionOutcome:
    """一次 group 展开的事件、告警、冲突和成败状态。"""
    events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    success: bool = True


@dataclass(slots=True)
class AuthenticationCleanupOutcome:
    """按类别统计认证清洗删除项，报告中只记录数量而不泄露内容。"""
    removed: dict[str, int] = field(default_factory=dict)

    def record(self, category: str, count: int = 1) -> None:
        """累加某类删除项；忽略零和负数。"""
        if count > 0:
            self.removed[category] = self.removed.get(category, 0) + count

    @property
    def total(self) -> int:
        """返回所有类别的删除总数。"""
        return sum(self.removed.values())


def normalized_command(value: str) -> str:
    """归一化命令空白与大小写，用于语义冲突比较。"""
    return " ".join(value.strip().rstrip(";").split()).lower()


def interface_parent(name: str) -> str:
    """从子接口名中提取物理父接口名。"""
    return name.rsplit(".", 1)[0] if "." in name else name


def interface_unit(name: str) -> str | None:
    """返回点号后的逻辑单元编号；物理接口返回 ``None``。"""
    return name.rsplit(".", 1)[1] if "." in name else None
