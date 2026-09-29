"""Cisco IOS XR 配置解析、group 展开、接口改写和认证清洗。"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from typing import Iterable

from ..constants import LAB_PASSWORD, LAB_USERNAME
from .common import (
    AuthenticationCleanupOutcome,
    GroupExpansionOutcome,
    InterfaceSpec,
    interface_parent,
    interface_unit,
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
    compact = re.sub(r"\s+", "", value.strip())
    match = re.match(r"([A-Za-z-]+)(.*)", compact)
    if not match:
        return compact
    prefix, suffix = match.groups()
    expanded = _CISCO_PREFIXES.get(prefix.lower(), prefix)
    return f"{expanded}{suffix}"


def _cisco_interface_kind(name: str) -> str:
    """按 IOS XR 命名规则区分物理口、聚合口、环回口和管理口。"""
    parent = interface_parent(name).lower()
    if parent.startswith("loopback"):
        return "loopback"
    if parent.startswith("mgmteth"):
        return "management"
    if parent.startswith("bundle-ether"):
        return "bundle"
    return "physical"


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
        return canonical_cisco_interface(match.group(1)) if match else None


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
        return [block for block in self.blocks if block.active and block.interface_name]

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取接口父子关系、VLAN 和类型，供 NNI/UNI 分类使用。"""
        result: list[InterfaceSpec] = []
        for block in self._interface_blocks():
            name = block.interface_name
            assert name is not None
            vlan = None
            for line in block.lines:
                match = re.match(r"\s*encapsulation\s+dot1q\s+(\d+)", line, re.IGNORECASE)
                if match:
                    vlan = int(match.group(1))
                    break
            result.append(
                InterfaceSpec(
                    name=name,
                    parent=interface_parent(name),
                    unit=interface_unit(name),
                    vlan=vlan,
                    kind=_cisco_interface_kind(name),
                )
            )
        return result

    def bundle_members(self) -> dict[str, str]:
        """返回物理成员接口到 Bundle-Ether 的映射。"""
        result: dict[str, str] = {}
        for block in self._interface_blocks():
            name = block.interface_name
            assert name is not None
            if _cisco_interface_kind(name) != "physical" or interface_unit(name) is not None:
                continue
            for line in block.lines:
                match = re.match(r"\s*bundle\s+id\s+(\d+)\b", line, re.IGNORECASE)
                if match:
                    result[name] = f"Bundle-Ether{match.group(1)}"
                    break
        return result

    def resolve_interface(self, value: str) -> str:
        """把拓扑接口名转换成配置解析器使用的规范形式。"""
        canonical = canonical_cisco_interface(value)
        known = {spec.name for spec in self.interface_specs()}
        parents = {spec.parent for spec in self.interface_specs()}
        if canonical in known or canonical in parents:
            return canonical
        return canonical

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口及其所有已配置子接口。"""
        parent = canonical_cisco_interface(parent)
        return sorted(
            {spec.name for spec in self.interface_specs() if spec.parent == parent},
            key=lambda value: (interface_unit(value) is not None, value),
        )

    def _find_interface_block(self, name: str) -> CiscoBlock | None:
        """按规范化名称查找一个有效接口块。"""
        canonical = canonical_cisco_interface(name)
        return next((block for block in self._interface_blocks() if block.interface_name == canonical), None)

    @staticmethod
    def _parse_cisco_nodes(lines: Iterable[str], origin: str = "explicit") -> list[_CiscoNode]:
        """把块内缩进文本转成树，以便按完整路径合并 group。"""
        root = _CiscoNode("<root>", is_block=True, origin=origin)
        stack: list[tuple[int, _CiscoNode]] = [(-1, root)]
        for raw in lines:
            if not raw.strip() or raw.strip() == "!":
                continue
            expanded = raw.expandtabs(8)
            indent = len(expanded) - len(expanded.lstrip())
            while len(stack) > 1 and indent <= stack[-1][0]:
                stack.pop()
            node = _CiscoNode(raw.strip(), origin=origin)
            parent = stack[-1][1]
            parent.children.append(node)
            parent.is_block = True
            stack.append((indent, node))
        return root.children

    @staticmethod
    def _render_cisco_nodes(nodes: list[_CiscoNode], depth: int = 1) -> list[str]:
        """把临时语法树重新渲染为 IOS XR 缩进文本。"""
        lines: list[str] = []
        for node in nodes:
            lines.append(" " * depth + node.command)
            lines.extend(CiscoDocument._render_cisco_nodes(node.children, depth + 1))
        return lines

    @staticmethod
    def _cisco_group_names(command: str, keyword: str) -> list[str]:
        """解析 apply/exclude-group 后的单个名称或名称列表。"""
        match = re.match(rf"{re.escape(keyword)}s?\s+(.+?)\s*$", command.strip(), re.IGNORECASE)
        if not match:
            return []
        return [token.strip("'\"") for token in match.group(1).strip().strip("[]").split() if token]

    @staticmethod
    def _cisco_pattern_match(pattern: str, target: str) -> bool:
        """匹配精确选择器或引号内的 IOS XR 正则选择器。"""
        pattern = " ".join(pattern.strip().split())
        target = " ".join(target.strip().split())
        if "'" not in pattern and '"' not in pattern:
            if pattern.lower().startswith("interface ") and target.lower().startswith("interface "):
                return canonical_cisco_interface(pattern.split(maxsplit=1)[1]) == canonical_cisco_interface(
                    target.split(maxsplit=1)[1]
                )
            return pattern.lower() == target.lower()
        pieces: list[str] = []
        cursor = 0
        for match in re.finditer(r"(['\"])(.*?)\1", pattern):
            pieces.append(re.escape(pattern[cursor : match.start()]).replace(r"\ ", r"\s+"))
            pieces.append(f"(?:{match.group(2)})")
            cursor = match.end()
        pieces.append(re.escape(pattern[cursor:]).replace(r"\ ", r"\s+"))
        try:
            return bool(re.fullmatch("".join(pieces), target, re.IGNORECASE))
        except re.error:
            return False

    @staticmethod
    def _selector_specificity(command: str) -> tuple[int, str]:
        """以正则中的字面量长度衡量选择器具体程度。"""
        literal = re.sub(r"(['\"])(.*?)\1", lambda item: re.sub(r"[.*+?\[\](){}|\\]", "", item.group(2)), command)
        return (len(literal), command)

    def _cisco_group_payload(
        self,
        group_roots: list[_CiscoNode],
        path: list[str],
    ) -> list[_CiscoNode]:
        """沿目标路径查找一个 group 应继承的配置片段。"""
        candidates = group_roots
        for component in path:
            matches = [node for node in candidates if self._cisco_pattern_match(node.command, component)]
            if not matches:
                return []
            matches.sort(key=lambda node: (-self._selector_specificity(node.command)[0], node.command))
            candidates = [child for match in matches for child in match.children]
        return candidates

    @staticmethod
    def _record_group_conflict(
        outcome: GroupExpansionOutcome,
        vendor: str,
        path: list[str],
        identity: str,
        winner: _CiscoNode,
        loser_command: str,
        loser_origin: str,
    ) -> None:
        """记录真实值冲突；内容完全相同的重复配置不算冲突。"""
        if _normalized_command(winner.command) == _normalized_command(loser_command):
            return
        outcome.conflicts.append(
            {
                "vendor": vendor,
                "path": " / ".join(path) or "<root>",
                "key": identity,
                "winner_source": winner.origin,
                "winner_value": winner.command,
                "loser_source": loser_origin,
                "loser_value": loser_command,
            }
        )

    def _merge_cisco_group_children(
        self,
        target: _CiscoNode,
        source_children: list[_CiscoNode],
        group_name: str,
        rank: tuple[int, int],
        path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> None:
        """按显式配置、层级和列表顺序把 group 子节点合入目标节点。"""
        for source in source_children:
            if self._cisco_group_names(source.command, "apply-group") or self._cisco_group_names(
                source.command, "exclude-group"
            ):
                continue
            if source.is_block or source.children:
                matches = [
                    item
                    for item in target.children
                    if item.is_block and self._cisco_pattern_match(source.command, item.command)
                ]
                if matches:
                    continue
                if "'" in source.command or '"' in source.command:
                    continue
                target.children.append(
                    _CiscoNode(
                        command=source.command,
                        is_block=True,
                        origin=f"group:{group_name}",
                        rank=rank,
                    )
                )
                continue

            identity = _cisco_command_identity(source.command, path=path)
            existing = next(
                (
                    item
                    for item in target.children
                    if not item.is_block and _cisco_command_identity(item.command, path=path) == identity
                ),
                None,
            )
            if existing is None:
                target.children.append(
                    _CiscoNode(
                        command=source.command,
                        origin=f"group:{group_name}",
                        rank=rank,
                    )
                )
                continue
            source_origin = f"group:{group_name}"
            if existing.origin != "explicit" and rank > existing.rank:
                self._record_group_conflict(
                    outcome,
                    self.vendor,
                    path,
                    identity,
                    _CiscoNode(source.command, origin=source_origin, rank=rank),
                    existing.command,
                    existing.origin,
                )
                existing.command = source.command
                existing.origin = source_origin
                existing.rank = rank
            else:
                self._record_group_conflict(
                    outcome,
                    self.vendor,
                    path,
                    identity,
                    existing,
                    source.command,
                    source_origin,
                )

    def expand_groups(self, known_interfaces: Iterable[str]) -> GroupExpansionOutcome:
        """事务式展开 IOS XR group，并执行本地/内层优先规则。"""

        outcome = GroupExpansionOutcome()
        # 第一步只收集定义；是否真正删除要等所有 apply-group 都验证成功。
        group_blocks: dict[str, CiscoBlock] = {}
        for block in self.blocks:
            match = re.match(r"group\s+(\S+)\s*$", block.header.strip(), re.IGNORECASE)
            if match and block.active:
                group_blocks[match.group(1)] = block
        if not group_blocks:
            return outcome

        # 带运行时变量或嵌套 apply-group 的组目前无法可靠静态求值。
        group_trees: dict[str, list[_CiscoNode]] = {}
        variable_groups: set[str] = set()
        nested_apply_groups: set[str] = set()

        def contains_group_control(nodes: list[_CiscoNode]) -> bool:
            """检测 group 内是否再次 apply 其他 group。"""
            return any(
                self._cisco_group_names(node.command, "apply-group")
                or contains_group_control(node.children)
                for node in nodes
            )

        for name, block in group_blocks.items():
            body = textwrap.dedent("\n".join(block.lines))
            if "$" in body:
                variable_groups.add(name)
            group_trees[name] = self._parse_cisco_nodes(body.splitlines(), origin=f"group:{name}")
            if contains_group_control(group_trees[name]):
                nested_apply_groups.add(name)

        # 在临时树上工作，失败时不写回 self.blocks，从而实现整体回滚。
        root = _CiscoNode("<root>", is_block=True)
        terminal_commands: list[str] = []
        for block in self.blocks:
            header = block.header.strip()
            if not block.active or header in {"", "!", "end-group"} or re.match(
                r"group\s+\S+", header, re.IGNORECASE
            ):
                continue
            if header.lower() in {"end", "commit"}:
                terminal_commands.append(header)
                continue
            structural = bool(
                re.match(
                    r"(interface|router|vrf|l2vpn|mpls|username|line|segment-routing|telemetry)\b",
                    header,
                    re.IGNORECASE,
                )
            )
            node = _CiscoNode(
                block.header,
                self._parse_cisco_nodes(block.lines),
                is_block=bool(block.lines) or structural,
            )
            root.children.append(node)

        # 为只出现在 Excel 中的接口创建临时节点，使正则 group 也能命中它们。
        existing_interfaces = {
            canonical_cisco_interface(node.command.split(maxsplit=1)[1])
            for node in root.children
            if re.match(r"interface\s+\S+", node.command, re.IGNORECASE)
        }
        for raw_name in known_interfaces:
            name = canonical_cisco_interface(raw_name)
            if name not in existing_interfaces:
                root.children.append(_CiscoNode(f"interface {name}", is_block=True, origin="synthetic"))
                existing_interfaces.add(name)

        all_applied: set[str] = set()
        unresolved = False

        def expand_node(
            node: _CiscoNode,
            path: list[str],
            inherited: list[tuple[str, tuple[int, int]]],
        ) -> None:
            """递归计算当前路径的有效 group 列表并合并继承配置。"""
            nonlocal unresolved
            local_names: list[str] = []
            excluded: set[str] = set()
            for child in node.children:
                local_names.extend(self._cisco_group_names(child.command, "apply-group"))
                excluded.update(self._cisco_group_names(child.command, "exclude-group"))
            all_applied.update(local_names)
            for name in local_names:
                if name not in group_trees:
                    outcome.warnings.append(f"IOS XR apply-group 引用了未定义的组 {name}")
                    unresolved = True
                elif name in variable_groups:
                    outcome.warnings.append(f"IOS XR 组 {name} 包含运行时变量，无法安全静态展开")
                    unresolved = True
                elif name in nested_apply_groups:
                    outcome.warnings.append(f"IOS XR 组 {name} 内再次引用 apply-group，无法安全静态展开")
                    unresolved = True

            # rank 越大优先级越高：路径越深越优先，同列表越靠前越优先。
            local = [(name, (len(path), -index)) for index, name in enumerate(local_names)]
            active: list[tuple[str, tuple[int, int]]] = []
            for item in [*local, *inherited]:
                if item[0] in excluded or any(existing[0] == item[0] for existing in active):
                    continue
                active.append(item)

            for group_name, rank in active:
                tree = group_trees.get(group_name)
                if tree is None:
                    continue
                payload = self._cisco_group_payload(tree, path)
                self._merge_cisco_group_children(node, payload, group_name, rank, path, outcome)

            node.children = [
                child
                for child in node.children
                if not self._cisco_group_names(child.command, "apply-group")
                and not self._cisco_group_names(child.command, "exclude-group")
            ]
            index = 0
            while index < len(node.children):
                child = node.children[index]
                if child.is_block:
                    expand_node(child, [*path, child.command], active)
                index += 1

        expand_node(root, [], [])
        if unresolved:
            # 任一引用无法求值都放弃临时树，原配置保持原样。
            outcome.events.append("IOS XR group 展开未完整解析，已整体回滚并保留原配置")
            outcome.conflicts.clear()
            outcome.success = False
            return outcome

        if not all_applied:
            return outcome

        # 只有成功解析全部引用后，才用展开后的树替换原配置块。
        rebuilt: list[CiscoBlock] = []
        for node in root.children:
            rebuilt.append(
                CiscoBlock(
                    header=node.command,
                    lines=self._render_cisco_nodes(node.children),
                )
            )
            rebuilt.append(CiscoBlock(header="!"))
        for command in terminal_commands or ["end"]:
            rebuilt.append(CiscoBlock(header=command))
        self.blocks = rebuilt
        outcome.events.extend(f"已展开 IOS XR 配置组 {name}" for name in sorted(all_applied))
        return outcome

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """停用指定接口；可选择连同全部子接口一起停用。"""
        canonical = canonical_cisco_interface(name)
        for block in self._interface_blocks():
            current = block.interface_name
            if current == canonical or (include_children and current and current.startswith(canonical + ".")):
                block.active = False

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """改名父接口及子接口，并可移除聚合成员属性。"""
        source = canonical_cisco_interface(source)
        target = canonical_cisco_interface(target)
        for block in list(self._interface_blocks()):
            current = block.interface_name
            if current != source and not (current and current.startswith(source + ".")):
                continue
            suffix = current[len(source) :] if current else ""
            new_name = target + suffix
            block.header = f"interface {new_name}"
            if strip_bundle:
                block.lines = [
                    line
                    for line in block.lines
                    if not re.match(r"\s*(bundle\s+id|lacp\b|aggregated-)\b", line, re.IGNORECASE)
                ]
            self._merge_duplicate_interface(block)

    def _merge_duplicate_interface(self, preferred: CiscoBlock) -> None:
        """接口改名发生碰撞时去重合并配置行。"""
        name = preferred.interface_name
        duplicates = [block for block in self._interface_blocks() if block.interface_name == name]
        if len(duplicates) < 2:
            return
        merged: list[str] = []
        for block in duplicates:
            for line in block.lines:
                if line not in merged:
                    merged.append(line)
            if block is not preferred:
                block.active = False
        preferred.lines = merged

    def map_uni(self, source: str, target_parent: str, vlan: int) -> str:
        """把一个 UNI 迁移到目标父接口的 Dot1Q 子接口。"""
        source = canonical_cisco_interface(source)
        target = f"{canonical_cisco_interface(target_parent)}.{vlan}"
        block = self._find_interface_block(source)
        if not block:
            return target
        block.header = f"interface {target}"
        filtered = [
            line
            for line in block.lines
            if not re.match(r"\s*(encapsulation\s+dot1q|bundle\s+id|lacp\b)", line, re.IGNORECASE)
        ]
        insertion = 1 if filtered and re.match(r"\s*description\b", filtered[0], re.IGNORECASE) else 0
        filtered.insert(insertion, f" encapsulation dot1q {vlan}")
        block.lines = filtered
        self._merge_duplicate_interface(block)
        return target

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在，并默认启用。"""
        canonical = canonical_cisco_interface(name)
        if self._find_interface_block(canonical):
            return
        self.blocks.append(CiscoBlock(header=f"interface {canonical}", lines=[" no shutdown"]))
        self.blocks.append(CiscoBlock(header="!"))

    def clean_authentication(self) -> AuthenticationCleanupOutcome:
        """删除旧账号、AAA、权限组及 line 下的认证引用。"""
        outcome = AuthenticationCleanupOutcome()
        top_level = (
            ("username", re.compile(r"^username\b", re.IGNORECASE)),
            ("aaa", re.compile(r"^aaa\b", re.IGNORECASE)),
            ("tacacs", re.compile(r"^(?:tacacs-server|tacacs)\b", re.IGNORECASE)),
            ("radius", re.compile(r"^(?:radius-server|radius)\b", re.IGNORECASE)),
            ("taskgroup", re.compile(r"^task-?group\b", re.IGNORECASE)),
            ("usergroup", re.compile(r"^user-?group\b", re.IGNORECASE)),
        )
        line_auth = re.compile(
            r"^(?:password|secret)\b"
            r"|^login\s+authentication\b"
            r"|^authorization\b"
            r"|^accounting\b"
            r"|^users\s+group\b",
            re.IGNORECASE,
        )
        # 顶层认证对象整块删除；line 块只删除认证相关子命令。
        for block in self.blocks:
            if not block.active:
                continue
            header = block.header.strip()
            matched_category = next(
                (category for category, pattern in top_level if pattern.match(header)),
                None,
            )
            if matched_category:
                block.active = False
                outcome.record(matched_category)
                continue
            if re.match(r"^line\b", header, re.IGNORECASE):
                retained = [line for line in block.lines if not line_auth.match(line.strip())]
                outcome.record("line-auth-reference", len(block.lines) - len(retained))
                block.lines = retained
        return outcome

    def add_lab_account(self) -> None:
        """在 end/commit 前插入使用内置 root-system 的实验账号。"""
        account = CiscoBlock(
            header=f"username {LAB_USERNAME}",
            lines=[f" secret 0 {LAB_PASSWORD}", " group root-system"],
        )
        terminal = next(
            (
                index
                for index, block in enumerate(self.blocks)
                if block.active and block.header.strip().lower() in {"end", "commit"}
            ),
            len(self.blocks),
        )
        self.blocks[terminal:terminal] = [account, CiscoBlock(header="!")]

    def replace_references(self, replacements: dict[str, str]) -> None:
        """在非接口头和块内容中更新所有已知接口引用。"""
        if not replacements:
            return
        canonical = {canonical_cisco_interface(k): v for k, v in replacements.items() if k != v}
        if not canonical:
            return
        names = sorted(canonical, key=len, reverse=True)
        pattern = re.compile(r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_.-])")
        for block in self.blocks:
            if not block.active:
                continue
            if not block.interface_name:
                block.header = pattern.sub(lambda match: canonical[match.group(1)], block.header)
            block.lines = [pattern.sub(lambda match: canonical[match.group(1)], line) for line in block.lines]

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
