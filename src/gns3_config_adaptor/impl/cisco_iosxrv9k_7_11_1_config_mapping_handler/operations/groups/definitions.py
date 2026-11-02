"""Cisco Group 定义和工作树准备。"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import _GroupDefinition, _GroupStatement
from .publishing import clone_statements


class CiscoGroupDefinitions:
    """提供 Cisco Group 定义发现和工作树准备方法。

    方法只读取正式文档，并经由发布模块构造展开前的工作语句树。
    """

    __slots__ = ("_document", "_selectors")

    def __init__(self, document, selectors):
        """绑定文档和选择器助手。

        构造阶段不读取 Group 定义或修改配置。
        """
        self._document = document
        self._selectors = selectors

    @property
    def top_level_nodes(self) -> list[_GroupStatement]:
        """把正式文档顶层节点转换为工作语句。

        每次读取生成独立副本，避免展开过程直接修改正式 AST。
        """
        from .publishing import from_context

        return [from_context(node) for node in self._document.root.nodes]

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
            self._selectors._parse_group_names(node.header, "apply-group")
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
            canonical_name = (
                self._document.interfaces.analysis.resolve_interface(
                    candidate_name
                )
            )
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
                self._selectors._parse_group_names(child.header, "apply-group")
            )
            if child.is_block:
                referenced_group_names.update(
                    self._collect_root_group_names(child)
                )
        return referenced_group_names
