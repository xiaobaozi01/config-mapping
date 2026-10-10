"""验证统一清洗规则的加载、路径语义和厂商树执行。"""

from __future__ import annotations

import unittest

from config_adaptor.adaptation.cleaning_rules import (
    AuthenticationCleaningRule,
    CiscoBannerCleaningRule,
    CleaningPoint,
    CleaningRuleError,
    CleaningRulesHandler,
)
from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.common.cleaning import matches_cleaning_path
from config_adaptor.juniper.document import JunosDocument


class CleaningPathTest(unittest.TestCase):
    def test_empty_star_and_double_star_depth_semantics(self):
        self.assertTrue(matches_cleaning_path((), ()))
        self.assertFalse(matches_cleaning_path(("router ospf 1",), ()))
        self.assertTrue(matches_cleaning_path(("interfaces",), ("*",)))
        self.assertFalse(matches_cleaning_path(("interfaces", "ge-0/0/0"), ("*",)))
        self.assertTrue(matches_cleaning_path((), ("**",)))
        self.assertTrue(
            matches_cleaning_path(
                ("router ospf 1", "area 0", "interface Gi0/0/0/0"),
                (r"^router\s+ospf\b", "**"),
            )
        )

    def test_concrete_four_level_cisco_path(self):
        document = CiscoDocument(
            "router bgp 65000\n"
            " vrf CUST-A\n"
            "  address-family ipv4 unicast\n"
            "   maximum-paths ebgp 8\n"
            "!\n"
        )
        hits = document.apply_cleaning_rule(
            (
                r"^router\s+bgp\s+65000$",
                r"^vrf\s+CUST-A$",
                r"^address-family\s+ipv4\s+unicast$",
            ),
            r"^maximum-paths\s+ebgp\b",
            "delete",
            None,
        )
        self.assertEqual(hits, 1)
        self.assertNotIn("maximum-paths", document.render())

    def test_junos_path_uses_normalized_headers(self):
        document = JunosDocument(
            "interfaces {\n"
            "    ge-0/0/0 {\n"
            "        unit 0 {\n"
            "            family inet {\n"
            "                sampling { input; }\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        hits = document.apply_cleaning_rule(
            (r"^interfaces$", "**"),
            r"^sampling$",
            "delete",
            None,
        )
        self.assertEqual(hits, 1)
        self.assertNotIn("sampling", document.render())


class CleaningRuleLoadingTest(unittest.TestCase):
    def test_default_rules_are_split_by_target_vendor(self):
        (cisco_path, cisco_vendor), (juniper_path, juniper_vendor) = (
            CleaningRulesHandler._RULE_SOURCES
        )
        self.assertEqual(cisco_vendor.value, "cisco_iosxr")
        self.assertEqual(juniper_vendor.value, "juniper_junos")
        self.assertEqual(cisco_path.parts[-4:], ("cisco", "rules", "xrv9000", "cleaning.yaml"))
        self.assertEqual(juniper_path.parts[-4:], ("juniper", "rules", "vmx", "cleaning.yaml"))
        rules = CleaningRulesHandler._configured_rules()
        self.assertTrue(
            all(rule.vendor == "cisco_iosxr" for rule in rules if rule.rule_id.startswith("cisco."))
        )
        self.assertTrue(
            all(rule.vendor == "juniper_junos" for rule in rules if rule.rule_id.startswith("juniper."))
        )

    def test_default_rules_include_enabled_ptp_and_disabled_optional_rules(self):
        rules = CleaningRulesHandler._configured_rules()
        self.assertTrue(next(rule for rule in rules if rule.rule_id == "cisco.remove.ptp.interface").enable)
        self.assertFalse(next(rule for rule in rules if rule.rule_id == "cisco.remove.pki").enable)
        self.assertIn("juniper.remove.hardware", {rule.rule_id for rule in rules})

    def test_vendor_prefix_is_added_to_local_id_with_supported_separators(self):
        rule = CleaningRulesHandler._parse_rule(
            {
                "id": "remove_phone-home.rule_2-test",
                "vendor": "juniper_junos",
                "enable": True,
                "category": "phone-home",
                "path": [],
                "match": "^phone-home$",
                "action": "delete",
                "reason": "test",
            },
            set(),
        )
        self.assertEqual(rule.rule_id, "juniper.remove_phone-home.rule_2-test")

    def test_enable_and_path_are_required_and_validated(self):
        with self.assertRaisesRegex(ValueError, "enable 必须是布尔值"):
            CleaningRulesHandler._parse_rule(
                {
                    "id": "invalid",
                    "vendor": "cisco_iosxr",
                    "category": "test",
                    "path": [],
                    "match": "^test$",
                    "action": "delete",
                    "reason": "test",
                },
                set(),
            )


class ProgrammaticCleaningRuleTest(unittest.TestCase):
    def test_banner_rule_deactivates_complete_ranges(self):
        document = CiscoDocument(
            "banner exec ^\n"
            "interface FutureBanner0\n"
            "= warning =\n"
            "^\n"
            "banner login ^C\n"
            "second warning\n"
            "^C trailing text\n"
            "hostname XR\n"
        )

        rule = CiscoBannerCleaningRule()
        result = rule.apply(document)

        self.assertEqual(rule.point, CleaningPoint.PRE_ANALYSIS)
        self.assertEqual(result.changed, 2)
        self.assertEqual(result.removed_by_type, {"banner": 2})
        self.assertEqual(document.render(), "hostname XR\n")

    def test_banner_rule_rejects_missing_terminator(self):
        document = CiscoDocument("banner exec ^\nunterminated\nhostname XR\n")
        with self.assertRaisesRegex(CleaningRuleError, "缺少结束分隔符"):
            CiscoBannerCleaningRule().apply(document)

    def test_authentication_rule_uses_post_rewrite_point(self):
        rule = AuthenticationCleaningRule(
            rule_id="cisco.remove.authentication",
            vendor="cisco_iosxr",
        )
        document = CiscoDocument("hostname XR\nusername old\n secret 0 old\n!\nend\n")

        result = rule.apply(document)

        self.assertEqual(rule.point, CleaningPoint.POST_REWRITE)
        self.assertEqual(rule.action, "delete")
        self.assertEqual(result.changed, 1)
        self.assertEqual(result.removed_by_type, {"username": 1})
        self.assertNotIn("username old", document.render())
        self.assertIn("hostname XR", document.render())
        self.assertIn("end", document.render())


if __name__ == "__main__":
    unittest.main()
