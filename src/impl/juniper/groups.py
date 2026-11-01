"""Juniper Junos group 展开算法。"""

from __future__ import annotations

import copy
import fnmatch
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field

from impl.common.errors import require_invariant
from impl.common.interface import (
    interface_parent,
)
from impl.common.interface import (
    normalized_command as _normalized_command,
)
from impl.common.outcomes import GroupExpansionOutcome
from impl.common.policies import GroupIdentityMode, WashingPolicy
from impl.common.semantic_identity import (
    SemanticDecision,
    find_opaque_ambiguity,
)

from .document import (
    JuniperNode,
    JunosDocument,
    canonical_junos_interface,
)
from .identity import resolve_junos_identity


@dataclass(frozen=True, slots=True)
class _GroupApplication:
    """保存一次 group 应用及其继承优先级和传递引用链。

    不可变值对象可用于集合去重，并让嵌套引用继承和比较顺序保持稳定。
    """

    name: str
    precedence: tuple[int, ...]
    dependency_chain: tuple[str, ...]


@dataclass(slots=True)
class _ResolvedGroupInheritance:
    """保存当前路径最终求得的 group 继承集合。

    候选、有效和排除集合同时保留，使递归可以向子节点传递完整求值依据。
    """

    candidate_applications: list[_GroupApplication]
    effective_applications: list[_GroupApplication]
    excluded_group_names: set[str]


@dataclass(slots=True)
class _ExpansionState:
    """保存一次 group 展开共享的定义、依赖和诊断状态。

    状态集中传递可避免递归方法反复构造索引，并让失败回滚看到同一份求值记录。
    """

    groups_container: JuniperNode
    group_definitions: dict[str, JuniperNode]
    group_dependencies: dict[str, list[str]]
    outcome: GroupExpansionOutcome
    washing_policy: WashingPolicy
    reachable_group_names: set[str] = field(default_factory=set)
    encountered_group_names: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _PreparedExpansion:
    """保存事务开始后已完成预处理的工作树和状态。

    预处理标记区分原配置是否变化，使无实际展开的调用不必替换文档根节点。
    """

    working_root: JuniperNode
    state: _ExpansionState
    preprocessing_changed: bool
    pruned_group_names: tuple[str, ...]


_ResolvedStatement = tuple[JuniperNode, SemanticDecision]


