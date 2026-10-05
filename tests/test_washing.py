"""验证管理面必清、可选能力清洗与 Bundle 扁平化清理。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config_adaptor.adaptation.service import convert
from config_adaptor.adaptation.washing import load_washing_policy
from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.common.policies import WashingPolicy
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures"


def washing_config(name: str) -> str:
    """读取认证与可选策略清洗使用的外部配置文件。"""
    return (FIXTURES / "washing_configs" / name).read_text(encoding="utf-8")


def conversion_fixture(name: str) -> tuple[Path, Path]:
    """返回端到端场景的拓扑文件和设备配置目录。"""
    root = FIXTURES / name
    return root / "topology.xlsx", root / "configs"


class WashingTest(unittest.TestCase):
    def test_mandatory_washing_removes_management_access_and_snmp(self):
        """默认只清管理面必删项，协议认证和业务能力继续保留。"""
        policy = WashingPolicy()

        cisco_document = CiscoDocument(washing_config("iosxr.cfg"))
        cisco_cleanup = cisco_document.clean_management_access()
        cisco_document.add_lab_account()
        cisco = cisco_document.render()
        self.assertNotIn("username old-user", cisco)
        self.assertNotIn("tacacs-server", cisco)
        self.assertNotIn("radius-server", cisco)
        self.assertNotIn("snmp-server", cisco)
        self.assertNotIn("ssh server", cisco)
        self.assertNotIn("ssh client", cisco)
        self.assertNotIn("telnet vrf", cisco)
        self.assertNotIn("OLDPASSWORD", cisco)
        self.assertIn("exec-timeout 10 0", cisco)
        self.assertIn("username labadmin", cisco)
        self.assertIn("key chain OSPF_KEYS", cisco)
        self.assertIn("authentication message-digest keychain OSPF_KEYS", cisco)
        self.assertIn("crypto pki trustpoint PROD-CA", cisco)
        self.assertIn("service cgn NAT1", cisco)
        self.assertIn("flow monitor PROD-FLOW", cisco)
        self.assertEqual(cisco_cleanup.removed["snmp"], 1)
        self.assertEqual(cisco_cleanup.removed["ssh"], 2)

        junos_document = JunosDocument(washing_config("junos.cfg"))
        junos_cleanup = junos_document.clean_management_access()
        junos_document.add_lab_account()
        junos = junos_document.render()
        self.assertNotIn("old-user", junos)
        self.assertNotIn("RADIUSSECRET", junos)
        self.assertNotIn("TACACSSECRET", junos)
        self.assertNotIn("radius-options", junos)
        self.assertNotIn("tacplus-options", junos)
        self.assertNotIn("accounting", junos)
        self.assertNotIn("community SNMPSECRET", junos)
        self.assertNotIn("ssh-known-hosts", junos)
        self.assertNotIn("authentication-order tacplus", junos)
        self.assertNotIn("telnet;", junos)
        self.assertIn("ftp;", junos)
        self.assertIn("user labadmin", junos)
        self.assertIn("authentication-key-chains", junos)
        self.assertIn("simple-password OSPFSECRET", junos)
        self.assertIn("ca-profile PROD-CA", junos)
        self.assertIn("rule-set PROD-NAT", junos)
        self.assertIn("flow-monitoring", junos)
        self.assertEqual(junos_cleanup.removed["snmp"], 1)
        self.assertGreaterEqual(junos_cleanup.removed["remote-access"], 3)

    def test_optional_washing_switches_remove_expanded_categories(self):
        """五个可选开关显式开启后才删除协议和兼容性配置。"""
        policy = load_washing_policy(FIXTURES / "washing_configs" / "all_optional.yaml")

        cisco_document = CiscoDocument(washing_config("iosxr.cfg"))
        cisco_document.clean_optional_features(policy)
        cisco = cisco_document.render()
        self.assertIn("username old-user", cisco)
        self.assertIn("tacacs-server", cisco)
        self.assertNotIn("key chain OSPF_KEYS", cisco)
        self.assertNotIn("authentication message-digest", cisco)
        self.assertNotIn("crypto pki", cisco)
        self.assertNotIn("hw-module", cisco)
        self.assertNotIn("service cgn", cisco)
        self.assertNotIn("flow monitor", cisco)

        junos_document = JunosDocument(washing_config("junos.cfg"))
        junos_document.clean_optional_features(policy)
        junos = junos_document.render()
        self.assertIn("old-user", junos)
        self.assertIn("radius-server", junos)
        self.assertNotIn("authentication-key-chains", junos)
        self.assertNotIn("simple-password OSPFSECRET", junos)
        self.assertNotIn("ca-profile PROD-CA", junos)
        self.assertNotIn("chassis {", junos)
        self.assertNotIn("rule-set PROD-NAT", junos)
        self.assertNotIn("nat-rules PROD-NAT", junos)
        self.assertNotIn("flow-monitoring", junos)
        self.assertNotIn("sampling {", junos)

        topology, config_dir = conversion_fixture("cross_vendor")
        with tempfile.TemporaryDirectory() as directory:
            context = convert(
                topology,
                config_dir,
                Path(directory) / "output",
                washing_policy_path=FIXTURES / "washing_configs" / "all_optional.yaml",
            )
            self.assertFalse(context.has_errors)
            self.assertTrue(context.washing_policy.protocol_authentication)
            self.assertTrue(context.washing_policy.flow_statistics)
            optional_events = [
                event for event in context.events if event["kind"] == "optional-washing"
            ]
            self.assertTrue(optional_events)
            self.assertIn("protocol_authentication", optional_events[0]["enabled"])
            authentication_events = [
                event for event in context.events if event["kind"] == "authentication"
            ]
            self.assertTrue(authentication_events)
            self.assertNotIn("pki", authentication_events[0]["removed_by_type"])

        combined = CiscoDocument(washing_config("iosxr.cfg"))
        combined.clean_authentication(policy)
        combined_rendered = combined.render()
        self.assertNotIn("username old-user", combined_rendered)
        self.assertNotIn("crypto pki", combined_rendered)

    def test_cisco_bundle_thresholds_are_removed_when_flattened(self):
        """Bundle 迁移到普通物理口时不遗留 minimum-active 或 LACP。"""
        document = CiscoDocument(washing_config("iosxr.cfg"))
        document.rename_interface_tree(
            "Bundle-Ether10",
            "GigabitEthernet0/0/0/0",
            strip_bundle=True,
        )
        rendered = document.render()
        self.assertNotIn("bundle minimum-active", rendered)
        self.assertNotIn("lacp switchover", rendered)
        self.assertIn("interface GigabitEthernet0/0/0/0", rendered)
        self.assertIn("ipv4 address 10.0.0.1 255.255.255.252", rendered)


if __name__ == "__main__":
    unittest.main()
