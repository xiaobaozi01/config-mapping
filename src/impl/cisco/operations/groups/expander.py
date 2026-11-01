"""Cisco Group 展开编排。"""

from __future__ import annotations

from collections.abc import Iterable

from impl.common.domain.outcomes import GroupExpansionOutcome
from impl.common.domain.policies import WashingPolicy

from ...document import CiscoDocument
from .definitions import CiscoGroupDefinitions
from .merger import CiscoGroupMerger
from .models import (
    _ExpansionState,
    _GroupApplication,
    _GroupControls,
    _GroupStatement,
)
from .publishing import (
    _rollback_expansion,
    clone_statements,
    from_context,
    publish_expansion,
)
from .selectors import CiscoGroupSelectors


class CiscoGroupExpander:
    """在工作副本上展开 IOS XR group，成功后原子提交。

    分析阶段不修改正式文档，使未解析引用或冲突能够整体回滚并保留原始配置。
    """

    def __init__(self, document: CiscoDocument):
        """绑定待处理的 IOS XR 文档。

        构造阶段不解析 group 或创建工作树，避免读取负责范围之外的配置成本。
        """
        self._document = document
        self._selectors = CiscoGroupSelectors()
        self._definitions = CiscoGroupDefinitions(document, self._selectors)
        self._merger = CiscoGroupMerger(document, self._selectors)

    @property
    def vendor(self) -> str:
        """返回底层 IOS XR 文档的厂商标识。

        统一标识用于冲突和报告字段，避免调用方依赖具体文档类型。
        """
        return self._document.vendor

    @property
    def top_level_nodes(self) -> list[_GroupStatement]:
        """把正式文档顶层节点复制为 group 工作语句。

        每次读取生成独立列表，防止展开中的语句修改直接污染正式文档。
        """
        return [from_context(node) for node in self._document.root.nodes]

    @top_level_nodes.setter
    def top_level_nodes(self, nodes: list[_GroupStatement]) -> None:
        """用展开后的语句树原子替换正式文档内容。

        替换只经由发布事务执行，使任何解析失败都维持原树状态。
        """
        publish_expansion(self._document, nodes)

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """编排 IOS XR group 的收集、展开、校验和事务式提交。

        所有活动引用都会被物化。返回值汇总事件、告警、冲突和规则命中数；
        只有完整展开成功才替换原文档，任何未解析引用都会整体回滚。
        """
        outcome = GroupExpansionOutcome()
        washing_policy = policy or WashingPolicy()

        if not self._definitions._has_applied_group():
            return outcome

        # 定义解析和工作树构建均不修改原根节点，便于任何失败直接回滚。
        group_definitions = self._definitions._load_group_definitions()
        working_root, terminal_commands = self._definitions._build_working_tree(
            known_interfaces
        )
        referenced_group_names = self._definitions._collect_root_group_names(
            working_root
        )
        if not referenced_group_names:
            return outcome

        # 递归期间共享引用范围、处理记录和错误状态，避免每层传递大量参数。
        state = _ExpansionState(
            group_definitions=group_definitions,
            referenced_group_names=referenced_group_names,
            outcome=outcome,
            washing_policy=washing_policy,
        )
        self._expand_node(working_root, [], [], state)
        if state.has_unresolvable_reference or not outcome.success:
            _rollback_expansion(outcome)
            return outcome
        if not state.encountered_group_names:
            return outcome

        # 只有完整展开成功才替换根节点的 children，这里是事务式提交点。
        self.top_level_nodes = self._rebuild_top_level_nodes(
            working_root,
            terminal_commands,
        )
        outcome.events.extend(
            f"已展开 IOS XR 配置组 {name}"
            for name in sorted(state.encountered_group_names)
        )
        return outcome

    def _expand_node(
        self,
        target_node: _GroupStatement,
        target_path: list[str],
        inherited_applications: list[_GroupApplication],
        state: _ExpansionState,
    ) -> None:
        """在当前节点应用有效 group，并递归处理其所有配置块。

        方法先合并当前路径对应的 group payload，再移除已消费的控制语句；
        合并可能追加新块，因此使用索引循环确保新物化节点也继续展开。
        ``state`` 会累计遇到的 group 引用、冲突和无法解析状态。
        """
        controls = self._read_local_group_controls(
            target_node,
            state.referenced_group_names,
        )
        state.encountered_group_names.update(controls.applied_group_names)
        self._validate_group_references(controls.applied_group_names, state)
        effective_applications = self._resolve_effective_applications(
            controls,
            inherited_applications,
            target_depth=len(target_path),
        )
        self._apply_group_applications(
            target_node,
            target_path,
            effective_applications,
            state,
        )

        self._rewrite_group_controls(
            target_node,
            state.referenced_group_names,
        )
        index = 0
        while index < len(target_node.statements):
            child = target_node.statements[index]
            if child.is_block:
                self._expand_node(
                    child,
                    [*target_path, child.header],
                    effective_applications,
                    state,
                )
            index += 1

    def _apply_group_applications(
        self,
        target_node: _GroupStatement,
        target_path: list[str],
        applications: list[_GroupApplication],
        state: _ExpansionState,
    ) -> None:
        """按优先级顺序合并当前路径的 group payload。

        每个应用读取同一目标路径的片段，后处理的低优先级应用只补充未覆盖语句。
        """
        for application in applications:
            definition = state.group_definitions.get(application.name)
            if definition is None:
                continue
            self._merger._merge_inherited_children(
                target_node,
                self._selectors._payload_nodes_at_path(
                    definition.body_nodes,
                    target_path,
                ),
                application.name,
                application.precedence,
                target_path,
                state.outcome,
                state.washing_policy,
            )

    def _read_local_group_controls(
        self,
        target_node: _GroupStatement,
        referenced_group_names: set[str],
    ) -> _GroupControls:
        """读取当前层级可解析的 apply 和 exclude 控制。

        只保留依赖集合中的名称，可隔离未使用模板中的陈旧引用。
        """
        applied_group_names: list[str] = []
        excluded_group_names: set[str] = set()
        for child in target_node.statements:
            applied_group_names.extend(
                name
                for name in self._selectors._parse_group_names(
                    child.header,
                    "apply-group",
                )
                if name in referenced_group_names
            )
            excluded_group_names.update(
                name
                for name in self._selectors._parse_group_names(
                    child.header,
                    "exclude-group",
                )
                if name in referenced_group_names
            )
        return _GroupControls(
            applied_group_names=tuple(applied_group_names),
            excluded_group_names=frozenset(excluded_group_names),
        )

    @staticmethod
    def _validate_group_references(
        group_names: tuple[str, ...],
        state: _ExpansionState,
    ) -> None:
        """校验活动 group 是否具备静态展开条件。

        缺失定义、运行时变量和嵌套 apply 都会使依赖闭包不完整，因此统一标记失败。
        """
        # 只校验从活动配置实际引用的 group；未使用定义不影响转换。
        for group_name in group_names:
            definition = state.group_definitions.get(group_name)
            if definition is None:
                state.outcome.warnings.append(
                    f"IOS XR apply-group 引用了未定义的组 {group_name}"
                )
                state.has_unresolvable_reference = True
            elif definition.has_runtime_variables:
                state.outcome.warnings.append(
                    f"IOS XR 组 {group_name} 包含运行时变量，无法安全静态展开"
                )
                state.has_unresolvable_reference = True
            elif definition.has_nested_apply:
                state.outcome.warnings.append(
                    f"IOS XR 组 {group_name} 内再次引用 apply-group，"
                    "无法安全静态展开"
                )
                state.has_unresolvable_reference = True

    @staticmethod
    def _resolve_effective_applications(
        controls: _GroupControls,
        inherited_applications: list[_GroupApplication],
        target_depth: int,
    ) -> list[_GroupApplication]:
        """计算当前路径实际生效的 group 应用。

        深层和靠前声明拥有更高优先级，exclude 会同时过滤本地及继承引用。
        """
        local_applications = [
            _GroupApplication(
                name=group_name,
                precedence=(target_depth, -list_index),
            )
            for list_index, group_name in enumerate(
                controls.applied_group_names
            )
        ]
        # 路径越深越具体，优先级越高；同层列表中越靠前越优先。
        # 本层 exclude 同时抑制本地引用和从父层继承下来的同名 group。
        effective_applications: list[_GroupApplication] = []
        for application in [
            *local_applications,
            *inherited_applications,
        ]:
            if application.name in controls.excluded_group_names or any(
                effective_application.name == application.name
                for effective_application in effective_applications
            ):
                continue
            effective_applications.append(application)
        return effective_applications

    def _rewrite_group_controls(
        self,
        target_node: _GroupStatement,
        handled_group_names: set[str],
    ) -> None:
        """从当前节点的控制语句中移除本次已经物化的 group 名称。

        一条列表语句中未处理的名称会按原格式保留；全部名称均已处理时删除
        整条语句。apply-group 和 exclude-group 使用相同处理规则。
        """
        retained_children: list[_GroupStatement] = []
        for child in target_node.statements:
            rewritten_header: str | None = child.header
            for control_keyword in ("apply-group", "exclude-group"):
                statement_group_names = self._selectors._parse_group_names(
                    child.header,
                    control_keyword,
                )
                if not statement_group_names:
                    continue
                rewritten_header = self._selectors._rewrite_group_control(
                    child.header,
                    control_keyword,
                    # 一条语句可同时引用多个 group，因此只摘掉本次处理的名称。
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
        target_node.statements = retained_children

    @staticmethod
    def _rebuild_top_level_nodes(
        working_root: _GroupStatement,
        terminal_commands: list[str],
    ) -> list[_GroupStatement]:
        """组装展开后的顶层节点和终止命令。

        统一补充分隔符并恢复 end/commit，保证事务结果仍是完整可解析的 Cisco 配置。
        """
        rebuilt_nodes: list[_GroupStatement] = []
        for top_level_node in working_root.statements:
            rebuilt_nodes.append(
                _GroupStatement(
                    header=top_level_node.header,
                    statements=clone_statements(
                        top_level_node.statements,
                        include_comments=True,
                    ),
                    comment=top_level_node.comment,
                )
            )
            if not top_level_node.comment or top_level_node.header:
                rebuilt_nodes.append(_GroupStatement(header="!"))
        for command in terminal_commands or ["end"]:
            rebuilt_nodes.append(_GroupStatement(header=command))
        return rebuilt_nodes
