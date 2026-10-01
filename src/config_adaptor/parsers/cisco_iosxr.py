"""Cisco IOS XR 配置解析、group 展开、接口改写和认证清洗。"""

from __future__ import annotations

import copy
import re
import textwrap
from dataclasses import dataclass, field
from typing import Iterable

from ..constants import LAB_PASSWORD, LAB_USERNAME
from ..models import WashingPolicy
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
    value = re.sub(r"\s+l2transport\s*$", "", value.strip(), flags=re.IGNORECASE)
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
    if parent.startswith("bvi"):
        return "gateway"
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
        return [block for block in self.blocks if block.active and block.interface_name]

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取接口父子关系、VLAN 和类型，供 NNI/UNI 分类使用。"""
        result: list[InterfaceSpec] = []
        for block in self._interface_blocks():
            name = block.interface_name
            assert name is not None
            vlan = None
            inner_vlan = None
            for line in block.lines:
                match = re.match(
                    r"\s*encapsulation\s+dot1q\s+(\d+)"
                    r"(?:\s+second-dot1q\s+(\d+))?",
                    line,
                    re.IGNORECASE,
                )
                if match:
                    vlan = int(match.group(1))
                    inner_vlan = int(match.group(2)) if match.group(2) else None
                    break
            # BVI 编号本身就是常用业务 VLAN，作为无显式封装时的保守提示值。
            if vlan is None:
                bvi = re.fullmatch(r"BVI(\d+)", interface_parent(name), re.IGNORECASE)
                if bvi and 1 <= int(bvi.group(1)) <= 4094:
                    vlan = int(bvi.group(1))
            result.append(
                InterfaceSpec(
                    name=name,
                    parent=interface_parent(name),
                    unit=interface_unit(name),
                    vlan=vlan,
                    kind=_cisco_interface_kind(name),
                    inner_vlan=inner_vlan,
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

    def business_interface_names(self) -> set[str]:
        """识别真正承载三层或二层业务的 IOS XR 接口。

        直接业务包括 IPv4/IPv6、l2transport、xconnect 等；此外，
        被 L2VPN、bridge-domain、路由协议等全局配置引用的接口也视为活跃业务口。
        BVI 不因自身有 IP 就自动迁移，只有所在 bridge-domain 还包含活跃
        attachment circuit，或编号命中活跃业务 VLAN 时才作为网关迁移。
        """
        specs = self.interface_specs()
        known = {spec.name for spec in specs}
        kinds = {spec.name: spec.kind for spec in specs}
        active: set[str] = set()
        direct = re.compile(
            r"^(?:ipv4\s+address|ipv6\s+address|xconnect\b|l2transport\b|"
            r"bridge-domain\b|l2vpn\b|ethernet-services\b)",
            re.IGNORECASE,
        )
        for block in self._interface_blocks():
            name = block.interface_name
            if (
                name
                and kinds.get(name) != "gateway"
                and (block.l2transport or any(direct.match(line.strip()) for line in block.lines))
            ):
                active.add(name)

        # 扫描接口定义之外的引用。使用边界匹配避免 Gi0/0/0/1
        # 误命中 Gi0/0/0/10。
        external = "\n".join(
            text
            for block in self.blocks
            if block.active and not block.interface_name
            for text in [block.header, *block.lines]
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

        # 先从仍活跃的二层接入口收集业务 VLAN，再沿 bridge-domain
        # 关联 routed interface。BVI 编号回退只用于没有显式关联的常见配置。
        spec_by_name = {spec.name: spec for spec in specs}
        active_vlans: set[int] = set()
        for name in active:
            spec = spec_by_name.get(name)
            block = self._find_interface_block(name)
            if not spec or not block or spec.kind == "gateway" or spec.vlan is None:
                continue
            rendered = "\n".join(block.lines)
            if block.l2transport or re.search(
                r"^\s*(?:xconnect|bridge-domain|l2vpn|ethernet-services)\b",
                rendered,
                re.IGNORECASE | re.MULTILINE,
            ):
                active_vlans.add(spec.vlan)

        def descendants(node: _CiscoNode) -> list[_CiscoNode]:
            result: list[_CiscoNode] = []
            for child in node.children:
                result.append(child)
                result.extend(descendants(child))
            return result

        for block in self.blocks:
            if not block.active or not re.match(r"^l2vpn\b", block.header.strip(), re.IGNORECASE):
                continue
            for node in self._parse_cisco_nodes(block.lines):
                stack = [node]
                while stack:
                    current = stack.pop()
                    stack.extend(current.children)
                    if not re.match(r"^bridge-domain\b", current.command, re.IGNORECASE):
                        continue
                    attachments: set[str] = set()
                    gateways: set[str] = set()
                    for child in descendants(current):
                        gateway_match = re.match(r"^routed\s+interface\s+(.+)$", child.command, re.IGNORECASE)
                        if gateway_match:
                            gateways.add(canonical_cisco_interface(gateway_match.group(1)))
                            continue
                        interface_match = re.match(r"^interface\s+(.+)$", child.command, re.IGNORECASE)
                        if interface_match:
                            attachments.add(canonical_cisco_interface(interface_match.group(1)))
                    if attachments & active:
                        active.update(gateway for gateway in gateways if gateway in known)

        for spec in specs:
            if spec.kind == "gateway" and spec.vlan in active_vlans:
                active.add(spec.name)
        return active

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
    def _rewrite_cisco_group_control(command: str, keyword: str, remaining: list[str]) -> str | None:
        """从 apply/exclude-group 语句中只移除已经展开的 group。"""
        match = re.match(
            rf"({re.escape(keyword)}s?)\s+(.+?)\s*$",
            command.strip(),
            re.IGNORECASE,
        )
        if not match:
            return command
        if not remaining:
            return None
        original_value = match.group(2).strip()
        value = " ".join(remaining)
        if original_value.startswith("[") and original_value.endswith("]"):
            value = f"[ {value} ]"
        return f"{match.group(1)} {value}"

    @staticmethod
    def _cisco_group_path_relevant(path: list[str], policy: WashingPolicy) -> bool:
        """判断 IOS XR 路径是否会影响接口迁移或已启用的清洗范围。"""
        if not path:
            return False
        top = _normalized_command(path[0])
        always = (
            "interface ",
            "router ",
            "vrf ",
            "l2vpn",
            "mpls ",
            "segment-routing",
            "username ",
            "aaa",
            "tacacs",
            "radius",
            "taskgroup ",
            "usergroup ",
            "line ",
            "snmp-server",
            "ssh ",
            "telnet ",
        )
        if top.startswith(always):
            return True
        if policy.protocol_authentication and top.startswith(("key chain", "key-chain")):
            return True
        if policy.pki and top.startswith(("crypto ", "crypto-key", "certificate ")):
            return True
        if policy.hardware and top.startswith(("hw-module ", "controller ", "platform ", "slot ")):
            return True
        if policy.nat and top.startswith(("nat ", "service-location ")):
            return True
        if policy.flow_statistics and top.startswith(("flow ", "flow-exporter ", "flow monitor ")):
            return True
        return False

    def _cisco_group_tree_relevant(self, nodes: list[_CiscoNode], policy: WashingPolicy) -> bool:
        """检查 group 定义中是否包含需要物化后再处理的配置。"""
        def walk(items: list[_CiscoNode], path: list[str]) -> bool:
            for item in items:
                current = [*path, item.command]
                if self._cisco_group_path_relevant(current, policy) or walk(item.children, current):
                    return True
            return False

        return walk(nodes, [])

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

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        mode: str = "relevant",
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """事务式展开 IOS XR group，并执行本地/内层优先规则。"""

        outcome = GroupExpansionOutcome()
        policy = policy or WashingPolicy()
        if mode == "preserve":
            return outcome
        if mode not in {"relevant", "strict"}:
            raise ValueError(f"未知 IOS XR group 处理模式: {mode}")
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

        selected_groups: set[str] = set()

        def select_groups(node: _CiscoNode, path: list[str]) -> None:
            """预先选出本次要展开的 group，未选中的控制语句保持原样。"""
            for child in node.children:
                names = self._cisco_group_names(child.command, "apply-group")
                for name in names:
                    tree = group_trees.get(name)
                    if mode == "strict" or (
                        not path and tree is None
                    ) or self._cisco_group_path_relevant(path, policy) or (
                        tree is not None and self._cisco_group_tree_relevant(tree, policy)
                    ):
                        selected_groups.add(name)
                if child.is_block:
                    select_groups(child, [*path, child.command])

        select_groups(root, [])
        if not selected_groups:
            return outcome

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
                local_names.extend(
                    name
                    for name in self._cisco_group_names(child.command, "apply-group")
                    if name in selected_groups
                )
                excluded.update(
                    name
                    for name in self._cisco_group_names(child.command, "exclude-group")
                    if name in selected_groups
                )
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

            retained: list[_CiscoNode] = []
            for child in node.children:
                rewritten: str | None = child.command
                for keyword in ("apply-group", "exclude-group"):
                    names = self._cisco_group_names(child.command, keyword)
                    if names:
                        rewritten = self._rewrite_cisco_group_control(
                            child.command,
                            keyword,
                            [name for name in names if name not in selected_groups],
                        )
                        break
                if rewritten is not None:
                    child.command = rewritten
                    retained.append(child)
            node.children = retained
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
        if mode == "relevant":
            for name, block in group_blocks.items():
                if name in all_applied:
                    continue
                rebuilt.extend([copy.deepcopy(block), CiscoBlock(header="end-group"), CiscoBlock(header="!")])
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
            block.header = f"interface {new_name}" + (" l2transport" if block.l2transport else "")
            if strip_bundle:
                block.lines = [
                    line
                    for line in block.lines
                    if not re.match(r"\s*(?:bundle\b|lacp\b|aggregated-)", line, re.IGNORECASE)
                ]
            self._merge_duplicate_interface(block)

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把一棵 IOS XR 接口配置复制到新物理口，用于 M-LAG 按对端拆分。"""
        source = canonical_cisco_interface(source)
        target = canonical_cisco_interface(target)
        originals = [
            block
            for block in self._interface_blocks()
            if block.interface_name == source
            or (block.interface_name and block.interface_name.startswith(source + "."))
        ]
        if not originals:
            # 拓扑口可能没有显式配置，仍需要生成可用目标口。
            self.blocks.append(CiscoBlock(header=f"interface {target}", lines=[" no shutdown"]))
            self.blocks.append(CiscoBlock(header="!"))
            return
        insert_at = max(self.blocks.index(block) for block in originals) + 1
        clones: list[CiscoBlock] = []
        for original in originals:
            clone = copy.deepcopy(original)
            current = original.interface_name or source
            new_name = target + current[len(source) :]
            clone.header = f"interface {new_name}" + (" l2transport" if original.l2transport else "")
            if strip_bundle:
                clone.lines = [
                    line
                    for line in clone.lines
                    if not re.match(r"\s*(?:bundle\b|lacp\b|aggregated-)", line, re.IGNORECASE)
                ]
            clones.extend([clone, CiscoBlock(header="!")])
        self.blocks[insert_at:insert_at] = clones
        for clone in clones:
            if clone.interface_name:
                self._merge_duplicate_interface(clone)

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

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str:
        """把 UNI 迁移到目标父接口，并统一重写为 QinQ 终结。"""
        source = canonical_cisco_interface(source)
        target = f"{canonical_cisco_interface(target_parent)}.{vlan}"
        block = self._find_interface_block(source)
        if not block:
            return target
        block.header = f"interface {target}" + (" l2transport" if block.l2transport else "")
        filtered = [
            line
            for line in block.lines
            if not re.match(
                r"\s*(?:encapsulation\b|rewrite\b|bundle\b|lacp\b)",
                line,
                re.IGNORECASE,
            )
        ]
        insertion = 1 if filtered and re.match(r"\s*description\b", filtered[0], re.IGNORECASE) else 0
        filtered.insert(insertion, f" encapsulation dot1q {vlan} second-dot1q {inner_vlan}")
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

    def clean_authentication(
        self,
        policy: WashingPolicy | None = None,
    ) -> AuthenticationCleanupOutcome:
        """清理管理面认证，并按策略删除明确启用的扩展配置。"""
        policy = policy or WashingPolicy()
        outcome = AuthenticationCleanupOutcome()
        top_level: list[tuple[str, re.Pattern[str]]] = [
            ("username", re.compile(r"^username\b", re.IGNORECASE)),
            ("aaa", re.compile(r"^aaa\b", re.IGNORECASE)),
            ("tacacs", re.compile(r"^(?:tacacs-server|tacacs)\b", re.IGNORECASE)),
            ("radius", re.compile(r"^(?:radius-server|radius)\b", re.IGNORECASE)),
            ("taskgroup", re.compile(r"^task-?group\b", re.IGNORECASE)),
            ("usergroup", re.compile(r"^user-?group\b", re.IGNORECASE)),
            ("snmp", re.compile(r"^snmp-server\b", re.IGNORECASE)),
            ("ssh", re.compile(r"^ssh\b", re.IGNORECASE)),
            ("telnet", re.compile(r"^telnet\b", re.IGNORECASE)),
        ]
        optional_top_level: list[tuple[bool, str, str]] = [
            (
                policy.pki,
                "pki",
                r"^(?:crypto\s+(?:pki|ca|key)\b|certificate\b|trustpoint\b)",
            ),
            (
                policy.hardware,
                "hardware",
                r"^(?:hw-module|platform|service-location|slot)\b",
            ),
            (policy.nat, "nat", r"^(?:nat|cgn|service\s+cgn)\b"),
            (
                policy.flow_statistics,
                "flow-statistics",
                r"^(?:flow(?:-exporter|-monitor)?|sampler|monitor-session)\b",
            ),
        ]
        top_level.extend(
            (category, re.compile(pattern, re.IGNORECASE))
            for enabled, category, pattern in optional_top_level
            if enabled
        )
        if policy.protocol_authentication:
            top_level.append(
                (
                    "protocol-auth-definition",
                    re.compile(r"^(?:key\s+chain|key-?chain)\b", re.IGNORECASE),
                )
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
        if policy.protocol_authentication:
            protocol_header = re.compile(
                r"^(?:router\s+(?:bgp|isis|ospf|ospfv3|rip)|mpls\s+ldp|rsvp)\b",
                re.IGNORECASE,
            )
            protocol_auth = re.compile(
                r"(?:^|\s)(?:authentication(?:-key(?:-chain)?|-algorithm|-type)?|"
                r"password|key-?chain)(?:\s|$)",
                re.IGNORECASE,
            )
            interface_auth = re.compile(
                r"^(?:authentication(?:-key(?:-chain)?|-algorithm|-type)?|key-?chain)\b",
                re.IGNORECASE,
            )

            def strip_sections(lines: list[str], pattern: re.Pattern[str]) -> tuple[list[str], int]:
                """删除命中命令及其更深缩进的子配置。"""
                retained: list[str] = []
                removed = 0
                skipped_indent: int | None = None
                for line in lines:
                    stripped = line.strip()
                    indent = len(line) - len(line.lstrip())
                    if skipped_indent is not None:
                        if stripped and indent > skipped_indent:
                            removed += 1
                            continue
                        skipped_indent = None
                    if pattern.search(stripped):
                        removed += 1
                        skipped_indent = indent
                        continue
                    retained.append(line)
                return retained, removed

            for block in self.blocks:
                if not block.active:
                    continue
                if protocol_header.match(block.header.strip()):
                    block.lines, removed = strip_sections(block.lines, protocol_auth)
                    outcome.record("protocol-auth-reference", removed)
                elif block.interface_name:
                    block.lines, removed = strip_sections(block.lines, interface_auth)
                    outcome.record("protocol-auth-reference", removed)
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
