"""厂商无关的接口数据结构和名称工具。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InterfaceKind(StrEnum):
    """定义接口在跨厂商流程中的业务类别。

    未知类型必须保留而不能按物理接口猜测，否则迁移可能删除控制接口或错误占用端口。
    """

    PHYSICAL = "physical"
    BUNDLE = "bundle"
    GATEWAY = "gateway"
    LOOPBACK = "loopback"
    MANAGEMENT = "management"
    VIRTUAL = "virtual"
    UNKNOWN = "unknown"


class BundleMode(StrEnum):
    """定义聚合成员绑定的协商模式。

    未知模式用于缺少配置或绑定冲突的情况，避免迁移阶段乐观假设链路可以工作。
    """

    ON = "on"
    ACTIVE = "active"
    PASSIVE = "passive"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class InterfaceSpec:
    """描述从原配置提取出的一个接口或逻辑单元。

    字段只保存跨厂商分析所需结果，使拓扑、映射和清理逻辑无需依赖具体 AST。
    """

    name: str
    parent: str
    unit: str | None
    vlan: int | None
    kind: InterfaceKind
    inner_vlan: int | None = None


@dataclass(frozen=True, slots=True)
class UniInterfacePlan:
    """保存一项 UNI 业务的原始身份、暂存名称及目标标签。

    源接口在 NNI 写入前暂存，避免目标口复用原 UNI 名称时混合配置。
    """

    source: str
    staged: str
    parent: str
    vlan: int
    inner_vlan: int | None = None
    source_vlan: int | None = None
    source_kind: InterfaceKind = InterfaceKind.PHYSICAL


def normalized_command(value: str) -> str:
    """归一化命令空白、结尾分号和大小写。

    语义身份比较必须忽略纯书写差异，否则同一配置槽位会被误判为两个冲突值。
    """

    return " ".join(value.strip().rstrip(";").split()).lower()


def interface_parent(name: str) -> str:
    """从带点号后缀的接口名中提取父接口。

    将父口和逻辑单元统一归到父口，使接口分类与聚合成员查询采用同一稳定键。
    """

    return name.rsplit(".", 1)[0] if "." in name else name


def interface_unit(name: str) -> str | None:
    """返回点号后的逻辑单元编号，物理接口返回 ``None``。

    保留原始编号字符串可避免前导零等信息在构造目标名称时丢失。
    """

    return name.rsplit(".", 1)[1] if "." in name else None


def referenced_interface_names(text: str, names: set[str]) -> set[str]:
    """识别文本中属于给定集合的完整接口名引用。

    边界字符检查阻止较短接口名命中更长名称的一部分，从而避免错误改写相邻引用。
    """
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
                position == 0 or text[position - 1] not in _REFERENCE_NAME_CHARS
            ) and (end == len(text) or text[end] not in _REFERENCE_NAME_CHARS):
                found.add(name)
                break
            start = position + 1
    return found


_REFERENCE_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
