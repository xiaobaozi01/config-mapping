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


def plan_nni_components(
    links: list[Link],
    bundles: dict[tuple[int, str], str | None],
) -> NniComponentPlan:
    if not links:
        return NniComponentPlan({}, {}, ())

    union = _UnionFind([link.row for link in links])
    bundle_rows: dict[tuple[tuple[str, str], str, str], list[int]] = defaultdict(list)
    for link in links:
        device_pair = tuple(sorted((link.a_device, link.z_device)))
        for device_name, _ in link.endpoints():
            bundle = bundles[(link.row, device_name)]
            if bundle:
                bundle_rows[(device_pair, device_name, bundle)].append(link.row)
    for rows in bundle_rows.values():
        for row in rows[1:]:
            union.union(rows[0], row)

    components: dict[int, list[int]] = defaultdict(list)
    for link in links:
        components[union.find(link.row)].append(link.row)

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

        row_links = [by_row[row] for row in ordered_rows]
        invalid_devices: list[str] = []
        device_names = sorted(
            {name for row_link in row_links for name, _ in row_link.endpoints()}
        )
        for device_name in device_names:
            local_bundles = [bundles.get((row, device_name)) for row in ordered_rows]
            if any(value is None for value in local_bundles) or len(set(local_bundles)) != 1:
                invalid_devices.append(device_name)
        if invalid_devices:
            errors.append(
                f"聚合 NNI 行 {list(ordered_rows)} 在同一对端组内的成员关系不一致，"
                f"涉及设备: {', '.join(invalid_devices)}"
            )
        for row in ordered_rows:
            if row != keep:
                redundant_rows[row] = keep

    return NniComponentPlan(component_rows, redundant_rows, tuple(errors))
