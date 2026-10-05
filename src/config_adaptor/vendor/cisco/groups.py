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
from ..identity import find_opaque_ambiguity
from .identity import resolve_cisco_identity


@dataclass(slots=True)
class _CiscoGroupDefinition:
    """一个 IOS XR group 定义及其静态分析结果。"""

    body: list[CiscoNode]
    has_runtime_variables: bool
    has_nested_apply: bool


@dataclass(slots=True)
class _CiscoExpansionState:
    """一次 group 展开中的共享状态，避免递归方法参数膨胀。"""

    definitions: dict[str, _CiscoGroupDefinition]
    selected_groups: set[str]
    outcome: GroupExpansionOutcome
    policy: WashingPolicy
    applied_groups: set[str] = field(default_factory=set)
    unresolved: bool = False


class CiscoGroupExpander:
    """事务式展开 IOS XR group，并维护继承优先级与冲突规则。"""

    def __init__(self, document):
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
    def top_level_nodes(self, value: list[CiscoNode]) -> None:
        """以展开完成的新节点列表原子替换底层文档内容。"""
        self._document.root.children = value

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
    def _parse_cisco_nodes(lines: Iterable[str], origin: str = "explicit") -> list[CiscoNode]:
        """使用文档解析器把配置行转换为父子节点树。

        Group 工作树与主文档必须共享同一缩进规则；委托统一入口可避免
        两套解析逻辑在多级 ``!`` 或混合缩进时得到不同的树。
        """
        return CiscoDocument._parse_cisco_nodes(lines, origin)

    @staticmethod
    def _render_cisco_nodes(nodes: list[CiscoNode], depth: int = 1) -> list[str]:
        """递归渲染一组节点，并保持节点当前的顺序。

        委托文档的统一渲染器，使 group 展开后的缩进和格式节点处理与
        普通配置完全一致。
        """
        return _render_cisco_nodes(nodes, depth)

    @staticmethod
    def _cisco_group_names(command: str, keyword: str) -> list[str]:
        """解析 group 控制语句中按声明顺序出现的名称。

        同时接受关键字单复数、单个名称、方括号列表及带引号名称；命令不
        匹配时返回空列表，供调用方把它当作普通配置处理。
        """
        match = re.match(rf"{re.escape(keyword)}s?\s+(.+?)\s*$", command.strip(), re.IGNORECASE)
        if not match:
            return []
        return [token.strip("'\"") for token in match.group(1).strip().strip("[]").split() if token]

    @staticmethod
    def _rewrite_cisco_group_control(command: str, keyword: str, remaining: list[str]) -> str | None:
        """使用剩余名称重写一条 apply/exclude-group 控制语句。

        保留原关键字形式和方括号列表风格；``remaining`` 为空表示整条控制
        语句已无意义，此时返回 ``None``。不匹配指定关键字时原样返回。
        """
        match = re.match(
            rf"({re.escape(keyword)}s?)\s+(.+?)\s*$",
            command.strip(),
            re.IGNORECASE,
        )
        if not match:
            return command
        if not remaining:
            return None
        original_value = match.group(2).strip()
        value = " ".join(remaining)
        if original_value.startswith("[") and original_value.endswith("]"):
            value = f"[ {value} ]"
        return f"{match.group(1)} {value}"

    @staticmethod
    def _cisco_pattern_match(pattern: str, target: str) -> bool:
        """判断 group 路径选择器是否匹配目标配置节点。

        无引号时执行忽略大小写的精确匹配，其中接口名先展开缩写再比较；
        引号内文本按正则表达式处理，其余部分按字面量处理。非法正则返回
        ``False``，不会中断整个展开流程。
        """
        pattern = " ".join(pattern.strip().split())
        target = " ".join(target.strip().split())
        if "'" not in pattern and '"' not in pattern:
            if pattern.lower().startswith("interface ") and target.lower().startswith("interface "):
                return canonical_cisco_interface(pattern.split(maxsplit=1)[1]) == canonical_cisco_interface(
                    target.split(maxsplit=1)[1]
                )
            return pattern.lower() == target.lower()
        pieces: list[str] = []
        cursor = 0
        for match in re.finditer(r"(['\"])(.*?)\1", pattern):
            pieces.append(re.escape(pattern[cursor : match.start()]).replace(r"\ ", r"\s+"))
            pieces.append(f"(?:{match.group(2)})")
            cursor = match.end()
        pieces.append(re.escape(pattern[cursor:]).replace(r"\ ", r"\s+"))
        try:
            return bool(re.fullmatch("".join(pieces), target, re.IGNORECASE))
        except re.error:
            return False

    @staticmethod
    def _selector_specificity(command: str) -> tuple[int, str]:
        """计算 IOS XR 选择器的确定性排序键。

        第一项是去掉正则元字符后的字面量长度，越长越具体；第二项使用原
        命令稳定打破并列，确保相同输入始终选中同一候选。
        """
        literal = re.sub(r"(['\"])(.*?)\1", lambda item: re.sub(r"[.*+?\[\](){}|\\]", "", item.group(2)), command)
        return (len(literal), command)

    def _resolve_group_payload(
        self,
        group_body: list[CiscoNode],
        path: list[str],
    ) -> list[CiscoNode]:
        """定位某个 group 在目标配置路径上提供的直接子配置。

        路径每深入一级，都选择所有匹配项并优先处理更具体的选择器，再把
        它们的子节点作为下一层候选；任一级无匹配时返回空列表。
        """
        candidates = group_body
        for component in path:
            matches = [node for node in candidates if self._cisco_pattern_match(node.header, component)]
            if not matches:
                return []
            matches.sort(key=lambda node: (-self._selector_specificity(node.header)[0], node.header))
            candidates = [child for match in matches for child in match.children]
        return candidates

    @staticmethod
    def _record_group_conflict(
        outcome: GroupExpansionOutcome,
        vendor: str,
        path: list[str],
        identity: str,
        rule_id: str | None,
        winner: CiscoNode,
        loser_command: str,
        loser_origin: str,
    ) -> None:
        """把一次不同值的语义覆盖追加到展开报告。

        记录路径、semantic identity、胜负来源和值，并在可用时附带规则 ID；
        归一化后内容相同的重复配置不算冲突。本方法不改工作树。
        """
        if _normalized_command(winner.header) == _normalized_command(loser_command):
            return
        conflict = {
            "vendor": vendor,
            "path": " / ".join(path) or "<root>",
            "key": identity,
            "winner_source": winner.origin,
            "winner_value": winner.header,
            "loser_source": loser_origin,
            "loser_value": loser_command,
        }
        if rule_id is not None:
            conflict["rule_id"] = rule_id
        outcome.conflicts.append(conflict)

    def _merge_group_payload(
        self,
        target_node: CiscoNode,
        payload_nodes: list[CiscoNode],
        group_name: str,
        rank: tuple[int, int],
        path: list[str],
        outcome: GroupExpansionOutcome,
        policy: WashingPolicy,
    ) -> None:
        """把一个 group 在当前路径的子节点合并到目标工作树节点。

        group 控制语句不会作为业务配置复制；块节点只在目标不存在且不是
        正则选择器时创建。叶子节点通过语义规则确定配置槽位，显式配置始终
        胜出，group 之间按 ``rank`` 决定覆盖顺序，同时更新冲突、规则命中、
        fallback 和未知语义歧义报告；fail 策略会把 outcome 标为失败。
        """
        for payload_node in payload_nodes:
            # apply/exclude 是继承控制语句，不是需要合并的业务配置。
            if self._cisco_group_names(
                payload_node.header, "apply-group"
            ) or self._cisco_group_names(
                payload_node.header, "exclude-group"
            ):
                continue
            if payload_node.is_block or payload_node.children:
                matching_nodes = [
                    item
                    for item in target_node.children
                    if item.is_block
                    and self._cisco_pattern_match(payload_node.header, item.header)
                ]
                if matching_nodes:
                    continue
                # 正则选择器只能应用到已有具体节点，不能把正则本身生成为配置块。
                if "'" in payload_node.header or '"' in payload_node.header:
                    continue
                target_node.children.append(
                    CiscoNode(
                        header=payload_node.header,
                        is_block=True,
                        origin=f"group:{group_name}",
                        rank=rank,
                    )
                )
                continue

            decision = resolve_cisco_identity(payload_node.header, path=path)
            if decision.matched:
                outcome.identity_rule_hits += 1
            else:
                outcome.identity_fallbacks += 1
            identity = decision.key
            resolved_children = [
                (item, resolve_cisco_identity(item.header, path=path))
                for item in target_node.children
                if not item.is_block
            ]
            existing_node = next(
                (
                    item
                    for item, item_decision in resolved_children
                    if item_decision.key == identity
                ),
                None,
            )
            if existing_node is None:
                if not decision.matched and policy.group_unknown_identity != "preserve":
                    family = decision.normalized.split(maxsplit=1)[0] if decision.normalized else ""
                    ambiguous = find_opaque_ambiguity(decision, resolved_children)
                    if ambiguous is not None:
                        detail = {
                            "vendor": self.vendor,
                            "path": " / ".join(path) or "<root>",
                            "family": family,
                            "existing_source": ambiguous.origin,
                            "existing_value": ambiguous.header,
                            "candidate_source": f"group:{group_name}",
                            "candidate_value": payload_node.header,
                        }
                        if detail not in outcome.ambiguities:
                            outcome.ambiguities.append(detail)
                            action = (
                                "已中止本次 group 展开"
                                if policy.group_unknown_identity == "fail"
                                else "已保留两个值"
                            )
                            outcome.warnings.append(
                                f"IOS XR 路径 {detail['path']} 下的未知命令族 {family} "
                                f"可能存在继承冲突，{action}"
                            )
                        if policy.group_unknown_identity == "fail":
                            outcome.success = False
                            return
                target_node.children.append(
                    CiscoNode(
                        header=payload_node.header,
                        origin=f"group:{group_name}",
                        rank=rank,
                    )
                )
                continue
            payload_origin = f"group:{group_name}"
            # 显式配置永远优先；group 之间则由层级深度和引用顺序 rank 决定。
            if existing_node.origin != "explicit" and rank > existing_node.rank:
                self._record_group_conflict(
                    outcome,
                    self.vendor,
                    path,
                    identity,
                    decision.rule_id,
                    CiscoNode(
                        payload_node.header,
                        origin=payload_origin,
                        rank=rank,
                    ),
                    existing_node.header,
                    existing_node.origin,
                )
                existing_node.header = payload_node.header
                existing_node.origin = payload_origin
                existing_node.rank = rank
            else:
                self._record_group_conflict(
                    outcome,
                    self.vendor,
                    path,
                    identity,
                    decision.rule_id,
                    existing_node,
                    payload_node.header,
                    payload_origin,
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
        policy = policy or WashingPolicy()

        # 定义解析和工作树构建均不修改原根节点，便于任何失败直接回滚。
        group_definitions = self._load_group_definitions()
        working_root, terminal_commands = self._build_working_tree(known_interfaces)
        selected_groups = self._select_groups(
            working_root,
        )
        if not selected_groups:
            return outcome

        # 递归期间会共享选中集、已应用集和错误状态，避免每层传递大量参数。
        state = _CiscoExpansionState(
            definitions=group_definitions,
            selected_groups=selected_groups,
            outcome=outcome,
            policy=policy,
        )
        self._expand_node(working_root, [], [], state)
        if state.unresolved or not outcome.success:
            outcome.events.append(
                "IOS XR group 展开未完整解析，已整体回滚并保留原配置"
            )
            outcome.conflicts.clear()
            outcome.success = False
            return outcome
        if not state.applied_groups:
            return outcome

        # 只有完整展开成功才替换根节点的 children，这里是事务式提交点。
        self.top_level_nodes = self._rebuild_top_level_nodes(
            working_root,
            terminal_commands,
        )
        outcome.events.extend(
            f"已展开 IOS XR 配置组 {name}"
            for name in sorted(state.applied_groups)
        )
        return outcome

    def _load_group_definitions(
        self,
    ) -> dict[str, _CiscoGroupDefinition]:
        """收集并解析文档中的全部有效 group 定义。

        每个定义对象集中保存原始定义节点、可合并的 body 副本以及
        运行时变量和嵌套 apply-group 标记。这里只建立索引，不立即报错；
        只有 group 实际被选中应用时才触发事务回滚。
        """
        definition_nodes: dict[str, CiscoNode] = {}
        for node in self.top_level_nodes:
            match = re.match(
                r"group\s+(\S+)\s*$",
                node.header.strip(),
                re.IGNORECASE,
            )
            if match and node.active:
                definition_nodes[match.group(1)] = node

        def contains_nested_apply(nodes: list[CiscoNode]) -> bool:
            return any(
                self._cisco_group_names(node.header, "apply-group")
                or contains_nested_apply(node.children)
                for node in nodes
            )

        definitions: dict[str, _CiscoGroupDefinition] = {}
        for group_name, definition_node in definition_nodes.items():
            body = _clone_cisco_nodes(
                definition_node.children,
                origin=f"group:{group_name}",
            )
            definitions[group_name] = _CiscoGroupDefinition(
                body=body,
                has_runtime_variables=any(
                    "$" in node.header for node in definition_node.walk()
                ),
                has_nested_apply=contains_nested_apply(body),
            )
        return definitions

    def _build_working_tree(
        self,
        known_interfaces: Iterable[str],
    ) -> tuple[CiscoNode, list[str]]:
        """从非 group 配置构建工作树，并补充拓扑中已知的接口节点。

        返回临时根节点和需在输出末尾恢复的 ``end``/``commit`` 命令；原始
        文档虚拟根节点不会在此阶段被修改。
        """
        working_root = CiscoNode("<root>", is_block=True)
        terminal_commands: list[str] = []
        for node in self.top_level_nodes:
            header = node.header.strip()
            if not node.active or header in {"", "!", "end-group"} or re.match(
                r"group\s+\S+",
                header,
                re.IGNORECASE,
            ):
                continue
            if header.lower() in {"end", "commit"}:
                terminal_commands.append(header)
                continue
            working_root.children.append(
                CiscoNode(
                    node.header,
                    _clone_cisco_nodes(node.children),
                    is_block=node.is_block,
                )
            )

        # 拓扑中出现但配置未声明的接口也需参与通配 group 匹配。
        existing_interfaces = {
            canonical_cisco_interface(node.header.split(maxsplit=1)[1])
            for node in working_root.children
            if re.match(r"interface\s+\S+", node.header, re.IGNORECASE)
        }
        for raw_name in known_interfaces:
            name = canonical_cisco_interface(raw_name)
            if name in existing_interfaces:
                continue
            working_root.children.append(
                CiscoNode(
                    f"interface {name}",
                    is_block=True,
                    origin="synthetic",
                )
            )
            existing_interfaces.add(name)
        return working_root, terminal_commands

    def _select_groups(
        self,
        working_root: CiscoNode,
    ) -> set[str]:
        """从工作树收集全部活动的顶层 group 引用。"""
        selected_groups: set[str] = set()

        def walk(node: CiscoNode) -> None:
            for child in node.children:
                for name in self._cisco_group_names(child.header, "apply-group"):
                    selected_groups.add(name)
                if child.is_block:
                    walk(child)

        walk(working_root)
        return selected_groups

    def _expand_node(
        self,
        node: CiscoNode,
        path: list[str],
        inherited_groups: list[tuple[str, tuple[int, int]]],
        state: _CiscoExpansionState,
    ) -> None:
        """在当前节点应用有效 group，并递归处理其所有配置块。

        方法先合并当前路径对应的 group payload，再移除已消费的控制语句；
        合并可能追加新块，因此使用索引循环确保新物化节点也继续展开。
        ``state`` 会累计已应用 group、冲突和无法解析状态。
        """
        active_groups = self._resolve_active_groups(
            node,
            path,
            inherited_groups,
            state,
        )
        for group_name, rank in active_groups:
            definition = state.definitions.get(group_name)
            if definition is None:
                continue
            self._merge_group_payload(
                node,
                self._resolve_group_payload(definition.body, path),
                group_name,
                rank,
                path,
                state.outcome,
                state.policy,
            )

        self._rewrite_group_controls(node, state.selected_groups)
        index = 0
        while index < len(node.children):
            child = node.children[index]
            if child.is_block:
                self._expand_node(
                    child,
                    [*path, child.header],
                    active_groups,
                    state,
                )
            index += 1

    def _resolve_active_groups(
        self,
        node: CiscoNode,
        path: list[str],
        inherited_groups: list[tuple[str, tuple[int, int]]],
        state: _CiscoExpansionState,
    ) -> list[tuple[str, tuple[int, int]]]:
        """计算当前配置层级真正生效的 group 及其优先级。

        本层引用优先于父层继承，路径越深优先级越高，同一列表中越靠前越
        优先；exclude 同时屏蔽本地和继承引用。实际引用若缺失、包含运行时
        变量或嵌套引用，会记录告警并将共享状态标记为不可提交。
        """
        local_group_names: list[str] = []
        excluded_group_names: set[str] = set()
        for child in node.children:
            local_group_names.extend(
                name
                for name in self._cisco_group_names(child.header, "apply-group")
                if name in state.selected_groups
            )
            excluded_group_names.update(
                name
                for name in self._cisco_group_names(child.header, "exclude-group")
                if name in state.selected_groups
            )

        state.applied_groups.update(local_group_names)
        # 只校验从活动配置实际引用的 group；未使用定义不影响转换。
        for name in local_group_names:
            definition = state.definitions.get(name)
            if definition is None:
                state.outcome.warnings.append(
                    f"IOS XR apply-group 引用了未定义的组 {name}"
                )
                state.unresolved = True
            elif definition.has_runtime_variables:
                state.outcome.warnings.append(
                    f"IOS XR 组 {name} 包含运行时变量，无法安全静态展开"
                )
                state.unresolved = True
            elif definition.has_nested_apply:
                state.outcome.warnings.append(
                    f"IOS XR 组 {name} 内再次引用 apply-group，无法安全静态展开"
                )
                state.unresolved = True

        local_groups = [
            (name, (len(path), -index))
            for index, name in enumerate(local_group_names)
        ]
        # 路径越深越具体，优先级越高；同层列表中越靠前越优先。
        # 本层 exclude 同时抑制本地引用和从父层继承下来的同名 group。
        active_groups: list[tuple[str, tuple[int, int]]] = []
        for group_ref in [*local_groups, *inherited_groups]:
            if group_ref[0] in excluded_group_names or any(
                existing_group[0] == group_ref[0]
                for existing_group in active_groups
            ):
                continue
            active_groups.append(group_ref)
        return active_groups

    def _rewrite_group_controls(
        self,
        node: CiscoNode,
        selected_groups: set[str],
    ) -> None:
        """从当前节点的控制语句中移除本次已经物化的 group 名称。

        一条列表语句中未选中的名称会按原格式保留；全部名称均已展开时删除
        整条语句。apply-group 和 exclude-group 使用相同处理规则。
        """
        retained_children: list[CiscoNode] = []
        for child in node.children:
            rewritten: str | None = child.header
            for keyword in ("apply-group", "exclude-group"):
                names = self._cisco_group_names(child.header, keyword)
                if not names:
                    continue
                rewritten = self._rewrite_cisco_group_control(
                    child.header,
                    keyword,
                    # 一条语句可同时引用多个 group，因此只摘掉本次已展开的名称。
                    [
                        name
                        for name in names
                        if name not in selected_groups
                    ],
                )
                break
            if rewritten is not None:
                child.header = rewritten
                retained_children.append(child)
        node.children = retained_children

    def _rebuild_top_level_nodes(
        self,
        working_root: CiscoNode,
        terminal_commands: list[str],
    ) -> list[CiscoNode]:
        """把展开后的工作树和终止命令重新组装为 IOS XR 顶层节点。"""
        rebuilt_nodes: list[CiscoNode] = []
        for node in working_root.children:
            rebuilt_nodes.append(
                CiscoNode(
                    header=node.header,
                    children=_clone_cisco_nodes(node.children),
                )
            )
            rebuilt_nodes.append(CiscoNode(header="!"))
        for command in terminal_commands or ["end"]:
            rebuilt_nodes.append(CiscoNode(header=command))
        return rebuilt_nodes
