"""验证五类 YAML 资源入口及 identity 规则包的加载边界。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import yaml

from gns3_config_adaptor.adaptation import profiles
from gns3_config_adaptor.adaptation.models import Vendor
from gns3_config_adaptor.impl.common.semantic_identity import load_rule_pack
from gns3_config_adaptor.impl.common.support.config_paths import (
    cleaning_rules_path,
    empty_blocks_path,
    identity_rules_path,
    image_profiles_path,
    input_normalization_path,
)

PROJECT_ROOT = Path(__file__).parents[1]


class ResourcePathTest(unittest.TestCase):

    def test_resource_accessors_resolve_vendor_files_and_identity_directory(
        self,
    ):
        for vendor, image in (
            ("cisco_iosxr", "xrv9000"),
            ("juniper_junos", "vmx"),
        ):
            directory = PROJECT_ROOT / "config" / "rules" / vendor / image
            for accessor, filename in (
                (cleaning_rules_path, "cleaning.yaml"),
                (input_normalization_path, "input_normalization.yaml"),
                (empty_blocks_path, "empty_blocks.yaml"),
            ):
                with self.subTest(vendor=vendor, resource=filename):
                    path = accessor(vendor=vendor)
                    self.assertIsInstance(path, Path)
                    self.assertEqual(path, directory / filename)
                    self.assertTrue(path.is_file())
            self.assertEqual(identity_rules_path(vendor=vendor), directory)
            self.assertTrue(identity_rules_path(vendor).is_dir())
            rules = load_rule_pack(directory, vendor=vendor, image=image)
            self.assertTrue(rules)
        self.assertEqual(
            image_profiles_path(), PROJECT_ROOT / "config/image_profiles.yaml"
        )
        self.assertTrue(image_profiles_path().is_file())

    def test_unknown_vendors_have_no_default_resource_paths(self):
        for vendor in ("huawei", "unknown"):
            for accessor in (
                cleaning_rules_path,
                input_normalization_path,
                empty_blocks_path,
                identity_rules_path,
            ):
                with self.subTest(vendor=vendor, accessor=accessor):
                    with self.assertRaisesRegex(ValueError, "没有可用目标镜像"):
                        accessor(vendor)

    def test_builtin_paths_do_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            for vendor, image in (
                ("cisco_iosxr", "xrv9000"),
                ("juniper_junos", "vmx"),
            ):
                root = PROJECT_ROOT / "config" / "rules" / vendor / image
                self.assertEqual(identity_rules_path(vendor), root)
                for accessor, filename in (
                    (cleaning_rules_path, "cleaning.yaml"),
                    (input_normalization_path, "input_normalization.yaml"),
                    (empty_blocks_path, "empty_blocks.yaml"),
                ):
                    self.assertEqual(accessor(vendor), root / filename)
            self.assertEqual(
                image_profiles_path(),
                PROJECT_ROOT / "config/image_profiles.yaml",
            )

    def test_profile_custom_paths_are_passed_through_without_resolving(self):
        for path in (Path("custom.yaml"), Path("/tmp/custom.yml")):
            self.assertIs(image_profiles_path(path), path)

    def test_profile_defaults_do_not_read_the_bundled_yaml(self):
        with patch.object(profiles, "image_profiles_path") as locate:
            loaded = profiles.load_profiles(None)
        locate.assert_not_called()
        for vendor, profile in loaded.items():
            self.assertEqual(
                profile.interfaces, profiles.DEFAULT_INTERFACES[vendor]
            )

    def test_explicit_profile_path_still_loads_custom_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.yaml"
            path.write_text(
                "profiles:\n  cisco_iosxr:\n    image: custom-image\n",
                encoding="utf-8",
            )
            with patch.object(
                profiles, "image_profiles_path", wraps=image_profiles_path
            ) as locate:
                loaded = profiles.load_profiles(path)
            locate.assert_called_once_with(path)
            self.assertEqual(loaded[Vendor.CISCO_IOSXR].image, "custom-image")


class ManifestYamlPathTest(unittest.TestCase):

    def test_custom_manifest_controls_rule_file_selection_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_manifest(root, ["second.yml", "first.yaml"])
            for rule_id, filename in (
                ("first", "first.yaml"),
                ("second", "second.yml"),
                ("unlisted", "unlisted.yaml"),
            ):
                payload = {
                    "rules": [
                        {
                            "id": rule_id,
                            "node": "leaf",
                            "path": [],
                            "statement": [rule_id],
                            "identity": [rule_id],
                            "merge": "replace",
                        }
                    ]
                }
                (root / filename).write_text(
                    yaml.safe_dump(payload), encoding="utf-8"
                )
            rules = load_rule_pack(root, vendor="cisco_iosxr", image="xrv9000")
            self.assertEqual(
                [rule.rule_id for rule in rules], ["second", "first"]
            )

    def test_invalid_manifest_paths_are_rejected_by_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in (
                "../outside.yaml",
                "nested/rules.yml",
                "/tmp/outside.yaml",
                "rules.json",
                "rules.YAML",
                "rules",
                "",
            ):
                with self.subTest(filename=filename):
                    self.write_manifest(root, [filename])
                    with self.assertRaisesRegex(ValueError, "非法规则文件路径"):
                        load_rule_pack(root)

    def test_missing_rule_file_still_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_manifest(root, ["missing.yaml"])
            with self.assertRaises(FileNotFoundError):
                load_rule_pack(root)

    @staticmethod
    def write_manifest(root, filenames):
        manifest = {
            "schema_version": 1,
            "target": {"vendor": "cisco_iosxr", "image": "xrv9000"},
            "rule_files": filenames,
        }
        (root / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
