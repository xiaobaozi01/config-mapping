"""厂商无关的接口数据结构和名称工具。"""

from __future__ import annotations

from dataclasses import dataclass
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
    inner_vlan: int | None = None


def normalized_command(value: str) -> str:
    """归一化命令空白与大小写，用于语义冲突比较。"""

    return " ".join(value.strip().rstrip(";").split()).lower()


def interface_parent(name: str) -> str:
    """从子接口名中提取物理父接口名。"""

    return name.rsplit(".", 1)[0] if "." in name else name


def interface_unit(name: str) -> str | None:
    """返回点号后的逻辑单元编号；物理接口返回 ``None``。"""

    return name.rsplit(".", 1)[1] if "." in name else None


def referenced_interface_names(text: str, names: set[str]) -> set[str]:
    """用字符串查找和原有边界规则识别完整接口引用。"""
    if not text or not names:
        return set()
    found: set[str] = set()
    for name in names:
        if not name:
            continue
        start = 0
        while (position := text.find(name, start)) != -1:
            end = position + len(name)
            if (
                (position == 0 or text[position - 1] not in _REFERENCE_NAME_CHARS)
                and (end == len(text) or text[end] not in _REFERENCE_NAME_CHARS)
            ):
                found.add(name)
                break
            start = position + 1
    return found


_REFERENCE_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
