"""Cisco IOS XR group 展开算法。"""

from __future__ import annotations

import copy
import re
import textwrap
from dataclasses import dataclass, field
from typing import Iterable

from ...models import WashingPolicy
from ...parsers.cisco_iosxr import (
    CiscoBlock,
    _CiscoNode,
    canonical_cisco_interface,
)
from ...parsers.common import GroupExpansionOutcome, normalized_command as _normalized_command
from ..identity import find_opaque_ambiguity
from .identity import resolve_cisco_identity


@dataclass(slots=True)
class _CiscoExpansionState:
    """一次 group 展开中的共享状态，避免递归方法参数膨胀。"""

    trees: dict[str, list[_CiscoNode]]
    variable_groups: set[str]
    nested_groups: set[str]
    selected_groups: set[str]
    outcome: GroupExpansionOutcome
    policy: WashingPolicy
    applied_groups: set[str] = field(default_factory=set)
    unresolved: bool = False


class CiscoGroupExpander:
    """事务式展开 IOS XR group，并维护继承优先级与冲突规则。"""

    def __init__(self, document):
        self._document = document

    @property
    def vendor(self) -> str:
        return self._document.vendor

    @property
    def blocks(self) -> list[CiscoBlock]:
        return self._document.blocks

    @blocks.setter
    def blocks(self, value: list[CiscoBlock]) -> None:
        self._document.blocks = value

    @staticmethod
    def parse_nodes(lines: Iterable[str], origin: str = "explicit") -> list[_CiscoNode]:
        return CiscoGroupExpander._parse_cisco_nodes(lines, origin)

    @staticmethod
    def render_nodes(nodes: list[_CiscoNode], depth: int = 1) -> list[str]:
        return CiscoGroupExpander._render_cisco_nodes(nodes, depth)

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
            lines.extend(CiscoGroupExpander._render_cisco_nodes(node.children, depth + 1))
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
        rule_id: str | None,
        winner: _CiscoNode,
        loser_command: str,
        loser_origin: str,
    ) -> None:
        """记录真实值冲突；内容完全相同的重复配置不算冲突。"""
        if _normalized_command(winner.command) == _normalized_command(loser_command):
            return
        conflict = {
            "vendor": vendor,
            "path": " / ".join(path) or "<root>",
            "key": identity,
            "winner_source": winner.origin,
            "winner_value": winner.command,
            "loser_source": loser_origin,
            "loser_value": loser_command,
        }
        if rule_id is not None:
            conflict["rule_id"] = rule_id
        outcome.conflicts.append(conflict)

    def _merge_cisco_group_children(
        self,
        target: _CiscoNode,
        source_children: list[_CiscoNode],
        group_name: str,
        rank: tuple[int, int],
        path: list[str],
        outcome: GroupExpansionOutcome,
        policy: WashingPolicy,
    ) -> None:
        """按显式配置、层级和列表顺序把 group 子节点合入目标节点。"""
        for source in source_children:
            # apply/exclude 是继承控制语句，不是需要合并的业务配置。
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
                # 正则选择器只能应用到已有具体节点，不能把正则本身生成为配置块。
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

            decision = resolve_cisco_identity(source.command, path=path)
            if decision.matched:
                outcome.identity_rule_hits += 1
            else:
                outcome.identity_fallbacks += 1
            identity = decision.key
            resolved_children = [
                (item, resolve_cisco_identity(item.command, path=path))
                for item in target.children
                if not item.is_block
            ]
            existing = next(
                (
                    item
                    for item, item_decision in resolved_children
                    if item_decision.key == identity
                ),
                None,
            )
            if existing is None:
                if not decision.matched and policy.group_unknown_identity != "preserve":
                    family = decision.normalized.split(maxsplit=1)[0] if decision.normalized else ""
                    ambiguous = find_opaque_ambiguity(decision, resolved_children)
                    if ambiguous is not None:
                        detail = {
                            "vendor": self.vendor,
                            "path": " / ".join(path) or "<root>",
                            "family": family,
                            "existing_source": ambiguous.origin,
                            "existing_value": ambiguous.command,
                            "candidate_source": f"group:{group_name}",
                            "candidate_value": source.command,
                        }
                        if detail not in outcome.ambiguities:
                            outcome.ambiguities.append(detail)
                            action = (
                                "已中止本次 group 展开"
                                if policy.group_unknown_identity == "fail"
                                else "已保留两个值"
                            )
                            outcome.warnings.append(
                                f"IOS XR 路径 {detail['path']} 下的未知命令族 {family} "
                                f"可能存在继承冲突，{action}"
                            )
                        if policy.group_unknown_identity == "fail":
                            outcome.success = False
                            return
                target.children.append(
                    _CiscoNode(
                        command=source.command,
                        origin=f"group:{group_name}",
                        rank=rank,
                    )
                )
                continue
            source_origin = f"group:{group_name}"
            # 显式配置永远优先；group 之间则由层级深度和引用顺序 rank 决定。
            if existing.origin != "explicit" and rank > existing.rank:
                self._record_group_conflict(
                    outcome,
                    self.vendor,
                    path,
                    identity,
                    decision.rule_id,
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
                    decision.rule_id,
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
        """编排 IOS XR group 的选择、展开、校验和事务式提交。"""
        outcome = GroupExpansionOutcome()
        policy = policy or WashingPolicy()
        if mode == "preserve":
            return outcome
        if mode not in {"relevant", "strict"}:
            raise ValueError(f"未知 IOS XR group 处理模式: {mode}")

        # 定义解析和工作树构建均不修改原 blocks，便于任何失败直接回滚。
        group_blocks, trees, variable_groups, nested_groups = (
            self._load_group_definitions()
        )
        if not group_blocks:
            return outcome

        root, terminal_commands = self._build_working_tree(known_interfaces)
        selected_groups = self._select_groups(root, trees, mode, policy)
        if not selected_groups:
            return outcome

        # 递归期间会共享选中集、已应用集和错误状态，避免每层传递大量参数。
        state = _CiscoExpansionState(
            trees=trees,
            variable_groups=variable_groups,
            nested_groups=nested_groups,
            selected_groups=selected_groups,
            outcome=outcome,
            policy=policy,
        )
        self._expand_node(root, [], [], state)
        if state.unresolved or not outcome.success:
            outcome.events.append(
                "IOS XR group 展开未完整解析，已整体回滚并保留原配置"
            )
            outcome.conflicts.clear()
            outcome.success = False
            return outcome
        if not state.applied_groups:
            return outcome

        # 只有完整展开成功才替换原 blocks，这里是事务式提交点。
        self.blocks = self._rebuild_blocks(
            root,
            terminal_commands,
            group_blocks,
            state.applied_groups,
            mode,
        )
        outcome.events.extend(
            f"已展开 IOS XR 配置组 {name}"
            for name in sorted(state.applied_groups)
        )
        return outcome

    def _load_group_definitions(
        self,
    ) -> tuple[
        dict[str, CiscoBlock],
        dict[str, list[_CiscoNode]],
        set[str],
        set[str],
    ]:
        """一次性解析 group 定义，并标记无法静态展开的类型。"""
        blocks: dict[str, CiscoBlock] = {}
        for block in self.blocks:
            match = re.match(
                r"group\s+(\S+)\s*$",
                block.header.strip(),
                re.IGNORECASE,
            )
            if match and block.active:
                blocks[match.group(1)] = block

        trees: dict[str, list[_CiscoNode]] = {}
        variable_groups: set[str] = set()
        nested_groups: set[str] = set()

        def contains_group_control(nodes: list[_CiscoNode]) -> bool:
            return any(
                self._cisco_group_names(node.command, "apply-group")
                or contains_group_control(node.children)
                for node in nodes
            )

        for name, block in blocks.items():
            body = textwrap.dedent("\n".join(block.lines))
            # 运行时变量和嵌套 apply-group 无法在离线阶段安全求值，
            # 先标记，等确定它们真的被应用时再触发整体回滚。
            if "$" in body:
                variable_groups.add(name)
            trees[name] = self._parse_cisco_nodes(
                body.splitlines(),
                origin=f"group:{name}",
            )
            if contains_group_control(trees[name]):
                nested_groups.add(name)
        return blocks, trees, variable_groups, nested_groups

    def _build_working_tree(
        self,
        known_interfaces: Iterable[str],
    ) -> tuple[_CiscoNode, list[str]]:
        root = _CiscoNode("<root>", is_block=True)
        terminal_commands: list[str] = []
        for block in self.blocks:
            header = block.header.strip()
            if not block.active or header in {"", "!", "end-group"} or re.match(
                r"group\s+\S+",
                header,
                re.IGNORECASE,
            ):
                continue
            if header.lower() in {"end", "commit"}:
                terminal_commands.append(header)
                continue
            structural = bool(
                re.match(
                    r"(interface|router|vrf|l2vpn|mpls|username|line|"
                    r"segment-routing|telemetry)\b",
                    header,
                    re.IGNORECASE,
                )
            )
            root.children.append(
                _CiscoNode(
                    block.header,
                    self._parse_cisco_nodes(block.lines),
                    is_block=bool(block.lines) or structural,
                )
            )

        # 拓扑中出现但配置未声明的接口也需参与通配 group 匹配。
        existing = {
            canonical_cisco_interface(node.command.split(maxsplit=1)[1])
            for node in root.children
            if re.match(r"interface\s+\S+", node.command, re.IGNORECASE)
        }
        for raw_name in known_interfaces:
            name = canonical_cisco_interface(raw_name)
            if name in existing:
                continue
            root.children.append(
                _CiscoNode(
                    f"interface {name}",
                    is_block=True,
                    origin="synthetic",
                )
            )
            existing.add(name)
        return root, terminal_commands

    def _select_groups(
        self,
        root: _CiscoNode,
        group_trees: dict[str, list[_CiscoNode]],
        mode: str,
        policy: WashingPolicy,
    ) -> set[str]:
        selected: set[str] = set()

        def walk(node: _CiscoNode, path: list[str]) -> None:
            for child in node.children:
                for name in self._cisco_group_names(child.command, "apply-group"):
                    tree = group_trees.get(name)
                    # strict 展开所有引用；relevant 只展开影响转换/清洗的路径。
                    # 根层未定义引用仍要选中，否则会被错误地忽略而无法报错。
                    if (
                        mode == "strict"
                        or (not path and tree is None)
                        or self._cisco_group_path_relevant(path, policy)
                        or (
                            tree is not None
                            and self._cisco_group_tree_relevant(tree, policy)
                        )
                    ):
                        selected.add(name)
                if child.is_block:
                    walk(child, [*path, child.command])

        walk(root, [])
        return selected

    def _expand_node(
        self,
        node: _CiscoNode,
        path: list[str],
        inherited: list[tuple[str, tuple[int, int]]],
        state: _CiscoExpansionState,
    ) -> None:
        """递归展开一棵工作树；只传递节点、路径和层级继承关系。"""
        active = self._resolve_active_groups(node, path, inherited, state)
        for group_name, rank in active:
            tree = state.trees.get(group_name)
            if tree is None:
                continue
            self._merge_cisco_group_children(
                node,
                self._cisco_group_payload(tree, path),
                group_name,
                rank,
                path,
                state.outcome,
                state.policy,
            )

        self._rewrite_group_controls(node, state.selected_groups)
        index = 0
        while index < len(node.children):
            child = node.children[index]
            if child.is_block:
                self._expand_node(
                    child,
                    [*path, child.command],
                    active,
                    state,
                )
            index += 1

    def _resolve_active_groups(
        self,
        node: _CiscoNode,
        path: list[str],
        inherited: list[tuple[str, tuple[int, int]]],
        state: _CiscoExpansionState,
    ) -> list[tuple[str, tuple[int, int]]]:
        """解析当前层级的 apply/exclude，校验后计算有效继承顺序。"""
        local_names: list[str] = []
        excluded: set[str] = set()
        for child in node.children:
            local_names.extend(
                name
                for name in self._cisco_group_names(child.command, "apply-group")
                if name in state.selected_groups
            )
            excluded.update(
                name
                for name in self._cisco_group_names(child.command, "exclude-group")
                if name in state.selected_groups
            )

        state.applied_groups.update(local_names)
        # 只校验当前实际应用的 group；relevant 模式下未用到的复杂 group 可原样保留。
        for name in local_names:
            if name not in state.trees:
                state.outcome.warnings.append(
                    f"IOS XR apply-group 引用了未定义的组 {name}"
                )
                state.unresolved = True
            elif name in state.variable_groups:
                state.outcome.warnings.append(
                    f"IOS XR 组 {name} 包含运行时变量，无法安全静态展开"
                )
                state.unresolved = True
            elif name in state.nested_groups:
                state.outcome.warnings.append(
                    f"IOS XR 组 {name} 内再次引用 apply-group，无法安全静态展开"
                )
                state.unresolved = True

        local = [
            (name, (len(path), -index))
            for index, name in enumerate(local_names)
        ]
        # 路径越深越具体，优先级越高；同层列表中越靠前越优先。
        # 本层 exclude 同时抑制本地引用和从父层继承下来的同名 group。
        active: list[tuple[str, tuple[int, int]]] = []
        for item in [*local, *inherited]:
            if item[0] in excluded or any(
                existing[0] == item[0] for existing in active
            ):
                continue
            active.append(item)
        return active

    def _rewrite_group_controls(
        self,
        node: _CiscoNode,
        selected_groups: set[str],
    ) -> None:
        """只移除已展开 group，未选中的引用保持原样。"""
        retained: list[_CiscoNode] = []
        for child in node.children:
            rewritten: str | None = child.command
            for keyword in ("apply-group", "exclude-group"):
                names = self._cisco_group_names(child.command, keyword)
                if not names:
                    continue
                rewritten = self._rewrite_cisco_group_control(
                    child.command,
                    keyword,
                    # 一条语句可同时引用多个 group，因此只摘掉本次已展开的名称。
                    [
                        name
                        for name in names
                        if name not in selected_groups
                    ],
                )
                break
            if rewritten is not None:
                child.command = rewritten
                retained.append(child)
        node.children = retained

    def _rebuild_blocks(
        self,
        root: _CiscoNode,
        terminal_commands: list[str],
        group_blocks: dict[str, CiscoBlock],
        all_applied: set[str],
        mode: str,
    ) -> list[CiscoBlock]:
        rebuilt: list[CiscoBlock] = []
        if mode == "relevant":
            # relevant 模式必须保留未应用 group 的定义，以及它们原有的引用关系。
            for name, block in group_blocks.items():
                if name in all_applied:
                    continue
                rebuilt.extend(
                    [
                        copy.deepcopy(block),
                        CiscoBlock(header="end-group"),
                        CiscoBlock(header="!"),
                    ]
                )
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
        return rebuilt
