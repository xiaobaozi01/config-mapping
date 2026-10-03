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
        """绑定待处理的 Junos 文档；构造阶段不解析或修改配置树。"""
        self._document = document

    @property
    def root(self) -> JunosNode:
        """读取底层文档当前的 Junos 配置根节点。"""
        return self._document.root

    @root.setter
    def root(self, value: JunosNode) -> None:
        """以成功展开的工作树原子替换底层文档根节点。"""
        self._document.root = value

    @staticmethod
    def _base_header(header: str) -> str:
        """返回用于 group 匹配的规范节点头文本。

        具体归一化由 ``JunosDocument`` 统一实现，会去除 ``inactive:``、
        ``protect:`` 等控制前缀，避免展开器与解析器采用不同语义。
        """
        return JunosDocument._base_header(header)

    @classmethod
    def _header_matches(cls, pattern_header: str, target_header: str) -> bool:
        """判断 group 节点选择器是否匹配目标配置节点。

        普通头部使用基础文本精确匹配；尖括号内的 Junos 通配表达式通过
        ``fnmatch`` 转换后参与整串匹配。表达式非法时返回 ``False``。
        """
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
        """解析控制语句中按声明顺序出现的 group 名称。

        同时支持单个名称、方括号列表和带引号名称，并忽略 inactive/protect
        前缀；语句不匹配指定关键字时返回空列表。
        """
        base = cls._base_header(statement).rstrip(";").strip()
        match = re.match(rf"{re.escape(keyword)}\s+(.+)$", base)
        if not match:
            return []
        value = match.group(1).strip().strip("[]").strip()
        return [token.strip("'\"") for token in value.split() if token]

    @classmethod
    def _rewrite_group_control(cls, statement: str, keyword: str, remaining: list[str]) -> str | None:
        """使用剩余名称重写一条 apply-groups/except 语句。

        保留控制前缀、关键字和原方括号列表风格；没有剩余名称时返回
        ``None`` 表示删除整条语句，不匹配指定关键字时原样返回。
        """
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
        """判断配置路径是否落在本次转换需要物化的范围内。

        接口、协议、路由实例、二层业务和管理访问路径始终相关；安全、硬件、
        NAT、PKI 和流量统计路径根据清洗策略开关决定。空路径返回 ``False``。
        """
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
        """递归判断一个 group 定义是否包含相关的有效配置路径。

        inactive 节点被跳过；任一节点或后代命中 ``_group_path_relevant`` 即
        返回 ``True``。该结果只用于 relevant 模式筛选，不修改 group。
        """
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
        """计算 Junos 通配选择器的确定性排序键。

        第一项是去掉通配元字符后的字面量长度，越长越具体；第二项使用原
        header 稳定打破并列，确保相同输入得到相同匹配顺序。
        """
        literal = re.sub(r"<([^>]+)>", lambda item: re.sub(r"[*?\[\]]", "", item.group(1)), header)
        return (len(literal), header)

    def _group_payload_for_path(self, group: JunosNode, path: list[str]) -> list[JunosNode]:
        """定位某个 group 在目标配置路径上提供的直接子配置。

        每一级只保留有效且匹配的块节点，并按选择器具体程度排序后合并其
        子节点作为下一层候选；任一级无匹配时返回空列表。
        """
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
        """把一次不同值的语义覆盖追加到展开报告。

        记录配置路径、semantic identity、胜负来源和值，并在可用时附带规则
        ID；归一化后相同的重复语句不算冲突。本方法不修改工作树。
        """
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
        """把一个 group 在当前路径的子节点合并到目标工作树节点。

        apply/except 控制语句交给依赖求值器处理，不复制为业务配置；块节点
        只在不存在且不是通配选择器时创建。叶子节点通过语义规则定位槽位，
        显式配置始终胜出，group 之间按 ``rank`` 覆盖，并同步更新冲突、规则
        命中、fallback 和歧义报告；fail 策略会把 outcome 标为失败。
        """
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
        """编排 Junos group 的选择、依赖校验、展开和事务式提交。

        ``relevant`` 只物化影响转换或清洗的 group，``strict`` 物化全部引用，
        ``preserve`` 保持原文不变。返回值汇总事件、告警、冲突和规则命中数；
        循环、缺失依赖或不安全语义冲突都会阻止工作树提交。
        """
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
        """深拷贝原配置并建立 group、接口等展开阶段需要的工作索引。

        拓扑中存在但配置未声明的接口会作为 synthetic 节点加入，以支持通配
        group 匹配；没有有效 ``groups`` 容器时返回 ``None``。
        """
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
        """递归收集一个 group 定义内声明的 apply-groups 依赖。

        结果按首次出现顺序去重，仅遍历有效节点；它描述定义中的直接引用，
        传递闭包和循环检查由 ``_select_dependency_closure`` 完成。
        """
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
        """从非 group 工作树中选择本次展开的根 group。

        strict 模式选择所有引用；relevant 模式结合当前配置路径、group 内容
        及传递依赖判断相关性，并缓存结果避免重复遍历。
        """
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
        """校验根 group 并计算其完整传递依赖集合。

        使用三色深度优先遍历区分未访问、递归中和已完成节点，从而发现循环；
        缺失定义或循环会写入告警并返回 ``False``。成功时把所有可达 group
        加入 ``state.selected_groups``，但尚不修改工作树。
        """
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
        """从候选引用中计算当前路径实际生效的 group 应用。

        引用链中任一名称被 except 排除时整条应用失效；其余候选按 rank 从
        高到低排序，每个 group 只保留优先级最高的一次。输入列表不会被修改。
        """
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
        """求解当前路径上由 group 内部继续引入的 apply/except 关系。

        先累积嵌套排除，再沿仍有效的引用链加入新的 group，反复迭代直至
        应用集与排除集稳定。返回全部应用、去重后的有效应用和最终排除集，
        并把新发现的依赖加入 ``state.applied_groups``。
        """
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
        """递归展开工作树当前节点，并向子层传递继承和排除关系。

        方法合并本地引用与父层引用，求解嵌套应用后按优先级写入 payload，
        再移除已消费的控制语句。合并可能追加新配置块，因此索引循环会继续
        处理新物化节点；groups 定义容器本身不会被展开。
        """
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
        """读取当前节点直接声明的 apply-groups 和 except。

        只返回本次已选中的 group；排除集从父层复制后追加本层规则，使本层
        except 同时作用于本地引用和从祖先继承的引用。工作树保持不变。
        """
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
        """从当前节点的控制语句中移除本次已经物化的 group 名称。

        一条列表中未选中的名称按原格式保留；全部名称均已展开时删除整条
        语句。普通配置节点和配置块不受影响。
        """
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
        """在工作树中隐藏已物化且不再被保留 group 依赖的定义。

        strict 模式隐藏整个 groups 容器；relevant 模式保留未展开定义及其传递
        依赖，防止留下悬空的 ``apply-groups`` 引用。
        """
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
