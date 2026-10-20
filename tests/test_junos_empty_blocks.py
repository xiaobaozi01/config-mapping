"""验证 vMX 空块 YAML 的结构和完整路径约束。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from config_adaptor.common.empty_blocks import EmptyBlockPolicy as CommonEmptyBlockPolicy
from config_adaptor.juniper.empty_blocks import (
    EmptyBlockPolicy,
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
        self.assertIs(EmptyBlockPolicy, CommonEmptyBlockPolicy)
        self.assertIs(policy, vmx_empty_block_policy())

    def test_vendor_wrapper_rejects_wrong_target_and_foreign_actions(self):
        base = {
            "schema_version": 1,
            "target": {"vendor": "juniper_junos", "image": "vmx"},
            "default_when_empty": "semicolon",
            "rules": [],
        }
        invalid = [
            {"target": {"vendor": "cisco_iosxr", "image": "vmx"}},
            {"target": {"vendor": "juniper_junos", "image": "xrv9000"}},
            {"default_when_empty": "keep"},
            {"rules": [{"path": ["**"], "action": "keep"}]},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            for change in invalid:
                with self.subTest(change=change):
                    path.write_text(yaml.safe_dump(base | change), encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
