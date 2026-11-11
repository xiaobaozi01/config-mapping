"""转换入口跳过源语义审计，继续拒绝映射完整性失败。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from gns3_config_adaptor.adaptation.service import convert

FIXTURES = Path(__file__).parent / "fixtures"


class CoreInterfacePipelineTest(unittest.TestCase):

    def test_input_loading_failure_invalidates_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            topology = FIXTURES / "iosxr_bundle" / "topology.xlsx"
            configs = FIXTURES / "iosxr_bundle" / "configs"
            self.assertFalse(convert(topology, configs, output).has_errors)
            profile = root / "invalid.yaml"
            profile.write_text(
                "profiles:\n  cisco_iosxr:\n    interfaces: [Gi0/0/0/0]\n"
            )
            with self.assertRaises(ValueError):
                convert(topology, configs, output, profiles_path=profile)
            self.assertFalse((output / "configs" / "R1.cfg").exists())
            self.assertFalse((output / "topology-adapted.xlsx").exists())
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(
                any("输入加载" in error for error in report["errors"])
            )

    def test_leaf_symlinks_never_overwrite_external_files(self):
        paths = (
            "configs/R1.cfg",
            "topology-adapted.xlsx",
            "README.txt",
            ".generated-artifacts.json",
            "report.json",
            "interface-mapping.json",
            "performance.json",
        )
        for relative in paths:
            with (
                self.subTest(path=relative),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                output = root / "output"
                (output / "configs").mkdir(parents=True)
                external = root / "user-owned-file"
                external.write_text("do not overwrite")
                (output / relative).symlink_to(external)
                context = convert(
                    FIXTURES / "iosxr_bundle" / "topology.xlsx",
                    FIXTURES / "iosxr_bundle" / "configs",
                    output,
                )
                self.assertFalse(context.has_errors)
                self.assertEqual(external.read_text(), "do not overwrite")
                self.assertFalse((output / relative).is_symlink())

    def test_parent_symlink_cannot_redirect_generated_configs_outside_output(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            external = root / "user-owned-folder"
            external.mkdir()
            (output / "configs").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "路径不安全"):
                convert(
                    FIXTURES / "iosxr_bundle" / "topology.xlsx",
                    FIXTURES / "iosxr_bundle" / "configs",
                    output,
                )
            self.assertEqual(list(external.iterdir()), [])
            self.assertEqual(
                json.loads((output / "report.json").read_text())["status"],
                "failed",
            )

    def test_failed_rerun_archives_generated_outputs_and_preserves_user_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = root / "input"
            shutil.copytree(FIXTURES / "iosxr_bundle" / "configs", configs)
            output = root / "output"
            topology = FIXTURES / "iosxr_bundle" / "topology.xlsx"
            first = convert(topology, configs, output)
            self.assertFalse(first.has_errors)
            old_config = (output / "configs" / "R1.cfg").read_bytes()
            user_file = output / "configs" / "user-notes.cfg"
            user_file.write_text("user owned content")
            (configs / "R1.cfg").write_text("end-group\n")

            failed = convert(topology, configs, output)

            self.assertTrue(failed.has_errors)
            self.assertFalse((output / "configs" / "R1.cfg").exists())
            self.assertFalse((output / "configs" / "R2.cfg").exists())
            self.assertFalse((output / "topology-adapted.xlsx").exists())
            self.assertFalse((output / "README.txt").exists())
            self.assertEqual(user_file.read_text(), "user owned content")
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            archived = next(
                event
                for event in report["events"]
                if event["kind"] == "previous-output-archived"
            )
            archive = output / archived["archive"]
            self.assertEqual(
                (archive / "configs" / "R1.cfg").read_bytes(), old_config
            )
            self.assertTrue((archive / "topology-adapted.xlsx").is_file())

    def test_source_semantic_conflicts_are_not_audited_even_in_strict_mode(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = root / "configs"
            shutil.copytree(FIXTURES / "iosxr_bundle" / "configs", configs)
            config = (
                FIXTURES
                / "interface_compatibility"
                / "iosxr_scalar_conflicts.cfg"
            )
            shutil.copyfile(config, configs / "R1.cfg")
            shutil.copyfile(config, configs / "R2.cfg")
            topology = FIXTURES / "iosxr_bundle" / "topology.xlsx"
            workbook = load_workbook(topology)
            workbook["链接表"].delete_rows(3)
            topology = root / "single-link.xlsx"
            workbook.save(topology)

            default_output = root / "output"
            context = convert(topology, configs, default_output)

            self.assertFalse(context.has_errors)
            warnings = context.warnings + context.devices["R1"].warnings
            self.assertFalse(
                any("mtu 存在冲突" in warning for warning in warnings)
            )
            report = json.loads((default_output / "report.json").read_text())
            self.assertEqual(report["status"], "success")
            self.assertFalse(
                any(
                    "mtu 存在冲突" in event["message"]
                    for event in report["events"]
                )
            )
            self.assertTrue((default_output / "configs" / "R1.cfg").exists())

            strict_output = root / "strict-output"
            context = convert(
                topology,
                configs,
                strict_output,
                strict_input_validation=True,
            )

            self.assertFalse(context.has_errors)
            errors = context.errors + context.devices["R1"].errors
            warnings = context.warnings + context.devices["R1"].warnings
            self.assertFalse(
                any("mtu 存在冲突" in message for message in errors + warnings)
            )
            self.assertTrue((strict_output / "configs").exists())
            self.assertTrue((strict_output / "topology-adapted.xlsx").exists())

    def test_unknown_removed_interface_dependency_is_warned_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = root / "configs"
            shutil.copytree(FIXTURES / "iosxr_bundle" / "configs", configs)
            config_path = configs / "R1.cfg"
            config_path.write_text(
                config_path.read_text().replace(
                    "\nend\n",
                    "\ninterface GigabitEthernet0/0/0/99\n"
                    " ipv4 address 203.0.113.1 255.255.255.0\n"
                    "!\n"
                    "custom-service FOO\n"
                    " bind-interface GigabitEthernet0/0/0/99\n"
                    "!\n"
                    "end\n",
                    1,
                )
            )
            output = root / "output"
            context = convert(
                FIXTURES / "iosxr_bundle" / "topology.xlsx",
                configs,
                output,
            )

            self.assertFalse(context.has_errors)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "success")
            converted = (output / "configs" / "R1.cfg").read_text()
            self.assertNotIn(
                "bind-interface GigabitEthernet0/0/0/99", converted
            )
            self.assertIn("203.0.113.1", converted)
            self.assertIn(
                "custom-service FOO",
                (output / "configs" / "R1.cfg").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
