"""Juniper Junos 大括号配置解析、groups 展开、接口改写和认证清洗。"""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable

from ..constants import JUNOS_LAB_PASSWORD_HASH, LAB_USERNAME
from .common import (
    AuthenticationCleanupOutcome,
    GroupExpansionOutcome,
    InterfaceSpec,
    interface_parent,
    interface_unit,
    normalized_command as _normalized_command,
)


# 同一路径下只能出现一个有效值的语句，用于 group 冲突判断。
_JUNOS_SINGLE_VALUE_KEYS = {
    "description",
    "mtu",
    "vlan-id",
    "native-vlan-id",
    "encapsulation",
    "interface-type",
    "speed",
    "link-mode",
    "host-name",
    "domain-name",
    "router-id",
    "autonomous-system",
    "metric",
    "preference",
    "local-address",
    "source-address",
    "class",
    "authentication-key",
    "minimum-links",
}


def _junos_statement_identity(command: str) -> str:
    """生成 Junos 语句的语义键，保留 address 等可重复语句。"""
    normalized = _normalized_command(command)
    tokens = normalized.split()
    if not tokens:
        return ""
    if tokens[0] in _JUNOS_SINGLE_VALUE_KEYS:
        return f"single:{tokens[0]}"
    return f"multi:{normalized}"


def canonical_junos_interface(value: str) -> str:
    """去掉接口名中的无意义空白。"""
    return re.sub(r"\s+", "", value.strip())


def _junos_interface_kind(name: str) -> str:
    """按 Junos 命名规则识别物理、聚合、环回和管理接口。"""
    parent = interface_parent(name).lower()
    if parent.startswith("lo0"):
        return "loopback"
    if parent.startswith(("fxp", "em", "me", "vme")):
        return "management"
    if parent.startswith("ae"):
        return "bundle"
    return "physical"


