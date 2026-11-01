"""Junos Group 继承合并与冲突诊断。"""

from __future__ import annotations

from impl.common.domain.interface import (
    normalized_command as _normalized_command,
)
from impl.common.domain.outcomes import GroupExpansionOutcome
from impl.common.domain.policies import GroupIdentityMode, WashingPolicy
from impl.common.semantic_identity import (
    SemanticDecision,
    find_opaque_ambiguity,
)
from impl.common.support.errors import require_invariant

from ...document import (
    JuniperNode,
    JunosDocument,
)
from ...support.identity import resolve_junos_identity
from .models import _ResolvedStatement


class JunosGroupMerger:
    """提供 Junos Group 继承合并和冲突诊断。

    合并过程保持显式配置、嵌套层级和 Group 列表顺序定义的优先级。
    """

    __slots__ = ("_document", "_selectors")

    def __init__(self, document, selectors):
        """绑定文档和选择器助手。

        构造阶段不执行合并或修改配置。
        """
        self._document = document
        self._selectors = selectors

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
            self._selectors._parse_group_names(
                inherited_node.header, "apply-groups"
            )
            or self._selectors._parse_group_names(
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
            if not child.effective or not self._selectors._selector_matches(
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
            self._selectors._base_header(inherited_node.header),
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
                    self._selectors._base_header(item.header),
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
