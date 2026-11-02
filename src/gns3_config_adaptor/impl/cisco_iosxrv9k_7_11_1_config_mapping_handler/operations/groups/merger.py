"""Cisco Group 继承合并与冲突诊断。"""

from __future__ import annotations

from ....common.domain.interface import (
    normalized_command as _normalized_command,
)
from ....common.domain.outcomes import GroupExpansionOutcome
from ....common.domain.policies import GroupIdentityMode, WashingPolicy
from ....common.semantic_identity import (
    SemanticDecision,
    find_opaque_ambiguity,
)
from ...support.identity import resolve_cisco_identity
from .models import _GroupStatement


class CiscoGroupMerger:
    """提供 Cisco Group 继承合并和冲突诊断方法。

    合并过程保持显式配置、继承层级和同层声明顺序定义的优先级。
    """

    __slots__ = ("_document", "_selectors")

    def __init__(self, document, selectors):
        """绑定文档和选择器助手。

        构造阶段不执行合并或修改配置。
        """
        self._document = document
        self._selectors = selectors

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
            "vendor": self._document.vendor,
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
            if self._selectors._parse_group_names(
                inherited_node.header, "apply-group"
            ) or self._selectors._parse_group_names(
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
            and self._selectors._selector_matches(
                inherited_node.header, child.header
            )
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
            "vendor": self._document.vendor,
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