@dataclass
class JunosNode:
    """Junos 配置树节点；children=None 表示叶子语句。"""
    header: str
    children: list["JunosNode"] | None = None
    active: bool = True
    origin: str = "explicit"
    rank: tuple[int, int] = (1_000_000, 0)

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
            match = re.match(r"vlan-id\s+(\d+)\s*;", self._base_header(child.header))
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _header_matches(cls, pattern_header: str, target_header: str) -> bool:
        """匹配精确节点名或 ``<ge-*>`` 一类 Junos 通配节点。"""
        pattern = cls._base_header(pattern_header)
        target = cls._base_header(target_header)
        if "<" not in pattern:
            return pattern == target
        pieces: list[str] = []
        cursor = 0
        for match in re.finditer(r"<([^>]+)>", pattern):
            pieces.append(re.escape(pattern[cursor : match.start()]))
            pieces.append(fnmatch.translate(match.group(1)).removesuffix(r"\Z"))
            cursor = match.end()
        pieces.append(re.escape(pattern[cursor:]))
        try:
            return bool(re.fullmatch("".join(pieces), target))
        except re.error:
            return False

    @classmethod
    def _group_names(cls, statement: str, keyword: str) -> list[str]:
        """解析 apply-groups 或 apply-groups-except 的名称列表。"""
        base = cls._base_header(statement).rstrip(";").strip()
        match = re.match(rf"{re.escape(keyword)}\s+(.+)$", base)
        if not match:
            return []
        value = match.group(1).strip().strip("[]").strip()
        return [token.strip("'\"") for token in value.split() if token]

    @staticmethod
    def _junos_selector_specificity(header: str) -> tuple[int, str]:
        """以通配表达式中的字面量长度衡量选择器具体程度。"""
        literal = re.sub(r"<([^>]+)>", lambda item: re.sub(r"[*?\[\]]", "", item.group(1)), header)
        return (len(literal), header)

    def _group_payload_for_path(self, group: JunosNode, path: list[str]) -> list[JunosNode]:
        """沿配置路径找到 group 在该层级应继承的子节点。"""
        assert group.children is not None
        candidates = group.children
        for component in path:
            matches = [
                item
                for item in candidates
                if item.active and item.is_block and self._header_matches(item.header, component)
            ]
            if not matches:
                return []
            matches.sort(key=lambda item: (-self._junos_selector_specificity(item.header)[0], item.header))
            candidates = [child for match in matches for child in (match.children or [])]
        return candidates

    @staticmethod
    def _record_junos_conflict(
        outcome: GroupExpansionOutcome,
        path: list[str],
        identity: str,
        winner: JunosNode,
        loser: JunosNode,
    ) -> None:
        """记录值不同的 group 冲突，完全相同的重复语句不记录。"""
        if _normalized_command(winner.header) == _normalized_command(loser.header):
            return
        outcome.conflicts.append(
            {
                "vendor": "juniper_junos",
                "path": " / ".join(path) or "<root>",
                "key": identity,
                "winner_source": winner.origin,
                "winner_value": winner.header,
                "loser_source": loser.origin,
                "loser_value": loser.header,
            }
        )

    def _merge_junos_group_children(
        self,
        target: JunosNode,
        source_children: list[JunosNode],
        group_name: str,
        rank: tuple[int, int],
        path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> bool:
        """按显式、嵌套层级和列表顺序合并 group 子节点。"""
        assert target.children is not None
        for source in source_children:
            if not source.active or not source.header:
                continue
            if self._group_names(source.header, "apply-groups"):
                outcome.warnings.append(
                    f"Junos 组 {group_name} 内再次引用 apply-groups，整台设备的 group 展开已回滚"
                )
                return False
            if self._group_names(source.header, "apply-groups-except"):
                continue
            if source.is_block:
                matches = [
                    item
                    for item in target.children
                    if item.active and item.is_block and self._header_matches(source.header, item.header)
                ]
                if matches or "<" in source.header:
                    continue
                target.children.append(
                    JunosNode(
                        source.header,
                        [],
                        origin=f"group:{group_name}",
                        rank=rank,
                    )
                )
                continue

            identity = _junos_statement_identity(self._base_header(source.header))
            existing = next(
                (
                    item
                    for item in target.children
                    if item.active
                    and not item.is_block
                    and _junos_statement_identity(self._base_header(item.header)) == identity
                ),
                None,
            )
            candidate = JunosNode(
                source.header,
                origin=f"group:{group_name}",
                rank=rank,
            )
            if existing is None:
                target.children.append(candidate)
                continue
            if existing.origin != "explicit" and rank > existing.rank:
                self._record_junos_conflict(outcome, path, identity, candidate, existing)
                existing.header = candidate.header
                existing.origin = candidate.origin
                existing.rank = candidate.rank
            else:
                self._record_junos_conflict(outcome, path, identity, existing, candidate)
        return True

    def expand_groups(self, known_interfaces: Iterable[str]) -> GroupExpansionOutcome:
        """事务式展开 Junos groups，并执行本地/内层优先规则。"""

        outcome = GroupExpansionOutcome()
        # 所有修改都发生在深拷贝上；任一引用无法解析即可整体回滚。
        original_root = self.root
        working = copy.deepcopy(self.root)
        assert working.children is not None
        groups_container = next(
            (
                node
                for node in working.children
                if node.active and node.is_block and self._base_header(node.header) == "groups"
            ),
            None,
        )
        if not groups_container or groups_container.children is None:
            return outcome
        groups = {
            self._base_header(node.header): node
            for node in groups_container.children
            if node.active and node.is_block
        }

        # 将拓扑中出现但配置未声明的接口加入临时树，供通配 group 匹配。
        interfaces = next(
            (
                node
                for node in working.children
                if node.active and node.is_block and self._base_header(node.header) == "interfaces"
            ),
            None,
        )
        if interfaces is None:
            interfaces = JunosNode("interfaces", [])
            working.children.append(interfaces)
        assert interfaces.children is not None
        existing_names = {
            canonical_junos_interface(self._base_header(node.header).split()[0])
            for node in interfaces.children
            if node.active and node.is_block
        }
        for raw_name in known_interfaces:
            name = canonical_junos_interface(interface_parent(raw_name))
            if name not in existing_names:
                interfaces.children.append(JunosNode(name, [], origin="synthetic"))
                existing_names.add(name)

        all_applied: set[str] = set()
        unresolved = False

        def walk(
            node: JunosNode,
            path: list[str],
            inherited: list[tuple[str, tuple[int, int]]],
            inherited_excluded: set[str],
        ) -> None:
            """递归计算当前层级的有效 group、排除列表与继承优先级。"""
            nonlocal unresolved
            if node.children is None or node is groups_container:
                return
            local_excluded = set(inherited_excluded)
            local_names: list[str] = []
            for child in node.children:
                if not child.active or child.is_block:
                    continue
                local_names.extend(self._group_names(child.header, "apply-groups"))
                local_excluded.update(self._group_names(child.header, "apply-groups-except"))
            all_applied.update(local_names)
            for name in local_names:
                if name not in groups:
                    outcome.warnings.append(f"Junos apply-groups 引用了未定义的组 {name}")
                    unresolved = True

            # 层级越深越优先；同一列表中越靠前越优先。
            local = [(name, (len(path), -index)) for index, name in enumerate(local_names)]
            active: list[tuple[str, tuple[int, int]]] = []
            for item in [*local, *inherited]:
                if item[0] in local_excluded or any(existing[0] == item[0] for existing in active):
                    continue
                active.append(item)

            for group_name, rank in active:
                group = groups.get(group_name)
                if group is None:
                    continue
                payload = self._group_payload_for_path(group, path)
                if not self._merge_junos_group_children(
                    node,
                    payload,
                    group_name,
                    rank,
                    path,
                    outcome,
                ):
                    unresolved = True

            node.children = [
                child
                for child in node.children
                if not self._group_names(child.header, "apply-groups")
                and not self._group_names(child.header, "apply-groups-except")
            ]
            index = 0
            while index < len(node.children):
                child = node.children[index]
                if child.active and child.is_block and child is not groups_container:
                    walk(
                        child,
                        [*path, self._base_header(child.header)],
                        active,
                        local_excluded,
                    )
                index += 1

        walk(working, [], [], set())
        if unresolved:
            # 不把 working 写回，确保用户原始配置保持完整。
            self.root = original_root
            outcome.events.append("Junos group 展开未完整解析，已整体回滚并保留原配置")
            outcome.conflicts.clear()
            outcome.success = False
            return outcome
        if not all_applied:
            self.root = original_root
            return outcome

        # 全部展开成功后才隐藏 groups 定义和 apply 语句。
        groups_container.active = False
        self.root = working
        outcome.events.extend(f"已展开 Junos 配置组 {name}" for name in sorted(all_applied))
        return outcome

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取物理接口和 unit 的父子关系、VLAN 与接口类型。"""
        result: list[InterfaceSpec] = []
        for node in self._interface_nodes():
            parent = self._interface_name(node)
            units = self._unit_nodes(node)
            if not units:
                result.append(InterfaceSpec(parent, parent, None, None, _junos_interface_kind(parent)))
                continue
            for unit in units:
                number = self._unit_number(unit)
                result.append(
                    InterfaceSpec(
                        name=f"{parent}.{number}",
                        parent=parent,
                        unit=number,
                        vlan=self._find_vlan(unit),
                        kind=_junos_interface_kind(parent),
                    )
                )
        return result

    def bundle_members(self) -> dict[str, str]:
        """返回物理接口到 ae 聚合接口的映射。"""
        result: dict[str, str] = {}
        for node in self._interface_nodes():
            parent = self._interface_name(node)
            if _junos_interface_kind(parent) != "physical":
                continue
            rendered = self._render_node(node, 0)
            match = re.search(r"\b802\.3ad\s+(ae\d+)\s*;", rendered)
            if match:
                result[parent] = match.group(1)
        return result

    def resolve_interface(self, value: str) -> str:
        """规范化拓扑中的 Junos 接口名。"""
        return canonical_junos_interface(value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口下所有已配置 unit。"""
        parent = canonical_junos_interface(parent)
        return sorted(
            {spec.name for spec in self.interface_specs() if spec.parent == parent},
            key=lambda value: (interface_unit(value) is not None, value),
        )

    def _find_interface_node(self, name: str) -> JunosNode | None:
        """按父接口名查找接口节点。"""
        canonical = canonical_junos_interface(interface_parent(name))
        return next((node for node in self._interface_nodes() if self._interface_name(node) == canonical), None)

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """停用整个接口节点；Junos unit 随父节点一并停用。"""
        node = self._find_interface_node(name)
        if node:
            node.active = False

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """改名接口节点，并可移除原聚合相关 options。"""
        node = self._find_interface_node(source)
        if not node:
            return
        node.header = canonical_junos_interface(target)
        if strip_bundle and node.children is not None:
            node.children = [
                child
                for child in node.children
                if not (
                    child.is_block
                    and self._base_header(child.header) in {"aggregated-ether-options", "gigether-options", "ether-options"}
                )
            ]
        self._merge_duplicate_interface(node)

    def _merge_duplicate_interface(self, preferred: JunosNode) -> None:
        """接口改名碰撞时合并不重复的子节点并停用旧节点。"""
        name = self._interface_name(preferred)
        duplicates = [node for node in self._interface_nodes() if self._interface_name(node) == name]
        if len(duplicates) < 2:
            return
        assert preferred.children is not None
        existing = {self._render_node(child, 0) for child in preferred.children}
        for node in duplicates:
            if node is preferred or node.children is None:
                continue
            for child in node.children:
                rendered = self._render_node(child, 0)
                if rendered not in existing:
                    preferred.children.append(child)
                    existing.add(rendered)
            node.active = False

    def _ensure_target_parent(self, target_parent: str) -> JunosNode:
        """确保 UNI 目标口存在并支持灵活 VLAN 封装。"""
        existing = self._find_interface_node(target_parent)
        if existing:
            return existing
        interfaces = self._interfaces_block(create=True)
        assert interfaces is not None and interfaces.children is not None
        node = JunosNode(target_parent, [JunosNode("flexible-vlan-tagging;"), JunosNode("encapsulation flexible-ethernet-services;")])
        interfaces.children.append(node)
        return node

    def map_uni(self, source: str, target_parent: str, vlan: int) -> str:
        """复制源 unit 配置到目标父接口，并将 unit/vlan-id 统一为新 VLAN。"""
        source_parent = interface_parent(canonical_junos_interface(source))
        unit_number = interface_unit(source)
        source_node = self._find_interface_node(source_parent)
        target_node = self._ensure_target_parent(target_parent)
        assert target_node.children is not None
        unit_node = None
        if source_node:
            units = self._unit_nodes(source_node)
            if unit_number is not None:
                unit_node = next((item for item in units if self._unit_number(item) == unit_number), None)
            elif len(units) == 1:
                unit_node = units[0]
            elif not units:
                payload = [child.clone() for child in (source_node.children or [])]
                unit_node = JunosNode("unit 0", payload)
        if unit_node is None:
            unit_node = JunosNode("unit 0", [])
        else:
            unit_node = unit_node.clone()
        unit_node.header = f"unit {vlan}"
        assert unit_node.children is not None
        unit_node.children = [
            child for child in unit_node.children if not re.match(r"vlan-id\s+\d+\s*;", self._base_header(child.header))
        ]
        unit_node.children.insert(0, JunosNode(f"vlan-id {vlan};"))
        target_node.children.append(unit_node)
        return f"{target_parent}.{vlan}"

    def finalize_uni_source(self, source_parent: str) -> None:
        """所有 unit 迁移完成后停用源 UNI 父接口。"""
        self.remove_interface(source_parent, include_children=True)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在。"""
        self._ensure_target_parent(name)

    def clean_authentication(self) -> AuthenticationCleanupOutcome:
        """删除原 login/class、root 密码、认证顺序和外部服务器。"""
        system = self._top_block("system", create=True)
        assert system is not None and system.children is not None
        outcome = AuthenticationCleanupOutcome()
        blocked = {"login", "root-authentication", "authentication-order", "radius-server", "tacplus-server"}
        for child in system.children:
            base = self._base_header(child.header)
            first = base.split(maxsplit=1)[0].rstrip(";") if base else ""
            if first in blocked:
                child.active = False
                outcome.record(first)
        return outcome

    def add_lab_account(self) -> None:
        """写入实验账号，并设置 root 密码以满足 Junos 提交要求。"""
        system = self._top_block("system", create=True)
        assert system is not None and system.children is not None
        system.children.extend(
            [
                JunosNode(f'root-authentication encrypted-password "{JUNOS_LAB_PASSWORD_HASH}";'),
                JunosNode(
                    "login",
                    [
                        JunosNode(
                            f"user {LAB_USERNAME}",
                            [
                                JunosNode("class super-user;"),
                                JunosNode(
                                    "authentication",
                                    [JunosNode(f'encrypted-password "{JUNOS_LAB_PASSWORD_HASH}";')],
                                ),
                            ],
                        )
                    ],
                ),
            ]
        )

    def replace_references(self, replacements: dict[str, str]) -> None:
        """递归更新 interfaces 之外的协议和策略接口引用。"""
        relevant = {canonical_junos_interface(k): v for k, v in replacements.items() if k != v}
        if not relevant:
            return
        names = sorted(relevant, key=len, reverse=True)
        pattern = re.compile(r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_.-])")

        def walk(node: JunosNode, inside_interfaces: bool = False) -> None:
            """跳过接口定义本身，只处理其他层级中的引用。"""
            current_inside = inside_interfaces or (node is not self.root and self._base_header(node.header) == "interfaces")
            if node is not self.root and not current_inside:
                node.header = pattern.sub(lambda match: relevant[match.group(1)], node.header)
            if node.children:
                for child in node.children:
                    walk(child, current_inside)

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
