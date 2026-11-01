"""Cisco IOS XR group 展开算法。"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from impl.common.domain.interface import (
    normalized_command as _normalized_command,
)
from impl.common.domain.outcomes import GroupExpansionOutcome
from impl.common.domain.policies import GroupIdentityMode, WashingPolicy
from impl.common.semantic_identity import (
    SemanticDecision,
    find_opaque_ambiguity,
)

from ..document import CiscoDocument, canonical_cisco_interface
from ..support.identity import resolve_cisco_identity
from ..syntax.rewrite_helpers import append_top_level


@dataclass
class _GroupStatement:
    """表示 group 展开期间使用的可变配置语句节点。

    该轻量节点保留来源和继承优先级，使合并、渲染和工作树回滚独立于正式 Cisco AST。
    """

    header: str
    statements: list[_GroupStatement] = field(default_factory=list)
    is_block: bool = False
    origin: str = "explicit"
    rank: tuple[int, int] = (1_000_000, 0)
    comment: str | None = None

    @property
    def active(self):
        """返回工作语句是否需要参与展开。

        工作树节点在创建后保持活动，逻辑删除通过从列表移除而非状态标记完成。
        """
        return True

    @property
    def is_formatting(self):
        """返回语句是否只是空行或叹号分隔符。

        格式节点不参与 identity 合并，但仍可能在最终重写中保留。
        """
        return self.header in {"", "!"}

    def walk(self, *, include_self=False):
        """按深度优先顺序遍历非格式工作语句。

        跳过格式节点让 group 规则只处理实际命令，同时可选择包含当前节点。
        """
        pending = [self] if include_self else list(reversed(self.statements))
        while pending:
            item = pending.pop()
            if not item.is_formatting:
                yield item
            pending.extend(reversed(item.statements))


@dataclass(slots=True)
class _GroupDefinition:
    """保存一个 IOS XR group 的可物化节点和静态能力标记。

    在遍历前识别运行时变量和嵌套 apply 可避免使用无法安全展开的定义。
    """

    body_nodes: list[_GroupStatement]
    has_runtime_variables: bool
    has_nested_apply: bool


@dataclass(frozen=True, slots=True)
class _GroupApplication:
    """记录一个 group 引用及其继承优先级。

    优先级由路径深度和同级顺序组成，用于确定同名语句的覆盖方向。
    """

    name: str
    precedence: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _GroupControls:
    """保存一个配置层级直接声明的 apply 和 exclude 名称。

    将两类控制一起解析，后续可以同时抑制本地引用和从父层继承的引用。
    """

    applied_group_names: tuple[str, ...]
    excluded_group_names: frozenset[str]


@dataclass(slots=True)
class _ExpansionState:
    """保存一次 group 展开共享的索引、结果和失败状态。

    递归阶段共享状态，避免重复加载定义或通过长参数列表传递可变结果。
    """

    group_definitions: dict[str, _GroupDefinition]
    referenced_group_names: set[str]
    outcome: GroupExpansionOutcome
    washing_policy: WashingPolicy
    encountered_group_names: set[str] = field(default_factory=set)
    has_unresolvable_reference: bool = False


class CiscoGroupExpander:
    """在工作副本上展开 IOS XR group，成功后原子提交。

    分析阶段不修改正式文档，使未解析引用或冲突能够整体回滚并保留原始配置。
    """

    def __init__(self, document: CiscoDocument):
        """绑定待处理的 IOS XR 文档。

        构造阶段不解析 group 或创建工作树，避免读取负责范围之外的配置成本。
        """
        self._document = document

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

    @staticmethod
    def _parse_group_names(command: str, control_keyword: str) -> list[str]:
        """解析 group 控制语句中按声明顺序出现的名称。

        同时接受关键字单复数、单个名称、方括号列表及带引号名称；不匹配的命令返回
        空列表，使调用方能够安全地把普通配置与真正的 group 控制语句区分开。
        """
        stripped = command.strip()
        if not stripped.lower().startswith(control_keyword):
            return []
        match = re.match(
            rf"{re.escape(control_keyword)}s?\s+(.+?)\s*$",
            stripped,
            re.IGNORECASE,
        )
        if not match:
            return []
        return [
            token.strip("'\"")
            for token in match.group(1).strip().strip("[]").split()
            if token
        ]

    @staticmethod
    def _rewrite_group_control(
        command: str,
        control_keyword: str,
        remaining_group_names: list[str],
    ) -> str | None:
        """使用剩余名称重写一条 apply/exclude-group 控制语句。

        保留原关键字形式和方括号列表风格；没有剩余名称表示整条控制语句已
        无意义，此时返回 ``None``。不匹配指定关键字时原样返回。
        """
        match = re.match(
            rf"({re.escape(control_keyword)}s?)\s+(.+?)\s*$",
            command.strip(),
            re.IGNORECASE,
        )
        if not match:
            return command
        if not remaining_group_names:
            return None
        original_value = match.group(2).strip()
        value = " ".join(remaining_group_names)
        if original_value.startswith("[") and original_value.endswith("]"):
            value = f"[ {value} ]"
        return f"{match.group(1)} {value}"

    @staticmethod
    def _selector_matches(selector_header: str, target_header: str) -> bool:
        """判断 group 路径选择器是否匹配目标配置节点。

        无引号时执行忽略大小写的精确匹配，其中接口名先展开缩写再比较；
        引号内文本按正则表达式处理，其余部分按字面量处理。非法正则返回
        ``False``，不会中断整个展开流程。
        """
        selector_header = " ".join(selector_header.strip().split())
        target_header = " ".join(target_header.strip().split())
        if "'" not in selector_header and '"' not in selector_header:
            if selector_header.lower().startswith(
                "interface "
            ) and target_header.lower().startswith("interface "):
                return canonical_cisco_interface(
                    selector_header.split(maxsplit=1)[1]
                ) == canonical_cisco_interface(
                    target_header.split(maxsplit=1)[1]
                )
            return selector_header.lower() == target_header.lower()
        pieces: list[str] = []
        cursor = 0
        for match in re.finditer(r"(['\"])(.*?)\1", selector_header):
            pieces.append(
                re.escape(selector_header[cursor : match.start()]).replace(
                    r"\ ", r"\s+"
                )
            )
            pieces.append(f"(?:{match.group(2)})")
            cursor = match.end()
        pieces.append(
            re.escape(selector_header[cursor:]).replace(r"\ ", r"\s+")
        )
        try:
            return bool(
                re.fullmatch("".join(pieces), target_header, re.IGNORECASE)
            )
        except re.error:
            return False

    @staticmethod
    def _selector_literal_length(selector_header: str) -> int:
        """计算 IOS XR 选择器去除正则元字符后的字面量长度。

        返回去掉正则元字符后的字面量长度，越长越具体；调用方使用原始
        selector header 稳定打破并列。
        """
        literal = re.sub(
            r"(['\"])(.*?)\1",
            lambda item: re.sub(r"[.*+?\[\](){}|\\]", "", item.group(2)),
            selector_header,
        )
        return len(literal)

    def _payload_nodes_at_path(
        self,
        definition_nodes: list[_GroupStatement],
        target_path: list[str],
    ) -> list[_GroupStatement]:
        """定位某个 group 在目标配置路径上提供的直接子配置。

        每层优先处理更具体的选择器，减少多个通配项同时命中时的不确定性；任一级
        没有匹配就立即停止，防止其他分支的子配置被错误继承到当前路径。
        """
        candidate_nodes = definition_nodes
        for target_header in target_path:
            matches = [
                node
                for node in candidate_nodes
                if self._selector_matches(node.header, target_header)
            ]
            if not matches:
                return []
            matches.sort(
                key=lambda node: (
                    -self._selector_literal_length(node.header),
                    node.header,
                )
            )
            candidate_nodes = [
                child for match in matches for child in match.statements
            ]
        return candidate_nodes

    def _record_group_conflict(
        self,
        outcome: GroupExpansionOutcome,
        target_path: list[str],
        identity_key: str,
        rule_id: str | None,
        winner_node: _GroupStatement,
        loser_node: _GroupStatement,
    ) -> None:
        """把一次不同值的语义覆盖追加到展开报告。

        冲突必须保留路径、语义身份、胜负来源和值，才能解释最终为何采用某个继承值；
        归一化后相同的重复配置不算冲突，避免重复声明产生无意义的告警。
        """
        if _normalized_command(winner_node.header) == _normalized_command(
            loser_node.header
        ):
            return
        conflict = {
            "vendor": self.vendor,
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
        target_node: _GroupStatement,
        inherited_nodes: list[_GroupStatement],
        group_name: str,
        precedence: tuple[int, int],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> None:
        """把一个 group 在当前路径的子节点合并到目标工作树节点。

        group 控制语句不会作为业务配置复制；块节点只在目标不存在且不是
        正则选择器时创建。叶子节点通过语义规则确定配置槽位，显式配置始终
        胜出，group 之间按优先级决定覆盖顺序。具体的块、叶子及歧义处理由
        各自方法负责，本方法只按定义顺序分派节点。
        """
        for inherited_node in inherited_nodes:
            # apply/exclude 是继承控制语句，不是需要合并的业务配置。
            if self._parse_group_names(
                inherited_node.header, "apply-group"
            ) or self._parse_group_names(
                inherited_node.header, "exclude-group"
            ):
                continue
            if inherited_node.is_block or inherited_node.statements:
                self._merge_inherited_block(
                    target_node,
                    inherited_node,
                    group_name,
                    precedence,
                )
                continue
            should_continue = self._merge_inherited_statement(
                target_node,
                inherited_node,
                group_name,
                precedence,
                target_path,
                outcome,
                washing_policy,
            )
            if not should_continue:
                return

    def _merge_inherited_block(
        self,
        target_node: _GroupStatement,
        inherited_node: _GroupStatement,
        group_name: str,
        precedence: tuple[int, int],
    ) -> None:
        """在目标中不存在匹配块时创建继承块。

        只创建具体 header 并继续递归 payload，可使后续子语句拥有真实路径；正则选择器
        不能直接物化为配置节点，因此只匹配已有块。
        """
        matching_block_exists = any(
            child.is_block
            and self._selector_matches(inherited_node.header, child.header)
            for child in target_node.statements
        )
        if matching_block_exists:
            return
        # 正则选择器只能应用到已有具体节点，不能把正则本身生成为配置块。
        if "'" in inherited_node.header or '"' in inherited_node.header:
            return
        target_node.statements.append(
            _GroupStatement(
                header=inherited_node.header,
                is_block=True,
                origin=f"group:{group_name}",
                rank=precedence,
                comment=inherited_node.comment,
            )
        )

    @staticmethod
    def _statement_decisions(target_node, target_path):
        """计算当前层显式叶子的语义 identity。

        覆盖、去重和歧义判断共享同一决策列表，避免不同步骤采用不一致的命令身份。
        """
        return [
            (child, resolve_cisco_identity(child.header, path=target_path))
            for child in target_node.statements
            if not child.is_block and not child.is_formatting
        ]

    @staticmethod
    def _find_matching_statement(decisions, key):
        """查找 identity 对应的首条显式语句。

        固定选择首条重复项可保持原有覆盖顺序，避免集合遍历导致结果漂移。
        """
        for statement, decision in decisions:
            if decision.key == key:
                return statement
        return None

    def _merge_inherited_statement(
        self,
        target_node: _GroupStatement,
        inherited_node: _GroupStatement,
        group_name: str,
        precedence: tuple[int, int],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> bool:
        """把一个继承叶子按语义 identity 合并到目标节点。

        先匹配已有槽位，再处理未知命令冲突；返回值表示展开是否还能继续。
        """
        identity_decision, inherited_statement = self._prepare_merge(
            inherited_node,
            group_name,
            precedence,
            target_path,
            outcome,
        )
        existing_statement_decisions = self._statement_decisions(
            target_node, target_path
        )
        existing_statement = self._find_matching_statement(
            existing_statement_decisions, identity_decision.key
        )
        if existing_statement is not None:
            self._merge_statement_by_precedence(
                existing_statement,
                inherited_statement,
                identity_decision,
                target_path,
                outcome,
            )
            return True

        if self._handle_unknown_identity_ambiguity(
            identity_decision,
            existing_statement_decisions,
            inherited_statement,
            target_path,
            outcome,
            washing_policy,
        ):
            return False

        target_node.statements.append(inherited_statement)
        return True

    @staticmethod
    def _prepare_merge(
        inherited_node: _GroupStatement,
        group_name: str,
        precedence: tuple[int, int],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> tuple[SemanticDecision, _GroupStatement]:
        """解析继承语句并构造带来源的独立副本。

        规则命中统计和副本创建在同一阶段完成，确保后续覆盖不会修改 group 定义本身。
        """
        identity_decision = resolve_cisco_identity(
            inherited_node.header,
            path=target_path,
        )
        if identity_decision.matched:
            outcome.identity_rule_hits += 1
        else:
            outcome.identity_fallbacks += 1
        inherited_statement = _GroupStatement(
            header=inherited_node.header,
            origin=f"group:{group_name}",
            rank=precedence,
            comment=inherited_node.comment,
        )
        return identity_decision, inherited_statement

    def _ambiguity_record(
        self,
        identity_decision,
        ambiguous_existing_node,
        inherited_statement,
        target_path,
    ):
        """构造未知命令族冲突的可审计记录。

        同时保留现有与候选语句的来源和值，使告警能够指出具体继承冲突。
        """
        command_family = (
            identity_decision.normalized.split(maxsplit=1)[0]
            if identity_decision.normalized
            else ""
        )
        ambiguity_record = {
            "vendor": self.vendor,
            "path": " / ".join(target_path) or "<root>",
            "family": command_family,
            "existing_source": ambiguous_existing_node.origin,
            "existing_value": ambiguous_existing_node.header,
            "candidate_source": inherited_statement.origin,
            "candidate_value": inherited_statement.header,
        }
        return ambiguity_record

    def _handle_unknown_identity_ambiguity(
        self,
        identity_decision: SemanticDecision,
        existing_statement_decisions: list[
            tuple[_GroupStatement, SemanticDecision]
        ],
        inherited_statement: _GroupStatement,
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> bool:
        """按策略处理未覆盖 identity 的潜在冲突。

        保留模式允许并存，告警模式记录风险，失败模式终止事务；统一决策避免静默选择错误值。
        """
        if (
            identity_decision.matched
            or washing_policy.group_unknown_identity
            == GroupIdentityMode.PRESERVE
        ):
            return False
        ambiguous_existing_node = find_opaque_ambiguity(
            identity_decision,
            existing_statement_decisions,
        )
        if ambiguous_existing_node is None:
            return False

        ambiguity_record = self._ambiguity_record(
            identity_decision,
            ambiguous_existing_node,
            inherited_statement,
            target_path,
        )
        command_family = ambiguity_record["family"]
        if ambiguity_record not in outcome.ambiguities:
            outcome.ambiguities.append(ambiguity_record)
            resolution_message = (
                "已中止本次 group 展开"
                if washing_policy.group_unknown_identity
                == GroupIdentityMode.FAIL
                else "已保留两个值"
            )
            outcome.warnings.append(
                f"IOS XR 路径 {ambiguity_record['path']} 下的未知命令族 "
                f"{command_family} 可能存在继承冲突，{resolution_message}"
            )
        if washing_policy.group_unknown_identity == GroupIdentityMode.FAIL:
            outcome.success = False
            return True
        return False

    def _merge_statement_by_precedence(
        self,
        existing_statement: _GroupStatement,
        inherited_statement: _GroupStatement,
        identity_decision: SemanticDecision,
        target_path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> None:
        """根据显式配置和 group 优先级合并同名叶子。

        显式配置始终优先，group 之间只在候选优先级更高时覆盖，并记录实际胜出方向。
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
        """编排 IOS XR group 的收集、展开、校验和事务式提交。

        所有活动引用都会被物化。返回值汇总事件、告警、冲突和规则命中数；
        只有完整展开成功才替换原文档，任何未解析引用都会整体回滚。
        """
        outcome = GroupExpansionOutcome()
        washing_policy = policy or WashingPolicy()

        if not self._has_applied_group():
            return outcome

        # 定义解析和工作树构建均不修改原根节点，便于任何失败直接回滚。
        group_definitions = self._load_group_definitions()
        working_root, terminal_commands = self._build_working_tree(
            known_interfaces
        )
        referenced_group_names = self._collect_root_group_names(working_root)
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

    def _has_applied_group(self) -> bool:
        """快速判断活动配置是否包含 apply-group。

        无引用时直接返回可避免克隆整棵 AST，同时字符串预筛不会漏掉实际引用。
        """
        return any(
            "apply-group" in node.header.lower()
            for top in self.top_level_nodes
            if top.active
            and not re.match(r"group\s+\S+", top.header.strip(), re.IGNORECASE)
            for node in top.walk(include_self=True)
        )

    def _load_group_definitions(
        self,
    ) -> dict[str, _GroupDefinition]:
        """收集并解析文档中的全部有效 group 定义。

        每个定义对象保存可合并的节点副本、运行时变量和嵌套 apply-group
        标记。这里只建立索引，不立即报错；
        只有 group 实际被选中应用时才触发事务回滚。
        """
        group_definitions: dict[str, _GroupDefinition] = {}
        for top_level_node in self.top_level_nodes:
            match = re.match(
                r"group\s+(\S+)\s*$",
                top_level_node.header.strip(),
                re.IGNORECASE,
            )
            if not match or not top_level_node.active:
                continue
            group_name = match.group(1)
            body_nodes = clone_statements(
                top_level_node.statements,
                origin=f"group:{group_name}",
            )
            group_definitions[group_name] = _GroupDefinition(
                body_nodes=body_nodes,
                has_runtime_variables=any(
                    "$" in node.header for node in top_level_node.walk()
                ),
                has_nested_apply=self._contains_nested_apply(body_nodes),
            )
        return group_definitions

    def _contains_nested_apply(self, nodes: list[_GroupStatement]) -> bool:
        """递归判断 group 定义是否包含嵌套 apply-group。

        当前展开器不支持嵌套引用，提前标记可在实际使用该定义时安全回滚。
        """
        return any(
            self._parse_group_names(node.header, "apply-group")
            or self._contains_nested_apply(node.statements)
            for node in nodes
        )

    def _build_working_tree(
        self,
        candidate_interface_names: Iterable[str],
    ) -> tuple[_GroupStatement, list[str]]:
        """复制非 group 配置，并补充用于 selector 匹配的接口节点。

        ``end`` 和 ``commit`` 单独保存，待展开完成后恢复到输出末尾。整个
        构建过程只修改临时根节点，不影响原文档。
        """
        working_root = _GroupStatement("<root>", is_block=True)
        terminal_commands: list[str] = []
        for source_node in self.top_level_nodes:
            header = source_node.header.strip()
            if (
                not source_node.active
                or (header in {"", "!"} and not source_node.comment)
                or header == "end-group"
                or re.match(r"group\s+\S+", header, re.IGNORECASE)
            ):
                continue
            if header.lower() in {"end", "commit"}:
                terminal_commands.append(header)
                continue
            working_root.statements.append(
                _GroupStatement(
                    source_node.header,
                    clone_statements(
                        source_node.statements, include_comments=True
                    ),
                    is_block=source_node.is_block,
                    comment=source_node.comment,
                )
            )

        # 配置中缺失的候选接口也要参与通配 group 匹配。
        existing_interface_names = {
            self._document._parser.interface_name(node.header)
            for node in working_root.statements
            if self._document._parser.interface_name(node.header)
        }
        for candidate_name in candidate_interface_names:
            canonical_name = self._document.resolve_interface(candidate_name)
            if canonical_name in existing_interface_names:
                continue
            working_root.statements.append(
                _GroupStatement(
                    f"interface {canonical_name}",
                    is_block=True,
                    origin="synthetic",
                )
            )
            existing_interface_names.add(canonical_name)
        return working_root, terminal_commands

    def _collect_root_group_names(
        self,
        root_node: _GroupStatement,
    ) -> set[str]:
        """递归收集工作树中出现的所有 apply-group 名称。

        去重集合作为依赖分析入口，使同一模板的多次引用只验证一次。
        """
        referenced_group_names: set[str] = set()
        for child in root_node.statements:
            referenced_group_names.update(
                self._parse_group_names(child.header, "apply-group")
            )
            if child.is_block:
                referenced_group_names.update(
                    self._collect_root_group_names(child)
                )
        return referenced_group_names

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
            self._merge_inherited_children(
                target_node,
                self._payload_nodes_at_path(
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
                for name in self._parse_group_names(
                    child.header,
                    "apply-group",
                )
                if name in referenced_group_names
            )
            excluded_group_names.update(
                name
                for name in self._parse_group_names(
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
                statement_group_names = self._parse_group_names(
                    child.header,
                    control_keyword,
                )
                if not statement_group_names:
                    continue
                rewritten_header = self._rewrite_group_control(
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


def from_context(context, origin="explicit"):
    """把 Cisco 解析上下文复制成 group 工作语句。

    保持 header、块类型、注释和来源，使后续合并可以操作独立副本而不污染正式 AST。
    """
    return _GroupStatement(
        context.header,
        [from_context(child, origin) for child in context.nodes],
        context.is_block,
        origin=origin,
        comment=context.comment,
    )


def clone_statements(
    nodes: Iterable[_GroupStatement],
    *,
    include_formatting: bool = False,
    include_comments: bool = False,
    origin: str | None = None,
) -> list[_GroupStatement]:
    """深拷贝节点列表，并可过滤格式节点或统一重设来源。

    文档保真渲染需要格式行，但 group 语义合并只需要命令节点；集中克隆可以
    在两个场景间安全转换，同时确保对工作树的修改不会污染持久化文档 AST。
    """
    result: list[_GroupStatement] = []
    for node in nodes:
        if (
            node.is_formatting
            and not include_formatting
            and not (include_comments and node.comment)
        ):
            continue
        clone = copy.copy(node)
        if origin is not None:
            clone.origin = origin
        clone.statements = clone_statements(
            node.statements,
            include_formatting=include_formatting,
            include_comments=include_comments,
            origin=origin,
        )
        result.append(clone)
    return result


def render_statements(
    nodes: Iterable[_GroupStatement], depth: int = 1
) -> list[str]:
    """把 group 工作语句渲染为带缩进的配置行。

    该渲染仅服务候选事务，保留格式和注释后再交给Editor 作为新片段解析。
    """
    lines = []
    for node in nodes:
        if node.is_formatting:
            lines.append(
                " " * depth + (node.comment or node.header)
                if node.header or node.comment
                else ""
            )
        else:
            line = " " * depth + node.header
            if node.comment:
                line += " " + node.comment
            lines.append(line)
            lines.extend(render_statements(node.statements, depth + 1))
    return lines


def _signature(statement):
    """生成工作语句的结构签名。

    签名忽略无语义格式节点但保留注释，用于判断展开结果是否真的改变正式配置。
    """
    return (
        statement.header,
        statement.comment,
        tuple(
            _signature(child)
            for child in statement.statements
            if not child.is_formatting or child.comment
        ),
    )


def publish_expansion(document, statements):
    """把展开结果中真正变化的块发布到正式文档。

    复用原上下文删除或替换并追加新块，可保留未受影响显式配置的原始格式。
    """
    from collections import defaultdict, deque

    candidates = defaultdict(deque)
    for statement in statements:
        if not statement.is_formatting:
            candidates[statement.header].append(statement)
    transaction = document.edit()
    consumed = set()
    for context in document.root.nodes:
        if context.is_formatting:
            continue
        choices = candidates[context.header]
        if not choices:
            transaction.delete(context, "移除已展开的 group 定义或控制语句")
            continue
        statement = choices.popleft()
        consumed.add(id(statement))
        if _signature(from_context(context)) != _signature(statement):
            text = "\n".join(render_statements([statement], 0)) + "\n"
            transaction.replace_subtree(context, text, "物化 group 继承配置")
    for statement in statements:
        if not statement.is_formatting and id(statement) not in consumed:
            text = "\n".join(render_statements([statement], 0)) + "\n"
            append_top_level(
                transaction, document, text, "新增 group 继承配置块"
            )
    transaction.apply()


def _rollback_expansion(outcome: GroupExpansionOutcome) -> None:
    """记录整体回滚并清除未提交的冲突明细。

    失败时统一重置成功标记，防止调用方把部分分析结果当成已发布配置。
    """
    outcome.events.append("IOS XR group 展开未完整解析，已整体回滚并保留原配置")
    outcome.conflicts.clear()
    outcome.success = False
