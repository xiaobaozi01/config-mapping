"""验证 vMX 空块 YAML 的结构和完整路径约束。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config_adaptor.juniper.empty_blocks import (
    load_empty_block_policy,
    vmx_empty_block_policy,
)


class JunosEmptyBlockRulesTest(unittest.TestCase):
    def test_vmx_empty_block_policy_is_explicit(self):
        policy = vmx_empty_block_policy()
        self.assertEqual(policy.default, "semicolon")
        self.assertEqual(policy.action_for(("security",)), "omit")
        self.assertEqual(policy.action_for(("system", "services")), "omit")
        self.assertEqual(policy.action_for(("system", "login")), "omit")
        self.assertEqual(policy.action_for(("interfaces", "ge-0/0/0")), "semicolon")

    def test_invalid_empty_block_paths_fail_fast(self):
        entries = (
            "rules: security",
            "rules: [{path: [], action: omit}]",
            "rules: [{path: [security], action: omit}, {path: [security], action: semicolon}]",
            "rules: [{path: [security], action: invalid}]",
            "rules: [{path: ['^(security$'], action: omit}]",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            for entry in entries:
                with self.subTest(entry=entry):
                    path.write_text(
                        "schema_version: 1\n"
                        "target: {vendor: juniper_junos, image: vmx}\n"
                        "default_when_empty: semicolon\n"
                        + entry + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        load_empty_block_policy(path)

    def test_yaml_can_override_default_with_semicolon_or_braces(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            path.write_text(
                "schema_version: 1\n"
                "target: {vendor: juniper_junos, image: vmx}\n"
                "default_when_empty: braces\n"
                "rules: [{path: [interfaces, ge-0/0/0], action: semicolon}, "
                "{path: [security], action: omit}]\n",
                encoding="utf-8",
            )
            policy = load_empty_block_policy(path)
        self.assertEqual(policy.action_for(("interfaces", "ge-0/0/0")), "semicolon")
        self.assertEqual(policy.action_for(("security",)), "omit")
        self.assertEqual(policy.action_for(("system", "services")), "braces")

    def test_paths_support_regex_and_wildcards_like_cleaning_rules(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            path.write_text(
                "schema_version: 1\n"
                "target: {vendor: juniper_junos, image: vmx}\n"
                "default_when_empty: semicolon\n"
                "rules:\n"
                "  - {path: ['^interfaces$', '^ge-', '^unit \\d+$', '^family inet$'], action: braces}\n"
                "  - {path: ['^protocols$', '*'], action: omit}\n"
                "  - {path: ['^routing-instances$', '**', '^bgp$'], action: omit}\n",
                encoding="utf-8",
            )
            policy = load_empty_block_policy(path)
        cases = {
            ("interfaces", "ge-0/0/1", "unit 10", "family inet"): "braces",
            ("interfaces", "xe-0/0/1", "unit 10", "family inet"): "semicolon",
            ("interfaces", "ge-0/0/1", "unit 10", "family inet6"): "semicolon",
            ("protocols", "bgp"): "omit",
            ("protocols", "bgp", "group G"): "semicolon",
            ("routing-instances", "VRF", "protocols", "bgp"): "omit",
            ("routing-instances", "bgp"): "omit",
            ("SYSTEM", "SERVICES"): "semicolon",
        }
        for block_path, action in cases.items():
            with self.subTest(path=block_path):
                self.assertEqual(policy.action_for(block_path), action)

    def test_first_matching_rule_wins(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            path.write_text(
                "schema_version: 1\n"
                "target: {vendor: juniper_junos, image: vmx}\n"
                "default_when_empty: semicolon\n"
                "rules:\n"
                "  - {path: ['^system$', '^login$'], action: braces}\n"
                "  - {path: ['^system$', '*'], action: omit}\n",
                encoding="utf-8",
            )
            policy = load_empty_block_policy(path)
        self.assertEqual(policy.action_for(("system", "login")), "braces")
        self.assertEqual(policy.action_for(("system", "services")), "omit")


if __name__ == "__main__":
    unittest.main()
