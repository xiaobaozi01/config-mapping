"""验证 IOS XR group/apply-group 展开的优先级、通配与回滚边界。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config_adaptor.cisco.document import CiscoDocument


FIXTURES = Path(__file__).parent / "fixtures" / "group_configs"


def group_config(name: str) -> str:
    """读取 IOS XR group 展开测试配置。"""
    return (FIXTURES / name).read_text(encoding="utf-8")


class CiscoGroupExpansionTest(unittest.TestCase):
    """覆盖 IOS XR group 的静态展开、冲突与失败回滚。"""

    def test_iosxr_group_regex_expansion(self):
        document = CiscoDocument(group_config("iosxr_group_regex.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertFalse(outcome.warnings)
        self.assertTrue(outcome.events)
        self.assertIn("mtu 9000", rendered)
        self.assertNotIn("group COMMON", rendered)
        self.assertNotIn("apply-group COMMON", rendered)

    def test_full_expansion_rejects_unrelated_invalid_iosxr_group(self):
        source = group_config("iosxr_invalid_unrelated_group.cfg")
        document = CiscoDocument(source)
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        self.assertFalse(outcome.success)
        self.assertTrue(any("TELEMETRY" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_iosxr_expands_non_interface_group(self):
        document = CiscoDocument(group_config("iosxr_non_interface_group.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertIn("telemetry model-driven", rendered)
        self.assertIn("destination-group LAB", rendered)
        self.assertNotIn("group TELEMETRY", rendered)
        self.assertNotIn("apply-group TELEMETRY", rendered)

    def test_iosxr_group_conflict_precedence_and_semantic_keys(self):
        document = CiscoDocument(group_config("iosxr_group_conflicts.cfg"))
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
        document = CiscoDocument(group_config("iosxr_group_order.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 1111", rendered)
        self.assertNotIn("mtu 2222", rendered)
        self.assertEqual(outcome.conflicts[0]["winner_source"], "group:FIRST")

    def test_iosxr_longest_regex_and_negated_single_value(self):
        document = CiscoDocument(group_config("iosxr_group_specificity.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 2000", rendered)
        self.assertNotIn("mtu 1000", rendered)
        self.assertIn("description generic", rendered)
        self.assertIn("bandwidth 1000000", rendered)
        self.assertIn("no shutdown", rendered)
        self.assertNotIn("\n shutdown", rendered)
        self.assertTrue(outcome.conflicts)

    def test_undefined_root_group_fails_without_any_definitions(self):
        source = group_config("iosxr_undefined_root_group.cfg")
        cisco = CiscoDocument(source)
        cisco_outcome = cisco.expand_groups([])
        self.assertFalse(cisco_outcome.success)
        self.assertTrue(any("MISSING" in item for item in cisco_outcome.warnings))
        self.assertEqual(cisco.render(), source)


if __name__ == "__main__":
    unittest.main()
