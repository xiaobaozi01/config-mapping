"""Juniper Junos group 展开算法。"""

from __future__ import annotations

import copy
import fnmatch
import re
from dataclasses import dataclass, field
from typing import Iterable

from ...models import WashingPolicy
from ...parsers.common import (
    GroupExpansionOutcome,
    interface_parent,
    normalized_command as _normalized_command,
)
from ...parsers.juniper_junos import (
    JunosDocument,
    JunosNode,
    canonical_junos_interface,
)
from ..identity import find_opaque_ambiguity
from .identity import resolve_junos_identity


GroupApplication = tuple[str, tuple[int, ...], tuple[str, ...]]


@dataclass(slots=True)
class _JunosExpansionState:
    """一次 group 展开的共享状态，让递归仅表达树遍历关系。"""

    groups_container: JunosNode
    groups: dict[str, JunosNode]
    dependencies: dict[str, list[str]]
    outcome: GroupExpansionOutcome
    policy: WashingPolicy
    selected_groups: set[str] = field(default_factory=set)
    applied_groups: set[str] = field(default_factory=set)


class JunosGroupExpander:
    """事务式展开 Junos groups，并维护嵌套继承与排除规则。"""

    def __init__(self, document: JunosDocument):
        self._document = document

    @property
    def root(self) -> JunosNode:
        return self._document.root

    @root.setter
    def root(self, value: JunosNode) -> None:
        self._document.root = value

    @staticmethod
    def _base_header(header: str) -> str:
        return JunosDocument._base_header(header)

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
        rule_id: str | None,
        winner: JunosNode,
        loser: JunosNode,
    ) -> None:
        """记录值不同的 group 冲突，完全相同的重复语句不记录。"""
        if _normalized_command(winner.header) == _normalized_command(loser.header):
            return
        conflict = {
            "vendor": "juniper_junos",
            "path": " / ".join(path) or "<root>",
            "key": identity,
            "winner_source": winner.origin,
            "winner_value": winner.header,
            "loser_source": loser.origin,
            "loser_value": loser.header,
        }
        if rule_id is not None:
            conflict["rule_id"] = rule_id
        outcome.conflicts.append(conflict)

    def _merge_junos_group_children(
        self,
        target: JunosNode,
        source_children: list[JunosNode],
        group_name: str,
        rank: tuple[int, ...],
        path: list[str],
        outcome: GroupExpansionOutcome,
        policy: WashingPolicy,
    ) -> None:
        """按显式、嵌套层级和列表顺序合并 group 子节点。"""
        assert target.children is not None
        for source in source_children:
            if not source.active or not source.header:
                continue
            if self._group_names(source.header, "apply-groups") or self._group_names(
                source.header, "apply-groups-except"
            ):
                # Group 控制语句由依赖求值器处理，不作为普通配置写入目标树。
                continue
            if source.is_block:
                matches = [
                    item
                    for item in target.children
                    if item.active and item.is_block and self._header_matches(source.header, item.header)
                ]
                if matches or "<" in source.header:
                    # 通配选择器只用于匹配已有具体节点，不能把 ``<ge-*>`` 生成为真实配置块。
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

            decision = resolve_junos_identity(self._base_header(source.header), path=path)
            if decision.matched:
                outcome.identity_rule_hits += 1
            else:
                outcome.identity_fallbacks += 1
            identity = decision.key
            resolved_children = [
                (
                    item,
                    resolve_junos_identity(self._base_header(item.header), path=path),
                )
                for item in target.children
                if item.active and not item.is_block
            ]
            existing = next(
                (
                    item
                    for item, item_decision in resolved_children
                    if item_decision.key == identity
                ),
                None,
            )
            candidate = JunosNode(
                source.header,
                origin=f"group:{group_name}",
                rank=rank,
            )
            if existing is None:
                if not decision.matched and policy.group_unknown_identity != "preserve":
                    family = decision.normalized.split(maxsplit=1)[0] if decision.normalized else ""
                    ambiguous = find_opaque_ambiguity(decision, resolved_children)
                    if ambiguous is not None:
                        detail = {
                            "vendor": "juniper_junos",
                            "path": " / ".join(path) or "<root>",
                            "family": family,
                            "existing_source": ambiguous.origin,
                            "existing_value": ambiguous.header,
                            "candidate_source": f"group:{group_name}",
                            "candidate_value": source.header,
                        }
                        if detail not in outcome.ambiguities:
                            outcome.ambiguities.append(detail)
                            action = (
                                "已中止本次 group 展开"
                                if policy.group_unknown_identity == "fail"
                                else "已保留两个值"
                            )
                            outcome.warnings.append(
                                f"Junos 路径 {detail['path']} 下的未知语句族 {family} "
                                f"可能存在继承冲突，{action}"
                            )
                        if policy.group_unknown_identity == "fail":
                            outcome.success = False
                            return
                target.children.append(candidate)
                continue
            # 显式配置始终优先；group 之间按层级和引用顺序 rank 决定胜出者。
            if existing.origin != "explicit" and rank > existing.rank:
                self._record_junos_conflict(
                    outcome,
                    path,
                    identity,
                    decision.rule_id,
                    candidate,
                    existing,
                )
                existing.header = candidate.header
                existing.origin = candidate.origin
                existing.rank = candidate.rank
            else:
                self._record_junos_conflict(
                    outcome,
                    path,
                    identity,
                    decision.rule_id,
                    existing,
                    candidate,
                )

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        mode: str = "relevant",
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """编排 Junos group 的选择、依赖校验、展开和事务式提交。"""
        outcome = GroupExpansionOutcome()
        policy = policy or WashingPolicy()
        if mode == "preserve":
            return outcome
        if mode not in {"relevant", "strict"}:
            raise ValueError(f"未知 Junos group 处理模式: {mode}")

        # 所有展开先在深拷贝的工作树上进行，失败时原文档不会被部分修改。
        prepared = self._prepare_working_tree(known_interfaces)
        if prepared is None:
            return outcome
        working, groups_container, groups = prepared
        state = _JunosExpansionState(
            groups_container=groups_container,
            groups=groups,
            dependencies={
                name: self._group_dependencies(group)
                for name, group in groups.items()
            },
            outcome=outcome,
            policy=policy,
        )
        selected_roots = self._select_root_groups(
            working,
            mode,
            policy,
            state,
        )
        if not selected_roots:
            return outcome

        # 在改写工作树前先校验整个依赖闭包，循环或缺失定义都整体回滚。
        if not self._select_dependency_closure(selected_roots, state):
            outcome.events.append(
                "Junos group 展开未完整解析，已整体回滚并保留原配置"
            )
            outcome.success = False
            return outcome

        self._walk_expansion_tree(working, [], [], set(), state)
        if not outcome.success:
            outcome.events.append(
                "Junos group 存在未覆盖的潜在语义冲突，已整体回滚并保留原配置"
            )
            outcome.conflicts.clear()
            return outcome
        if not state.applied_groups:
            return outcome

        # 展开完成后才隐藏已物化的 group 定义，并将工作树原子替换回文档。
        self._commit_group_visibility(state, mode)
        self.root = working
        outcome.events.extend(
            f"已展开 Junos 配置组 {name}"
            for name in sorted(state.selected_groups)
        )
        return outcome

    def _prepare_working_tree(
        self,
        known_interfaces: Iterable[str],
    ) -> tuple[JunosNode, JunosNode, dict[str, JunosNode]] | None:
        # 事务边界：本方法以后的所有变更都只发生在 working 上。
        working = copy.deepcopy(self.root)
        assert working.children is not None
        groups_container = next(
            (
                node
                for node in working.children
                if node.active
                and node.is_block
                and self._base_header(node.header) == "groups"
            ),
            None,
        )
        if groups_container is None or groups_container.children is None:
            return None
        groups = {
            self._base_header(node.header): node
            for node in groups_container.children
            if node.active and node.is_block
        }

        # 拓扑中出现但配置未声明的接口也需参与通配 group 匹配。
        interfaces = next(
            (
                node
                for node in working.children
                if node.active
                and node.is_block
                and self._base_header(node.header) == "interfaces"
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
            if name in existing_names:
                continue
            interfaces.children.append(JunosNode(name, [], origin="synthetic"))
            existing_names.add(name)
        return working, groups_container, groups

    def _group_dependencies(self, group: JunosNode) -> list[str]:
        result: list[str] = []

        def collect(node: JunosNode) -> None:
            if node.children is None:
                for name in self._group_names(node.header, "apply-groups"):
                    if name not in result:
                        result.append(name)
                return
            for child in node.children:
                if child.active:
                    collect(child)

        collect(group)
        return result

    def _select_root_groups(
        self,
        working: JunosNode,
        mode: str,
        policy: WashingPolicy,
        state: _JunosExpansionState,
    ) -> set[str]:
        selected: set[str] = set()
        relevance_cache: dict[str, bool] = {}

        def is_relevant(name: str, visiting: set[str] | None = None) -> bool:
            if name in relevance_cache:
                return relevance_cache[name]
            group = state.groups.get(name)
            if group is None:
                return False
            visiting = set(visiting or ())
            if name in visiting:
                return False
            visiting.add(name)
            # 相关性会沿依赖传递：当前 group 本身无关，但它引用的 group 可能影响接口迁移。
            relevant = self._group_tree_relevant(group, policy) or any(
                is_relevant(dependency, visiting)
                for dependency in state.dependencies.get(name, [])
            )
            relevance_cache[name] = relevant
            return relevant

        def walk(node: JunosNode, path: list[str]) -> None:
            if node.children is None or node is state.groups_container:
                return
            for child in node.children:
                if not child.active:
                    continue
                names = (
                    self._group_names(child.header, "apply-groups")
                    if not child.is_block
                    else []
                )
                for name in names:
                    group = state.groups.get(name)
                    # strict 模式全部展开；relevant 模式只选中影响转换/清洗的引用。
                    # 根层未定义引用仍要选中，以便后续依赖校验能够正确报错。
                    if (
                        mode == "strict"
                        or (not path and group is None)
                        or self._group_path_relevant(path, policy)
                        or (group is not None and is_relevant(name))
                    ):
                        selected.add(name)
                if child.is_block:
                    walk(child, [*path, self._base_header(child.header)])

        walk(working, [])
        return selected

    def _select_dependency_closure(
        self,
        roots: set[str],
        state: _JunosExpansionState,
    ) -> bool:
        """校验并计算选中 group 的传递依赖闭包。"""
        states: dict[str, int] = {}
        stack: list[str] = []
        unresolved = False

        def visit(name: str) -> None:
            nonlocal unresolved
            visit_state = states.get(name, 0)
            # 0=未访问，1=当前递归链中，2=已完成；重新遇到 1 即构成环。
            if visit_state == 2:
                return
            if visit_state == 1:
                start = stack.index(name)
                cycle = [*stack[start:], name]
                state.outcome.warnings.append(
                    f"Junos group 存在循环引用: {' -> '.join(cycle)}"
                )
                unresolved = True
                return
            if name not in state.groups:
                state.outcome.warnings.append(
                    f"Junos apply-groups 引用了未定义的组 {name}"
                )
                unresolved = True
                return
            states[name] = 1
            stack.append(name)
            state.selected_groups.add(name)
            for dependency in state.dependencies.get(name, []):
                visit(dependency)
            stack.pop()
            states[name] = 2

        for name in sorted(roots):
            visit(name)
        return not unresolved

    @staticmethod
    def _effective_applications(
        applications: list[GroupApplication],
        excluded: set[str],
    ) -> list[GroupApplication]:
        """每个 group 只采用当前未排除路径中优先级最高的一次应用。"""
        # chain 中任何 group 被 except 排除，整条传递引入路径都失效。
        eligible = [
            application
            for application in applications
            if not any(name in excluded for name in application[2])
        ]
        result: list[GroupApplication] = []
        seen: set[str] = set()
        for application in sorted(
            eligible,
            key=lambda item: item[1],
            reverse=True,
        ):
            if application[0] in seen:
                continue
            seen.add(application[0])
            result.append(application)
        return result

    def _expand_nested_applications(
        self,
        path: list[str],
        applications: list[GroupApplication],
        excluded: set[str],
        state: _JunosExpansionState,
    ) -> tuple[list[GroupApplication], list[GroupApplication], set[str]]:
        """在当前配置路径上求值 group 内嵌套的 apply/except。"""
        expanded = list(applications)
        expanded_excluded = set(excluded)
        known = set(expanded)

        # group 内还可继续 apply/except 其他 group，因此反复求值直到排除集和应用集都不再变化。
        while True:
            changed = False
            current = self._effective_applications(expanded, expanded_excluded)
            nested_excluded: set[str] = set()
            # 先收集 except，因为它会改变哪些引用路径有效。
            for group_name, _rank, _chain in current:
                for child in self._group_payload_for_path(
                    state.groups[group_name], path
                ):
                    if child.active and not child.is_block:
                        nested_excluded.update(
                            name
                            for name in self._group_names(
                                child.header,
                                "apply-groups-except",
                            )
                            if name in state.selected_groups
                        )
            if not nested_excluded.issubset(expanded_excluded):
                expanded_excluded.update(nested_excluded)
                changed = True
                current = self._effective_applications(
                    expanded,
                    expanded_excluded,
                )

            # 再沿剩余有效路径展开新的 apply-groups，known 防止同一引用路径重复加入。
            for group_name, rank, chain in current:
                nested_names: list[str] = []
                for child in self._group_payload_for_path(
                    state.groups[group_name], path
                ):
                    if child.active and not child.is_block:
                        nested_names.extend(
                            name
                            for name in self._group_names(
                                child.header,
                                "apply-groups",
                            )
                            if name in state.selected_groups
                        )
                for index, name in enumerate(nested_names):
                    application = (
                        name,
                        (*rank[:-1], -1, -index, 0),
                        (*chain, name),
                    )
                    if application in known:
                        continue
                    known.add(application)
                    expanded.append(application)
                    state.applied_groups.add(name)
                    changed = True

            if not changed:
                return (
                    expanded,
                    self._effective_applications(expanded, expanded_excluded),
                    expanded_excluded,
                )

    def _walk_expansion_tree(
        self,
        node: JunosNode,
        path: list[str],
        inherited: list[GroupApplication],
        inherited_excluded: set[str],
        state: _JunosExpansionState,
    ) -> None:
        """递归合并当前层级有效的 group，并传递继承与排除关系。"""
        if node.children is None or node is state.groups_container:
            return

        local_names, local_excluded = self._local_group_controls(
            node,
            inherited_excluded,
            state.selected_groups,
        )
        state.applied_groups.update(local_names)

        # 层级越深越具体，优先级越高；同一 apply-groups 列表中越靠前越优先。
        # 引用链被保留在第三个元素中，供 apply-groups-except 排除整条传递路径。
        local = [
            (name, (len(path), -index, 0), (name,))
            for index, name in enumerate(local_names)
        ]
        active, effective, local_excluded = self._expand_nested_applications(
            path,
            [*local, *inherited],
            local_excluded,
            state,
        )

        for group_name, rank, _chain in effective:
            group = state.groups.get(group_name)
            if group is None:
                continue
            self._merge_junos_group_children(
                node,
                self._group_payload_for_path(group, path),
                group_name,
                rank,
                path,
                state.outcome,
                state.policy,
            )

        self._rewrite_group_controls(node, state.selected_groups)
        # 合并会向 node.children 追加节点，因此用索引循环继续处理新物化的块。
        index = 0
        while index < len(node.children):
            child = node.children[index]
            if (
                child.active
                and child.is_block
                and child is not state.groups_container
            ):
                self._walk_expansion_tree(
                    child,
                    [*path, self._base_header(child.header)],
                    active,
                    local_excluded,
                    state,
                )
            index += 1

    def _local_group_controls(
        self,
        node: JunosNode,
        inherited_excluded: set[str],
        selected_groups: set[str],
    ) -> tuple[list[str], set[str]]:
        """收集当前层级新增的 group 应用和累积排除集。"""
        assert node.children is not None
        local_names: list[str] = []
        local_excluded = set(inherited_excluded)
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
                for name in self._group_names(
                    child.header,
                    "apply-groups-except",
                )
                if name in selected_groups
            )
        return local_names, local_excluded

    def _rewrite_group_controls(
        self,
        node: JunosNode,
        selected_groups: set[str],
    ) -> None:
        """只移除已展开 group，未选中的 apply/except 保持原样。"""
        assert node.children is not None
        retained: list[JunosNode] = []
        for child in node.children:
            rewritten: str | None = child.header
            if not child.is_block:
                for keyword in ("apply-groups", "apply-groups-except"):
                    names = self._group_names(child.header, keyword)
                    if not names:
                        continue
                    rewritten = self._rewrite_group_control(
                        child.header,
                        keyword,
                        [
                            name
                            for name in names
                            if name not in selected_groups
                        ],
                    )
                    break
            if rewritten is not None:
                child.header = rewritten
                retained.append(child)
        node.children = retained

    def _commit_group_visibility(
        self,
        state: _JunosExpansionState,
        mode: str,
    ) -> None:
        groups_container = state.groups_container
        assert groups_container.children is not None
        if mode == "strict":
            groups_container.active = False
            return

        # relevant 模式保留未展开的 group；它们的传递依赖也必须保留，
        # 否则会在原样保留的 group 中制造悬空 apply-groups 引用。
        preserved = set(state.groups) - state.selected_groups
        pending = list(preserved)
        while pending:
            name = pending.pop()
            for dependency in state.dependencies.get(name, []):
                if dependency in state.groups and dependency not in preserved:
                    preserved.add(dependency)
                    pending.append(dependency)
        for group in groups_container.children:
            name = self._base_header(group.header)
            if name in state.selected_groups and name not in preserved:
                group.active = False
        groups_container.active = any(
            group.active for group in groups_container.children
        )
