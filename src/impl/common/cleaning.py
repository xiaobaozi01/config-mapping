"""清洗规则在不同厂商配置树之间共享的路径匹配原语。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True, slots=True)
class CleaningOperation:
    """描述一条厂商无关的声明式路径清洗操作。

    审计字段由调度层保留，使规则对象只表达匹配与修改本身，避免不同厂商重复实现
    报告结构。
    """

    path: tuple[str, ...]
    match: str
    action: str
    value: str | None = None
    interface_names: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class _CompiledPath:
    """保存已编译的路径组件及允许匹配的深度范围。

    预先拆分普通正则、单层通配和多层通配，避免每次遍历配置树时重新解释路径语法。
    """

    components: tuple[str | re.Pattern[str], ...]
    minimum_depth: int
    maximum_depth: int | None

    def matches(self, ancestors: tuple[str, ...]) -> bool:
        """判断一组完整祖先路径是否满足当前模板。

        使用滚动状态计算 ``*`` 和 ``**`` 的匹配，能够在不递归的前提下处理任意深度，
        同时保持规则路径的声明顺序。
        """
        depth = len(ancestors)
        if depth < self.minimum_depth or (
            self.maximum_depth is not None and depth > self.maximum_depth
        ):
            return False
        # 滚动状态保留原有 * / ** 语义，不为每次路径判断创建递归缓存函数。
        previous = [True] + [False] * depth
        for component in self.components:
            current = [False] * (depth + 1)
            if isinstance(component, str):
                if component == "**":
                    current[0] = previous[0]
                    for index in range(1, depth + 1):
                        current[index] = previous[index] or current[index - 1]
                else:
                    current[1:] = previous[:-1]
            else:
                for index, ancestor in enumerate(ancestors, start=1):
                    current[index] = previous[index - 1] and bool(
                        component.search(ancestor)
                    )
            previous = current
        return previous[-1]


@dataclass(frozen=True, slots=True)
class _CompiledOperation:
    """保存一条清洗操作编译后的路径、正则和公开审计元数据。

    原始输入下标独立保留，使删除和替换分轮执行后仍能按调用方输入顺序返回命中数。
    """

    index: int
    path: _CompiledPath
    pattern: re.Pattern[str]
    value: str | None
    interface_names: frozenset[str] | None


class _CleaningPhase:
    """保存单个动作轮次的编译规则和路径候选缓存。

    缓存仅存活于本次清洗调用，既避免跨文档污染，也避免同一祖先路径下重复筛选
    操作列表。
    """

    def __init__(self, action: str, operations: list[_CompiledOperation]):
        """创建固定动作轮次并建立空的路径候选缓存。

        操作列表在轮次内只读，因此可以安全地按祖先路径缓存筛选结果而不复制规则。
        """
        self.action = action
        self._operations = operations
        self._paths = tuple(
            dict.fromkeys(operation.path for operation in operations)
        )
        self._candidates: dict[
            tuple[str, ...], tuple[_CompiledOperation, ...]
        ] = {}

    def candidates(
        self, ancestors: tuple[str, ...]
    ) -> tuple[_CompiledOperation, ...]:
        """返回路径匹配当前祖先节点的操作，并缓存该路径的结果。

        同一父路径会遍历大量兄弟节点，缓存避免每个节点都重新执行完整路径匹配。
        """
        if ancestors not in self._candidates:
            matching_paths = {
                path for path in self._paths if path.matches(ancestors)
            }
            self._candidates[ancestors] = tuple(
                operation
                for operation in self._operations
                if operation.path in matching_paths
            )
        return self._candidates[ancestors]


def compile_cleaning_rules(
    operations: Sequence[CleaningOperation],
) -> tuple[_CleaningPhase, ...]:
    """把清洗操作编译为删除和替换两个有序执行轮次。

    所有错误都在任何文档修改前暴露，避免删除完成后才发现替换模板无效而留下部分
    修改；轮次内保留原输入顺序和下标，便于稳定审计。
    """
    paths: dict[tuple[str, ...], _CompiledPath] = {}
    phases: dict[str, list[_CompiledOperation]] = {"delete": [], "replace": []}
    for index, operation in enumerate(operations):
        if operation.action not in phases:
            raise ValueError(f"不支持的清洗动作: {operation.action}")
        if operation.action == "replace" and operation.value is None:
            raise ValueError("replace 清洗操作必须提供 value")
        if operation.value is not None and not isinstance(operation.value, str):
            raise ValueError(f"第 {index + 1} 条清洗操作的 value 必须是字符串")
        pattern = re.compile(operation.match, re.IGNORECASE)
        if operation.action == "replace":
            # sub 即使没有命中也会编译替换模板；提前拒绝无效捕获组或转义，
            # 防止删除轮完成后才报错，留下未审计的部分修改。
            try:
                pattern.sub(operation.value, "")
            except (re.error, IndexError) as exc:
                raise ValueError(
                    f"第 {index + 1} 条清洗操作包含无效替换模板: {exc}"
                ) from exc
        if operation.path not in paths:
            paths[operation.path] = _CompiledPath(
                tuple(
                    component
                    if component in {"*", "**"}
                    else re.compile(component, re.IGNORECASE)
                    for component in operation.path
                ),
                sum(component != "**" for component in operation.path),
                None if "**" in operation.path else len(operation.path),
            )
        phases[operation.action].append(
            _CompiledOperation(
                index,
                paths[operation.path],
                pattern,
                operation.value,
                operation.interface_names,
            )
        )
    return tuple(
        _CleaningPhase(action, group)
        for action, group in phases.items()
        if group
    )


def matches_cleaning_path(
    ancestors: tuple[str, ...], path: tuple[str, ...]
) -> bool:
    """判断完整父级路径是否命中规则路径。

    普通元素以不区分大小写的正则匹配一级父节点，``*`` 恰好消费一级，``**``
    消费零级或多级。匹配必须同时用完整条规则路径和完整祖先路径，因而空路径只会
    命中顶层节点。
    """

    @cache
    def match(rule_index: int, ancestor_index: int) -> bool:
        """递归判断规则后缀是否能完整匹配祖先后缀。

        缓存已计算的索引组合，使 ``**`` 回溯不会对同一后缀重复展开。
        """
        if rule_index == len(path):
            return ancestor_index == len(ancestors)
        component = path[rule_index]
        if component == "**":
            return match(rule_index + 1, ancestor_index) or (
                ancestor_index < len(ancestors)
                and match(rule_index, ancestor_index + 1)
            )
        if ancestor_index == len(ancestors):
            return False
        if component == "*" or re.search(
            component,
            ancestors[ancestor_index],
            re.IGNORECASE,
        ):
            return match(rule_index + 1, ancestor_index + 1)
        return False

    return match(0, 0)
