"""Junos Group 定义、依赖、exclusion 和工作树准备。"""

from __future__ import annotations

import copy
from collections.abc import Iterable

from impl.common.domain.interface import (
    interface_parent,
)
from impl.common.domain.outcomes import GroupExpansionOutcome
from impl.common.domain.policies import WashingPolicy
from impl.common.support.errors import require_invariant

from ...syntax.ast import JuniperNode
from ...syntax.header_parser import canonical_junos_interface
from .models import _ExpansionState, _PreparedExpansion


class JunosGroupDefinitions:
    """提供 Junos Group 定义、依赖、排除和工作树准备。

    方法负责构建一次展开使用的定义索引，不长期保存文档或节点。
    """

    __slots__ = ("_document", "_selectors")

    def __init__(self, document, selectors):
        """绑定文档和选择器助手。

        构造阶段不复制配置树或建立 Group 索引。
        """
        self._document = document
        self._selectors = selectors

    def _iter_source_nodes(self):
        """深度优先迭代原配置中的全部节点。

        该入口只用于 group 文本预筛，不会复制或修改配置树。
        """
        stack = [self._document.root]
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(node.children)

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
        return self._resolve_dependency_closure(root_group_names, state)

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
        working_root = copy.deepcopy(self._document.root)
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
                name = self._selectors._group_definition_name(definition.header)
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
            names = self._selectors._parse_group_names(node.header, keyword)
            removed = excluded_names.intersection(names)
            if not removed:
                continue
            rewritten = self._selectors._rewrite_group_control(
                node.header,
                keyword,
                [name for name in names if name not in excluded_names],
            )
            node.active = rewritten is not None
            if rewritten is not None:
                node.header = rewritten
            pruned_names.update(removed)
            break

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
            and self._selectors._base_header(node.header) == "groups"
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
                name = self._selectors._group_definition_name(definition.header)
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
                and self._selectors._base_header(node.header) == "interfaces"
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
            canonical_junos_interface(
                self._selectors._base_header(node.header).split()[0]
            )
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
                for name in self._selectors._parse_group_names(
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
                for name in self._selectors._parse_group_names(
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
                for name in self._selectors._parse_group_names(
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
                    self._selectors._parse_group_names(
                        child.header, "apply-groups"
                    )
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
