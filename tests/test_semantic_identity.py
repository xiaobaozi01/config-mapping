"""验证 XRv9000/vMX 规则包、路径匹配和未知语句降级。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config_adaptor.models import WashingPolicy
from config_adaptor.parsers.cisco_iosxr import CiscoDocument
from config_adaptor.parsers.juniper_junos import JunosDocument
from config_adaptor.pipeline import convert
from config_adaptor.vendor.cisco.identity import resolve_cisco_identity
from config_adaptor.vendor.juniper.identity import resolve_junos_identity
from config_adaptor.washing import load_washing_policy


FIXTURES = Path(__file__).parent / "fixtures"


def semantic_config(name: str) -> str:
    """读取语义 identity 规则覆盖测试配置。"""
    return (FIXTURES / "semantic_identity" / name).read_text(encoding="utf-8")


class SemanticIdentityTest(unittest.TestCase):
    """规则 identity 必须同时保证覆盖关系和可重复关系。"""

    def test_iosxr_path_aware_bgp_rules(self):
        path = ["router bgp 65000", "neighbor 192.0.2.1", "address-family ipv4 unicast"]
        import_policy = resolve_cisco_identity("route-policy IMPORT in", path=path)
        replacement = resolve_cisco_identity("route-policy NEW-IMPORT in", path=path)
        export_policy = resolve_cisco_identity("route-policy EXPORT out", path=path)
        self.assertTrue(import_policy.matched)
        self.assertEqual(import_policy.identity, replacement.identity)
        self.assertNotEqual(import_policy.identity, export_policy.identity)

        first_timer = resolve_cisco_identity("timers bgp 30 90", path=["router bgp 65000"])
        second_timer = resolve_cisco_identity("timers bgp 60 180", path=["router bgp 65000"])
        self.assertEqual(first_timer.identity, second_timer.identity)
        self.assertEqual(first_timer.rule_id, "xrv9000.bgp.timers")

    def test_iosxr_negation_uses_positive_identity(self):
        path = ["interface GigabitEthernet0/0/0/0"]
        self.assertEqual(
            resolve_cisco_identity("shutdown", path=path).identity,
            resolve_cisco_identity("no shutdown", path=path).identity,
        )
        self.assertEqual(
            resolve_cisco_identity("no ipv4 address", path=path).identity,
            resolve_cisco_identity("ipv4 address 192.0.2.1/24", path=path).identity,
        )
        self.assertNotEqual(
            resolve_cisco_identity("ipv4 address 192.0.2.1/24", path=path).identity,
            resolve_cisco_identity(
                "ipv4 address 192.0.2.2/24 secondary",
                path=path,
            ).identity,
        )

    def test_junos_common_scalar_and_keyed_rules(self):
        bgp_path = ["protocols", "bgp", "group EDGE"]
        self.assertEqual(
            resolve_junos_identity("peer-as 65001;", path=bgp_path).identity,
            resolve_junos_identity("peer-as 65002;", path=bgp_path).identity,
        )

        interface_path = [
            "interfaces",
            "ge-0/0/0",
            "unit 0",
            "family inet",
        ]
        first = resolve_junos_identity("address 192.0.2.1/24;", path=interface_path)
        second = resolve_junos_identity("address 192.0.2.2/24;", path=interface_path)
        changed = resolve_junos_identity(
            "address 192.0.2.1/24 preferred;",
            path=interface_path,
        )
        self.assertNotEqual(first.identity, second.identity)
        self.assertEqual(first.identity, changed.identity)

    def test_junos_directional_rule_example(self):
        path = ["interfaces", "ge-0/0/0"]
        inherited_down = resolve_junos_identity("hold-time down 640;", path=path)
        local_down = resolve_junos_identity("hold-time down 0;", path=path)
        local_up = resolve_junos_identity("hold-time up 1000;", path=path)
        self.assertEqual(inherited_down.identity, local_down.identity)
        self.assertNotEqual(inherited_down.identity, local_up.identity)
        self.assertEqual(inherited_down.rule_id, "vmx.interface.hold-time")

    def test_known_rules_drive_group_override(self):
        cisco = CiscoDocument(semantic_config("cisco_group_bgp_override.cfg"))
        cisco_outcome = cisco.expand_groups([])
        self.assertTrue(cisco_outcome.success)
        self.assertIn("timers bgp 60 180", cisco.render())
        self.assertNotIn("timers bgp 30 90", cisco.render())
        self.assertEqual(cisco_outcome.conflicts[0]["rule_id"], "xrv9000.bgp.timers")

        junos = JunosDocument(semantic_config("junos_group_bgp_override.cfg"))
        junos_outcome = junos.expand_groups([])
        self.assertTrue(junos_outcome.success)
        self.assertIn("peer-as 65002;", junos.render())
        self.assertNotIn("peer-as 65001;", junos.render())
        self.assertEqual(junos_outcome.conflicts[0]["rule_id"], "vmx.bgp.peer-as")

    def test_unknown_identity_warns_or_rolls_back(self):
        source = semantic_config("cisco_group_unknown_identity.cfg")
        warning_document = CiscoDocument(source)
        warning_outcome = warning_document.expand_groups(
            ["GigabitEthernet0/0/0/0"],
            policy=WashingPolicy(group_unknown_identity="warn"),
        )
        self.assertTrue(warning_outcome.success)
        self.assertEqual(len(warning_outcome.ambiguities), 1)
        self.assertIn("vendor-knob inherited", warning_document.render())
        self.assertIn("vendor-knob local", warning_document.render())

        failing_document = CiscoDocument(source)
        before = failing_document.render()
        failing_outcome = failing_document.expand_groups(
            ["GigabitEthernet0/0/0/0"],
            policy=WashingPolicy(group_unknown_identity="fail"),
        )
        self.assertFalse(failing_outcome.success)
        self.assertEqual(len(failing_outcome.ambiguities), 1)
        self.assertEqual(failing_document.render(), before)

    def test_unknown_identity_policy_is_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "washing.yaml"
            path.write_text(
                "group_handling:\n  unknown_identity: fail\n",
                encoding="utf-8",
            )
            self.assertEqual(load_washing_policy(path).group_unknown_identity, "fail")

    def test_removed_group_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "washing.yaml"
            path.write_text(
                "group_handling:\n  mode: relevant\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "未知 group 处理配置: mode"):
                load_washing_policy(path)

    def test_conversion_report_contains_identity_coverage(self):
        fixture = FIXTURES / "iosxr_bundle"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(fixture / "topology.xlsx", fixture / "configs", output)
            self.assertFalse(context.has_errors)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            summary = report["summary"]
            self.assertGreater(summary["group_identity_rule_hits"], 0)
            self.assertEqual(summary["group_identity_fallbacks"], 0)
            self.assertEqual(summary["group_identity_coverage"], 1.0)
            self.assertEqual(summary["group_identity_ambiguity_count"], 0)


if __name__ == "__main__":
    unittest.main()
