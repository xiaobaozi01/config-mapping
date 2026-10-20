"""验证 XRv9000 空块策略校验与递归渲染。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.common.empty_blocks import EmptyBlockPolicy as CommonEmptyBlockPolicy
from config_adaptor.cisco.empty_blocks import (
    EmptyBlockPolicy,
    load_empty_block_policy,
    xrv9000_empty_block_policy,
)


class CiscoEmptyBlockRulesTest(unittest.TestCase):
    def test_default_policy_preserves_empty_blocks(self):
        policy = xrv9000_empty_block_policy()
        self.assertEqual(policy.default, "keep")
        self.assertEqual(policy.action_for(("interface Loopback0",)), "keep")
        self.assertIs(EmptyBlockPolicy, CommonEmptyBlockPolicy)
        self.assertIs(policy, xrv9000_empty_block_policy())

    def test_vendor_wrapper_rejects_wrong_target_and_foreign_actions(self):
        base = {
            "schema_version": 1,
            "target": {"vendor": "cisco_iosxr", "image": "xrv9000"},
            "default_when_empty": "keep",
            "rules": [],
        }
        invalid = [
            {"target": {"vendor": "juniper_junos", "image": "xrv9000"}},
            {"target": {"vendor": "cisco_iosxr", "image": "vmx"}},
        ]
        for action in ("semicolon", "braces"):
            invalid.extend([
                {"default_when_empty": action},
                {"rules": [{"path": ["**"], "action": action}]},
            ])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            for change in invalid:
                with self.subTest(change=change):
                    path.write_text(yaml.safe_dump(base | change), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_empty_block_policy(path)

    def test_paths_and_first_matching_rule(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            path.write_text(
                "schema_version: 1\n"
                "target: {vendor: cisco_iosxr, image: xrv9000}\n"
                "default_when_empty: keep\n"
                "rules:\n"
                "  - {path: ['^router ospf CORE$', '^area 0$'], action: keep}\n"
                "  - {path: ['^router ospf\\b', '*'], action: omit}\n"
                "  - {path: ['^router bgp\\b', '**', '^address-family ipv4 unicast$'], action: omit}\n",
                encoding="utf-8",
            )
            policy = load_empty_block_policy(path)
        for block_path, action in (
            (("ROUTER OSPF CORE", "area 0"), "keep"),
            (("router ospf CORE", "area 1"), "omit"),
            (("router ospf CORE", "area 1", "interface Loopback0"), "keep"),
            (("router bgp 65000", "address-family ipv4 unicast"), "omit"),
            (("router bgp 65000", "vrf BLUE", "address-family ipv4 unicast"), "omit"),
            (("address-family ipv4 unicast",), "keep"),
        ):
            with self.subTest(path=block_path):
                self.assertEqual(policy.action_for(block_path), action)


class CiscoEmptyBlockRenderingTest(unittest.TestCase):
    def render_with_policy(self, document, default="omit", rules=()):
        with patch(
            "config_adaptor.cisco.document.xrv9000_empty_block_policy",
            return_value=EmptyBlockPolicy(default, rules),
        ):
            return document.render()

    def test_default_preserves_original_empty_blocks_and_leaf_commands(self):
        source = "hostname XR\n!\ninterface Loopback0\n!\nrouter ospf CORE\n!\nend\n"
        self.assertEqual(CiscoDocument(source).render(), source)

    def test_omit_empty_blocks_preserves_leaf_commands_and_collapses_separators(self):
        document = CiscoDocument(
            "hostname XR\n!\ninterface Loopback0\n!\nrouter ospf CORE\n!\nend\n"
        )
        self.assertEqual(self.render_with_policy(document), "hostname XR\n!\nend\n")

    def test_cleaned_descendants_make_ancestors_empty_without_mutating_ast(self):
        document = CiscoDocument(
            "router ospf CORE\n area 0\n  interface Loopback0\n"
            "   authentication key-chain PROD\n  !\n !\n!\nend\n"
        )
        document.apply_cleaning_rule(("**",), "^authentication", "delete", None)
        self.assertEqual(self.render_with_policy(document), "!\nend\n")
        # 策略仅控制输出，后续仍可修改或以另一策略渲染原树。
        self.assertTrue(document.root.children[0].active)
        self.assertIn("area 0", document.render())

    def test_comments_and_formatting_do_not_keep_empty_block(self):
        source = "interface Loopback0\n ! note\n\n !\n!\nend\n"
        document = CiscoDocument(source)
        self.assertEqual(document.render(), source)
        self.assertEqual(self.render_with_policy(document), "!\nend\n")

    def test_path_override_keeps_empty_child_and_its_parent(self):
        document = CiscoDocument(
            "router ospf CORE\n area 0\n  interface Loopback0\n   cost 10\n"
            "  !\n !\n!\nend\n"
        )
        document.apply_cleaning_rule(("**",), "^cost", "delete", None)
        rules = ((("^router ospf CORE$", "^area 0$", "^interface Loopback0$"), "keep"),)
        rendered = self.render_with_policy(document, rules=rules)
        self.assertIn("router ospf CORE\n area 0\n  interface Loopback0\n", rendered)
        self.assertNotIn("cost", rendered)

    def test_omit_rule_only_affects_matching_empty_blocks(self):
        document = CiscoDocument(
            "router ospf CORE\n area 0\n  authentication key-chain PROD\n !\n"
            " area 1\n  interface Loopback0\n !\n!\nend\n"
        )
        document.apply_cleaning_rule(("**",), "^authentication", "delete", None)
        rules = ((("^router ospf CORE$", "*"), "omit"),)
        rendered = self.render_with_policy(document, default="keep", rules=rules)
        self.assertNotIn("area 0", rendered)
        self.assertIn(" area 1\n  interface Loopback0\n", rendered)


if __name__ == "__main__":
    unittest.main()
