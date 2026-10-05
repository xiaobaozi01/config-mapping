"""使用独立 cfg/xlsx fixture 验证端到端转换流程。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from config_adaptor.errors import InvariantViolation
from config_adaptor.pipeline import convert


FIXTURES = Path(__file__).parent / "fixtures"


def conversion_fixture(name: str) -> tuple[Path, Path]:
    """返回端到端场景的拓扑文件和设备配置目录。"""
    root = FIXTURES / name
    return root / "topology.xlsx", root / "configs"


class ConversionTest(unittest.TestCase):
    """覆盖两个厂商的 NNI、UNI、认证及 group 关键边界。"""

    def test_iosxr_bundle_nni_uni_and_auth(self):
        topology, config_dir = conversion_fixture("iosxr_bundle")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            self.assertEqual(context.devices["R1"].profile.uni_parent, "GigabitEthernet0/0/0/7")
            self.assertIn("GigabitEthernet0/0/0/6", context.devices["R1"].profile.nni_interfaces)
            converted = (output / "configs" / "R1.cfg").read_text(encoding="utf-8")
            self.assertNotIn("Bundle-Ether10", converted)
            self.assertNotIn("old-user", converted)
            self.assertNotIn("tacacs-server", converted)
            self.assertNotIn("taskgroup OLD-TASKS", converted)
            self.assertNotIn("usergroup OLD-USERS", converted)
            self.assertNotIn("login authentication OLD-AUTH", converted)
            self.assertNotIn("authorization commands OLD-AUTHZ", converted)
            self.assertNotIn("accounting commands OLD-ACCT", converted)
            self.assertNotIn("users group OLD-USERS", converted)
            self.assertNotIn("ACCESS-UNI", converted)
            self.assertIn("username labadmin", converted)
            self.assertIn("group root-system", converted)
            self.assertIn("line template vty", converted)
            self.assertIn("exec-timeout 10 0", converted)
            self.assertIn("interface GigabitEthernet0/0/0/0", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.2", converted)
            self.assertIn("encapsulation dot1q 2 second-dot1q 2", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertIn("interface Loopback0", converted)
            self.assertLess(converted.index("username labadmin"), converted.index("end"))

            adapted = load_workbook(output / "topology-adapted.xlsx")
            adapted_links = adapted["链接表"]
            self.assertEqual(adapted_links.max_row, 2)
            self.assertEqual(adapted_links.cell(2, 2).value, "GigabitEthernet0/0/0/0")
            self.assertEqual(adapted["设备列表"].cell(2, 3).value, "configs/R1.cfg")
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["summary"]["skipped_links"], 1)
            authentication = next(event for event in report["events"] if event["kind"] == "authentication")
            self.assertEqual(authentication["removed_by_type"]["taskgroup"], 1)
            self.assertEqual(authentication["removed_by_type"]["usergroup"], 1)
            self.assertEqual(authentication["removed_by_type"]["line-auth-reference"], 5)
            adjustment = next(
                event for event in report["events"] if event["kind"] == "simulation-adaptation"
            )
            self.assertEqual(adjustment["mode"], "stable")
            self.assertEqual(adjustment["image"], "xrv9000")
            self.assertIn("GigabitEthernet0/0/0/0", adjustment["target_interfaces"])

    def test_unknown_and_virtual_interfaces_are_preserved_but_not_mapped(self):
        """未识别及已知虚拟接口保留原配置，且未知类型生成审计告警。"""
        topology, source_config_dir = conversion_fixture("iosxr_bundle")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "configs"
            shutil.copytree(source_config_dir, config_dir)
            config_path = config_dir / "R1.cfg"
            source = config_path.read_text(encoding="utf-8")
            source = source.replace(
                "\nend\n",
                "\ninterface Tunnel-ip100\n"
                " ipv4 address 198.51.100.1 255.255.255.252\n"
                "!\n"
                "interface FutureVirtual0\n"
                " ipv4 address 203.0.113.1 255.255.255.252\n"
                "!\n"
                "end\n",
            )
            config_path.write_text(source, encoding="utf-8")

            output = root / "output"
            context = convert(topology, config_dir, output)

            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "R1.cfg").read_text(encoding="utf-8")
            self.assertIn("interface Tunnel-ip100", converted)
            self.assertIn("interface FutureVirtual0", converted)
            self.assertTrue(
                any(
                    "FutureVirtual0 类型无法识别" in warning
                    for warning in context.devices["R1"].warnings
                )
            )
            self.assertFalse(
                any(
                    "Tunnel-ip100 类型无法识别" in warning
                    for warning in context.devices["R1"].warnings
                )
            )
            mapped_sources = {
                mapping.source_interface for mapping in context.devices["R1"].mappings
            }
            self.assertNotIn("Tunnel-ip100", mapped_sources)
            self.assertNotIn("FutureVirtual0", mapped_sources)

    def test_non_physical_nni_endpoint_fails_before_mapping(self):
        """链接表引用未知接口时必须失败，不能猜测为物理口继续转换。"""
        topology, source_config_dir = conversion_fixture("iosxr_bundle")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "configs"
            shutil.copytree(source_config_dir, config_dir)
            config_path = config_dir / "R1.cfg"
            source = config_path.read_text(encoding="utf-8").replace(
                "\nend\n",
                "\ninterface FutureVirtual0\n"
                " ipv4 address 203.0.113.1 255.255.255.252\n"
                "!\n"
                "end\n",
            )
            config_path.write_text(source, encoding="utf-8")

            invalid_topology = root / "topology.xlsx"
            workbook = load_workbook(topology)
            workbook["链接表"].cell(2, 2).value = "FutureVirtual0"
            workbook.save(invalid_topology)

            output = root / "output"
            context = convert(invalid_topology, config_dir, output)

            self.assertTrue(context.has_errors)
            self.assertTrue(
                any(
                    "FutureVirtual0" in error and "不能作为物理 NNI" in error
                    for error in context.errors
                )
            )
            self.assertFalse((output / "configs").exists())

    def test_junos_bundle_nni_uni_and_auth(self):
        topology, config_dir = conversion_fixture("junos_bundle")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            self.assertEqual(context.devices["J1"].profile.uni_parent, "ge-0/0/7")
            self.assertIn("ge-0/0/6", context.devices["J1"].profile.nni_interfaces)
            converted = (output / "configs" / "J1.cfg").read_text(encoding="utf-8")
            self.assertNotIn("ae0", converted)
            self.assertNotIn("old-user", converted)
            self.assertNotIn("radius-server", converted)
            self.assertNotIn("ACCESS-UNI", converted)
            self.assertIn("user labadmin", converted)
            self.assertIn("ge-0/0/0.0", converted)
            self.assertIn("ge-0/0/7", converted)
            self.assertIn("unit 100", converted)
            self.assertIn("vlan-tags outer 100 inner 100;", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertIn("lo0", converted)

    def test_cross_vendor_iosxr_to_junos_nni_and_aggregate(self):
        """Cisco 与 Juniper 端点独立适配，并共同删除冗余聚合成员行。"""
        topology, config_dir = conversion_fixture("cross_vendor")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)

            cisco = (output / "configs" / "C1.cfg").read_text(encoding="utf-8")
            self.assertNotIn("Bundle-Ether10", cisco)
            self.assertIn("interface GigabitEthernet0/0/0/0", cisco)
            self.assertIn("ipv4 address 10.0.0.1 255.255.255.252", cisco)
            self.assertIn("interface GigabitEthernet0/0/0/1", cisco)
            self.assertIn("ipv4 address 10.0.1.1 255.255.255.252", cisco)
            self.assertIn("  interface GigabitEthernet0/0/0/0", cisco)
            self.assertIn("  interface GigabitEthernet0/0/0/1", cisco)

            juniper = (output / "configs" / "J1.cfg").read_text(encoding="utf-8")
            self.assertNotIn("ae20", juniper)
            self.assertIn("ge-0/0/0", juniper)
            self.assertIn("address 10.0.0.2/30;", juniper)
            self.assertIn("ge-0/0/1", juniper)
            self.assertIn("address 10.0.1.2/30;", juniper)
            self.assertIn("interface ge-0/0/0.0;", juniper)
            self.assertIn("interface ge-0/0/1.0;", juniper)

            adapted = load_workbook(output / "topology-adapted.xlsx")
            links = adapted["链接表"]
            self.assertEqual(links.max_row, 3)
            self.assertEqual(links.cell(2, 2).value, "GigabitEthernet0/0/0/0")
            self.assertEqual(links.cell(2, 4).value, "ge-0/0/0")
            self.assertEqual(links.cell(3, 2).value, "GigabitEthernet0/0/0/1")
            self.assertEqual(links.cell(3, 4).value, "ge-0/0/1")

            mappings = json.loads(
                (output / "interface-mapping.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any(
                    item["device"] == "C1"
                    and item["source_interface"] == "Bundle-Ether10"
                    and item["target_interface"] == "GigabitEthernet0/0/0/0"
                    for item in mappings
                )
            )
            self.assertTrue(
                any(
                    item["device"] == "J1"
                    and item["source_interface"] == "ae20"
                    and item["target_interface"] == "ge-0/0/0"
                    for item in mappings
                )
            )
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["summary"]["active_links"], 2)
            self.assertEqual(report["summary"]["skipped_links"], 1)

    def test_skipped_nni_is_not_reclassified_as_uni(self):
        """预检跳过的跨范围链路仍保留 NNI 角色，不进入 UNI 汇聚。"""
        topology, config_dir = conversion_fixture("iosxr_duplicate_vlan")
        with tempfile.TemporaryDirectory() as directory:
            temporary_topology = Path(directory) / "topology.xlsx"
            workbook = load_workbook(topology)
            workbook["设备列表"].append(["H1", "Huawei", None])
            workbook["链接表"].cell(2, 3).value = "H1"
            workbook["链接表"].cell(2, 4).value = "GigabitEthernet0/0/0"
            workbook.save(temporary_topology)

            output = Path(directory) / "output"
            context = convert(temporary_topology, config_dir, output)
            self.assertFalse(context.has_errors)
            mappings = context.devices["R1"].mappings
            self.assertTrue(
                any(
                    item.role == "NNI"
                    and item.action == "skip"
                    and item.source_interface == "GigabitEthernet0/0/0/5"
                    for item in mappings
                )
            )
            self.assertFalse(
                any(
                    item.role == "UNI"
                    and item.source_interface == "GigabitEthernet0/0/0/5"
                    for item in mappings
                )
            )
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            validation = next(
                event for event in report["events"] if event["kind"] == "topology-preflight"
            )
            self.assertEqual(validation["active_links"], 0)
            self.assertEqual(validation["skipped_links"], 1)

    def test_iosxr_mlag_is_split_by_peer_and_references_are_cloned(self):
        """同一 Bundle 跨对端时分配多个物理口，全局引用同步展开。"""
        topology, config_dir = conversion_fixture("iosxr_mlag")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "A.cfg").read_text(encoding="utf-8")
            self.assertNotIn("Bundle-Ether10", converted)
            self.assertIn("interface GigabitEthernet0/0/0/0.100 l2transport", converted)
            self.assertIn("interface GigabitEthernet0/0/0/1.100 l2transport", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.2 l2transport", converted)
            self.assertIn("encapsulation dot1q 2 second-dot1q 2", converted)
            self.assertNotIn("rewrite egress tag", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertNotIn("interface BVI300", converted)
            self.assertNotIn("interface BVI400", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.3", converted)
            self.assertIn("encapsulation dot1q 3 second-dot1q 3", converted)
            self.assertIn("ipv4 address 198.51.100.1 255.255.255.0", converted)
            self.assertNotIn("203.0.113.1", converted)
            self.assertTrue(
                any(
                    "BVI300" in warning and "不使用 BVI 编号" in warning
                    for warning in context.devices["A"].warnings
                )
            )
            self.assertIn("interface GigabitEthernet0/0/0/0.100", converted)
            self.assertIn("interface GigabitEthernet0/0/0/1.100", converted)

            adapted = load_workbook(output / "topology-adapted.xlsx")
            links = adapted["链接表"]
            self.assertEqual(links.max_row, 3)
            self.assertEqual(links.cell(2, 2).value, "GigabitEthernet0/0/0/0")
            self.assertEqual(links.cell(3, 2).value, "GigabitEthernet0/0/0/1")

    def test_junos_mlag_is_split_by_peer_and_uni_uses_qinq(self):
        """Junos ae 跨对端拆分，且 UNI 的旧 VLAN 终结被统一清理。"""
        topology, config_dir = conversion_fixture("junos_mlag")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "JA.cfg").read_text(encoding="utf-8")
            self.assertNotIn("ae0", converted)
            self.assertIn("ge-0/0/0", converted)
            self.assertIn("ge-0/0/1", converted)
            self.assertIn("interface ge-0/0/0.0;", converted)
            self.assertIn("interface ge-0/0/1.0;", converted)
            self.assertIn("ge-0/0/7", converted)
            self.assertIn("vlan-tags outer 2 inner 2;", converted)
            self.assertNotIn("vlan-id-list", converted)
            self.assertNotIn("input-vlan-map", converted)
            self.assertNotIn("output-vlan-map", converted)
            self.assertNotIn("interface-mode trunk", converted)
            self.assertNotIn("UNUSED-BARE-PORT", converted)
            self.assertNotIn("irb {", converted)
            self.assertIn("unit 300", converted)
            self.assertIn("address 198.51.100.1/24;", converted)
            self.assertNotIn("203.0.113.1/24", converted)
            mappings = json.loads((output / "interface-mapping.json").read_text(encoding="utf-8"))
            removed_irb = next(item for item in mappings if item["source_interface"] == "irb.400")
            self.assertEqual(removed_irb["action"], "remove-bare")

    def test_duplicate_uni_vlan_is_reassigned_per_device(self):
        topology, config_dir = conversion_fixture("iosxr_duplicate_vlan")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertFalse(context.has_errors)
            converted = (output / "configs" / "R1.cfg").read_text(encoding="utf-8")
            self.assertIn("interface GigabitEthernet0/0/0/7.100", converted)
            self.assertIn("interface GigabitEthernet0/0/0/7.2", converted)
            self.assertEqual(converted.count("encapsulation dot1q 100"), 1)
            self.assertEqual(converted.count("encapsulation dot1q 2"), 1)
            self.assertIn("encapsulation dot1q 100 second-dot1q 201", converted)
            self.assertIn("encapsulation dot1q 2 second-dot1q 100", converted)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(report["summary"]["group_conflict_count"], 1)
            self.assertEqual(report["group_conflicts"][0]["winner_source"], "explicit")

    def test_unresolved_applied_group_fails_conversion_before_mapping(self):
        topology, config_dir = conversion_fixture("junos_unresolved")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertTrue(context.has_errors)
            self.assertFalse((output / "configs").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("group" in message for message in report["errors"]))

    def test_internal_invariant_failure_writes_report_then_reraises(self):
        """内部编程错误不被吞掉，但 CLI 仍能获得可诊断的失败报告。"""
        topology, config_dir = conversion_fixture("iosxr_bundle")

        class BrokenPipeline:
            def execute(self, context) -> None:
                raise InvariantViolation("测试内部不变量")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch(
                "config_adaptor.pipeline.build_default_pipeline",
                return_value=BrokenPipeline(),
            ):
                with self.assertRaisesRegex(
                    InvariantViolation,
                    "测试内部不变量",
                ):
                    convert(topology, config_dir, output)

            self.assertFalse((output / "configs").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(
                any(
                    "InvariantViolation" in message
                    and "测试内部不变量" in message
                    for message in report["errors"]
                )
            )
            event = next(
                item for item in report["events"]
                if item["kind"] == "internal-error"
            )
            self.assertEqual(event["phase"], "核心转换流水线")
            self.assertEqual(event["exception_type"], "InvariantViolation")

    def test_render_invariant_failure_rewrites_report_as_failed(self):
        """输出阶段的结构错误不应留下成功报告或部分配置。"""
        topology, config_dir = conversion_fixture("junos_bundle")

        class CorruptingPipeline:
            def execute(self, context) -> None:
                context.devices["J1"].document.root.children = None

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch(
                "config_adaptor.pipeline.build_default_pipeline",
                return_value=CorruptingPipeline(),
            ):
                with self.assertRaisesRegex(
                    InvariantViolation,
                    "Junos 文档根节点",
                ):
                    convert(topology, config_dir, output)

            self.assertFalse((output / "configs").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            event = next(
                item for item in report["events"]
                if item["kind"] == "internal-error"
            )
            self.assertEqual(event["phase"], "输出结果")
            self.assertEqual(event["exception_type"], "InvariantViolation")


if __name__ == "__main__":
    unittest.main()
