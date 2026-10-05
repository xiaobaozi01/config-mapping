"""NNI 拓扑的纯规划逻辑。

本模块不修改配置文档和 Excel 链路，只把聚合成员计算成不可变计划。这样
M-LAG、同对端聚合和异常成员关系可以脱离厂商 AST 单独测试。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ...models import Link


class _UnionFind:
    """按 Excel 行号合并同一聚合链路的物理成员。"""

    def __init__(self, values: list[int]):
        self._parent = {value: value for value in values}

    def find(self, value: int) -> int:
        while self._parent[value] != value:
            self._parent[value] = self._parent[self._parent[value]]
            value = self._parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


@dataclass(slots=True, frozen=True)
class NniEndpointPlan:
    """某设备端点从源接口迁移到目标物理口的执行计划。"""

    logical_sources: tuple[str, ...]
    bundles: tuple[str, ...]
    target: str
    link_rows: tuple[int, ...]
    source_members: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class NniComponentPlan:
    """聚合链路分组结果；键是保留行，值是组内全部行。"""

    component_rows: dict[int, tuple[int, ...]]
    redundant_rows: dict[int, int]
    errors: tuple[str, ...]


# 按 (行号, 设备名) 查询该端点聚合到的 bundle 名；None 表示该端点未聚合。
BundleLookup = dict[tuple[int, str], str | None]


def plan_nni_components(
    links: list[Link],
    bundles: BundleLookup,
) -> NniComponentPlan:
    """做什么：把 NNI 链接行按 bundle 聚合成组件，产出保留/冗余/错误计划。

    为什么：Excel 中一条聚合 NNI 常拆成多行（每行一个成员接口），只有先识别
    它们属于同一条物理链路，后续才能决定保留哪一行、把其余行标记为冗余。
    流程：并查集合并共享 bundle 的行 → 按根行号分组 → 逐组件选保留行、标冗余、
    校验成员一致性。
    """
    if not links:
        return NniComponentPlan({}, {}, ())

    # 第 1 步：用并查集合并共享同一 bundle 的行。
    # 聚合 NNI 的多行通过「两端设备对 + 端点设备名 + bundle 名」这组键识别为
    # 同一条物理链路；并查集让传递共享的行也归入同一组件（A 与 B 同 bundle、
    # B 与 C 同 bundle，则 A、B、C 归为一组）。
    union = _UnionFind([link.row for link in links])
    rows_by_bundle: dict[tuple[tuple[str, str], str, str], list[int]] = defaultdict(list)
    for link in links:
        sorted_pair = sorted((link.a_device, link.z_device))
        device_pair: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
        for device_name, _ in link.endpoints():
            bundle = bundles[(link.row, device_name)]
            if bundle:
                rows_by_bundle[(device_pair, device_name, bundle)].append(link.row)
    for rows in rows_by_bundle.values():
        for row in rows[1:]:
            union.union(rows[0], row)

    # 第 2 步：按并查集根行号把全部行聚成组件；每个连通块即一个聚合组件。
    components: dict[int, list[int]] = defaultdict(list)
    for link in links:
        components[union.find(link.row)].append(link.row)

    # 第 3 步：逐组件选保留行（最小行号）、把其余行标为冗余，并校验成员一致性。
    by_row = {link.row: link for link in links}
    component_rows: dict[int, tuple[int, ...]] = {}
    redundant_rows: dict[int, int] = {}
    errors: list[str] = []
    for rows in components.values():
        ordered_rows = tuple(sorted(rows))
        keep = min(ordered_rows)
        component_rows[keep] = ordered_rows
        if len(ordered_rows) <= 1:
            continue

        error = _validate_component_bundles(ordered_rows, by_row, bundles)
        if error is not None:
            errors.append(error)
        for row in ordered_rows:
            if row != keep:
                redundant_rows[row] = keep

    return NniComponentPlan(component_rows, redundant_rows, tuple(errors))


def _validate_component_bundles(
    ordered_rows: tuple[int, ...],
    by_row: dict[int, Link],
    bundles: BundleLookup,
) -> str | None:
    """做什么：校验一个组件内各成员行在每个设备端点上 bundle 是否一致。

    为什么：同一物理链路上的所有成员行，在同一设备端点上应指向同一个 bundle；
    任一设备端点上 bundle 缺失或不唯一都说明成员关系不一致。一致时返回
    ``None``，否则返回描述错误的文本。
    """
    row_links = [by_row[row] for row in ordered_rows]
    device_names = sorted(
        {name for row_link in row_links for name, _ in row_link.endpoints()}
    )
    invalid_devices: list[str] = []
    for device_name in device_names:
        local_bundles = [bundles.get((row, device_name)) for row in ordered_rows]
        if any(value is None for value in local_bundles) or len(set(local_bundles)) != 1:
            invalid_devices.append(device_name)
    if not invalid_devices:
        return None
    return (
        f"聚合 NNI 行 {list(ordered_rows)} 在同一对端组内的成员关系不一致，"
        f"涉及设备: {', '.join(invalid_devices)}"
    )
