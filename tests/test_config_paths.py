"""验证随程序发布的规则 YAML 路径统一由公共访问器解析。"""

from __future__ import annotations

import unittest
from pathlib import Path

from impl.common.support.config_paths import (
    RuleYamlFile,
    rule_pack_dir,
    rule_yaml_path,
)

PROJECT_ROOT = Path(__file__).parents[1]


class RuleYamlPathTest(unittest.TestCase):

    def test_rule_yaml_accessors_accept_vendor_dir_keyword(self):
        expected_dir = PROJECT_ROOT / "config" / "rules" / "cisco" / "xrv9000"

        self.assertEqual(
            rule_pack_dir(vendor_dir="cisco", image="xrv9000"),
            expected_dir,
        )
        self.assertEqual(
            rule_yaml_path(
                vendor_dir="cisco",
                image="xrv9000",
                rule_file=RuleYamlFile.CLEANING,
            ),
            expected_dir / "cleaning.yaml",
        )

    def test_rule_yaml_accessors_resolve_existing_vendor_files(self):
        cases = (
            ("cisco", "xrv9000"),
            ("juniper", "vmx"),
        )
        rule_files = (
            (RuleYamlFile.MANIFEST, "manifest.yaml"),
            (RuleYamlFile.CLEANING, "cleaning.yaml"),
            (
                RuleYamlFile.INPUT_NORMALIZATION,
                "input_normalization.yaml",
            ),
            (RuleYamlFile.EMPTY_BLOCKS, "empty_blocks.yaml"),
        )

        for vendor, image in cases:
            expected_dir = PROJECT_ROOT / "config" / "rules" / vendor / image
            with self.subTest(vendor=vendor, image=image):
                self.assertEqual(rule_pack_dir(vendor, image), expected_dir)
                self.assertTrue(expected_dir.is_dir())
            for rule_file, filename in rule_files:
                with self.subTest(
                    vendor=vendor,
                    image=image,
                    rule_file=rule_file,
                ):
                    path = rule_yaml_path(vendor, image, rule_file)
                    self.assertEqual(path, expected_dir / filename)
                    self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
