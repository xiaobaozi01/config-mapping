"""验证路由协议无关功能的接口类型约束清理。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config_adaptor.adaptation.cleaning_rules import CleaningRulesHandler
from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures" / "type_constraints"


def apply_categories(document, vendor: str, categories: set[str]) -> str:
    """按 category 执行 YAML 规则（忽略 enable），模拟用户同时开启一组可选规则。"""
    for rule in CleaningRulesHandler._configured_rules():
        if rule.vendor == vendor and rule.category in categories:
            rule.apply(document)
    return document.render()


class TypeConstraintCleaningTest(unittest.TestCase):
    """验证启用的接口类型约束清洗规则按规格删除配置。"""

    def test_cisco_interface_link_discovery_and_oam_are_removed(self):
        """接口下的 LLDP/CDP、以太网 CFM/OAM 全部删除。"""
        doc = CiscoDocument((FIXTURES / "iosxr.cfg").read_text())
        handler = CleaningRulesHandler._configured_rules()
        lldp_rule = next(r for r in handler if r.rule_id == "cisco.remove.interface.link.discovery")
        cfm_rule = next(r for r in handler if r.rule_id == "cisco.remove.interface.ethernet.oam")

        lldp_rule.apply(doc)
        cfm_rule.apply(doc)
        output = doc.render()

        # 接口块内的 LLDP/CDP/CFM/OAM 被删除，顶层 ethernet cfm 由另一条规则处理
        self.assertNotIn("lldp", output)
        self.assertNotIn("cdp", output)
        self.assertNotIn("mep domain", output)
        self.assertNotIn("ethernet oam", output)
        self.assertIn("ethernet cfm\n domain MD1", output)

    def test_cisco_global_ethernet_oam_is_removed(self):
        """顶层以太网 CFM 配置删除。"""
        doc = CiscoDocument((FIXTURES / "iosxr.cfg").read_text())
        rule = next(
            r
            for r in CleaningRulesHandler._configured_rules()
            if r.rule_id == "cisco.remove.ethernet.oam"
        )

        rule.apply(doc)
        output = doc.render()

        self.assertNotIn("ethernet cfm\n domain", output)

    def test_cisco_bundle_bfd_on_physical_interface_is_removed(self):
        """非 Bundle-Ether 接口上的 BFD 配置（扁平化后残留）删除。"""
        doc = CiscoDocument((FIXTURES / "iosxr_bundle_bfd.cfg").read_text())
        rule = next(
            r
            for r in CleaningRulesHandler._configured_rules()
            if r.rule_id == "cisco.remove.interface.bundle.bfd"
        )

        rule.apply(doc)
        output = doc.render()

        # 物理口上的 BFD 被删除，Bundle-Ether 上的保留
        self.assertIn(
            "interface GigabitEthernet0/0/0/0\n description flattened-from-bundle\n"
            " ipv4 address 10.0.1.1 255.255.255.252\n",
            output,
        )
        self.assertIn("interface Bundle-Ether10\n bfd mode ietf\n", output)

    def test_cisco_service_policy_is_removed(self):
        """接口 QoS 挂载删除。"""
        doc = CiscoDocument((FIXTURES / "iosxr.cfg").read_text())
        rule = next(
            r
            for r in CleaningRulesHandler._configured_rules()
            if r.rule_id == "cisco.remove.interface.service.policy"
        )

        rule.apply(doc)
        output = doc.render()

        self.assertNotIn("service-policy", output)

    def test_cisco_router_ospf_bfd_is_preserved(self):
        """路由协议内的 BFD 配置不受影响。"""
        doc = CiscoDocument((FIXTURES / "iosxr.cfg").read_text())
        for rule in CleaningRulesHandler._configured_rules():
            if rule.enable:
                rule.apply(doc)
        output = doc.render()

        self.assertIn("router ospf 1", output)
        self.assertIn("bfd fast-detect", output)

    def test_junos_lldp_and_oam_protocols_are_removed(self):
        """protocols 下的 LLDP 和以太网 OAM 删除。"""
        doc = JunosDocument((FIXTURES / "junos.cfg").read_text())
        handler = CleaningRulesHandler._configured_rules()
        lldp_rule = next(r for r in handler if r.rule_id == "juniper.remove.protocols.link.discovery")
        oam_rule = next(r for r in handler if r.rule_id == "juniper.remove.protocols.ethernet.oam")

        lldp_rule.apply(doc)
        oam_rule.apply(doc)
        output = doc.render()

        self.assertNotIn("lldp", output)
        self.assertNotIn("lldp-med", output)
        self.assertNotIn("oam {", output)

    def test_junos_class_of_service_interfaces_are_removed(self):
        """class-of-service interfaces 挂载删除。"""
        doc = JunosDocument((FIXTURES / "junos.cfg").read_text())
        rule = next(
            r
            for r in CleaningRulesHandler._configured_rules()
            if r.rule_id == "juniper.remove.cos.interfaces"
        )

        rule.apply(doc)
        output = doc.render()

        self.assertIn("class-of-service {", output)
        self.assertIn("scheduler-maps {", output)
        self.assertNotIn("interfaces {", output.split("class-of-service {")[1].split("}")[0])

    def test_junos_ospf_bfd_is_preserved(self):
        """路由协议内的 BFD 配置不受影响。"""
        doc = JunosDocument((FIXTURES / "junos.cfg").read_text())
        for rule in CleaningRulesHandler._configured_rules():
            if rule.enable:
                rule.apply(doc)
        output = doc.render()

        self.assertIn("protocols {", output)
        self.assertIn("ospf {", output)
        self.assertIn("bfd-liveness-detection {", output)


class OptionalCleaningRuleCompletenessTest(unittest.TestCase):
    """验证可选规则开启后定义与引用一并删除，不残留悬空引用。"""

    def test_cisco_auth_reference_covers_md5_key_and_isis_passwords(self):
        output = apply_categories(
            CiscoDocument((FIXTURES / "iosxr_optional.cfg").read_text()),
            "cisco_iosxr",
            {"protocol-auth-definition", "protocol-auth-reference"},
        )
        self.assertNotIn("message-digest", output)
        self.assertNotIn("hello-password", output)
        self.assertNotIn("lsp-password", output)
        self.assertIn("   cost 10\n", output)
        self.assertIn("  address-family ipv4 unicast\n", output)

    def test_cisco_flow_statistics_removes_interface_references(self):
        output = apply_categories(
            CiscoDocument((FIXTURES / "iosxr_optional.cfg").read_text()),
            "cisco_iosxr",
            {"flow-statistics"},
        )
        self.assertNotIn("monitor-map", output)
        self.assertNotIn("sampler-map", output)
        self.assertNotIn("flow ipv4 monitor", output)
        self.assertNotIn("monitor-session", output)
        self.assertIn(" ipv4 address 10.0.0.1 255.255.255.0\n", output)

    def test_junos_isis_hello_authentication_is_removed(self):
        output = apply_categories(
            JunosDocument((FIXTURES / "junos_optional.cfg").read_text()),
            "juniper_junos",
            {"protocol-auth-reference"},
        )
        self.assertNotIn("hello-authentication", output)
        self.assertIn("metric 10;", output)

    def test_junos_flow_statistics_removes_instance_references_and_empty_blocks(self):
        output = apply_categories(
            JunosDocument((FIXTURES / "junos_optional.cfg").read_text()),
            "juniper_junos",
            {"flow-statistics"},
        )
        self.assertNotIn("sampling", output)
        self.assertNotIn("forwarding-options", output)
        self.assertNotIn("fpc 0", output)
        self.assertIn("address 10.0.0.1/24;", output)

    def test_junos_hardware_keeps_aggregated_devices(self):
        output = apply_categories(
            JunosDocument((FIXTURES / "junos_optional.cfg").read_text()),
            "juniper_junos",
            {"hardware"},
        )
        self.assertIn("device-count 4;", output)
        self.assertNotIn("fpc 0", output)


if __name__ == "__main__":
    unittest.main()
