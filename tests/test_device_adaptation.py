"""验证单设备入口与全量转换结果一致，并覆盖单设备特有的输入边界。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config_adaptor.adaptation import DeviceLink, adapt_device
from config_adaptor.adaptation.factory import parse_document
from config_adaptor.adaptation.service import convert
from config_adaptor.adaptation.topology import load_topology


FIXTURES = Path(__file__).parent / "fixtures"

CONSISTENCY_FIXTURES = (
    "iosxr_bundle",
    "junos_bundle",
    "iosxr_mlag",
    "junos_mlag",
    "cross_vendor",
    "iosxr_duplicate_vlan",
)


def _device_links(topology, device_name: str) -> list[DeviceLink]:
    """按 Excel 行顺序取出某台设备参与的链路，转换为单设备入参。"""
    vendors = {device.name: device.vendor.value for device in topology.devices}
    result: list[DeviceLink] = []
    for link in sorted(topology.links, key=lambda item: item.row):
        if link.a_device == device_name:
            peer, local, remote = link.z_device, link.a_interface, link.z_interface
        elif link.z_device == device_name:
            peer, local, remote = link.a_device, link.z_interface, link.a_interface
        else:
            continue
        result.append(DeviceLink(local, peer, remote, vendors[peer]))
    return result


def _without_rows(mappings: list[dict]) -> list[dict]:
    """单设备模式按输入顺序重新编号链路，比较映射时忽略行号。"""
    return [{key: value for key, value in item.items() if key != "link_rows"} for item in mappings]


class DeviceAdaptationConsistencyTest(unittest.TestCase):
    def test_single_device_matches_full_conversion(self):
        for name in CONSISTENCY_FIXTURES:
            root = FIXTURES / name
            topology = load_topology(root / "topology.xlsx")
            with tempfile.TemporaryDirectory() as temp:
                output = Path(temp)
                context = convert(root / "topology.xlsx", root / "configs", output)
                self.assertFalse(context.has_errors, name)
                full_mappings = json.loads(
                    (output / "interface-mapping.json").read_text(encoding="utf-8")
                )
                for device in topology.devices:
                    with self.subTest(fixture=name, device=device.name):
                        text = (root / "configs" / device.config_file).read_text(
                            encoding="utf-8-sig"
                        )
                        result = adapt_device(
                            device.name,
                            device.vendor.value,
                            text,
                            _device_links(topology, device.name),
                        )
                        self.assertTrue(result.success, result.errors)
                        expected = (output / "configs" / f"{device.name}.cfg").read_text(
                            encoding="utf-8"
                        )
                        self.assertEqual(result.config, expected)
                        self.assertEqual(
                            _without_rows([item.to_dict() for item in result.mappings]),
                            _without_rows(
                                [item for item in full_mappings if item["device"] == device.name]
                            ),
                        )

    def test_link_results_report_target_interfaces(self):
        root = FIXTURES / "cross_vendor"
        topology = load_topology(root / "topology.xlsx")
        text = (root / "configs" / "C1.cfg").read_text(encoding="utf-8-sig")
        links = _device_links(topology, "C1")

        result = adapt_device("C1", "cisco_iosxr", text, links)

        self.assertEqual(len(result.links), len(links))
        for item, link in zip(result.links, links):
            self.assertEqual(item["source_interface"], link.local_interface)
        active = [item for item in result.links if item["active"]]
        self.assertTrue(active)
        self.assertTrue(all(item["target_interface"] for item in active))


class DeviceAdaptationBoundaryTest(unittest.TestCase):
    def _iosxr_bundle_r1(self) -> tuple[str, list[DeviceLink]]:
        root = FIXTURES / "iosxr_bundle"
        topology = load_topology(root / "topology.xlsx")
        text = (root / "configs" / "R1.cfg").read_text(encoding="utf-8-sig")
        return text, _device_links(topology, "R1")

    def test_huawei_peer_link_is_skipped_and_interface_kept_as_nni(self):
        text, links = self._iosxr_bundle_r1()
        huawei_links = [
            DeviceLink(link.local_interface, "HW1", link.peer_interface, "华为")
            for link in links
        ]

        result = adapt_device("R1", "cisco_iosxr", text, huawei_links)

        self.assertTrue(result.success, result.errors)
        self.assertTrue(all(not item["active"] for item in result.links))
        self.assertTrue(all(item["target_interface"] is None for item in result.links))
        self.assertTrue(result.mappings)
        self.assertTrue(
            all(item.role == "NNI" and item.action == "skip" for item in result.mappings
                if item.source_interface in {link.local_interface for link in links})
        )

    def test_unsupported_local_vendor_fails(self):
        result = adapt_device("HW1", "huawei", "sysname HW1\n")

        self.assertFalse(result.success)
        self.assertIsNone(result.config)
        self.assertTrue(result.errors)

    def test_peer_cannot_be_local_device(self):
        text, links = self._iosxr_bundle_r1()
        bad = [DeviceLink(links[0].local_interface, "R1", "Gi0/0/0/9", "cisco")]

        result = adapt_device("R1", "cisco_iosxr", text, bad)

        self.assertFalse(result.success)
        self.assertIsNone(result.config)

    def test_unresolved_group_fails_without_config(self):
        root = FIXTURES / "junos_unresolved"
        topology = load_topology(root / "topology.xlsx")
        for device in topology.devices:
            text = (root / "configs" / device.config_file).read_text(encoding="utf-8-sig")
            result = adapt_device(
                device.name,
                device.vendor.value,
                text,
                _device_links(topology, device.name),
            )
            if not result.success:
                self.assertIsNone(result.config)
                return
        self.fail("junos_unresolved 中至少一台设备应转换失败")


class PhysicalPortSummaryTest(unittest.TestCase):
    def _adapt(self, fixture: str, device_name: str, links=None):
        root = FIXTURES / fixture
        topology = load_topology(root / "topology.xlsx")
        device = next(item for item in topology.devices if item.name == device_name)
        text = (root / "configs" / device.config_file).read_text(encoding="utf-8-sig")
        if links is None:
            links = _device_links(topology, device_name)
        result = adapt_device(device_name, device.vendor.value, text, links)
        self.assertTrue(result.success, result.errors)
        # 拓扑可以写接口简称，物理口汇总使用配置中的规范名。
        document = parse_document(device.vendor.value, text)
        self.resolve = document.resolve_interface
        return result, links

    def test_summary_invariants_hold_for_all_fixtures(self):
        for name in CONSISTENCY_FIXTURES:
            topology = load_topology(FIXTURES / name / "topology.xlsx")
            for device in topology.devices:
                with self.subTest(fixture=name, device=device.name):
                    result, _ = self._adapt(name, device.name)
                    sources = [item.source_interface for item in result.physical_mappings]
                    self.assertEqual(len(sources), len(set(sources)))
                    self.assertEqual(
                        result.unused_source_interfaces,
                        [
                            item.source_interface
                            for item in result.physical_mappings
                            if item.target_interface is None
                        ],
                    )
                    profile_ports = (
                        [f"GigabitEthernet0/0/0/{index}" for index in range(8)]
                        if device.vendor.value == "cisco_iosxr"
                        else [f"ge-0/0/{index}" for index in range(8)]
                    )
                    used = {
                        item.target_interface
                        for item in result.physical_mappings
                        if item.target_interface
                    }
                    self.assertFalse(used & set(result.unused_target_interfaces))
                    self.assertTrue(set(result.unused_target_interfaces) <= set(profile_ports[:-2]))
                    self.assertFalse(set(profile_ports[-2:]) & set(result.unused_target_interfaces))

    def test_unselected_bundle_member_has_no_target(self):
        root = FIXTURES / "iosxr_bundle"
        topology = load_topology(root / "topology.xlsx")
        first_link = _device_links(topology, "R1")[:1]

        result, _ = self._adapt("iosxr_bundle", "R1", first_link)

        by_source = {item.source_interface: item for item in result.physical_mappings}
        selected = by_source[self.resolve(first_link[0].local_interface)]
        self.assertEqual(selected.action, "selected-member")
        self.assertEqual(selected.target_interface, "GigabitEthernet0/0/0/0")
        other = by_source["GigabitEthernet0/0/0/1"]
        self.assertEqual(other.action, "remove")
        self.assertIsNone(other.target_interface)
        self.assertIn("GigabitEthernet0/0/0/1", result.unused_source_interfaces)

    def test_huawei_peer_leaves_nni_target_unused(self):
        root = FIXTURES / "iosxr_bundle"
        topology = load_topology(root / "topology.xlsx")
        links = [
            DeviceLink(link.local_interface, "HW1", link.peer_interface, "华为")
            for link in _device_links(topology, "R1")
        ]

        result, _ = self._adapt("iosxr_bundle", "R1", links)

        by_source = {item.source_interface: item for item in result.physical_mappings}
        for link in links:
            source = by_source[self.resolve(link.local_interface)]
            self.assertEqual(source.action, "skip")
            self.assertIsNone(source.target_interface)
        self.assertIn("GigabitEthernet0/0/0/0", result.unused_target_interfaces)

    def test_device_without_uni_reports_uni_port_unused(self):
        result, _ = self._adapt("iosxr_mlag", "B")

        self.assertFalse([item for item in result.physical_mappings if item.role == "UNI"])
        self.assertIn("GigabitEthernet0/0/0/5", result.unused_target_interfaces)
        self.assertNotIn("GigabitEthernet0/0/0/6", result.unused_target_interfaces)
        self.assertNotIn("GigabitEthernet0/0/0/7", result.unused_target_interfaces)

    def test_failed_result_has_empty_summary(self):
        result = adapt_device("HW1", "huawei", "sysname HW1\n")

        self.assertEqual(result.physical_mappings, [])
        self.assertEqual(result.unused_source_interfaces, [])
        self.assertEqual(result.unused_target_interfaces, [])


if __name__ == "__main__":
    unittest.main()
