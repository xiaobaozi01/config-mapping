"""拓扑工作簿加载测试。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from openpyxl import Workbook

from adaptation.topology import load_topology


def _write_topology(
    path: Path,
    link_headers: list[str],
    link_values: list[str],
) -> None:
    workbook = Workbook()
    devices = workbook.active
    devices.title = "设备列表"
    devices.append(["设备名称", "厂商"])
    devices.append(["R1", "Cisco"])
    devices.append(["R2", "Cisco"])

    links = workbook.create_sheet("链接表")
    links.append(link_headers)
    links.append(link_values)
    workbook.save(path)


class TopologyLoadTest(TestCase):
    """从拓扑工作簿加载稳定的链路身份。"""

    def test_link_name_is_loaded_from_spreadsheet(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "topology.xlsx"
            _write_topology(
                path,
                ["A端设备", "A端接口", "Z端设备", "Z端接口", "链路名称"],
                ["R1", "Gi0", "R2", "Gi1", "core-link"],
            )

            topology = load_topology(path)

        self.assertEqual("core-link", topology.links[0].link_name)

    def test_link_name_is_derived_when_column_is_absent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "topology.xlsx"
            _write_topology(
                path,
                ["A端设备", "A端接口", "Z端设备", "Z端接口"],
                ["R1", "Gi0", "R2", "Gi1"],
            )

            topology = load_topology(path)

        self.assertEqual("R1:Gi0 <-> R2:Gi1", topology.links[0].link_name)
