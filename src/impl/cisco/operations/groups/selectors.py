"""Cisco Group 控制语句和选择器处理。"""

from __future__ import annotations

import re

from ...syntax.semantics import canonical_cisco_interface
from .models import _GroupStatement


class CiscoGroupSelectors:
    """提供 Cisco Group 控制语句和路径选择器处理。

    所有方法只解析文本或匹配工作语句，不直接修改正式文档。
    """

    @staticmethod
    def _parse_group_names(command: str, control_keyword: str) -> list[str]:
        """解析 group 控制语句中按声明顺序出现的名称。

        同时接受关键字单复数、单个名称、方括号列表及带引号名称；不匹配的命令
        返回空列表，使调用方能够区分普通配置与真正的 group 控制语句。
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

        长度越长代表选择器越具体；调用方使用原始 header 稳定打破并列。
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
