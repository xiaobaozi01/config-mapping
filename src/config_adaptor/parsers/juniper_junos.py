"""Juniper Junos 大括号配置解析、groups 展开、接口改写和认证清洗。"""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable

from ..constants import JUNOS_LAB_PASSWORD_HASH, LAB_USERNAME
from ..models import WashingPolicy
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
    if parent == "irb":
        return "gateway"
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

    @classmethod
    def _rewrite_group_control(cls, statement: str, keyword: str, remaining: list[str]) -> str | None:
        """从 apply-groups 语句中只移除已经展开的 group。"""
        base = cls._base_header(statement)
        match = re.match(rf"({re.escape(keyword)})\s+(.+?)\s*;\s*$", base)
        if not match:
            return statement
        if not remaining:
            return None
        prefix = statement[: statement.find(base)] if base in statement else ""
        original_value = match.group(2).strip()
        value = " ".join(remaining)
        if original_value.startswith("[") and original_value.endswith("]"):
            value = f"[ {value} ]"
        return f"{prefix}{match.group(1)} {value};"

    @classmethod
    def _group_path_relevant(cls, path: list[str], policy: WashingPolicy) -> bool:
        """判断 Junos 路径是否影响接口迁移或已启用的清洗范围。"""
        if not path:
            return False
        normalized = [_normalized_command(cls._base_header(component)) for component in path]
        top = normalized[0]
        if top in {
            "interfaces",
            "protocols",
            "routing-instances",
            "logical-systems",
            "bridge-domains",
            "vlans",
            "l2vpn",
            "routing-options",
        }:
            return True
        if top == "snmp":
            return True
        if top == "system" and len(normalized) > 1:
            second = normalized[1]
            if second.startswith(
                (
                    "login",
                    "root-authentication",
                    "authentication-order",
                    "radius-",
                    "tacplus-",
                    "accounting",
                )
            ):
                return True
            if second == "services" and len(normalized) > 2:
                return normalized[2].startswith(("ssh", "telnet", "netconf"))
        if top == "security" and len(normalized) > 1:
            second = normalized[1]
            if second == "ssh-known-hosts":
                return True
            if policy.protocol_authentication and second == "authentication-key-chains":
                return True
            if policy.pki and second in {"pki", "certificates"}:
                return True
            if policy.nat and second in {"nat", "services"}:
                return True
        if policy.hardware and top == "chassis":
            return True
        if policy.nat and top in {"services", "service-set"}:
            return True
        if policy.flow_statistics and top == "forwarding-options":
            return True
        return False

    def _group_tree_relevant(self, group: JunosNode, policy: WashingPolicy) -> bool:
        """检查 group 定义中是否包含需要物化后再处理的配置。"""
        def walk(items: list[JunosNode], path: list[str]) -> bool:
            for item in items:
                if not item.active:
                    continue
                current = [*path, self._base_header(item.header)]
                if self._group_path_relevant(current, policy):
                    return True
                if item.children is not None and walk(item.children, current):
                    return True
            return False

        return walk(group.children or [], [])

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

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        mode: str = "relevant",
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """事务式展开 Junos groups，并执行本地/内层优先规则。"""

        outcome = GroupExpansionOutcome()
        policy = policy or WashingPolicy()
        if mode == "preserve":
            return outcome
        if mode not in {"relevant", "strict"}:
            raise ValueError(f"未知 Junos group 处理模式: {mode}")
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

        selected_groups: set[str] = set()

        def select_groups(node: JunosNode, path: list[str]) -> None:
            """预先选出本次要展开的 group，未选中的控制语句保持原样。"""
            if node.children is None or node is groups_container:
                return
            for child in node.children:
                if not child.active:
                    continue
                names = self._group_names(child.header, "apply-groups") if not child.is_block else []
                for name in names:
                    group = groups.get(name)
                    if mode == "strict" or (
                        not path and group is None
                    ) or self._group_path_relevant(path, policy) or (
                        group is not None and self._group_tree_relevant(group, policy)
                    ):
                        selected_groups.add(name)
                if child.is_block:
                    select_groups(child, [*path, self._base_header(child.header)])

        select_groups(working, [])
        if not selected_groups:
            self.root = original_root
            return outcome

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
                local_names.extend(
                    name
                    for name in self._group_names(child.header, "apply-groups")
                    if name in selected_groups
                )
                local_excluded.update(
                    name
                    for name in self._group_names(child.header, "apply-groups-except")
                    if name in selected_groups
                )
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

            retained: list[JunosNode] = []
            for child in node.children:
                rewritten: str | None = child.header
                if not child.is_block:
                    for keyword in ("apply-groups", "apply-groups-except"):
                        names = self._group_names(child.header, keyword)
                        if names:
                            rewritten = self._rewrite_group_control(
                                child.header,
                                keyword,
                                [name for name in names if name not in selected_groups],
                            )
                            break
                if rewritten is not None:
                    child.header = rewritten
                    retained.append(child)
            node.children = retained
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

        # 全部展开成功后才隐藏已物化的定义；无关 group 和应用语句继续保留。
        if mode == "strict":
            groups_container.active = False
        else:
            for group in groups_container.children:
                if self._base_header(group.header) in all_applied:
                    group.active = False
            groups_container.active = any(group.active for group in groups_container.children)
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
                vlan = self._find_vlan(unit)
                # IRB unit 编号通常对应业务 VLAN，无显式 vlan-id 时用作提示值。
                if vlan is None and parent.lower() == "irb" and number.isdigit() and 1 <= int(number) <= 4094:
                    vlan = int(number)
                result.append(
                    InterfaceSpec(
                        name=f"{parent}.{number}",
                        parent=parent,
                        unit=number,
                        vlan=vlan,
                        kind=_junos_interface_kind(parent),
                        inner_vlan=self._find_inner_vlan(unit),
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

    def business_interface_names(self) -> set[str]:
        """识别承载三层、二层及活跃广播域网关的 Junos 接口。

        IRB 不因自身配置地址就自动迁移；只有 bridge-domain/vlan 中仍有
        活跃接入口，或其 unit 命中活跃业务 VLAN 时才进入 UNI 计划。
        """
        specs = self.interface_specs()
        known = {spec.name for spec in specs}
        kinds = {spec.name: spec.kind for spec in specs}
        active: set[str] = set()
        business_pattern = re.compile(
            r"\b(?:family\s+(?:inet6?|ccc|bridge|ethernet-switching)|"
            r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|input-vlan-map|output-vlan-map)\b"
        )
        for node in self._interface_nodes():
            parent = self._interface_name(node)
            units = self._unit_nodes(node)
            if not units:
                rendered = self._render_node(node, 0)
                if _junos_interface_kind(parent) != "gateway" and business_pattern.search(rendered):
                    active.add(parent)
                continue
            for unit in units:
                rendered = self._render_node(unit, 0)
                name = f"{parent}.{self._unit_number(unit)}"
                if kinds.get(name) != "gateway" and business_pattern.search(rendered):
                    active.add(name)

        # protocols/l2circuit/bridge-domains/vlans/routing-instances 等树中的引用
        # 均是业务活跃的可验证信号。
        external = "\n".join(
            self._render_node(node, 0)
            for node in (self.root.children or [])
            if node.active and self._base_header(node.header) not in {"interfaces", "groups"}
        )
        for name in known:
            if kinds.get(name) != "gateway" and re.search(
                rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])", external
            ):
                active.add(name)
        for parent in {spec.parent for spec in specs}:
            parent_specs = [spec for spec in specs if spec.parent == parent]
            if parent_specs and parent_specs[0].kind != "gateway" and re.search(
                rf"(?<![A-Za-z0-9_.-]){re.escape(parent)}(?![A-Za-z0-9_.-])", external
            ):
                active.update(spec.name for spec in parent_specs)

        def expand_vlan_tokens(value: str) -> set[int]:
            """展开 Junos VLAN 列表中的单值及 ``100-110`` 范围。"""
            result: set[int] = set()
            for start, end, single in re.findall(r"(?:(\d+)\s*-\s*(\d+))|(\d+)", value):
                if single:
                    number = int(single)
                    if 1 <= number <= 4094:
                        result.add(number)
                    continue
                lower, upper = int(start), int(end)
                if 1 <= lower <= upper <= 4094:
                    result.update(range(lower, upper + 1))
            return result

        spec_by_name = {spec.name: spec for spec in specs}
        active_vlans: set[int] = set()
        active_vlan_names: set[str] = set()
        for name in list(active):
            spec = spec_by_name.get(name)
            if not spec or spec.kind == "gateway":
                continue
            node = self._find_interface_node(spec.parent)
            if not node:
                continue
            units = self._unit_nodes(node)
            unit = next(
                (item for item in units if spec.unit is not None and self._unit_number(item) == spec.unit),
                node if spec.unit is None else None,
            )
            if unit is None:
                continue
            rendered = self._render_node(unit, 0)
            is_l2 = bool(
                re.search(
                    r"\b(?:family\s+(?:ccc|bridge|ethernet-switching)|"
                    r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
                    r"vlan-id-list|vlan\s+members|input-vlan-map|output-vlan-map)\b",
                    rendered,
                )
            )
            if is_l2 and spec.vlan is not None:
                active_vlans.add(spec.vlan)
            for match in re.finditer(r"vlan-id-list\s+\[([^\]]+)\]", rendered):
                active_vlans.update(expand_vlan_tokens(match.group(1)))
            for match in re.finditer(r"vlan\s+members\s+(?:\[([^\]]+)\]|([^;\s]+))\s*;", rendered):
                payload = match.group(1) or match.group(2) or ""
                active_vlans.update(expand_vlan_tokens(payload))
                active_vlan_names.update(
                    token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload)
                    if not token.isdigit()
                )

        # bridge-domains/vlans 既可能位于根层级，也可能嵌在 routing-instance。
        # 每个广播域只有在关联活跃接入口、活跃 VLAN ID 或活跃 VLAN 名时，
        # 才把其 routing-interface/l3-interface IRB 纳入迁移。
        def walk_domains(node: JunosNode) -> None:
            if node.children is None:
                return
            base = self._base_header(node.header).rstrip(";")
            if base in {"bridge-domains", "vlans"}:
                for domain in node.children:
                    if not domain.active or domain.children is None:
                        continue
                    rendered = self._render_node(domain, 0)
                    interfaces = {
                        canonical_junos_interface(value)
                        for value in re.findall(r"(?<![-\w])interface\s+([^;\s]+)\s*;", rendered)
                    }
                    gateways = {
                        canonical_junos_interface(value)
                        for value in re.findall(
                            r"\b(?:routing-interface|l3-interface)\s+([^;\s]+)\s*;", rendered
                        )
                    }
                    vlan_ids = {
                        int(value)
                        for value in re.findall(r"\bvlan-id\s+(\d+)\s*;", rendered)
                        if 1 <= int(value) <= 4094
                    }
                    domain_name = self._base_header(domain.header).split()[0].rstrip(";")
                    if interfaces & active or vlan_ids & active_vlans or domain_name in active_vlan_names:
                        active.update(gateway for gateway in gateways if gateway in known)
            for child in node.children:
                if child.active:
                    walk_domains(child)

        walk_domains(self.root)
        for spec in specs:
            if spec.kind == "gateway" and spec.vlan in active_vlans:
                active.add(spec.name)
        return active

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

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把 Junos 接口树复制到新物理口，用于 M-LAG 按对端拆分。"""
        source_node = self._find_interface_node(source)
        interfaces = self._interfaces_block(create=True)
        assert interfaces is not None and interfaces.children is not None
        if source_node is None:
            clone = JunosNode(canonical_junos_interface(target), [])
        else:
            clone = source_node.clone()
            clone.header = canonical_junos_interface(target)
            clone.active = True
            if strip_bundle and clone.children is not None:
                clone.children = [
                    child
                    for child in clone.children
                    if not (
                        child.is_block
                        and self._base_header(child.header)
                        in {"aggregated-ether-options", "gigether-options", "ether-options"}
                    )
                ]
        interfaces.children.append(clone)
        self._merge_duplicate_interface(clone)

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

    def _strip_vlan_termination(self, nodes: list[JunosNode]) -> list[JunosNode]:
        """递归删除旧标签匹配和 VLAN rewrite，同时保留业务 family/CCC 类型。"""
        blocked_statements = re.compile(
            r"^(?:vlan-id(?:-list)?|vlan-tags|native-vlan-id|"
            r"input-vlan-map|output-vlan-map|interface-mode|"
            r"flexible-vlan-tagging|stacked-vlan-tagging|vlan-tagging)\b"
        )
        retained: list[JunosNode] = []
        for node in nodes:
            base = self._base_header(node.header).rstrip(";")
            if blocked_statements.match(base) or re.match(r"^vlan\s+members\b", base):
                continue
            # family ethernet-switching/bridge 中的 ``vlan { members ... }``
            # 整块属于旧入口匹配，不能随 QinQ 目标 unit 保留。
            if node.is_block and base == "vlan":
                continue
            clone = node.clone()
            if clone.children is not None:
                clone.children = self._strip_vlan_termination(clone.children)
            retained.append(clone)
        return retained

    def _ensure_target_parent(self, target_parent: str) -> JunosNode:
        """确保 UNI 目标口存在并支持灵活 VLAN 封装。"""
        existing = self._find_interface_node(target_parent)
        if existing:
            node = existing
        else:
            interfaces = self._interfaces_block(create=True)
            assert interfaces is not None and interfaces.children is not None
            node = JunosNode(target_parent, [])
            interfaces.children.append(node)
        assert node.children is not None
        # 目标口可能在原配置中已有单层/堆叠标签设置，统一清理后再写入
        # 本次转换唯一允许的 flexible QinQ 父接口属性。
        node.children = [
            child
            for original in node.children
            for child in (
                [original]
                if original.is_block and self._base_header(original.header).startswith("unit ")
                else self._strip_vlan_termination([original])
            )
        ]
        node.children = [
            child
            for child in node.children
            if not re.match(r"^encapsulation\s+(?:ethernet-bridge|vlan-bridge)\b", self._base_header(child.header))
        ]
        required = ["flexible-vlan-tagging;", "encapsulation flexible-ethernet-services;"]
        existing_headers = {self._base_header(child.header) for child in node.children}
        for statement in reversed(required):
            if statement not in existing_headers:
                node.children.insert(0, JunosNode(statement))
        return node

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str:
        """复制源 unit 到目标父接口，并统一重写为 QinQ vlan-tags。"""
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
        unit_node.children = self._strip_vlan_termination(unit_node.children)
        unit_node.children.insert(0, JunosNode(f"vlan-tags outer {vlan} inner {inner_vlan};"))
        target_node.children.append(unit_node)
        return f"{target_parent}.{vlan}"

    def finalize_uni_source(self, source_parent: str) -> None:
        """所有 unit 迁移完成后停用源 UNI 父接口。"""
        self.remove_interface(source_parent, include_children=True)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在。"""
        self._ensure_target_parent(name)

    def clean_authentication(
        self,
        policy: WashingPolicy | None = None,
    ) -> AuthenticationCleanupOutcome:
        """清理管理面认证，并按策略删除明确启用的扩展配置。"""
        policy = policy or WashingPolicy()
        system = self._top_block("system", create=True)
        assert system is not None and system.children is not None
        outcome = AuthenticationCleanupOutcome()
        blocked = {
            "login",
            "root-authentication",
            "authentication-order",
            "radius-server",
            "tacplus-server",
            "radius-options",
            "tacplus-options",
            "accounting",
        }
        for child in system.children:
            base = self._base_header(child.header)
            first = base.split(maxsplit=1)[0].rstrip(";") if base else ""
            if first in blocked:
                child.active = False
                outcome.record(first)

        def first_token(node: JunosNode) -> str:
            """返回节点语句的第一个关键字。"""
            base = self._base_header(node.header)
            return base.split(maxsplit=1)[0].rstrip(";") if base else ""

        def disable_matching(
            node: JunosNode,
            keywords: set[str],
            category: str,
            recursive: bool = False,
        ) -> None:
            """按层级关键字停用子节点，并只在需要时递归。"""
            if node.children is None:
                return
            for child in node.children:
                if not child.active:
                    continue
                if first_token(child) in keywords:
                    child.active = False
                    outcome.record(category)
                    continue
                if recursive:
                    disable_matching(child, keywords, category, recursive=True)

        services = next(
            (
                child
                for child in system.children
                if child.active and child.is_block and self._base_header(child.header) == "services"
            ),
            None,
        )
        if services:
            disable_matching(services, {"ssh", "outbound-ssh", "telnet"}, "remote-access", True)
            for child in services.children or []:
                if (
                    child.active
                    and first_token(child) == "netconf"
                    and child.children is not None
                    and not any(grandchild.active for grandchild in child.children)
                ):
                    child.active = False
                    outcome.record("remote-access")

        snmp = self._top_block("snmp")
        if snmp:
            snmp.active = False
            outcome.record("snmp")

        security = self._top_block("security")
        if security:
            disable_matching(security, {"ssh-known-hosts"}, "ssh-trust")

        if policy.protocol_authentication:
            if security:
                disable_matching(
                    security,
                    {"authentication-key-chains"},
                    "protocol-auth-definition",
                )
            protocol_keywords = {
                "authentication",
                "authentication-key",
                "authentication-key-chain",
                "authentication-algorithm",
                "authentication-type",
            }
            for root_name in ("protocols", "routing-instances", "logical-systems", "interfaces"):
                root = self._top_block(root_name)
                if root:
                    disable_matching(
                        root,
                        protocol_keywords,
                        "protocol-auth-reference",
                        recursive=True,
                    )

        if policy.pki:
            if security:
                disable_matching(security, {"pki", "certificates"}, "pki")
            disable_matching(system, {"certificates"}, "pki")

        if policy.hardware:
            chassis = self._top_block("chassis")
            if chassis:
                chassis.active = False
                outcome.record("hardware")

        if policy.nat:
            if security:
                disable_matching(security, {"nat"}, "nat")
            services_top = self._top_block("services")
            if services_top:
                disable_matching(services_top, {"nat", "nat-rules"}, "nat", recursive=True)

        if policy.flow_statistics:
            services_top = self._top_block("services")
            if services_top:
                disable_matching(services_top, {"flow-monitoring"}, "flow-statistics", True)
            forwarding = self._top_block("forwarding-options")
            if forwarding:
                disable_matching(forwarding, {"sampling"}, "flow-statistics", True)
            interfaces = self._top_block("interfaces")
            if interfaces:
                disable_matching(interfaces, {"sampling"}, "flow-statistics", True)
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
