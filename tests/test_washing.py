"""验证管理面必清、声明式清洗规则与 Bundle 扁平化清理。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from config_adaptor.adaptation.cleaning_rules import CleaningRulesHandler
from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures"


def washing_config(name: str) -> str:
    """读取认证与可选策略清洗使用的外部配置文件。"""
    return (FIXTURES / "washing_configs" / name).read_text(encoding="utf-8")


class WashingTest(unittest.TestCase):
    def test_mandatory_washing_removes_management_access_and_snmp(self):
        """默认只清管理面必删项，协议认证和业务能力继续保留。"""
        cisco_document = CiscoDocument(washing_config("iosxr.cfg"))
        cisco_cleanup = cisco_document.clean_management_access()
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
        self.assertIn("key chain OSPF_KEYS", cisco)
        self.assertIn("authentication message-digest keychain OSPF_KEYS", cisco)
        self.assertIn("crypto pki trustpoint PROD-CA", cisco)
        self.assertIn("service cgn NAT1", cisco)
        self.assertIn("flow monitor PROD-FLOW", cisco)
        self.assertEqual(cisco_cleanup.removed["snmp"], 1)
        self.assertEqual(cisco_cleanup.removed["ssh"], 2)

        junos_document = JunosDocument(washing_config("junos.cfg"))
        junos_cleanup = junos_document.clean_management_access()
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
        self.assertIn("authentication-key-chains", junos)
        self.assertIn("simple-password OSPFSECRET", junos)
        self.assertIn("ca-profile PROD-CA", junos)
        self.assertIn("rule-set PROD-NAT", junos)
        self.assertIn("flow-monitoring", junos)
        self.assertEqual(junos_cleanup.removed["snmp"], 1)
        self.assertGreaterEqual(junos_cleanup.removed["remote-access"], 3)

    def test_enabled_rules_remove_optional_categories(self):
        """启用 YAML 规则后删除协议认证和镜像不需要的可选能力。"""
        optional_categories = {
            "protocol-auth-definition",
            "protocol-auth-reference",
            "pki",
            "hardware",
            "nat",
            "flow-statistics",
        }
        rules = tuple(
            replace(rule, enable=True)
            for rule in CleaningRulesHandler._configured_rules()
            if rule.category in optional_categories
        )

        def apply(document, vendor: str) -> None:
            for rule in rules:
                if rule.vendor == vendor:
                    document.apply_cleaning_rule(
                        rule.path,
                        rule.match,
                        rule.action,
                        rule.value,
                    )

        cisco_document = CiscoDocument(washing_config("iosxr.cfg"))
        apply(cisco_document, "cisco_iosxr")
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
        apply(junos_document, "juniper_junos")
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

        combined = CiscoDocument(washing_config("iosxr.cfg"))
        apply(combined, "cisco_iosxr")
        combined.clean_management_access()
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
