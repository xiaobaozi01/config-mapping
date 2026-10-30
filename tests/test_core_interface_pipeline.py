"""转换入口不会把未知接入依赖或已知接口冲突输出成成功配置。"""

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from config_adaptor.adaptation.service import convert
from openpyxl import load_workbook


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
            profile.write_text("profiles:\n  cisco_iosxr:\n    interfaces: [Gi0/0/0/0]\n")
            with self.assertRaises(ValueError):
                convert(topology, configs, output, profiles_path=profile)
            self.assertFalse((output / "configs" / "R1.cfg").exists())
            self.assertFalse((output / "topology-adapted.xlsx").exists())
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("输入加载" in error for error in report["errors"]))

    def test_leaf_symlinks_never_overwrite_external_files(self):
        paths = (
            "configs/R1.cfg", "topology-adapted.xlsx", "README.txt",
            ".generated-artifacts.json", "report.json", "interface-mapping.json",
        )
        for relative in paths:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                (output / "configs").mkdir(parents=True)
                external = root / "user-owned-file"
                external.write_text("do not overwrite")
                (output / relative).symlink_to(external)
                context = convert(
                    FIXTURES / "iosxr_bundle" / "topology.xlsx",
                    FIXTURES / "iosxr_bundle" / "configs", output,
                )
                self.assertFalse(context.has_errors)
                self.assertEqual(external.read_text(), "do not overwrite")
                self.assertFalse((output / relative).is_symlink())

    def test_parent_symlink_cannot_redirect_generated_configs_outside_output(self):
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
                    FIXTURES / "iosxr_bundle" / "configs", output,
                )
            self.assertEqual(list(external.iterdir()), [])
            self.assertEqual(json.loads((output / "report.json").read_text())["status"], "failed")

    def test_failed_rerun_archives_generated_outputs_and_preserves_user_files(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                configs = root / "input"
                shutil.copytree(FIXTURES / "iosxr_bundle" / "configs", configs)
                output = root / "output"
                topology = FIXTURES / "iosxr_bundle" / "topology.xlsx"
                first = convert(topology, configs, output)
                self.assertFalse(first.has_errors)
                old_config = (output / "configs" / "R1.cfg").read_bytes()
                if legacy:
                    (output / ".generated-artifacts.json").unlink()
                user_file = output / "configs" / "user-notes.cfg"
                user_file.write_text("user owned content")
                shutil.copyfile(FIXTURES / "core_pruning" / "iosxr_unknown.cfg", configs / "R1.cfg")

                failed = convert(topology, configs, output)

                self.assertTrue(failed.has_errors)
                self.assertFalse((output / "configs" / "R1.cfg").exists())
                self.assertFalse((output / "configs" / "R2.cfg").exists())
                self.assertFalse((output / "topology-adapted.xlsx").exists())
                self.assertFalse((output / "README.txt").exists())
                self.assertEqual(user_file.read_text(), "user owned content")
                report = json.loads((output / "report.json").read_text())
                self.assertEqual(report["status"], "failed")
                archived = next(event for event in report["events"] if event["kind"] == "previous-output-archived")
                archive = output / archived["archive"]
                self.assertEqual((archive / "configs" / "R1.cfg").read_bytes(), old_config)
                self.assertTrue((archive / "topology-adapted.xlsx").is_file())

    def run_failure(self, config: Path):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = root / "configs"
            shutil.copytree(FIXTURES / "iosxr_bundle" / "configs", configs)
            shutil.copyfile(config, configs / "R1.cfg")
            output = root / "output"
            topology = FIXTURES / "iosxr_bundle" / "topology.xlsx"
            if config.name == "iosxr_scalar_conflicts.cfg":
                # 这个用例专测最终标量冲突，输入须是两端普通物理口的单链路。
                shutil.copyfile(config, configs / "R2.cfg")
                workbook = load_workbook(topology)
                workbook["链接表"].delete_rows(3)
                topology = root / "single-link.xlsx"
                workbook.save(topology)
            context = convert(topology, configs, output)
            self.assertTrue(context.has_errors)
            self.assertFalse((output / "configs").exists())
            self.assertFalse((output / "topology-adapted.xlsx").exists())
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("设备 R1" in error for error in report["errors"]))
            return report

    def test_unknown_removed_interface_dependency_stops_before_nni_mapping(self):
        report = self.run_failure(FIXTURES / "core_pruning" / "iosxr_unknown.cfg")
        self.assertTrue(any("custom-service FOO" in error for error in report["errors"]))
        self.assertFalse(any(event["kind"] == "simulation-adaptation" for event in report["events"]))

    def test_conflicting_target_interface_properties_fail_final_validation(self):
        report = self.run_failure(FIXTURES / "interface_compatibility" / "iosxr_scalar_conflicts.cfg")
        self.assertTrue(any("mtu 存在冲突" in error for error in report["errors"]))
        self.assertTrue(any(event["kind"] == "interface-validation-error" for event in report["events"]))


if __name__ == "__main__":
    unittest.main()
