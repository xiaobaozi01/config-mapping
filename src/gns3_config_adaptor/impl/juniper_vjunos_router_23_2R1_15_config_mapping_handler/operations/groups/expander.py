"""Junos Group 展开编排。"""

from __future__ import annotations

from collections.abc import Iterable

from ....common.domain.outcomes import GroupExpansionOutcome
from ....common.domain.policies import WashingPolicy
from ....common.support.errors import require_invariant
from ...document import (
    JuniperNode,
    JunosDocument,
)
from .definitions import JunosGroupDefinitions
from .merger import JunosGroupMerger
from .models import (
    _ExpansionState,
    _GroupApplication,
    _ResolvedGroupInheritance,
)
from .selectors import JunosGroupSelectors


class JunosGroupExpander:
    """在配置副本上展开 Junos groups，并统一提交完整结果。




    所有引用、排除和冲突先在工作树上求解，任何失败都可以保持原文档不变。

    """

    def __init__(self, document: JunosDocument):
        """绑定 Junos 文档并复用其 header 解析入口。

        构造阶段不修改文档，使同一对象可在任意当前修订上启动一次独立展开事务。
        """
        self._document = document
        self._selectors = JunosGroupSelectors()
        self._definitions = JunosGroupDefinitions(document, self._selectors)
        self._merger = JunosGroupMerger(document, self._selectors)
        self._working_root = document.root

    def _iter_source_nodes(self) -> Iterable[JuniperNode]:
        """深度优先迭代原配置中的全部节点。

        该入口只服务展开前的字符串筛选，因此不会复制或修改原树。
        """
        stack = [self.root]
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(node.children)

    @property
    def root(self) -> JuniperNode:
        """读取底层文档当前的 Junos 根节点。

        每次读取最新根节点，确保事务只对调用时的配置修订生效。
        """
        return self._working_root

    @root.setter
    def root(self, root_node: JuniperNode) -> None:
        """用成功展开的工作树替换底层文档根节点。

        赋值是唯一提交点，未完成全部校验前不会暴露部分物化结果。
        """
        self._document.edit().replace_root(root_node)
        self._working_root = self._document.root

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """编排 Junos group 的收集、依赖校验、展开和事务式提交。

        所有活动引用及其完整依赖都会被物化。返回值汇总事件、告警、冲突和
        规则命中数；循环、缺失依赖或不安全语义冲突都会阻止工作树提交。
        """
        outcome = GroupExpansionOutcome()
        washing_policy = policy or WashingPolicy()
        if not self._definitions._has_group_controls():
            return outcome
        prepared = self._definitions._prepare_expansion(outcome, washing_policy)
        working_root = prepared.working_root
        state = prepared.state

        # except 是排除规则而不是继承依赖，但活动配置中的引用仍必须指向已定义
        # group。先校验可覆盖“只有 except、没有 apply”的配置。
        if not self._validate_root_exclusions(working_root, state):
            return self._fail_expansion(
                outcome,
                "Junos group 展开未完整解析，已整体回滚并保留原配置",
            )

        root_group_names = self._definitions._collect_root_group_names(
            working_root,
            state.groups_container,
        )
        if not root_group_names:
            if prepared.preprocessing_changed:
                self.root = working_root
            self._definitions._record_pruned_groups(
                outcome, prepared.pruned_group_names
            )
            return outcome

        # 在改写工作树前先校验整个依赖闭包，循环或缺失定义都整体回滚。
        if not self._definitions._validate_dependency_chain(
            root_group_names, state
        ) or not self._validate_selected_exclusions(state):
            return self._fail_expansion(
                outcome,
                "Junos group 展开未完整解析，已整体回滚并保留原配置",
            )

        self._definitions._add_known_interfaces(working_root, known_interfaces)
        self._walk_expansion_tree(working_root, [], [], set(), state)
        if not outcome.success:
            return self._fail_expansion(
                outcome,
                "Junos group 存在未覆盖的潜在语义冲突，已整体回滚并保留原配置",
                clear_conflicts=True,
            )
        if not state.encountered_group_names:
            return outcome

        result = self._commit_expansion(working_root, state)
        self._definitions._record_pruned_groups(
            result, prepared.pruned_group_names
        )
        return result

    def _validate_root_exclusions(
        self,
        working_root: JuniperNode,
        state: _ExpansionState,
    ) -> bool:
        """校验 group 定义之外的所有活动 except 引用。

        根配置可能只有 ``apply-groups-except`` 而没有任何 apply，
        如果等到选择展开根之后才检查，这类悬空引用会被提前返回漏掉。
        """
        names = self._definitions._root_group_exclusions(
            working_root,
            state.groups_container,
        )
        return self._definitions._validate_group_exclusions(
            names,
            state.group_definitions,
            state.outcome,
        )

    def _validate_selected_exclusions(
        self,
        state: _ExpansionState,
    ) -> bool:
        """校验从已选 group 依赖闭包中可达的 except 引用。

        只有实际参与展开的模板才会影响输出；未使用模板中的陈旧
        except 不应阻断转换，也不应因校验而成为新的依赖边。
        """
        excluded_group_names: list[str] = []
        for name in sorted(state.reachable_group_names):
            for excluded in self._definitions._group_exclusions(
                state.group_definitions[name]
            ):
                if excluded not in excluded_group_names:
                    excluded_group_names.append(excluded)
        return self._definitions._validate_group_exclusions(
            excluded_group_names,
            state.group_definitions,
            state.outcome,
        )

    @staticmethod
    def _fail_expansion(
        outcome: GroupExpansionOutcome,
        message: str,
        *,
        clear_conflicts: bool = False,
    ) -> GroupExpansionOutcome:
        """统一标记展开失败并记录事务回滚事件。

        根引用、依赖闭包、except 和语义冲突都可能导致回滚，
        集中收口可保证失败标记、事件和冲突清理的行为一致。
        """
        if clear_conflicts:
            outcome.conflicts.clear()
        outcome.events.append(message)
        outcome.success = False
        return outcome

    def _commit_expansion(
        self,
        working_root: JuniperNode,
        state: _ExpansionState,
    ) -> GroupExpansionOutcome:
        """隐藏已物化的 group，原子提交工作树并生成成功事件。

        只有全部校验和展开完成后才能替换原文档，否则调用方
        可能看到部分物化、部分保留 group 的不完整配置。
        """
        state.groups_container.active = False
        self.root = working_root
        state.outcome.events.extend(
            f"已展开 Junos 配置组 {name}"
            for name in sorted(state.reachable_group_names)
        )
        return state.outcome

    @staticmethod
    def _select_effective_applications(
        candidate_applications: list[_GroupApplication],
        excluded_group_names: set[str],
    ) -> list[_GroupApplication]:
        """从候选引用中计算当前路径实际生效的 group 应用。

        引用链中任一名称被 except 排除时整条应用失效；其余候选按优先级从
        高到低排序，每个 group 只保留优先级最高的一次。输入列表不会被修改。
        """
        # 依赖链中任何 group 被 except 排除，整条传递引入路径都失效。
        eligible_applications = [
            application
            for application in candidate_applications
            if not any(
                name in excluded_group_names
                for name in application.dependency_chain
            )
        ]
        effective_applications: list[_GroupApplication] = []
        seen_group_names: set[str] = set()
        for application in sorted(
            eligible_applications,
            key=lambda item: item.precedence,
            reverse=True,
        ):
            if application.name in seen_group_names:
                continue
            seen_group_names.add(application.name)
            effective_applications.append(application)
        return effective_applications

    def _group_controls_for_path(
        self,
        application: _GroupApplication,
        target_path: list[str],
        control_keyword: str,
        state: _ExpansionState,
    ) -> list[str]:
        """读取一个 group 在指定配置路径直接声明的控制引用。

        嵌套 apply 和 except 都需要先按路径取出 group payload，再筛选
        已进入依赖闭包的名称；共用读取逻辑可避免两类控制语句解析分叉。
        """
        group_names: list[str] = []
        for child in self._selectors._payload_nodes_at_path(
            state.group_definitions[application.name],
            target_path,
        ):
            if not child.effective or child.is_block:
                continue
            group_names.extend(
                name
                for name in self._selectors._parse_group_names(
                    child.header,
                    control_keyword,
                )
                if name in state.reachable_group_names
            )
        return group_names

    def _collect_nested_exclusions(
        self,
        target_path: list[str],
        effective_applications: list[_GroupApplication],
        state: _ExpansionState,
    ) -> set[str]:
        """收集当前有效 group 在指定路径声明的嵌套排除项。

        排除规则会改变哪些引用链仍然有效，必须在发现新的嵌套
        apply 之前独立汇总，以便固定点迭代先重算当前有效集。
        """
        excluded_group_names: set[str] = set()
        for application in effective_applications:
            excluded_group_names.update(
                self._group_controls_for_path(
                    application,
                    target_path,
                    "apply-groups-except",
                    state,
                )
            )
        return excluded_group_names

    def _discover_nested_applications(
        self,
        target_path: list[str],
        effective_applications: list[_GroupApplication],
        seen_applications: set[_GroupApplication],
        state: _ExpansionState,
    ) -> list[_GroupApplication]:
        """从当前有效 group 发现尚未求值的嵌套 apply-groups。

        新引用需要继承父引用的优先级和依赖链，并通过已见集合
        防止同一传递路径重复入队；单独建模可使固定点主循环只关心是否收敛。
        """
        discovered: list[_GroupApplication] = []
        for application in effective_applications:
            nested_names = self._group_controls_for_path(
                application,
                target_path,
                "apply-groups",
                state,
            )
            for index, name in enumerate(nested_names):
                nested = _GroupApplication(
                    name=name,
                    precedence=(
                        *application.precedence[:-1],
                        -1,
                        -index,
                        0,
                    ),
                    dependency_chain=(*application.dependency_chain, name),
                )
                if nested in seen_applications:
                    continue
                seen_applications.add(nested)
                discovered.append(nested)
        return discovered

    def _resolve_group_inheritance_for_path(
        self,
        target_path: list[str],
        inherited_applications: list[_GroupApplication],
        inherited_excluded_group_names: set[str],
        state: _ExpansionState,
    ) -> _ResolvedGroupInheritance:
        """求解当前路径上的 group 应用、排除和最终有效集。

        group 内可以继续声明 apply/except，并且 except 会反过来
        改变哪些引用链仍然有效；因此必须围绕当前路径进行固定点迭代，
        直到应用集和排除集都不再变化。
        """
        candidate_applications = list(inherited_applications)
        excluded_group_names = set(inherited_excluded_group_names)
        seen_applications = set(candidate_applications)

        # 每轮先应用新排除，再从仍然有效的 group 发现嵌套引用；两个集合
        # 都只会单调增长且受依赖闭包限制，因此无新增内容时即已收敛。
        while True:
            candidate_applications, excluded_group_names, progressed = (
                self._advance_inheritance(
                    target_path,
                    candidate_applications,
                    excluded_group_names,
                    seen_applications,
                    state,
                )
            )
            if not progressed:
                return _ResolvedGroupInheritance(
                    candidate_applications=candidate_applications,
                    effective_applications=self._select_effective_applications(
                        candidate_applications,
                        excluded_group_names,
                    ),
                    excluded_group_names=excluded_group_names,
                )

    def _advance_inheritance(
        self,
        target_path: list[str],
        candidate_applications: list[_GroupApplication],
        excluded_group_names: set[str],
        seen_applications: set[_GroupApplication],
        state: _ExpansionState,
    ) -> tuple[list[_GroupApplication], set[str], bool]:
        """执行一轮排除和嵌套引用的固定点更新。

        新排除先于新引用生效，避免刚发现的嵌套应用使用已经失效的继承链。
        """
        progressed = False
        effective_applications = self._select_effective_applications(
            candidate_applications,
            excluded_group_names,
        )
        nested_exclusions = self._collect_nested_exclusions(
            target_path,
            effective_applications,
            state,
        )
        if not nested_exclusions.issubset(excluded_group_names):
            excluded_group_names.update(nested_exclusions)
            progressed = True
            effective_applications = self._select_effective_applications(
                candidate_applications,
                excluded_group_names,
            )
        new_applications = self._discover_nested_applications(
            target_path,
            effective_applications,
            seen_applications,
            state,
        )
        if new_applications:
            candidate_applications.extend(new_applications)
            progressed = True
        return candidate_applications, excluded_group_names, progressed

    def _expand_current_node(
        self,
        target_node: JuniperNode,
        target_path: list[str],
        inherited_applications: list[_GroupApplication],
        inherited_excluded_group_names: set[str],
        state: _ExpansionState,
    ) -> tuple[list[_GroupApplication], set[str]]:
        """展开一个工作树节点，并返回应传递给子节点的继承状态。

        节点求值需要合并本地和父层引用、物化 payload 并消费控制语句；
        将这些操作与树遍历分开，可以让递归方法只负责在父子节点间传递状态。
        """
        locally_applied_group_names, excluded_group_names = (
            self._read_local_group_controls(
                target_node,
                inherited_excluded_group_names,
                state.reachable_group_names,
            )
        )
        state.encountered_group_names.update(locally_applied_group_names)
        local_applications = _local_applications(
            target_path, locally_applied_group_names
        )
        resolved_inheritance = self._resolve_group_inheritance_for_path(
            target_path,
            [*local_applications, *inherited_applications],
            excluded_group_names,
            state,
        )
        state.encountered_group_names.update(
            application.name
            for application in resolved_inheritance.candidate_applications
        )
        self._merge_effective_groups(
            target_node,
            target_path,
            resolved_inheritance.effective_applications,
            state,
        )
        self._rewrite_group_controls(
            target_node,
            state.reachable_group_names,
        )
        return (
            resolved_inheritance.candidate_applications,
            resolved_inheritance.excluded_group_names,
        )

    def _merge_effective_groups(
        self,
        target_node: JuniperNode,
        target_path: list[str],
        applications: list[_GroupApplication],
        state: _ExpansionState,
    ) -> None:
        """按优先级把全部有效 group payload 合并到目标节点。

        只处理已解析出的有效应用，使缺失定义和排除项不会进入物化阶段。
        """
        for application in applications:
            group_definition = state.group_definitions.get(application.name)
            if group_definition is None:
                continue
            self._merger._merge_inherited_children(
                target_node,
                self._selectors._payload_nodes_at_path(
                    group_definition,
                    target_path,
                ),
                application.name,
                application.precedence,
                target_path,
                state.outcome,
                state.washing_policy,
            )

    def _walk_expansion_tree(
        self,
        target_node: JuniperNode,
        target_path: list[str],
        inherited_applications: list[_GroupApplication],
        inherited_excluded_group_names: set[str],
        state: _ExpansionState,
    ) -> None:
        """递归遍历工作树，在每个块节点展开并把继承状态传给子节点。

        group 继承沿配置树逐层传递，因此展开当前节点后，必须把求解
        出的引用集与排除集作为子节点的继承输入；而合并会向 target_node.children 追加
        新块，只有用索引循环才能在遍历过程中一并处理本轮物化出的块。
        """
        if not target_node.is_block or target_node is state.groups_container:
            return
        child_applications, child_excluded_group_names = (
            self._expand_current_node(
                target_node,
                target_path,
                inherited_applications,
                inherited_excluded_group_names,
                state,
            )
        )
        # 合并会追加节点，因此用索引循环继续处理新物化的块。
        index = 0
        while index < len(target_node.children):
            child = target_node.children[index]
            if (
                child.effective
                and child.is_block
                and child is not state.groups_container
            ):
                self._walk_expansion_tree(
                    child,
                    [*target_path, self._selectors._base_header(child.header)],
                    child_applications,
                    child_excluded_group_names,
                    state,
                )
            index += 1

    def _read_local_group_controls(
        self,
        target_node: JuniperNode,
        inherited_excluded_group_names: set[str],
        reachable_group_names: set[str],
    ) -> tuple[list[str], set[str]]:
        """读取当前节点直接声明的 apply-groups 和 except。

        只返回依赖闭包内的 group；排除集从父层复制后追加本层规则，使本层
        except 同时作用于本地引用和从祖先继承的引用。工作树保持不变。
        """
        require_invariant(
            target_node.is_block,
            "Junos group 控制语句只能从块节点读取",
        )
        locally_applied_group_names: list[str] = []
        excluded_group_names = set(inherited_excluded_group_names)
        for child in target_node.children:
            if not child.effective or child.is_block:
                continue
            locally_applied_group_names.extend(
                name
                for name in self._selectors._parse_group_names(
                    child.header,
                    "apply-groups",
                )
                if name in reachable_group_names
            )
            excluded_group_names.update(
                name
                for name in self._selectors._parse_group_names(
                    child.header,
                    "apply-groups-except",
                )
                if name in reachable_group_names
            )
        return locally_applied_group_names, excluded_group_names

    def _rewrite_group_controls(
        self,
        target_node: JuniperNode,
        handled_group_names: set[str],
    ) -> None:
        """从当前节点的控制语句中移除本次已经物化的 group 名称。

        一条列表中未处理的名称按原格式保留；全部名称均已处理时删除整条
        语句。普通配置节点和配置块不受影响。
        """
        require_invariant(
            target_node.is_block,
            "Junos group 控制语句只能在块节点中改写",
        )
        retained_children: list[JuniperNode] = []
        for child in target_node.children:
            rewritten_header: str | None = child.header
            if child.effective and not child.is_block:
                for control_keyword in (
                    "apply-groups",
                    "apply-groups-except",
                ):
                    statement_group_names = self._selectors._parse_group_names(
                        child.header,
                        control_keyword,
                    )
                    if not statement_group_names:
                        continue
                    rewritten_header = self._selectors._rewrite_group_control(
                        child.header,
                        control_keyword,
                        [
                            name
                            for name in statement_group_names
                            if name not in handled_group_names
                        ],
                    )
                    break
            if rewritten_header is not None:
                child.header = rewritten_header
                retained_children.append(child)
        target_node.children = retained_children


def _local_applications(
    target_path: list[str],
    group_names: list[str],
) -> list[_GroupApplication]:
    """为当前节点声明的 group 引用建立本地优先级。

    越靠近当前节点的声明获得越高层级，同一层则按声明顺序决定覆盖优先级。
    """
    return [
        _GroupApplication(
            name=name,
            precedence=(len(target_path), -index, 0),
            dependency_chain=(name,),
        )
        for index, name in enumerate(group_names)
    ]
