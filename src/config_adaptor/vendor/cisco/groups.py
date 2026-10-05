"""Cisco IOS XR group 展开算法。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ...models import WashingPolicy
from ...parsers.cisco_iosxr import (
    CiscoDocument,
    CiscoNode,
    _clone_cisco_nodes,
    _render_cisco_nodes,
    canonical_cisco_interface,
)
from ...parsers.common import GroupExpansionOutcome, normalized_command as _normalized_command
from ..identity import SemanticDecision, find_opaque_ambiguity
from .identity import resolve_cisco_identity


@dataclass(slots=True)
class _GroupDefinition:
    """一个 IOS XR group 定义及其静态分析结果。"""

    body_nodes: list[CiscoNode]
    has_runtime_variables: bool
    has_nested_apply: bool


@dataclass(frozen=True, slots=True)
class _GroupApplication:
    """一个已解析的 group 应用及其继承优先级。"""

    name: str
    precedence: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _GroupControls:
    """一个配置层级中声明的 group 应用和排除控制。"""

    applied_group_names: tuple[str, ...]
    excluded_group_names: frozenset[str]


@dataclass(slots=True)
class _ExpansionState:
    """一次 group 展开中的共享状态，避免递归方法参数膨胀。"""

    group_definitions: dict[str, _GroupDefinition]
    referenced_group_names: set[str]
    outcome: GroupExpansionOutcome
    washing_policy: WashingPolicy
    encountered_group_names: set[str] = field(default_factory=set)
    has_unresolvable_reference: bool = False


class CiscoGroupExpander:
    """事务式展开 IOS XR group，并维护继承优先级与冲突规则。"""

    def __init__(self, document: CiscoDocument):
        """绑定待处理的 IOS XR 文档；构造阶段不解析或修改配置。"""
        self._document = document

    @property
    def vendor(self) -> str:
        """返回底层文档的厂商标识，供冲突和审计记录使用。"""
        return self._document.vendor

    @property
    def top_level_nodes(self) -> list[CiscoNode]:
        """读取底层文档虚拟根节点下的 IOS XR 顶层节点。"""
        return self._document.root.children

    @top_level_nodes.setter
    def top_level_nodes(self, nodes: list[CiscoNode]) -> None:
        """以展开完成的新节点列表原子替换底层文档内容。"""
        self._document.root.children = nodes

    @staticmethod
    def parse_nodes(lines: Iterable[str], origin: str = "explicit") -> list[CiscoNode]:
        """把 IOS XR 块内文本解析为可供 group 合并的语法树。

        ``origin`` 会写入每个节点，用来区分显式配置和继承配置；此方法是
        厂商文档层复用的公开入口，不会修改传入文档。
        """
        return CiscoDocument._parse_cisco_nodes(lines, origin)

    @staticmethod
    def render_nodes(nodes: list[CiscoNode], depth: int = 1) -> list[str]:
        """将语法树递归渲染为 IOS XR 缩进文本。

        ``depth`` 表示顶层节点的空格数，子节点每深入一级增加一个空格；
        返回新字符串列表，不修改节点本身。
        """
        return _render_cisco_nodes(nodes, depth)

    @staticmethod
    def _parse_group_names(command: str, control_keyword: str) -> list[str]:
        """解析 group 控制语句中按声明顺序出现的名称。

        同时接受关键字单复数、单个名称、方括号列表及带引号名称；命令不
        匹配时返回空列表，供调用方把它当作普通配置处理。
        """
        match = re.match(
            rf"{re.escape(control_keyword)}s?\s+(.+?)\s*$",
            command.strip(),
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
            lambda item: re.sub(
                r"[.*+?\[\](){}|\\]", "", item.group(2)
            ),
            selector_header,
        )
        return len(literal)

    def _payload_nodes_at_path(
        self,
        definition_nodes: list[CiscoNode],
        target_path: list[str],
    ) -> list[CiscoNode]:
        """定位某个 group 在目标配置路径上提供的直接子配置。

        路径每深入一级，都选择所有匹配项并优先处理更具体的选择器，再把
        它们的子节点作为下一层候选；任一级无匹配时返回空列表。
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
                child for match in matches for child in match.children
            ]
        return candidate_nodes

    def _record_group_conflict(
        self,
        outcome: GroupExpansionOutcome,
        target_path: list[str],
        identity_key: str,
        rule_id: str | None,
        winner_node: CiscoNode,
        loser_node: CiscoNode,
    ) -> None:
        """把一次不同值的语义覆盖追加到展开报告。

        记录路径、semantic identity、胜负来源和值，并在可用时附带规则 ID；
        归一化后内容相同的重复配置不算冲突。本方法不改工作树。
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
        target_node: CiscoNode,
        inherited_nodes: list[CiscoNode],
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
            if inherited_node.is_block or inherited_node.children:
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
        target_node: CiscoNode,
        inherited_node: CiscoNode,
        group_name: str,
        precedence: tuple[int, int],
    ) -> None:
        """在目标中不存在匹配块时创建一个可继续递归展开的继承块。"""
        matching_block_exists = any(
            child.is_block
            and self._selector_matches(inherited_node.header, child.header)
            for child in target_node.children
        )
        if matching_block_exists:
            return
        # 正则选择器只能应用到已有具体节点，不能把正则本身生成为配置块。
        if "'" in inherited_node.header or '"' in inherited_node.header:
            return
        target_node.children.append(
            CiscoNode(
                header=inherited_node.header,
                is_block=True,
                origin=f"group:{group_name}",
                rank=precedence,
            )
        )

    def _merge_inherited_statement(
        self,
        target_node: CiscoNode,
        inherited_node: CiscoNode,
        group_name: str,
        precedence: tuple[int, int],
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> bool:
        """按 semantic identity 将一个继承叶子合并到目标节点。"""
        identity_decision = resolve_cisco_identity(
            inherited_node.header,
            path=target_path,
        )
        if identity_decision.matched:
            outcome.identity_rule_hits += 1
        else:
            outcome.identity_fallbacks += 1

        inherited_statement = CiscoNode(
            header=inherited_node.header,
            origin=f"group:{group_name}",
            rank=precedence,
        )
        existing_statement_decisions = [
            (
                child,
                resolve_cisco_identity(child.header, path=target_path),
            )
            for child in target_node.children
            if not child.is_block
        ]
        existing_statement = next(
            (
                child
                for child, child_decision in existing_statement_decisions
                if child_decision.key == identity_decision.key
            ),
            None,
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

        target_node.children.append(inherited_statement)
        return True

    def _handle_unknown_identity_ambiguity(
        self,
        identity_decision: SemanticDecision,
        existing_statement_decisions: list[
            tuple[CiscoNode, SemanticDecision]
        ],
        inherited_statement: CiscoNode,
        target_path: list[str],
        outcome: GroupExpansionOutcome,
        washing_policy: WashingPolicy,
    ) -> bool:
        """记录未知命令族歧义，并返回本次展开是否必须中止。"""
        if (
            identity_decision.matched
            or washing_policy.group_unknown_identity == "preserve"
        ):
            return False
        ambiguous_existing_node = find_opaque_ambiguity(
            identity_decision,
            existing_statement_decisions,
        )
        if ambiguous_existing_node is None:
            return False

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
        if ambiguity_record not in outcome.ambiguities:
            outcome.ambiguities.append(ambiguity_record)
            resolution_message = (
                "已中止本次 group 展开"
                if washing_policy.group_unknown_identity == "fail"
                else "已保留两个值"
            )
            outcome.warnings.append(
                f"IOS XR 路径 {ambiguity_record['path']} 下的未知命令族 "
                f"{command_family} 可能存在继承冲突，{resolution_message}"
            )
        if washing_policy.group_unknown_identity == "fail":
            outcome.success = False
            return True
        return False

    def _merge_statement_by_precedence(
        self,
        existing_statement: CiscoNode,
        inherited_statement: CiscoNode,
        identity_decision: SemanticDecision,
        target_path: list[str],
        outcome: GroupExpansionOutcome,
    ) -> None:
        """按显式配置和 group 优先级决定叶子覆盖方向并记录冲突。"""
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
            outcome.events.append(
                "IOS XR group 展开未完整解析，已整体回滚并保留原配置"
            )
            outcome.conflicts.clear()
            outcome.success = False
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
            body_nodes = _clone_cisco_nodes(
                top_level_node.children,
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

    def _contains_nested_apply(self, nodes: list[CiscoNode]) -> bool:
        """判断节点树中是否存在嵌套的 apply-group 控制语句。"""
        return any(
            self._parse_group_names(node.header, "apply-group")
            or self._contains_nested_apply(node.children)
            for node in nodes
        )

    def _build_working_tree(
        self,
        candidate_interface_names: Iterable[str],
    ) -> tuple[CiscoNode, list[str]]:
        """复制非 group 配置，并补充用于 selector 匹配的接口节点。

        ``end`` 和 ``commit`` 单独保存，待展开完成后恢复到输出末尾。整个
        构建过程只修改临时根节点，不影响原文档。
        """
        working_root = CiscoNode("<root>", is_block=True)
        terminal_commands: list[str] = []
        for source_node in self.top_level_nodes:
            header = source_node.header.strip()
            if (
                not source_node.active
                or header in {"", "!", "end-group"}
                or re.match(r"group\s+\S+", header, re.IGNORECASE)
            ):
                continue
            if header.lower() in {"end", "commit"}:
                terminal_commands.append(header)
                continue
            working_root.children.append(
                CiscoNode(
                    source_node.header,
                    _clone_cisco_nodes(source_node.children),
                    is_block=source_node.is_block,
                )
            )

        # 配置中缺失的候选接口也要参与通配 group 匹配。
        existing_interface_names = {
            canonical_cisco_interface(node.header.split(maxsplit=1)[1])
            for node in working_root.children
            if re.match(r"interface\s+\S+", node.header, re.IGNORECASE)
        }
        for candidate_name in candidate_interface_names:
            canonical_name = canonical_cisco_interface(candidate_name)
            if canonical_name in existing_interface_names:
                continue
            working_root.children.append(
                CiscoNode(
                    f"interface {canonical_name}",
                    is_block=True,
                    origin="synthetic",
                )
            )
            existing_interface_names.add(canonical_name)
        return working_root, terminal_commands

    def _collect_root_group_names(
        self,
        root_node: CiscoNode,
    ) -> set[str]:
        """递归收集工作树中的全部 apply-group 引用名称。"""
        referenced_group_names: set[str] = set()
        for child in root_node.children:
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
        target_node: CiscoNode,
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
        while index < len(target_node.children):
            child = target_node.children[index]
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
        target_node: CiscoNode,
        target_path: list[str],
        applications: list[_GroupApplication],
        state: _ExpansionState,
    ) -> None:
        """把当前路径上生效的 group payload 依次合并到目标节点。"""
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
        target_node: CiscoNode,
        referenced_group_names: set[str],
    ) -> _GroupControls:
        """读取当前层级中属于本次展开范围的 apply/exclude 控制。"""
        applied_group_names: list[str] = []
        excluded_group_names: set[str] = set()
        for child in target_node.children:
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
        """校验实际 apply 的 group 是否可以安全静态展开。"""
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
        """按路径深度、列表顺序和 exclude 计算当前有效 group 应用。"""
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
        target_node: CiscoNode,
        handled_group_names: set[str],
    ) -> None:
        """从当前节点的控制语句中移除本次已经物化的 group 名称。

        一条列表语句中未处理的名称会按原格式保留；全部名称均已处理时删除
        整条语句。apply-group 和 exclude-group 使用相同处理规则。
        """
        retained_children: list[CiscoNode] = []
        for child in target_node.children:
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
        target_node.children = retained_children

    @staticmethod
    def _rebuild_top_level_nodes(
        working_root: CiscoNode,
        terminal_commands: list[str],
    ) -> list[CiscoNode]:
        """把展开后的工作树和终止命令重新组装为 IOS XR 顶层节点。"""
        rebuilt_nodes: list[CiscoNode] = []
        for top_level_node in working_root.children:
            rebuilt_nodes.append(
                CiscoNode(
                    header=top_level_node.header,
                    children=_clone_cisco_nodes(top_level_node.children),
                )
            )
            rebuilt_nodes.append(CiscoNode(header="!"))
        for command in terminal_commands or ["end"]:
            rebuilt_nodes.append(CiscoNode(header=command))
        return rebuilt_nodes
