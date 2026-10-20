"""验证空块策略公共加载器的结构校验、完整路径与规则优先级。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from config_adaptor.common.empty_blocks import EmptyBlockPolicy, load_empty_block_policy


class EmptyBlockPolicyTest(unittest.TestCase):
    def load_policy(self, path):
        return load_empty_block_policy(
            path, vendor="test_vendor", image="test_image", actions=("omit", "keep")
        )

    def test_invalid_rules_fail_fast(self):
        base = {
            "schema_version": 1,
            "target": {"vendor": "test_vendor", "image": "test_image"},
            "default_when_empty": "keep",
            "rules": [],
        }
        invalid = [
            {"schema_version": 2},
            {"target": {"vendor": "test_vendor", "image": "wrong_image"}},
            {"schema_version": True},
            {"target": {"vendor": "wrong_vendor", "image": "test_image"}},
            {"default_when_empty": "semicolon"},
            {"default_when_empty": []},
            {"rules": "invalid"},
            {"rules": [{"path": [], "action": "omit"}]},
            {"rules": [{"path": [" "], "action": "omit"}]},
            {"rules": [{"path": [1], "action": "omit"}]},
            {"rules": [{"path": ["["], "action": "omit"}]},
            {"rules": [{"path": ["*"], "action": "braces"}]},
            {"rules": [{"path": ["*"], "action": []}]},
            {"rules": [{"path": ["*"]}]},
            {"rules": [{"path": ["*"], "action": "keep"}] * 2},
            {"extra": True},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            for change in invalid:
                with self.subTest(change=change):
                    path.write_text(yaml.safe_dump(base | change), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.load_policy(path)

            for payload in (
                None, [], "invalid",
                {key: value for key, value in base.items() if key != "rules"},
            ):
                with self.subTest(payload=payload):
                    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.load_policy(path)

    def test_paths_support_regex_and_wildcards_like_cleaning_rules(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            path.write_text(
                "schema_version: 1\n"
                "target: {vendor: test_vendor, image: test_image}\n"
                "default_when_empty: keep\n"
                "rules:\n"
                "  - {path: ['^interfaces$', '^ge-', '^unit \\d+$', '^family inet$'], action: omit}\n"
                "  - {path: ['^protocols$', '*'], action: omit}\n"
                "  - {path: ['^routing-instances$', '**', '^bgp$'], action: omit}\n",
                encoding="utf-8",
            )
            policy = self.load_policy(path)
        cases = {
            ("interfaces", "ge-0/0/1", "unit 10", "family inet"): "omit",
            ("INTERFACES", "GE-0/0/1", "UNIT 10", "FAMILY INET"): "omit",
            ("interfaces", "xe-0/0/1", "unit 10", "family inet"): "keep",
            ("interfaces", "ge-0/0/1", "unit 10", "family inet6"): "keep",
            ("protocols", "bgp"): "omit",
            ("protocols", "bgp", "group G"): "keep",
            ("routing-instances", "VRF", "protocols", "bgp"): "omit",
            ("routing-instances", "bgp"): "omit",
            ("SYSTEM", "SERVICES"): "keep",
        }
        for block_path, action in cases.items():
            with self.subTest(path=block_path):
                self.assertEqual(policy.action_for(block_path), action)

    def test_first_matching_rule_wins(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty_blocks.yaml"
            path.write_text(
                "schema_version: 1\n"
                "target: {vendor: test_vendor, image: test_image}\n"
                "default_when_empty: omit\n"
                "rules:\n"
                "  - {path: ['^system$', '^login$'], action: keep}\n"
                "  - {path: ['^system$', '*'], action: omit}\n",
                encoding="utf-8",
            )
            policy = self.load_policy(path)
        self.assertIsInstance(policy, EmptyBlockPolicy)
        self.assertEqual(policy.action_for(("system", "login")), "keep")
        self.assertEqual(policy.action_for(("system", "services")), "omit")


if __name__ == "__main__":
    unittest.main()
