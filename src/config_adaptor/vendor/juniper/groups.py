"""Juniper Junos group 展开算法。"""

from __future__ import annotations

import copy
import fnmatch
import re
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
    _junos_statement_identity,
    canonical_junos_interface,
)


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
        rank: tuple[int, ...],
        path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> None:
        """按显式、嵌套层级和列表顺序合并 group 子节点。"""
        assert target.children is not None
        for source in source_children:
            if not source.active or not source.header:
                continue
            if self._group_names(source.header, "apply-groups") or self._group_names(
                source.header, "apply-groups-except"
            ):
                # Group 控制语句由 expand_groups 的依赖求值器处理，不作为
                # 普通配置写入目标树。
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

        def group_dependencies(group: JunosNode) -> list[str]:
            """按出现顺序收集一个 Group 内直接引用的其他 Group。"""
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

        dependencies = {name: group_dependencies(group) for name, group in groups.items()}
        relevance_cache: dict[str, bool] = {}

        def group_is_relevant(name: str, visiting: set[str] | None = None) -> bool:
            """Group 的相关性包含其传递引用，循环稍后由依赖校验报告。"""
            if name in relevance_cache:
                return relevance_cache[name]
            group = groups.get(name)
            if group is None:
                return False
            visiting = set(visiting or ())
            if name in visiting:
                return False
            visiting.add(name)
            relevant = self._group_tree_relevant(group, policy) or any(
                group_is_relevant(dependency, visiting)
                for dependency in dependencies.get(name, [])
            )
            relevance_cache[name] = relevant
            return relevant

        selected_roots: set[str] = set()

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
                        group is not None and group_is_relevant(name)
                    ):
                        selected_roots.add(name)
                if child.is_block:
                    select_groups(child, [*path, self._base_header(child.header)])

        select_groups(working, [])
        if not selected_roots:
            self.root = original_root
            return outcome

        # 对本次选中的 Group 求传递依赖闭包，并在改写配置前完成未定义
        # 引用和循环引用校验，确保失败时可以无损回滚。
        selected_groups: set[str] = set()
        dependency_state: dict[str, int] = {}
        dependency_stack: list[str] = []
        unresolved = False

        def select_dependency(name: str) -> None:
            nonlocal unresolved
            state = dependency_state.get(name, 0)
            if state == 2:
                return
            if state == 1:
                start = dependency_stack.index(name)
                cycle = [*dependency_stack[start:], name]
                outcome.warnings.append(
                    f"Junos group 存在循环引用: {' -> '.join(cycle)}"
                )
                unresolved = True
                return
            group = groups.get(name)
            if group is None:
                outcome.warnings.append(f"Junos apply-groups 引用了未定义的组 {name}")
                unresolved = True
                return
            dependency_state[name] = 1
            dependency_stack.append(name)
            selected_groups.add(name)
            for dependency in dependencies.get(name, []):
                select_dependency(dependency)
            dependency_stack.pop()
            dependency_state[name] = 2

        for name in sorted(selected_roots):
            select_dependency(name)

        if unresolved:
            self.root = original_root
            outcome.events.append("Junos group 展开未完整解析，已整体回滚并保留原配置")
            outcome.success = False
            return outcome

        all_applied: set[str] = set()

        # (Group 名称, 优先级, 引用链)。引用链使 apply-groups-except 能够
        # 同时排除某 Group 及仅通过它引入的传递依赖；保留所有候选路径，
        # 则被排除高优先级路径后仍可回退到独立应用的同名 Group。
        GroupApplication = tuple[str, tuple[int, ...], tuple[str, ...]]

        def eligible_applications(
            applications: list[GroupApplication],
            excluded: set[str],
        ) -> list[GroupApplication]:
            return [
                application
                for application in applications
                if not any(name in excluded for name in application[2])
            ]

        def effective_applications(
            applications: list[GroupApplication],
            excluded: set[str],
        ) -> list[GroupApplication]:
            """每个 Group 只采用当前未排除路径中优先级最高的一次应用。"""
            result: list[GroupApplication] = []
            seen: set[str] = set()
            for application in sorted(
                eligible_applications(applications, excluded),
                key=lambda item: item[1],
                reverse=True,
            ):
                if application[0] in seen:
                    continue
                seen.add(application[0])
                result.append(application)
            return result

        def expand_nested_applications(
            path: list[str],
            applications: list[GroupApplication],
            excluded: set[str],
        ) -> tuple[list[GroupApplication], list[GroupApplication], set[str]]:
            """在当前真实配置路径求值 Group 内的 apply/except 控制语句。"""
            expanded = list(applications)
            expanded_excluded = set(excluded)
            known = set(expanded)

            while True:
                changed = False
                effective = effective_applications(expanded, expanded_excluded)

                # 先应用排除，再沿仍有效的引用路径增加传递 Group。
                nested_excluded: set[str] = set()
                for group_name, _rank, _chain in effective:
                    group = groups[group_name]
                    for child in self._group_payload_for_path(group, path):
                        if child.active and not child.is_block:
                            nested_excluded.update(
                                name
                                for name in self._group_names(
                                    child.header, "apply-groups-except"
                                )
                                if name in selected_groups
                            )
                if not nested_excluded.issubset(expanded_excluded):
                    expanded_excluded.update(nested_excluded)
                    changed = True
                    effective = effective_applications(expanded, expanded_excluded)

                for group_name, rank, chain in effective:
                    group = groups[group_name]
                    nested_names: list[str] = []
                    for child in self._group_payload_for_path(group, path):
                        if child.active and not child.is_block:
                            nested_names.extend(
                                name
                                for name in self._group_names(child.header, "apply-groups")
                                if name in selected_groups
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
                        all_applied.add(name)
                        changed = True

                if not changed:
                    return (
                        expanded,
                        effective_applications(expanded, expanded_excluded),
                        expanded_excluded,
                    )

        def walk(
            node: JunosNode,
            path: list[str],
            inherited: list[GroupApplication],
            inherited_excluded: set[str],
        ) -> None:
            """递归计算当前层级的有效 group、排除列表与继承优先级。"""
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
            # 层级越深越优先；同一列表中越靠前越优先。
            local: list[GroupApplication] = [
                (name, (len(path), -index, 0), (name,))
                for index, name in enumerate(local_names)
            ]
            active, effective, local_excluded = expand_nested_applications(
                path,
                [*local, *inherited],
                local_excluded,
            )

            for group_name, rank, _chain in effective:
                group = groups.get(group_name)
                if group is None:
                    continue
                payload = self._group_payload_for_path(group, path)
                self._merge_junos_group_children(
                    node,
                    payload,
                    group_name,
                    rank,
                    path,
                    outcome,
                )

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
        if not all_applied:
            self.root = original_root
            return outcome

        # 全部展开成功后才隐藏已物化的定义；无关 group 和应用语句继续保留。
        if mode == "strict":
            groups_container.active = False
        else:
            # 未应用的 Group 仍按原文保留；它们引用的定义也必须保留，
            # 避免为了本次业务展开而制造悬空引用。
            preserved = set(groups) - selected_groups
            pending = list(preserved)
            while pending:
                name = pending.pop()
                for dependency in dependencies.get(name, []):
                    if dependency in groups and dependency not in preserved:
                        preserved.add(dependency)
                        pending.append(dependency)
            for group in groups_container.children:
                name = self._base_header(group.header)
                if name in selected_groups and name not in preserved:
                    group.active = False
            groups_container.active = any(group.active for group in groups_container.children)
        self.root = working
        outcome.events.extend(
            f"已展开 Junos 配置组 {name}" for name in sorted(selected_groups)
        )
        return outcome