class JunosGroupExpander:
    """在配置副本上展开 Junos groups，并统一提交完整结果。

    所有引用、排除和冲突先在工作树上求解，任何失败都可以保持原文档不变。
    """

    def __init__(self, document: JunosDocument):
        """绑定 Junos 文档并复用其 header 解析入口。

        构造阶段不修改文档，使同一对象可在任意当前修订上启动一次独立展开事务。
        """
        self._document = document
        self._base_header = document._parser.base_header
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

    @staticmethod
    def _base_header(header: str) -> str:
        """返回用于 group 匹配的规范节点头文本。

        具体归一化由 ``JunosDocument`` 统一实现，会去除 ``inactive:``、
        ``protect:`` 等控制前缀，避免展开器与解析器采用不同语义。
        """
        return JunosDocument._base_header(header)

    @classmethod
    def _selector_matches(
        cls, selector_header: str, target_header: str
    ) -> bool:
        """判断 group 选择器是否匹配配置节点，兼容叶子和块节点。

        普通头部使用基础文本精确匹配；尖括号内的 Junos 通配表达式通过
        ``fnmatch`` 转换后参与整串匹配。表达式非法时返回 ``False``。
        """
        pattern = cls._base_header(selector_header).rstrip(";").strip()
        target = cls._base_header(target_header).rstrip(";").strip()
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
    def _parse_group_names(
        cls,
        statement: str,
        control_keyword: str,
    ) -> list[str]:
        """解析控制语句中按声明顺序出现的 group 名称。

        忽略 inactive/protect 前缀可让同一控制语句在停用或保护状态下仍保持相同
        语义；不匹配时返回空列表，避免把普通 Junos 语句误当作 group 控制。
        """
        if control_keyword not in statement:
            return []
        base = cls._base_header(statement).rstrip(";").strip()
        match = re.match(rf"{re.escape(control_keyword)}\s+(.+)$", base)
        if not match:
            return []
        value = match.group(1).strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1].strip()
        lexer = shlex.shlex(value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            return list(lexer)
        except ValueError:
            # 引号未闭合时保守地视为不可解析，避免把半个名称当成有效 Group。
            return []

    @classmethod
    def _group_definition_name(cls, header: str) -> str:
        """解析 group 定义的规范名称。

        使用与 Junos 命令行相同的引号分词规则，避免含空格的名称被截断。
        """
        base = cls._base_header(header).strip()
        lexer = shlex.shlex(base, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError:
            return base
        return tokens[0] if len(tokens) == 1 else base

    @staticmethod
    def _format_group_name(name: str) -> str:
        """把 group 名称格式化为合法 Junos 词法形式。

        仅对必须字符添加引号，使普通名称保持简洁且特殊名称仍可无损回写。
        """
        if re.fullmatch(r"[^\s\[\]\"']+", name):
            return name
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @classmethod
    def _rewrite_group_control(
        cls,
        statement: str,
        control_keyword: str,
        remaining_group_names: list[str],
    ) -> str | None:
        """使用剩余名称重写一条 apply-groups/except 语句。

        只移除已经物化的名称并保留原有列表风格，可避免丢失尚未处理的控制引用；
        空列表对应整条语句删除，使输出不会留下无意义的 group 控制。
        """
        base = cls._base_header(statement)
        match = re.match(
            rf"({re.escape(control_keyword)})\s+(.+?)\s*;?\s*$",
            base,
        )
        if not match:
            return statement
        if not remaining_group_names:
            return None
        prefix = statement[: statement.find(base)] if base in statement else ""
        original_value = match.group(2).strip()
        value = " ".join(
            cls._format_group_name(name) for name in remaining_group_names
        )
        if original_value.startswith("[") and original_value.endswith("]"):
            value = f"[ {value} ]"
        return f"{prefix}{match.group(1)} {value};"

    @staticmethod
    def _selector_literal_length(selector_header: str) -> int:
        """计算 Junos 通配选择器去除通配元字符后的字面量长度。

        返回值越大表示选择器越具体；调用方使用原始 header 稳定打破并列。
        """
        literal = re.sub(
            r"<([^>]+)>",
            lambda item: re.sub(r"[*?\[\]]", "", item.group(1)),
            selector_header,
        )
        return len(literal)

    def _payload_nodes_at_path(
        self,
        group_definition: JuniperNode,
        target_path: list[str],
    ) -> list[JuniperNode]:
        """定位某个 group 在目标配置路径上提供的直接子配置。

        每一级只保留有效且匹配的块节点，并按选择器具体程度排序后合并其
        子节点作为下一层候选；任一级无匹配时返回空列表。
        """
        require_invariant(
            group_definition.is_block,
            "Junos group 定义必须是包含子节点的块节点",
        )
        candidate_nodes = group_definition.children
        for target_header in target_path:
            matches = [
                item
                for item in candidate_nodes
                if item.effective
                and item.is_block
                and self._selector_matches(item.header, target_header)
            ]
            if not matches:
                return []
            matches.sort(
                key=lambda item: (
                    -self._selector_literal_length(item.header),
                    item.header,
                )
            )
            candidate_nodes = [
                child for match in matches for child in (match.children or [])
            ]
        return candidate_nodes

    @staticmethod
    def _record_group_conflict(
        outcome: GroupExpansionOutcome,
        target_path: list[str],
        identity_key: str,
        rule_id: str | None,
        winner_node: JuniperNode,
        loser_node: JuniperNode,
    ) -> None:
        """把一次不同值的语义覆盖追加到展开报告。

        记录路径、语义身份、胜负来源和值，使用户能够追溯最终配置为何采用某个值；
        归一化后相同的重复语句不报冲突，避免仅由书写差异制造告警。
        """
        if _normalized_command(winner_node.header) == _normalized_command(
            loser_node.header
        ):
            return
        conflict = {
            "vendor": JunosDocument.vendor,
            "path": " / ".join(target_path) or "<root>",
            "key": identity_key,
            "winner_source": winner_node.origin,
            "winner_value": winner_node.header,
            "loser_source": loser_node.origin,
            "loser_value": loser_node.header,
        }
        if rule_id is not None:
            conflict["rule_id"] = rule_id
        outcome.conflicts.append(conflict)

    def _merge_inherited_children(
        self,
        target_node: JuniperNode,
        inherited_children: list[JuniperNode],
        group_name: str,
        precedence: tuple[int, ...],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> None:
        """按节点类型把 group payload 分发给块或语句合并器。

        不同类型需要不同冲突规则，集中分发可保证同一 payload 不会走错合并路径。
        """
        require_invariant(
            target_node.is_block,
            "Junos group 合并目标必须是块节点",
        )
        for inherited_node in inherited_children:
            if not self._is_mergeable_group_child(inherited_node):
                continue
            if inherited_node.is_block:
                self._merge_inherited_block(
                    target_node,
                    inherited_node,
                    group_name,
                    precedence,
                )
                continue
            self._merge_inherited_statement(
                target_node,
                inherited_node,
                group_name,
                precedence,
                target_path,
                outcome,
                washing_policy,
            )

    def _is_mergeable_group_child(self, inherited_node: JuniperNode) -> bool:
        """判断 group 子节点是否应作为业务配置参与合并。

        无效节点不应物化；apply/except 属于继承控制语句，
        已由依赖求值阶段处理，不能被当作普通配置复制到目标树。
        """
        if not inherited_node.effective or not inherited_node.header:
            return False
        return not (
            self._parse_group_names(inherited_node.header, "apply-groups")
            or self._parse_group_names(
                inherited_node.header,
                "apply-groups-except",
            )
        )

    def _merge_inherited_block(
        self,
        target_node: JuniperNode,
        inherited_node: JuniperNode,
        group_name: str,
        precedence: tuple[int, ...],
    ) -> None:
        """合并 group 块，并让匹配的叶子节点承接继承子配置。

        普通块需要先建立结构，通配块只选择已有节点；已有叶子可升级为块，
        再由后续递归合并其子配置。
        """
        require_invariant(
            target_node.is_block,
            "Junos group 块合并目标必须是块节点",
        )
        matched = False
        has_payload = any(
            child.effective and child.header
            for child in inherited_node.children or []
        )
        for child in target_node.children:
            if not child.effective or not self._selector_matches(
                inherited_node.header, child.header
            ):
                continue
            matched = True
            if not child.is_block and has_payload:
                child.header = child.header.rstrip().rstrip(";").rstrip()
                child.children = []
                child.kind = "block"
                child.declared_in_source = True

        # 通配选择器只匹配已有具体节点，不生成 ``<ge-*>`` 块。
        if matched or "<" in inherited_node.header:
            return
        target_node.children.append(
            JuniperNode(
                inherited_node.header,
                [],
                origin=f"group:{group_name}",
                rank=precedence,
                comment=inherited_node.comment,
            )
        )

    def _merge_inherited_statement(
        self,
        target_node: JuniperNode,
        inherited_node: JuniperNode,
        group_name: str,
        precedence: tuple[int, ...],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> None:
        """按语义 identity 定位槽位并按继承优先级合并叶子语句。

        文本不同的 Junos 语句可能表示同一配置槽位，必须先进行
        语义归一化，再统一处理显式配置、group 优先级和未知语义策略。
        """
        require_invariant(
            target_node.is_block,
            "Junos group 语句合并目标必须是块节点",
        )
        (
            identity_decision,
            existing_statement_decisions,
            existing_statement,
            inherited_statement,
        ) = self._prepare_merge(
            target_node,
            inherited_node,
            group_name,
            precedence,
            target_path,
            outcome,
        )
        if existing_statement is None:
            if self._handle_unknown_identity_ambiguity(
                identity_decision,
                existing_statement_decisions,
                inherited_statement,
                target_path,
                outcome,
                washing_policy,
            ):
                return
            target_node.children.append(inherited_statement)
            return
        self._merge_statement_by_precedence(
            existing_statement,
            inherited_statement,
            identity_decision,
            target_path,
            outcome,
        )

    def _prepare_merge(
        self,
        target_node: JuniperNode,
        inherited_node: JuniperNode,
        group_name: str,
        precedence: tuple[int, ...],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> tuple[
        SemanticDecision,
        list[_ResolvedStatement],
        JuniperNode | None,
        JuniperNode,
    ]:
        """解析继承语句的 identity、现有槽位决策和待合并副本。

        这些值共同决定覆盖方向，提前集中解析可避免多个合并分支采用不同优先级。
        """
        identity_decision = resolve_junos_identity(
            self._base_header(inherited_node.header),
            path=target_path,
        )
        if identity_decision.matched:
            outcome.identity_rule_hits += 1
        else:
            outcome.identity_fallbacks += 1
        decisions = self._resolve_target_statements(target_node, target_path)
        existing = next(
            (
                item
                for item, item_decision in decisions
                if item_decision.key == identity_decision.key
            ),
            None,
        )
        inherited_statement = JuniperNode(
            inherited_node.header,
            origin=f"group:{group_name}",
            rank=precedence,
            comment=inherited_node.comment,
        )
        return identity_decision, decisions, existing, inherited_statement

    def _resolve_target_statements(
        self,
        target_node: JuniperNode,
        target_path: list[str],
    ) -> list[_ResolvedStatement]:
        """解析目标节点中所有有效叶子语句的语义 identity。

        候选 group 语句需要和同一路径下的现有语句比较；集中解析
        可以让合并方法只关心 identity 匹配和优先级。
        """
        require_invariant(
            target_node.is_block,
            "Junos identity 解析目标必须是块节点",
        )
        return [
            (
                item,
                resolve_junos_identity(
                    self._base_header(item.header),
                    path=target_path,
                ),
            )
            for item in target_node.children
            if item.effective and not item.is_block
        ]

    def _handle_unknown_identity_ambiguity(
        self,
        identity_decision: SemanticDecision,
        existing_statement_decisions: list[_ResolvedStatement],
        inherited_statement: JuniperNode,
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> bool:
        """识别并记录未知 identity 歧义，返回是否拒绝候选语句。

        规则未覆盖的语句无法安全判定是追加还是覆盖，需要按
        ``preserve``、``warn`` 或 ``fail`` 策略统一审计和决策。
        """
        if (
            identity_decision.matched
            or washing_policy.group_unknown_identity
            == GroupIdentityMode.PRESERVE
        ):
            return False
        ambiguous_existing_statement = find_opaque_ambiguity(
            identity_decision,
            existing_statement_decisions,
        )
        if ambiguous_existing_statement is None:
            return False

        ambiguity_record = self._ambiguity_record(
            identity_decision,
            ambiguous_existing_statement,
            inherited_statement,
            target_path,
        )
        _record_ambiguity(outcome, ambiguity_record, washing_policy)
        should_reject = (
            washing_policy.group_unknown_identity == GroupIdentityMode.FAIL
        )
        if should_reject:
            outcome.success = False
        return should_reject

    @staticmethod
    def _ambiguity_record(
        identity_decision: SemanticDecision,
        existing_statement: JuniperNode,
        inherited_statement: JuniperNode,
        target_path: list[str],
    ) -> dict[str, str]:
        """构造未知语句族冲突的可审计明细。

        同时保留双方来源和值，使告警或失败能够在没有原始 AST 的情况下复查。
        """
        command_family = (
            identity_decision.normalized.split(maxsplit=1)[0]
            if identity_decision.normalized
            else ""
        )
        return {
            "vendor": JunosDocument.vendor,
            "path": " / ".join(target_path) or "<root>",
            "family": command_family,
            "existing_source": existing_statement.origin,
            "existing_value": existing_statement.header,
            "candidate_source": inherited_statement.origin,
            "candidate_value": inherited_statement.header,
        }

    def _merge_statement_by_precedence(
        self,
        existing_statement: JuniperNode,
        inherited_statement: JuniperNode,
        identity_decision: SemanticDecision,
        target_path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> None:
        """按显式配置和 group 优先级选择胜出语句并记录冲突。

        Junos 继承要求显式配置高于 group，group 之间又受层级和
        引用顺序影响；将胜负判定收口可避免多个合并路径出现不同规则。
        """
        if (
            existing_statement.origin != "explicit"
            and inherited_statement.rank > existing_statement.rank
        ):
            self._record_group_conflict(
                outcome,
                target_path,
                identity_decision.key,
                identity_decision.rule_id,
                inherited_statement,
                existing_statement,
            )
            existing_statement.header = inherited_statement.header
            existing_statement.comment = inherited_statement.comment
            existing_statement.origin = inherited_statement.origin
            existing_statement.rank = inherited_statement.rank
            return
        self._record_group_conflict(
            outcome,
            target_path,
            identity_decision.key,
            identity_decision.rule_id,
            existing_statement,
            inherited_statement,
        )

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
        if not self._has_group_controls():
            return outcome
        prepared = self._prepare_expansion(outcome, washing_policy)
        working_root = prepared.working_root
        state = prepared.state

        # except 是排除规则而不是继承依赖，但活动配置中的引用仍必须指向已定义
        # group。先校验可覆盖“只有 except、没有 apply”的配置。
        if not self._validate_root_exclusions(working_root, state):
            return self._fail_expansion(
                outcome,
                "Junos group 展开未完整解析，已整体回滚并保留原配置",
            )

        root_group_names = self._collect_root_group_names(
            working_root,
            state.groups_container,
        )
        if not root_group_names:
            if prepared.preprocessing_changed:
                self.root = working_root
            self._record_pruned_groups(outcome, prepared.pruned_group_names)
            return outcome

        # 在改写工作树前先校验整个依赖闭包，循环或缺失定义都整体回滚。
        if not self._validate_dependency_chain(root_group_names, state):
            return self._fail_expansion(
                outcome,
                "Junos group 展开未完整解析，已整体回滚并保留原配置",
            )

        self._add_known_interfaces(working_root, known_interfaces)
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
        self._record_pruned_groups(result, prepared.pruned_group_names)
        return result

    def _has_group_controls(self) -> bool:
        """快速判断配置是否包含 group 控制语句或定义。

        没有相关文本时跳过深拷贝，避免大型 prefix-set 和策略配置承担无意义事务成本。
        """
        return any(
            "groups" in node.header.lower()
            for node in self._iter_source_nodes()
        )

    def _validate_dependency_chain(
        self,
        root_group_names: set[str],
        state: _ExpansionState,
    ) -> bool:
        """校验活动 group 的完整依赖闭包和可达排除项。

        两项校验都必须在修改工作树前完成，使缺失定义和循环不会造成部分展开。
        """
        return self._resolve_dependency_closure(
            root_group_names, state
        ) and self._validate_selected_exclusions(state)

    def _prepare_expansion(
        self,
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> _PreparedExpansion:
        """深拷贝原配置，规范化工作树并建立展开状态。

        所有展开必须在工作副本上进行，才能在校验失败时整体回滚；
        同时归并容器和建立依赖索引，使后续阶段共享统一视图。
        """
        # 事务边界：本方法以后的所有变更都只发生在 working_root 上。
        working_root = copy.deepcopy(self.root)
        require_invariant(
            working_root.is_block,
            "Junos 工作树根节点必须可包含子节点",
        )
        pruned_group_names = self._prune_excluded_groups(
            working_root, set(washing_policy.junos_excluded_groups)
        )
        groups_container, group_definitions, normalized_groups = (
            self._normalize_group_containers(working_root)
        )
        state = _ExpansionState(
            groups_container=groups_container,
            group_definitions=group_definitions,
            group_dependencies={
                name: self._group_dependencies(group)
                for name, group in group_definitions.items()
            },
            outcome=outcome,
            washing_policy=washing_policy,
        )
        return _PreparedExpansion(
            working_root=working_root,
            state=state,
            preprocessing_changed=bool(pruned_group_names) or normalized_groups,
            pruned_group_names=tuple(sorted(pruned_group_names)),
        )

    @staticmethod
    def _record_pruned_groups(
        outcome: GroupExpansionOutcome,
        group_names: tuple[str, ...],
    ) -> None:
        """记录成功提交的 group 裁剪事件。

        延迟到提交后报告，避免失败回滚时把未生效的裁剪误报给调用方。
        """
        outcome.events.extend(
            f"已裁剪 Junos 配置组 {name} 及其引用" for name in group_names
        )

    def _prune_excluded_groups(
        self,
        root_node: JuniperNode,
        excluded_names: set[str],
    ) -> set[str]:
        """从工作树中移除指定 group 定义及其控制引用。

        单 RE 镜像不应继承 re1 的配置；若只删除定义，剩余引用会在后续
        依赖校验时报未定义。建立索引前同步裁剪定义和引用，才能阻止其内容展开，
        并使这次修改随 group 展开的工作树一起提交或回滚。
        """
        if not excluded_names:
            return set()
        pruned_names = self._prune_excluded_definitions(
            root_node, excluded_names
        )
        self._prune_group_references(root_node, excluded_names, pruned_names)
        return pruned_names

    def _prune_excluded_definitions(
        self,
        root_node: JuniperNode,
        excluded_names: set[str],
    ) -> set[str]:
        """停用被排除的 group 定义并清理空容器。

        同时清理无活动子节点的容器，避免重建索引时把空 block 当作有效定义来源。
        """
        pruned_names: set[str] = set()
        for container in self._group_containers(root_node):
            for definition in container.children or []:
                if not definition.effective or not definition.is_block:
                    continue
                name = self._group_definition_name(definition.header)
                if name in excluded_names:
                    definition.active = False
                    pruned_names.add(name)
            if not any(child.active for child in container.children or []):
                container.active = False
        return pruned_names

    def _prune_group_references(
        self,
        node: JuniperNode,
        excluded_names: set[str],
        pruned_names: set[str],
    ) -> None:
        """递归删除活动 group 控制语句中的排除名称。

        仅在叶子控制语句上改写，块节点继续向下遍历，保持普通配置层级不变。
        """
        if not node.effective:
            return
        if node.is_block:
            for child in node.children or []:
                self._prune_group_references(
                    child, excluded_names, pruned_names
                )
            return
        for keyword in ("apply-groups", "apply-groups-except"):
            names = self._parse_group_names(node.header, keyword)
            removed = excluded_names.intersection(names)
            if not removed:
                continue
            rewritten = self._rewrite_group_control(
                node.header,
                keyword,
                [name for name in names if name not in excluded_names],
            )
            node.active = rewritten is not None
            if rewritten is not None:
                node.header = rewritten
            pruned_names.update(removed)
            break

    def _validate_root_exclusions(
        self,
        working_root: JuniperNode,
        state: _ExpansionState,
    ) -> bool:
        """校验 group 定义之外的所有活动 except 引用。

        根配置可能只有 ``apply-groups-except`` 而没有任何 apply，
        如果等到选择展开根之后才检查，这类悬空引用会被提前返回漏掉。
        """
        names = self._root_group_exclusions(
            working_root,
            state.groups_container,
        )
        return self._validate_group_exclusions(
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
            for excluded in self._group_exclusions(
                state.group_definitions[name]
            ):
                if excluded not in excluded_group_names:
                    excluded_group_names.append(excluded)
        return self._validate_group_exclusions(
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

    def _group_containers(self, root_node: JuniperNode) -> list[JuniperNode]:
        """返回根节点下全部有效的顶层 ``groups`` 容器。

        规范的 Junos 输出通常只有一个容器，但手写或拼接配置可能重复
        声明。完整收集能让存在性检查和后续规范化看到每个容器中的定义。
        """
        return [
            node
            for node in (root_node.children or [])
            if node.effective
            and node.is_block
            and self._base_header(node.header) == "groups"
        ]

    def _normalize_group_containers(
        self,
        root_node: JuniperNode,
    ) -> tuple[JuniperNode, dict[str, JuniperNode], bool]:
        """把多个 groups 容器及重复的同名定义合并为规范单容器。

        后续遍历需要一个明确的跳过边界，而依赖索引必须包含所有定义。
        先规范化可继续使用单个 ``groups_container``，避免每个递归方法都处理列表；
        同名定义的子节点按原始出现顺序合并，以符合配置片段的合并输入场景。
        """
        require_invariant(
            root_node.is_block,
            "Junos group 规范化的根节点必须可包含子节点",
        )
        group_containers = self._group_containers(root_node)
        if not group_containers:
            return JuniperNode("groups", []), {}, False

        canonical_container, group_definitions, definitions_changed = (
            self._merge_group_definitions(group_containers)
        )
        containers_changed = len(group_containers) > 1
        if containers_changed:
            self._remove_extra_group_containers(root_node, group_containers)
        return (
            canonical_container,
            group_definitions,
            definitions_changed or containers_changed,
        )

    def _merge_group_definitions(
        self,
        group_containers: list[JuniperNode],
    ) -> tuple[JuniperNode, dict[str, JuniperNode], bool]:
        """建立 group 索引，并把重复的同名定义合并到首个定义。

        手写或拼接配置可能分段声明同名 group，后续依赖和 payload
        查询需要一个唯一定义入口，同时保留各片段的原始出现顺序。
        """
        canonical_container = group_containers[0]
        require_invariant(
            canonical_container.is_block,
            "Junos groups 容器必须是块节点",
        )
        normalized_children: list[JuniperNode] = []
        group_definitions: dict[str, JuniperNode] = {}
        definitions_changed = False

        for group_container in group_containers:
            for definition in group_container.children or []:
                if not definition.effective or not definition.is_block:
                    normalized_children.append(definition)
                    continue
                name = self._group_definition_name(definition.header)
                existing_definition = group_definitions.get(name)
                if existing_definition is None:
                    group_definitions[name] = definition
                    normalized_children.append(definition)
                    continue
                require_invariant(
                    existing_definition.is_block,
                    f"Junos group {name} 定义必须是块节点",
                )
                existing_definition.children.extend(definition.children or [])
                definitions_changed = True

        canonical_container.children = normalized_children
        return canonical_container, group_definitions, definitions_changed

    @staticmethod
    def _remove_extra_group_containers(
        root_node: JuniperNode,
        group_containers: list[JuniperNode],
    ) -> None:
        """从根节点移除已合并到规范容器的其余 groups 块。

        展开器的遍历需要一个明确的 group 定义边界，保留多个容器
        会让跳过判定、引用索引和最终隐藏行为变得不一致。
        """
        require_invariant(
            root_node.is_block,
            "Junos group 容器清理的根节点必须可包含子节点",
        )
        extra_container_ids = {
            id(container) for container in group_containers[1:]
        }
        root_node.children = [
            node
            for node in root_node.children
            if id(node) not in extra_container_ids
        ]

    def _add_known_interfaces(
        self,
        working_root: JuniperNode,
        known_interfaces: Iterable[str],
    ) -> None:
        """把拓扑已知但配置未声明的接口加入工作树。

        Junos 通配 group 需要具体接口作为匹配目标；这一步放在活动引用
        校验之后，避免仅清理 inactive group 时把 synthetic 接口意外写入配置。
        """
        require_invariant(
            working_root.is_block,
            "Junos 工作树根节点必须可包含子节点",
        )

        # 拓扑中出现但配置未声明的接口也需参与通配 group 匹配。
        interfaces_container = next(
            (
                node
                for node in working_root.children
                if node.effective
                and node.is_block
                and self._base_header(node.header) == "interfaces"
            ),
            None,
        )
        if interfaces_container is None:
            interfaces_container = JuniperNode("interfaces", [])
            working_root.children.append(interfaces_container)
        require_invariant(
            interfaces_container.is_block,
            "Junos interfaces 节点必须是块节点",
        )
        existing_interface_names = {
            canonical_junos_interface(self._base_header(node.header).split()[0])
            for node in interfaces_container.children
            if node.effective and node.is_block
        }
        for raw_interface_name in known_interfaces:
            canonical_name = canonical_junos_interface(
                interface_parent(raw_interface_name)
            )
            if canonical_name in existing_interface_names:
                continue
            interfaces_container.children.append(
                JuniperNode(canonical_name, [], origin="synthetic")
            )
            existing_interface_names.add(canonical_name)

    def _group_dependencies(self, group: JuniperNode) -> list[str]:
        """递归收集一个 group 定义内声明的 apply-groups 依赖。

        结果按首次出现顺序去重，仅遍历有效节点；它描述定义中的直接引用，
        传递闭包和循环检查由 ``_resolve_dependency_closure`` 完成。
        """
        dependency_names: list[str] = []

        def collect(node: JuniperNode) -> None:
            """收集节点子树中的直接 apply-groups 名称。

            按首次出现顺序去重，使依赖闭包和告警顺序保持稳定。
            """
            if not node.is_block:
                for name in self._parse_group_names(
                    node.header,
                    "apply-groups",
                ):
                    if name not in dependency_names:
                        dependency_names.append(name)
                return
            for child in node.children:
                if child.effective:
                    collect(child)

        collect(group)
        return dependency_names

    def _group_exclusions(self, node: JuniperNode) -> list[str]:
        """递归收集指定子树内有效的 ``apply-groups-except`` 引用。

        except 只是取消某条继承关系，并不会引入 group 内容，所以
        这里单独收集它用于存在性校验，不能复用 ``_group_dependencies``，否则
        会把被排除的 group 错误地加入展开集合和循环依赖检测。
        """
        excluded_group_names: list[str] = []

        def collect(current: JuniperNode) -> None:
            """收集当前子树中的有效 except 名称。

            保留首次出现顺序，避免重复告警并维持与源配置一致的诊断顺序。
            """
            # 控制语句是叶子节点；统一交给解析器处理列表和带引号名称。
            if not current.is_block:
                for name in self._parse_group_names(
                    current.header,
                    "apply-groups-except",
                ):
                    # 保留首次出现顺序，既避免重复告警，也让告警顺序与原配置一致。
                    if name not in excluded_group_names:
                        excluded_group_names.append(name)
                return
            for child in current.children:
                # inactive 节点不会在设备上生效，不应因其引用缺失而阻断转换。
                if child.effective:
                    collect(child)

        collect(node)
        return excluded_group_names

    def _root_group_exclusions(
        self,
        root: JuniperNode,
        container: JuniperNode,
    ) -> list[str]:
        """收集主配置树中、group 定义之外的有效 except 引用。

        主配置中的 except 即使没有配套的 ``apply-groups``，仍然是一个
        需要校验的活动引用，不能被“没有待展开根 group”的提前返回漏掉；同时
        必须跳过 groups 容器，因为未使用模板中的引用要等模板被选中后再校验。
        ``groups_container`` 是工作树中唯一的 groups 容器（归一化已保证），因此这里
        只需一次身份比较，不必维护容器集合。
        """
        excluded_group_names: list[str] = []

        def collect(node: JuniperNode) -> None:
            """收集模板定义之外的根级 except 名称。

            跳过 groups 容器，使未使用模板中的陈旧引用只在模板被选中后校验。
            """
            # group 定义由 _group_exclusions 在依赖闭包确定后按需检查。
            if node is container:
                return
            if not node.is_block:
                for name in self._parse_group_names(
                    node.header,
                    "apply-groups-except",
                ):
                    if name not in excluded_group_names:
                        excluded_group_names.append(name)
                return
            for child in node.children:
                if child.effective:
                    collect(child)

        collect(root)
        return excluded_group_names

    @staticmethod
    def _validate_group_exclusions(
        excluded_group_names: Iterable[str],
        group_definitions: dict[str, JuniperNode],
        outcome: GroupExpansionOutcome,
    ) -> bool:
        """检查每个 except 名称是否有对应定义并记录缺失告警。

        引用合法性和继承依赖是两件事。单独校验可以拦截最终会被 Junos
        拒绝的悬空引用，同时保证 except 不会选择 group、触发展开或形成依赖环。
        """
        valid = True
        for name in excluded_group_names:
            if name in group_definitions:
                continue
            outcome.warnings.append(
                f"Junos apply-groups-except 引用了未定义的组 {name}"
            )
            valid = False
        return valid

    def _collect_root_group_names(
        self,
        working_root: JuniperNode,
        groups_container: JuniperNode,
    ) -> set[str]:
        """收集 group 定义之外的全部活动根引用。

        不进入 groups 容器，避免把模板内部的控制语句误当成主配置根引用。
        """
        root_group_names: set[str] = set()

        def walk(node: JuniperNode) -> None:
            """深度遍历主配置并累积根级 apply-groups 名称。

            只在块节点继续向下，叶子控制语句解析后即可结束当前分支。
            """
            if not node.is_block or node is groups_container:
                return
            for child in node.children:
                if not child.effective:
                    continue
                names = (
                    self._parse_group_names(child.header, "apply-groups")
                    if not child.is_block
                    else []
                )
                for name in names:
                    root_group_names.add(name)
                if child.is_block:
                    walk(child)

        walk(working_root)
        return root_group_names

    def _resolve_dependency_closure(
        self,
        root_group_names: set[str],
        state: _ExpansionState,
    ) -> bool:
        """解析根 group 的完整传递依赖闭包，并校验其合法性。

        使用三色深度优先遍历区分未访问、递归中和已完成节点，从而发现循环；
        缺失定义或循环会写入告警并返回 ``False``。成功时把所有可达 group
        加入 ``state.reachable_group_names``，但尚不修改工作树。
        """
        visit_states: dict[str, int] = {}
        dependency_stack: list[str] = []
        has_unresolved_dependency = False

        def visit(name: str) -> None:
            """用三色 DFS 访问一个 group 依赖节点。

            当前递归链中的节点再次出现即形成循环，必须记录后停止继续展开。
            """
            nonlocal has_unresolved_dependency
            visit_state = visit_states.get(name, 0)
            # 0=未访问，1=当前递归链中，2=已完成；重新遇到 1 即构成环。
            if visit_state == 2:
                return
            if visit_state == 1:
                cycle_start = dependency_stack.index(name)
                cycle = [*dependency_stack[cycle_start:], name]
                state.outcome.warnings.append(
                    f"Junos group 存在循环引用: {' -> '.join(cycle)}"
                )
                has_unresolved_dependency = True
                return
            if name not in state.group_definitions:
                state.outcome.warnings.append(
                    f"Junos apply-groups 引用了未定义的组 {name}"
                )
                has_unresolved_dependency = True
                return
            visit_states[name] = 1
            dependency_stack.append(name)
            state.reachable_group_names.add(name)
            for dependency in state.group_dependencies.get(name, []):
                visit(dependency)
            dependency_stack.pop()
            visit_states[name] = 2

        for name in sorted(root_group_names):
            visit(name)
        return not has_unresolved_dependency

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
        for child in self._payload_nodes_at_path(
            state.group_definitions[application.name],
            target_path,
        ):
            if not child.effective or child.is_block:
                continue
            group_names.extend(
                name
                for name in self._parse_group_names(
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
            self._merge_inherited_children(
                target_node,
                self._payload_nodes_at_path(
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
                    [*target_path, self._base_header(child.header)],
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
                for name in self._parse_group_names(
                    child.header,
                    "apply-groups",
                )
                if name in reachable_group_names
            )
            excluded_group_names.update(
                name
                for name in self._parse_group_names(
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
                    statement_group_names = self._parse_group_names(
                        child.header,
                        control_keyword,
                    )
                    if not statement_group_names:
                        continue
                    rewritten_header = self._rewrite_group_control(
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


def _record_ambiguity(
    outcome: GroupExpansionOutcome,
    record: dict[str, str],
    washing_policy: WashingPolicy,
) -> None:
    """去重记录未知语句族冲突并生成策略警告。

    同一冲突只写一次警告，保留全部不同冲突供调用方按策略告警或中止。
    """
    if record in outcome.ambiguities:
        return
    outcome.ambiguities.append(record)
    resolution = "已中止本次 group 展开"
    if washing_policy.group_unknown_identity != GroupIdentityMode.FAIL:
        resolution = "已保留两个值"
    outcome.warnings.append(
        f"Junos 路径 {record['path']} 下的未知语句族 "
        f"{record['family']} 可能存在继承冲突，{resolution}"
    )
