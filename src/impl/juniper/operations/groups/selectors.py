"""Junos Group 控制语句和通配选择器处理。"""

from __future__ import annotations

import fnmatch
import re
import shlex

from impl.common.support.errors import require_invariant

from ...document import (
    JuniperNode,
)
from ...syntax.ast import base_header as syntax_base_header


class JunosGroupSelectors:
    """提供 Junos Group 控制语句和通配选择器处理。

    所有方法只解析或匹配文本，不直接修改正式配置树。
    """

    @staticmethod
    def _base_header(header: str) -> str:
        """返回用于 group 匹配的规范节点头文本。

        具体归一化由 ``JunosDocument`` 统一实现，会去除 ``inactive:``、
        ``protect:`` 等控制前缀，避免展开器与解析器采用不同语义。
        """
        return syntax_base_header(header)

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
