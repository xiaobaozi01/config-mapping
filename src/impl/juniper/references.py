"""Junos reference expansion independent of document lifecycle."""

import re

from .ast import JuniperNode


class _ReferenceRewriter:
    """保存单次接口引用替换的匹配状态。

    替换模式只匹配原始名称，使一对多结果不会在同一轮继续被其他映射再次改写。
    """

    def __init__(self, document, replacements: dict[str, list[str]]):
        """编译来源名称的边界正则并保存目标映射。

        名称按长度倒序拼接，避免较短接口名抢先匹配较长名称的一部分。
        """
        self.document = document
        self.replacements = replacements
        names = sorted(replacements, key=len, reverse=True)
        self.pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])("
            + "|".join(map(re.escape, names))
            + r")(?![A-Za-z0-9_.-])"
        )

    def expand(self, value: str) -> list[str]:
        """把一个 header 展开为所有目标名称组合。

        同时替换多个来源时会生成独立变体，避免先把一个名称改写后再误命中新结果。
        """
        matches = list(self.pattern.finditer(value))
        if not matches:
            return [value]
        variants = [""]
        cursor = 0
        for match in matches:
            prefix = value[cursor : match.start()]
            variants = [
                current + prefix + target
                for current in variants
                for target in self.replacements[match.group(1)]
            ]
            cursor = match.end()
        return list(
            dict.fromkeys(current + value[cursor:] for current in variants)
        )

    def walk(self, node: JuniperNode, path: tuple[str, ...] = ()) -> None:
        """递归改写有效节点中的接口引用。

        根 interfaces 下的定义名称由迁移步骤负责，仅其内部引用在这里替换，避免重复改名。
        """
        if not node.active or not node.is_block:
            return
        rebuilt = []
        for child in node.children:
            # inactive/protect 是配置语法前缀，仍需改写；逻辑删除和注释不参与。
            if not child.active or child.is_comment_only:
                rebuilt.append(child)
                continue
            variants = (
                [child.header]
                if path == ("interfaces",)
                else self.expand(child.header)
            )
            for header in variants:
                clone = (
                    child
                    if len(variants) == 1 and header == child.header
                    else child.clone()
                )
                clone.header = header
                base = self.document._parser.base_header(header).rstrip(";")
                self.walk(clone, (*path, base))
                rebuilt.append(clone)
        node.children = rebuilt


def replace_references(document, replacements: dict[str, list[str]]) -> None:
    """按映射表改写 Junos 配置中的接口引用。

    接口定义名称由迁移步骤处理，本方法只更新定义之外的引用且过滤恒等映射。
    """
    relevant = {
        document.resolve_interface(source): list(dict.fromkeys(targets))
        for source, targets in replacements.items()
        if targets and targets != [source]
    }
    if relevant:
        with document.edit().bulk() as working_root:
            _ReferenceRewriter(document, relevant).walk(working_root)
