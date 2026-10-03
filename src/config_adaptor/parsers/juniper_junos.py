"""Juniper Junos 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
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


def _junos_statement_identity(command: str, path: list[str] | None = None) -> str:
    """兼容旧内部调用；新 group 实现直接使用结构化 decision。"""
    from ..vendor.juniper.identity import resolve_junos_identity

    return resolve_junos_identity(command, path=path or []).key


def canonical_junos_interface(value: str) -> str:
    """去掉接口名中的无意义空白。"""
    return re.sub(r"\s+", "", value.strip())


_JUNOS_PHYSICAL_INTERFACE = re.compile(
    r"^(?:fe|ge|xe|et|mge|so|se|t1|e1|ct|coc|sat|xle)-\d",
    re.IGNORECASE,
)
_JUNOS_VIRTUAL_INTERFACE = re.compile(
    r"^(?:(?:gr|ip|lt|mt|pd|pe|sp|st|vtep|demux|reth)(?:-|\d)|"
    r"(?:dsc|lsi|pimd|pime|tap)$)",
    re.IGNORECASE,
)


def _junos_interface_kind(name: str) -> InterfaceKind:
    """按 Junos 接口名前缀分类；未识别类型不得回退为物理口。"""
    parent = interface_parent(name).lower()
    if parent == "lo0":
        return InterfaceKind.LOOPBACK
    if re.fullmatch(r"(?:(?:fxp|em|me)\d+|vme\d*)", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"ae\d+", parent):
        return InterfaceKind.BUNDLE
    if parent == "irb":
        return InterfaceKind.GATEWAY
    if _JUNOS_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _JUNOS_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


@dataclass
class JunosNode:
    """Junos 配置树节点；children=None 表示叶子语句。"""
    header: str
    children: list["JunosNode"] | None = None
    active: bool = True
    origin: str = "explicit"
    rank: tuple[int, ...] = (1_000_000, 0)

    @property
    def is_block(self) -> bool:
        """判断节点是否拥有大括号子层级。"""
        return self.children is not None

    def clone(self) -> "JunosNode":
        """深拷贝节点，避免迁移配置时修改原节点。"""
        return copy.deepcopy(self)


class JunosDocument:
    """可修改的 Junos 配置树，对责任链暴露统一操作接口。"""
    vendor = "juniper_junos"

    def __init__(self, text: str):
        """创建虚拟根节点并解析完整配置。"""
        self.root = JunosNode("<root>", [])
        self._parse(text)

    def _parse(self, text: str) -> None:
        """使用栈解析大括号层级，并检查括号是否平衡。"""
        stack = [self.root]
        for number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                stack[-1].children.append(JunosNode(""))
                continue
            if stripped in {"}", "};"}:
                if len(stack) == 1:
                    raise ValueError(f"Junos 配置第 {number} 行出现多余右大括号")
                stack.pop()
                continue
            if stripped.endswith("{"):
                node = JunosNode(stripped[:-1].strip(), [])
                stack[-1].children.append(node)
                stack.append(node)
                continue
            if "{" in stripped or "}" in stripped:
                # 不常见的行内大括号语法原样保留，但不伪装成已解析层级。
                stack[-1].children.append(JunosNode(stripped))
                continue
            stack[-1].children.append(JunosNode(stripped))
        if len(stack) != 1:
            raise ValueError("Junos 配置大括号不平衡")

    @staticmethod
    def _base_header(header: str) -> str:
        """去除 inactive/protect 前缀，返回用于语义匹配的语句。"""
        result = header.strip()
        for prefix in ("inactive:", "protect:"):
            if result.startswith(prefix):
                result = result[len(prefix) :].strip()
        return result

    def _top_block(self, name: str, create: bool = False) -> JunosNode | None:
        """查找顶层块；create=True 时按需创建。"""
        assert self.root.children is not None
        for node in self.root.children:
            if node.active and node.is_block and self._base_header(node.header) == name:
                return node
        if create:
            node = JunosNode(name, [])
            self.root.children.append(node)
            return node
        return None

    def _interfaces_block(self, create: bool = False) -> JunosNode | None:
        """返回顶层 interfaces 块。"""
        return self._top_block("interfaces", create=create)

    def _interface_nodes(self) -> list[JunosNode]:
        """返回 interfaces 下仍有效的接口节点。"""
        block = self._interfaces_block()
        if not block or block.children is None:
            return []
        return [node for node in block.children if node.active and node.is_block]

    def _interface_name(self, node: JunosNode) -> str:
        """从接口节点头提取规范化名称。"""
        return canonical_junos_interface(self._base_header(node.header).split()[0])

    def _unit_nodes(self, interface: JunosNode) -> list[JunosNode]:
        """列出接口下的所有 unit 块。"""
        if interface.children is None:
            return []
        return [
            node
            for node in interface.children
            if node.is_block and self._base_header(node.header).startswith("unit ")
        ]

    def _unit_number(self, node: JunosNode) -> str:
        """从 unit 节点头提取编号。"""
        return self._base_header(node.header).split(maxsplit=1)[1]

    def _find_vlan(self, unit: JunosNode) -> int | None:
        """读取 unit 的 vlan-id；未配置时返回 None。"""
        if unit.children is None:
            return None
        for child in unit.children:
            statement = self._base_header(child.header)
            match = re.match(r"vlan-id\s+(\d+)\s*;", statement)
            if match:
                return int(match.group(1))
            tags = re.match(r"vlan-tags\s+outer\s+(\d+)\s+inner\s+\d+\s*;", statement)
            if tags:
                return int(tags.group(1))
        return None

    def _find_inner_vlan(self, unit: JunosNode) -> int | None:
        """读取 ``vlan-tags outer ... inner ...`` 中的内层 VLAN。"""
        if unit.children is None:
            return None
        for child in unit.children:
            match = re.match(
                r"vlan-tags\s+outer\s+\d+\s+inner\s+(\d+)\s*;",
                self._base_header(child.header),
            )
            if match:
                return int(match.group(1))
        return None

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        mode: str = "relevant",
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """委托独立的 Junos Group 展开器。"""
        from ..vendor.juniper.groups import JunosGroupExpander

        return JunosGroupExpander(self).expand_groups(known_interfaces, mode, policy)

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取物理接口和 unit 的父子关系、VLAN 与接口类型。"""
        return self._interfaces().interface_specs(self)

    @staticmethod
    def _interfaces():
        """延迟加载接口函数模块，避免语法模型与操作模块循环导入。"""
        from ..vendor.juniper import interfaces

        return interfaces

    def interface_kind(self, name: str) -> InterfaceKind:
        """返回接口类别，供拓扑端点校验复用同一厂商规则。"""
        return self._interfaces().interface_kind(self, name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理接口到 ae 聚合接口的映射。"""
        return self._interfaces().bundle_members(self)

    def resolve_interface(self, value: str) -> str:
        """规范化拓扑中的 Junos 接口名。"""
        return self._interfaces().resolve_interface(self, value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口下所有已配置 unit。"""
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别承载三层、二层及活跃广播域网关的 Junos 接口。

        IRB 不因自身配置地址就自动迁移；只有 bridge-domain/vlan 中仍有
        活跃接入口，或其 unit 命中活跃业务 VLAN 时才进入 UNI 计划。
        """
        return self._interfaces().business_interface_names(self)

    def _find_interface_node(self, name: str) -> JunosNode | None:
        """按父接口名查找接口节点。"""
        return self._interfaces().find_interface_node(self, name)

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """停用整个接口节点；Junos unit 随父节点一并停用。"""
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """改名接口节点，并可移除原聚合相关 options。"""
        self._interfaces().rename_interface_tree(self, source, target, strip_bundle)

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把 Junos 接口树复制到新物理口，用于 M-LAG 按对端拆分。"""
        self._interfaces().clone_interface_tree(self, source, target, strip_bundle)

    def _merge_duplicate_interface(self, preferred: JunosNode) -> None:
        """接口改名碰撞时合并不重复的子节点并停用旧节点。"""
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def _strip_vlan_termination(self, nodes: list[JunosNode]) -> list[JunosNode]:
        """递归删除旧标签匹配和 VLAN rewrite，同时保留业务 family/CCC 类型。"""
        return self._interfaces()._strip_vlan_termination(self, nodes)

    def _ensure_target_parent(self, target_parent: str) -> JunosNode:
        """确保 UNI 目标口存在并支持灵活 VLAN 封装。"""
        return self._interfaces()._ensure_target_parent(self, target_parent)

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str:
        """复制源 unit 到目标父接口，并统一重写为 QinQ vlan-tags。"""
        return self._interfaces().map_uni(self, source, target_parent, vlan, inner_vlan)

    def finalize_uni_source(self, source_parent: str) -> None:
        """所有 unit 迁移完成后停用源 UNI 父接口。"""
        self.remove_interface(source_parent, include_children=True)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在。"""
        self._interfaces().ensure_parent_interface(self, name)

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """委托给独立的模拟参数适配函数。"""
        from ..vendor.juniper.simulation import adapt_to_simulation

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
        from ..vendor.juniper.cleaning import clean_management_access

        return clean_management_access(self)

    def clean_optional_features(
        self,
        policy: WashingPolicy,
    ) -> CleanupOutcome:
        """委托给独立的配置清洗函数。"""
        from ..vendor.juniper.cleaning import clean_optional_features

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
        from ..vendor.juniper.cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        match: str,
        action: str,
        value: str | None,
    ) -> int:
        """递归应用外部规则，同时封装 Junos AST 的遍历细节。"""
        pattern = re.compile(match, re.IGNORECASE)
        hits = 0

        def walk(node: JunosNode, path: list[str]) -> None:
            nonlocal hits
            if node is self.root:
                next_path = path
            else:
                component = self._base_header(node.header).split(maxsplit=1)[0].rstrip(";")
                next_path = [*path, component] if component else path
                dotted = ".".join(next_path)
                if node.active and (pattern.search(dotted) or pattern.search(node.header)):
                    hits += 1
                    if action == "delete":
                        node.active = False
                    elif action == "replace":
                        node.header = pattern.sub(value or "", node.header)
                    elif action == "mask":
                        node.header = "<masked>;"
            if node.children:
                for child in node.children:
                    walk(child, next_path)

        walk(self.root, [])
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """递归更新 interfaces 之外的引用，并复制一对多 M-LAG 节点。"""
        relevant = {
            canonical_junos_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items()
            if targets and targets != [source]
        }
        if not relevant:
            return
        names = sorted(relevant, key=len, reverse=True)
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_.-])"
        )

        def expand(value: str) -> list[str]:
            """按原节点头一次性展开，避免目标名再命中另一个源名。"""
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
                    for target in relevant[match.group(1)]
                ]
                cursor = match.end()
            return list(dict.fromkeys(current + value[cursor:] for current in variants))

        def walk(node: JunosNode, inside_interfaces: bool = False) -> None:
            """在父节点上就地复制子节点，接口定义树本身不二次替换。"""
            if node.children is None:
                return
            current_inside = inside_interfaces or (
                node is not self.root and self._base_header(node.header) == "interfaces"
            )
            rebuilt: list[JunosNode] = []
            for child in node.children:
                variants = [child.header] if current_inside else expand(child.header)
                for header in variants:
                    clone = child if len(variants) == 1 and header == child.header else child.clone()
                    clone.header = header
                    walk(clone, current_inside)
                    rebuilt.append(clone)
            node.children = rebuilt

        walk(self.root)

    def _render_node(self, node: JunosNode, depth: int) -> str:
        """递归渲染单个节点，并跳过 inactive 的内部标记节点。"""
        if not node.active:
            return ""
        indent = "    " * depth
        if node.children is None:
            return indent + node.header
        lines = [indent + node.header + " {"]
        for child in node.children:
            rendered = self._render_node(child, depth + 1)
            if rendered:
                lines.append(rendered)
        lines.append(indent + "}")
        return "\n".join(lines)

    def render(self) -> str:
        """把有效配置树输出为标准大括号格式。"""
        assert self.root.children is not None
        rendered = [self._render_node(node, 0) for node in self.root.children if node.active]
        return "\n".join(item for item in rendered if item).rstrip() + "\n"
