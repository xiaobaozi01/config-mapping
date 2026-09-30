"""使用独立 cfg/xlsx fixture 验证端到端转换和 group 优先级。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from config_adaptor.parsers.cisco_iosxr import CiscoDocument
from config_adaptor.parsers.juniper_junos import JunosDocument
from config_adaptor.pipeline import convert


FIXTURES = Path(__file__).parent / "fixtures"


class ConversionTest(unittest.TestCase):
    """覆盖两个厂商的 NNI、UNI、认证及 group 关键边界。"""

    @staticmethod
    def fixture_config(name: str) -> str:
        """读取单项 group 测试使用的外部配置文件。"""
        return (FIXTURES / "group_configs" / name).read_text(encoding="utf-8")

    @staticmethod
    def conversion_fixture(name: str) -> tuple[Path, Path]:
        """返回端到端场景的拓扑文件和设备配置目录。"""
        root = FIXTURES / name
        return root / "topology.xlsx", root / "configs"

    def test_iosxr_bundle_nni_uni_and_auth(self):
        topology, config_dir = self.conversion_fixture("iosxr_bundle")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            self.assertEqual(context.devices["R1"].profile.uni_parent, "GigabitEthernet0/0/0/7")
            self.assertIn("GigabitEthernet0/0/0/6", context.devices["R1"].profile.nni_interfaces)
            converted = (output / "configs" / "R1.cfg").read_text(encoding="utf-8")
            self.assertNotIn("Bundle-Ether10", converted)
            self.assertNotIn("old-user", converted)
            self.assertNotIn("tacacs-server", converted)
            self.assertNotIn("taskgroup OLD-TASKS", converted)
            self.assertNotIn("usergroup OLD-USERS", converted)
            self.assertNotIn("login authentication OLD-AUTH", converted)
            self.assertNotIn("authorization commands OLD-AUTHZ", converted)
            self.assertNotIn("accounting commands OLD-ACCT", converted)
            self.assertNotIn("users group OLD-USERS", converted)
            self.assertNotIn("ACCESS-UNI", converted)
            self.assertIn("username labadmin", converted)
            self.assertIn("group root-system", converted)
            self.assertIn("line template vty", converted)
            self.assertIn("exec-timeout 10 0", converted)
            self.assertIn("interface GigabitEthernet0/0/0/0", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.2", converted)
            self.assertIn("encapsulation dot1q 2 second-dot1q 2", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertIn("interface Loopback0", converted)
            self.assertLess(converted.index("username labadmin"), converted.index("end"))

            adapted = load_workbook(output / "topology-adapted.xlsx")
            adapted_links = adapted["链接表"]
            self.assertEqual(adapted_links.max_row, 2)
            self.assertEqual(adapted_links.cell(2, 2).value, "GigabitEthernet0/0/0/0")
            self.assertEqual(adapted["设备列表"].cell(2, 3).value, "configs/R1.cfg")
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["summary"]["skipped_links"], 1)
            authentication = next(event for event in report["events"] if event["kind"] == "authentication")
            self.assertEqual(authentication["removed_by_type"]["taskgroup"], 1)
            self.assertEqual(authentication["removed_by_type"]["usergroup"], 1)
            self.assertEqual(authentication["removed_by_type"]["line-auth-reference"], 5)

    def test_junos_bundle_nni_uni_and_auth(self):
        topology, config_dir = self.conversion_fixture("junos_bundle")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            self.assertEqual(context.devices["J1"].profile.uni_parent, "ge-0/0/7")
            self.assertIn("ge-0/0/6", context.devices["J1"].profile.nni_interfaces)
            converted = (output / "configs" / "J1.cfg").read_text(encoding="utf-8")
            self.assertNotIn("ae0", converted)
            self.assertNotIn("old-user", converted)
            self.assertNotIn("radius-server", converted)
            self.assertNotIn("ACCESS-UNI", converted)
            self.assertIn("user labadmin", converted)
            self.assertIn("ge-0/0/0.0", converted)
            self.assertIn("ge-0/0/7", converted)
            self.assertIn("unit 100", converted)
            self.assertIn("vlan-tags outer 100 inner 100;", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertIn("lo0", converted)

    def test_iosxr_mlag_is_split_by_peer_and_references_are_cloned(self):
        """同一 Bundle 跨对端时分配多个物理口，全局引用同步展开。"""
        topology, config_dir = self.conversion_fixture("iosxr_mlag")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "A.cfg").read_text(encoding="utf-8")
            self.assertNotIn("Bundle-Ether10", converted)
            self.assertIn("interface GigabitEthernet0/0/0/0.100 l2transport", converted)
            self.assertIn("interface GigabitEthernet0/0/0/1.100 l2transport", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.2 l2transport", converted)
            self.assertIn("encapsulation dot1q 2 second-dot1q 2", converted)
            self.assertNotIn("rewrite egress tag", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertNotIn("interface BVI300", converted)
            self.assertNotIn("interface BVI400", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.300", converted)
            self.assertIn("ipv4 address 198.51.100.1 255.255.255.0", converted)
            self.assertNotIn("203.0.113.1", converted)
            self.assertIn("interface GigabitEthernet0/0/0/0.100", converted)
            self.assertIn("interface GigabitEthernet0/0/0/1.100", converted)

            adapted = load_workbook(output / "topology-adapted.xlsx")
            links = adapted["链接表"]
            self.assertEqual(links.max_row, 3)
            self.assertEqual(links.cell(2, 2).value, "GigabitEthernet0/0/0/0")
            self.assertEqual(links.cell(3, 2).value, "GigabitEthernet0/0/0/1")

    def test_junos_mlag_is_split_by_peer_and_uni_uses_qinq(self):
        """Junos ae 跨对端拆分，且 UNI 的旧 VLAN 终结被统一清理。"""
        topology, config_dir = self.conversion_fixture("junos_mlag")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "JA.cfg").read_text(encoding="utf-8")
            self.assertNotIn("ae0", converted)
            self.assertIn("ge-0/0/0", converted)
            self.assertIn("ge-0/0/1", converted)
            self.assertIn("interface ge-0/0/0.0;", converted)
            self.assertIn("interface ge-0/0/1.0;", converted)
            self.assertIn("ge-0/0/7", converted)
            self.assertIn("vlan-tags outer 2 inner 2;", converted)
            self.assertNotIn("vlan-id-list", converted)
            self.assertNotIn("input-vlan-map", converted)
            self.assertNotIn("output-vlan-map", converted)
            self.assertNotIn("interface-mode trunk", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertNotIn("irb {", converted)
            self.assertIn("unit 300", converted)
            self.assertIn("address 198.51.100.1/24;", converted)
            self.assertNotIn("203.0.113.1/24", converted)
            mappings = json.loads((output / "interface-mapping.json").read_text(encoding="utf-8"))
            removed_irb = next(item for item in mappings if item["source_interface"] == "irb.400")
            self.assertEqual(removed_irb["action"], "remove-bare")

    def test_duplicate_uni_vlan_is_reassigned_per_device(self):
        topology, config_dir = self.conversion_fixture("iosxr_duplicate_vlan")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "R1.cfg").read_text(encoding="utf-8")
            self.assertIn("interface GigabitEthernet0/0/0/7.100", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.2", converted)
            self.assertEqual(converted.count("encapsulation dot1q 100"), 1)
            self.assertEqual(converted.count("encapsulation dot1q 2"), 1)
            self.assertIn("encapsulation dot1q 100 second-dot1q 201", converted)
            self.assertIn("encapsulation dot1q 2 second-dot1q 100", converted)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(report["summary"]["group_conflict_count"], 1)
            self.assertEqual(report["group_conflicts"][0]["winner_source"], "explicit")

    def test_iosxr_group_regex_expansion(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_regex.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertFalse(outcome.warnings)
        self.assertTrue(outcome.events)
        self.assertIn("mtu 9000", rendered)
        self.assertNotIn("group COMMON", rendered)
        self.assertNotIn("apply-group COMMON", rendered)

    def test_junos_wildcard_group_expansion(self):
        document = JunosDocument(self.fixture_config("junos_group_wildcard.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertFalse(outcome.warnings)
        self.assertTrue(outcome.events)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups COMMON", rendered)

    def test_iosxr_group_conflict_precedence_and_semantic_keys(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_conflicts.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 1500", rendered)
        self.assertNotIn("mtu 8000", rendered)
        self.assertNotIn("mtu 9000", rendered)
        self.assertIn("ipv4 address 192.0.2.1", rendered)
        self.assertNotIn("ipv4 address 10.0.0.1", rendered)
        self.assertIn("ipv4 access-group GROUP-IN ingress", rendered)
        self.assertIn("ipv4 access-group EXPLICIT-OUT egress", rendered)
        self.assertGreaterEqual(len(outcome.conflicts), 3)
        self.assertTrue(all(item["winner_source"] == "explicit" for item in outcome.conflicts))

    def test_iosxr_first_group_in_list_wins(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_order.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 1111", rendered)
        self.assertNotIn("mtu 2222", rendered)
        self.assertEqual(outcome.conflicts[0]["winner_source"], "group:FIRST")

    def test_iosxr_longest_regex_and_negated_single_value(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_specificity.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 2000", rendered)
        self.assertNotIn("mtu 1000", rendered)
        self.assertIn("description generic", rendered)
        self.assertIn("bandwidth 1000000", rendered)
        self.assertIn("no shutdown", rendered)
        self.assertNotIn("\n shutdown", rendered)
        self.assertTrue(outcome.conflicts)

    def test_junos_nested_list_and_except_precedence(self):
        document = JunosDocument(self.fixture_config("junos_group_precedence.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertIn("mtu 3333;", rendered)
        self.assertIn("mtu 2222;", rendered)
        self.assertIn("mtu 4444;", rendered)
        self.assertNotIn("mtu 1111;", rendered)
        self.assertIn("address 10.0.0.1/32;", rendered)
        self.assertIn("address 10.0.0.2/32;", rendered)
        self.assertTrue(outcome.conflicts)

    def test_group_expansion_rolls_back_on_unresolved_reference(self):
        source = self.fixture_config("junos_group_unresolved.cfg")
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.warnings)
        self.assertIn("apply-groups MISSING", document.render())
        self.assertIn("groups {", document.render())

    def test_unresolved_applied_group_fails_conversion_before_mapping(self):
        topology, config_dir = self.conversion_fixture("junos_unresolved")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertTrue(context.has_errors)
            self.assertFalse((output / "configs").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("group" in message for message in report["errors"]))


if __name__ == "__main__":
    unittest.main()
