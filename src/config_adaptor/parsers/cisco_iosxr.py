"""Cisco IOS XR 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Iterable

from ..models import SimulationAdaptationPolicy, WashingPolicy
from .common import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceKind,
    InterfaceSpec,
    SimulationAdaptationOutcome,
    interface_parent,
    normalized_command as _normalized_command,
)


@dataclass
class _CiscoNode:
    """按缩进构造的临时语法节点，只在 group 展开阶段使用。"""
    command: str
    children: list["_CiscoNode"] = field(default_factory=list)
    is_block: bool = False
    origin: str = "explicit"
    rank: tuple[int, int] = (1_000_000, 0)


# 同一路径下这些命令只能有一个有效值，用于判断 group 配置冲突。
_CISCO_SINGLE_VALUE_KEYS = {
    "description",
    "mtu",
    "bandwidth",
    "shutdown",
    "vrf",
    "encapsulation",
    "load-interval",
    "link-status",
    "router-id",
    "metric",
    "cost",
    "priority",
    "hello-interval",
    "dead-interval",
    "passive",
}


def _cisco_command_identity(
    command: str,
    has_children: bool = False,
    path: list[str] | None = None,
) -> str:
    """生成命令的语义键，区分单值命令和允许重复的命令。"""
    normalized = _normalized_command(command)
    tokens = normalized.split()
    if not tokens:
        return ""
    if has_children:
        return f"block:{normalized}"
    if tokens[0] == "no" and len(tokens) > 1:
        positive = _cisco_command_identity(" ".join(tokens[1:]), path=path)
        if positive.startswith("single:"):
            return positive
    if tokens[0] in _CISCO_SINGLE_VALUE_KEYS:
        return f"single:{tokens[0]}"
    if tokens[:2] == ["ipv4", "address"]:
        return "single:ipv4 address" if "secondary" not in tokens else f"multi:{normalized}"
    if tokens[:2] == ["ipv6", "address"]:
        return f"multi:{normalized}"
    if len(tokens) >= 3 and tokens[:2] in (["ipv4", "access-group"], ["ipv6", "access-group"]):
        direction = tokens[-1] if tokens[-1] in {"ingress", "egress", "in", "out"} else "default"
        return f"single:{tokens[0]} access-group {direction}"
    if tokens[0] == "service-policy":
        direction = tokens[1] if len(tokens) > 1 and tokens[1] in {"input", "output"} else "default"
        return f"single:service-policy {direction}"
    if tokens[0] == "logging" and len(tokens) > 1:
        if tokens[1] == "host":
            return f"multi:{normalized}"
        return f"single:logging {tokens[1]}"
    if tokens[0] == "network":
        in_bgp = any(_normalized_command(component).startswith("router bgp") for component in (path or []))
        return f"multi:{normalized}" if in_bgp else "single:network"
    if tokens[:2] in (["bundle", "id"], ["lacp", "period"], ["apply-group", ""]):
        return f"single:{' '.join(tokens[:2])}"
    if tokens[0] == "neighbor" and len(tokens) >= 3:
        return f"single:neighbor {tokens[1]} {tokens[2]}"
    return f"multi:{normalized}"


# 拓扑中经常使用接口缩写，统一展开后才能与配置块可靠匹配。
_CISCO_PREFIXES = {
    "gi": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "te": "TenGigE",
    "tengige": "TenGigE",
    "hu": "HundredGigE",
    "hundredgige": "HundredGigE",
    "fo": "FortyGigE",
    "fortygige": "FortyGigE",
    "tf": "TwentyFiveGigE",
    "twentyfivegige": "TwentyFiveGigE",
    "be": "Bundle-Ether",
    "bundle-ether": "Bundle-Ether",
    "lo": "Loopback",
    "loopback": "Loopback",
    "mgmteth": "MgmtEth",
}


def canonical_cisco_interface(value: str) -> str:
    """去除空白并展开常见 IOS XR 接口缩写。"""
    value = re.sub(r"\s+l2transport\s*$", "", value.strip(), flags=re.IGNORECASE)
    compact = re.sub(r"\s+", "", value.strip())
    match = re.match(r"([A-Za-z-]+)(.*)", compact)
    if not match:
        return compact
    prefix, suffix = match.groups()
    expanded = _CISCO_PREFIXES.get(prefix.lower(), prefix)
    return f"{expanded}{suffix}"


_CISCO_PHYSICAL_INTERFACE = re.compile(
    r"^(?:Ethernet|FastEthernet|GigabitEthernet|TenGigE|TwentyFiveGigE|"
    r"FortyGigE|FiftyGigE|HundredGigE|FourHundredGigE|POS|Serial)\d",
    re.IGNORECASE,
)
_CISCO_VIRTUAL_INTERFACE = re.compile(
    r"^(?:Tunnel(?:-ip|-te)?|Null|PW-Ether|VASI(?:Left|Right)?|NVE|Multilink)\d",
    re.IGNORECASE,
)


def _cisco_interface_kind(name: str) -> InterfaceKind:
    """按 IOS XR 接口名前缀分类；未识别类型不得回退为物理口。"""
    parent = interface_parent(name).lower()
    if re.fullmatch(r"loopback\d+", parent):
        return InterfaceKind.LOOPBACK
    if re.match(r"^mgmteth\d", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"bundle-ether\d+", parent):
        return InterfaceKind.BUNDLE
    if re.fullmatch(r"bvi\d+", parent):
        return InterfaceKind.GATEWAY
    if _CISCO_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _CISCO_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


@dataclass(slots=True)
class CiscoBlock:
    """IOS XR 一个顶层配置块；active=False 表示渲染时跳过。"""
    header: str
    lines: list[str] = field(default_factory=list)
    active: bool = True

    @property
    def interface_name(self) -> str | None:
        """如果当前块是 interface，返回规范化后的接口名。"""
        match = re.match(r"interface\s+(.+?)\s*$", self.header, re.IGNORECASE)
        if not match:
            return None
        # IOS XR 二层子接口会把 l2transport 写在 interface 头部，
        # 它是接口模式而不是接口名的一部分。
        value = re.sub(r"\s+l2transport\s*$", "", match.group(1), flags=re.IGNORECASE)
        return canonical_cisco_interface(value)

    @property
    def l2transport(self) -> bool:
        """判断该接口头是否含有 IOS XR ``l2transport`` 模式。"""
        return bool(re.match(r"interface\s+.+\s+l2transport\s*$", self.header, re.IGNORECASE))


class CiscoDocument:
    """可修改的 IOS XR 配置文档，对责任链暴露统一操作接口。"""
    vendor = "cisco_iosxr"

    def __init__(self, text: str):
        """保留顶层块顺序并解析原始文本。"""
        self.trailing_newline = text.endswith("\n")
        self.blocks = self._parse(text)

    @staticmethod
    def _parse(text: str) -> list[CiscoBlock]:
        """按顶层非缩进行和 ``!`` 分隔符切分配置。"""
        blocks: list[CiscoBlock] = []
        current: CiscoBlock | None = None
        inside_group = False
        for line in text.splitlines():
            if inside_group and current is not None:
                if line.strip().lower() == "end-group":
                    inside_group = False
                    current = None
                    blocks.append(CiscoBlock(header="end-group"))
                else:
                    current.lines.append(line.rstrip())
                continue
            if line and not line[0].isspace() and line.strip() != "!":
                current = CiscoBlock(header=line.rstrip())
                blocks.append(current)
                inside_group = bool(re.match(r"group\s+\S+", line, re.IGNORECASE))
            elif line.strip() == "!" and line[:1].isspace() and current is not None:
                # IOS XR 会在 l2vpn/router 等层级块内部用缩进 ``!``
                # 结束子模式；它不能被误判成整个顶层块的结束符。
                current.lines.append(line.rstrip())
            elif line.strip() == "!":
                current = None
                blocks.append(CiscoBlock(header="!"))
            elif current is None:
                blocks.append(CiscoBlock(header=line.rstrip()))
            else:
                current.lines.append(line.rstrip())
        return blocks

    def _interface_blocks(self) -> list[CiscoBlock]:
        """返回仍处于有效状态的接口配置块。"""
        return self._interfaces().interface_blocks(self)

    @staticmethod
    def _interfaces():
        """延迟加载接口函数模块，避免语法模型与操作模块循环导入。"""
        from ..vendor.cisco import interfaces

        return interfaces

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取接口父子关系、VLAN 和类型，供 NNI/UNI 分类使用。"""
        return self._interfaces().interface_specs(self)

    def interface_kind(self, name: str) -> InterfaceKind:
        """返回接口类别，供拓扑端点校验复用同一厂商规则。"""
        return self._interfaces().interface_kind(self, name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理成员接口到 Bundle-Ether 的映射。"""
        return self._interfaces().bundle_members(self)

    def resolve_interface(self, value: str) -> str:
        """把拓扑接口名转换成配置解析器使用的规范形式。"""
        return self._interfaces().resolve_interface(self, value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口及其所有已配置子接口。"""
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别真正承载三层或二层业务的 IOS XR 接口。

        直接业务包括 IPv4/IPv6、l2transport、xconnect 等；此外，
        被 L2VPN、bridge-domain、路由协议等全局配置引用的接口也视为活跃业务口。
        BVI 不因自身有 IP 或编号类似 VLAN 就自动迁移，只有显式关联它的
        bridge-domain 仍包含活跃 attachment circuit 时才作为网关迁移。
        """
        return self._interfaces().business_interface_names(self)

    def _find_interface_block(self, name: str) -> CiscoBlock | None:
        """按规范化名称查找一个有效接口块。"""
        return self._interfaces().find_interface_block(self, name)

    @staticmethod
    def _parse_cisco_nodes(lines: Iterable[str], origin: str = "explicit") -> list[_CiscoNode]:
        """兼容内部调用；具体语法树构造由 Group 模块维护。"""
        from ..vendor.cisco.groups import CiscoGroupExpander

        return CiscoGroupExpander.parse_nodes(lines, origin)

    @staticmethod
    def _render_cisco_nodes(nodes: list[_CiscoNode], depth: int = 1) -> list[str]:
        """兼容内部调用；具体渲染由 Group 模块维护。"""
        from ..vendor.cisco.groups import CiscoGroupExpander

        return CiscoGroupExpander.render_nodes(nodes, depth)

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        mode: str = "relevant",
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """委托独立的 IOS XR Group 展开器。"""
        from ..vendor.cisco.groups import CiscoGroupExpander

        return CiscoGroupExpander(self).expand_groups(known_interfaces, mode, policy)

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """停用指定接口；可选择连同全部子接口一起停用。"""
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """改名父接口及子接口，并可移除聚合成员属性。"""
        self._interfaces().rename_interface_tree(self, source, target, strip_bundle)

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把一棵 IOS XR 接口配置复制到新物理口，用于 M-LAG 按对端拆分。"""
        self._interfaces().clone_interface_tree(self, source, target, strip_bundle)

    def _merge_duplicate_interface(self, preferred: CiscoBlock) -> None:
        """接口改名发生碰撞时去重合并配置行。"""
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str:
        """把 UNI 迁移到目标父接口，并统一重写为 QinQ 终结。"""
        return self._interfaces().map_uni(self, source, target_parent, vlan, inner_vlan)

    def finalize_uni_source(self, source_parent: str) -> None:
        """所有子接口迁移完成后删除源 UNI 父接口。"""
        self.remove_interface(source_parent, include_children=True)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在，并默认启用。"""
        self._interfaces().ensure_parent_interface(self, name)

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """委托给独立的模拟参数适配函数。"""
        from ..vendor.cisco.simulation import adapt_to_simulation

        return adapt_to_simulation(self, policy, data_interfaces)

    def adjust_simulation_parameters(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """兼容旧入口；新代码使用 adapt_to_simulation。"""
        return self.adapt_to_simulation(policy, data_interfaces)

    def clean_management_access(self) -> CleanupOutcome:
        """委托给独立的配置清洗函数。"""
        from ..vendor.cisco.cleaning import clean_management_access

        return clean_management_access(self)

    def clean_optional_features(
        self,
        policy: WashingPolicy,
    ) -> CleanupOutcome:
        """委托给独立的配置清洗函数。"""
        from ..vendor.cisco.cleaning import clean_optional_features

        return clean_optional_features(self, policy)

    def clean_authentication(
        self,
        policy: WashingPolicy | None = None,
    ) -> CleanupOutcome:
        """兼容旧入口：组合管理面认证清洗和显式启用的可选清洗。"""
        outcome = self.clean_management_access()
        outcome.merge(self.clean_optional_features(policy or WashingPolicy()))
        return outcome

    def add_lab_account(self) -> None:
        """委托给独立的配置清洗函数添加实验账号。"""
        from ..vendor.cisco.cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        match: str,
        action: str,
        value: str | None,
    ) -> int:
        """在 IOS XR 顶层配置块上应用外部规则。

        规则模块只依赖厂商门面，不再读取 ``blocks`` 这一内部表示。
        """
        pattern = re.compile(match, re.IGNORECASE)
        hits = 0
        for block in self.blocks:
            if not block.active or not pattern.search(block.header.strip()):
                continue
            hits += 1
            if action == "delete":
                block.active = False
            elif action == "replace":
                block.header = pattern.sub(value or "", block.header)
            elif action == "mask":
                block.header = pattern.sub("<masked>", block.header)
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """在接口定义外更新引用，并将一对多 M-LAG 引用复制展开。"""
        if not replacements:
            return
        canonical = {
            canonical_cisco_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items()
            if targets and targets != [source]
        }
        if not canonical:
            return
        names = sorted(canonical, key=len, reverse=True)
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_.-])"
        )

        def expand(value: str) -> list[str]:
            """按原文一次性展开引用，避免新目标名再被当作旧源名级联替换。"""
            matches = list(pattern.finditer(value))
            if not matches:
                return [value]
            variants = [""]
            cursor = 0
            for match in matches:
                prefix = value[cursor : match.start()]
                variants = [
                    current + prefix + target
                    for current in variants
                    for target in canonical[match.group(1)]
                ]
                cursor = match.end()
            return list(dict.fromkeys(current + value[cursor:] for current in variants))

        rebuilt: list[CiscoBlock] = []
        for block in self.blocks:
            if not block.active or block.interface_name:
                if block.active:
                    block.lines = [line for raw in block.lines for line in expand(raw)]
                rebuilt.append(block)
                continue
            header_variants = expand(block.header)
            for header in header_variants:
                clone = copy.deepcopy(block)
                clone.header = header
                clone.lines = [line for raw in clone.lines for line in expand(raw)]
                rebuilt.append(clone)
        self.blocks = rebuilt

    def render(self) -> str:
        """按原顺序输出仍有效的配置块。"""
        lines: list[str] = []
        for block in self.blocks:
            if not block.active:
                continue
            lines.append(block.header)
            lines.extend(block.lines)
        text = "\n".join(lines).rstrip() + "\n"
        return text
