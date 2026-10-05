"""使用独立 cfg/xlsx fixture 验证端到端转换和 group 优先级。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from config_adaptor.models import SimulationAdaptationPolicy, Vendor, WashingPolicy
from config_adaptor.parsers.cisco_iosxr import CiscoDocument
from config_adaptor.parsers.juniper_junos import JunosDocument
from config_adaptor.pipeline import convert
from config_adaptor.profiles import load_profiles
from config_adaptor.washing import load_washing_policy


FIXTURES = Path(__file__).parent / "fixtures"


class ConversionTest(unittest.TestCase):
    """覆盖两个厂商的 NNI、UNI、认证及 group 关键边界。"""

    @staticmethod
    def fixture_config(name: str) -> str:
        """读取单项 group 测试使用的外部配置文件。"""
        return (FIXTURES / "group_configs" / name).read_text(encoding="utf-8")

    @staticmethod
    def washing_config(name: str) -> str:
        """读取认证与可选策略清洗使用的外部配置文件。"""
        return (FIXTURES / "washing_configs" / name).read_text(encoding="utf-8")

    @staticmethod
    def simulation_config(name: str) -> str:
        """读取模拟参数适配测试配置。"""
        return (FIXTURES / "simulation_adaptation" / name).read_text(encoding="utf-8")

    @staticmethod
    def conversion_fixture(name: str) -> tuple[Path, Path]:
        """返回端到端场景的拓扑文件和设备配置目录。"""
        root = FIXTURES / name
        return root / "topology.xlsx", root / "configs"

    def test_iosxr_bundle_nni_uni_and_auth(self):
        topology, config_dir = self.conversion_fixture("iosxr_bundle")
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
        topology, source_config_dir = self.conversion_fixture("iosxr_bundle")
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

    def test_unknown_l2_attachment_does_not_activate_gateway(self):
        """未知接口即使带二层业务，也不能间接触发 BVI 的 UNI 迁移。"""
        document = CiscoDocument(
            "interface FutureVirtual0.500 l2transport\n"
            " encapsulation dot1q 500\n"
            "!\n"
            "interface BVI500\n"
            " ipv4 address 192.0.2.1 255.255.255.0\n"
            "!\n"
            "l2vpn\n"
            " bridge group TEST\n"
            "  bridge-domain BD500\n"
            "   interface FutureVirtual0.500\n"
            "   routed interface BVI500\n"
            "!\n"
            "end\n"
        )

        business = document.business_interface_names()
        self.assertNotIn("FutureVirtual0.500", business)
        self.assertNotIn("BVI500", business)

    def test_bvi_number_is_not_used_as_vlan(self):
        """BVI 编号不参与 VLAN 推断，业务标签只能来自显式广播域关系。"""
        document = CiscoDocument(
            "interface Bundle-Ether10.100 l2transport\n"
            " encapsulation dot1q 100\n"
            "!\n"
            "interface GigabitEthernet0/0/0/1.300 l2transport\n"
            " encapsulation dot1q 300\n"
            " xconnect 192.0.2.1 300 encapsulation mpls\n"
            "!\n"
            "interface BVI300\n"
            " ipv4 address 192.0.2.254 255.255.255.0\n"
            "!\n"
            "interface BVI400\n"
            " ipv4 address 198.51.100.254 255.255.255.0\n"
            "!\n"
            "l2vpn\n"
            " bridge group TEST\n"
            "  bridge-domain CUSTOMER-A\n"
            "   interface Bundle-Ether10.100\n"
            "   routed interface BVI300\n"
            "!\n"
            "end\n"
        )

        specs = {spec.name: spec for spec in document.interface_specs()}
        self.assertIsNone(specs["BVI300"].vlan)
        self.assertEqual(specs["BVI300"].inner_vlan, 100)
        self.assertIsNone(specs["BVI400"].vlan)
        self.assertIsNone(specs["BVI400"].inner_vlan)
        business = document.business_interface_names()
        self.assertIn("BVI300", business)
        self.assertNotIn("BVI400", business)

    def test_non_physical_nni_endpoint_fails_before_mapping(self):
        """链接表引用未知接口时必须失败，不能猜测为物理口继续转换。"""
        topology, source_config_dir = self.conversion_fixture("iosxr_bundle")
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

    def test_junos_virtual_and_unknown_interface_classification(self):
        """Junos 虚拟前缀与未知前缀不会落入 physical。"""
        document = JunosDocument(
            "interfaces {\n"
            "    ge-0/0/0 {\n"
            "        disable;\n"
            "    }\n"
            "    gr-0/0/0 {\n"
            "        unit 0 {\n"
            "            family inet {\n"
            "                address 192.0.2.1/32;\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "    future0 {\n"
            "        unit 0 {\n"
            "            family inet {\n"
            "                address 198.51.100.1/32;\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        kinds = {spec.parent: spec.kind for spec in document.interface_specs()}
        self.assertEqual(kinds["ge-0/0/0"], "physical")
        self.assertEqual(kinds["gr-0/0/0"], "virtual")
        self.assertEqual(kinds["future0"], "unknown")
        business = document.business_interface_names()
        self.assertNotIn("gr-0/0/0.0", business)
        self.assertNotIn("future0.0", business)

    def test_iosxr_simulation_adaptation_is_scoped_and_idempotent(self):
        """只调整映射目标口，并放宽已存在的激进 BFD 参数。"""
        policy = SimulationAdaptationPolicy()
        document = CiscoDocument(self.simulation_config("iosxr.cfg"))
        first = document.adapt_to_simulation(policy, {"GigabitEthernet0/0/0/0"})
        rendered = document.render()

        self.assertNotIn("carrier-delay", rendered)
        self.assertEqual(rendered.count("speed 10000"), 1)
        self.assertEqual(rendered.count("\n shutdown"), 1)
        self.assertEqual(rendered.count("no shutdown"), 1)
        self.assertNotIn("bfd minimum-interval 50", rendered)
        self.assertNotIn("bfd minimum-interval 100", rendered)
        self.assertEqual(rendered.count("bfd minimum-interval 300"), 2)
        self.assertNotIn("bfd multiplier 1", rendered)
        self.assertNotIn("bfd multiplier 2", rendered)
        self.assertEqual(rendered.count("bfd multiplier 3"), 2)
        self.assertGreater(first.total, 0)

        second = document.adapt_to_simulation(policy, {"GigabitEthernet0/0/0/0"})
        self.assertEqual(second.total, 0)

    def test_junos_simulation_adaptation_is_scoped_and_idempotent(self):
        """Junos 目标口清除物理属性，非目标口保持不变。"""
        policy = SimulationAdaptationPolicy()
        document = JunosDocument(self.simulation_config("junos.cfg"))
        first = document.adapt_to_simulation(policy, {"ge-0/0/0"})
        rendered = document.render()

        self.assertNotIn("gigether-options", rendered)
        self.assertEqual(rendered.count("speed 10g;"), 1)
        self.assertEqual(rendered.count("disable;"), 1)
        self.assertIn("minimum-interval 300;", rendered)
        self.assertIn("minimum-receive-interval 300;", rendered)
        self.assertIn("multiplier 3;", rendered)
        self.assertGreater(first.total, 0)

        second = document.adapt_to_simulation(policy, {"ge-0/0/0"})
        self.assertEqual(second.total, 0)

    def test_compatible_simulation_adaptation_preserves_bfd_values(self):
        """compatible 模式只做接口兼容，不改协议稳定性参数。"""
        policy = SimulationAdaptationPolicy(mode="compatible")
        document = CiscoDocument(self.simulation_config("iosxr.cfg"))
        document.adapt_to_simulation(policy, {"GigabitEthernet0/0/0/0"})
        rendered = document.render()
        self.assertIn("bfd minimum-interval 50", rendered)
        self.assertIn("bfd multiplier 2", rendered)

    def test_simulation_adaptation_profile_and_off_mode(self):
        """示例 Profile 可加载，off 模式严格保持文档不变。"""
        profiles = load_profiles(Path("config/image_profiles.yaml"))
        cisco_profile = profiles[Vendor.CISCO_IOSXR]
        self.assertEqual(cisco_profile.image, "xrv9000")
        self.assertEqual(cisco_profile.simulation_adaptation.mode, "stable")
        self.assertEqual(cisco_profile.simulation_adaptation.bfd_minimum_interval_ms, 300)
        self.assertIs(cisco_profile.param_adjustment, cisco_profile.simulation_adaptation)

        source = self.simulation_config("iosxr.cfg")
        document = CiscoDocument(source)
        outcome = document.adapt_to_simulation(
            SimulationAdaptationPolicy(mode="off"),
            {"GigabitEthernet0/0/0/0"},
        )
        self.assertEqual(outcome.total, 0)
        self.assertEqual(document.render(), source)

        with tempfile.TemporaryDirectory() as directory:
            legacy_profile = Path(directory) / "legacy-profile.yaml"
            legacy_profile.write_text(
                "profiles:\n"
                "  cisco_iosxr:\n"
                "    param_adjustment:\n"
                "      mode: off\n",
                encoding="utf-8",
            )
            legacy = load_profiles(legacy_profile)
            self.assertEqual(legacy[Vendor.CISCO_IOSXR].simulation_adaptation.mode, "off")

    def test_junos_bundle_nni_uni_and_auth(self):
        topology, config_dir = self.conversion_fixture("junos_bundle")
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
        topology, config_dir = self.conversion_fixture("cross_vendor")
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
        topology, config_dir = self.conversion_fixture("iosxr_duplicate_vlan")
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

    def test_mandatory_washing_removes_management_access_and_snmp(self):
        """默认只清管理面必删项，协议认证和业务能力继续保留。"""
        policy = WashingPolicy()

        cisco_document = CiscoDocument(self.washing_config("iosxr.cfg"))
        cisco_cleanup = cisco_document.clean_management_access()
        cisco_document.add_lab_account()
        cisco = cisco_document.render()
        self.assertNotIn("username old-user", cisco)
        self.assertNotIn("tacacs-server", cisco)
        self.assertNotIn("radius-server", cisco)
        self.assertNotIn("snmp-server", cisco)
        self.assertNotIn("ssh server", cisco)
        self.assertNotIn("ssh client", cisco)
        self.assertNotIn("telnet vrf", cisco)
        self.assertNotIn("OLDPASSWORD", cisco)
        self.assertIn("exec-timeout 10 0", cisco)
        self.assertIn("username labadmin", cisco)
        self.assertIn("key chain OSPF_KEYS", cisco)
        self.assertIn("authentication message-digest keychain OSPF_KEYS", cisco)
        self.assertIn("crypto pki trustpoint PROD-CA", cisco)
        self.assertIn("service cgn NAT1", cisco)
        self.assertIn("flow monitor PROD-FLOW", cisco)
        self.assertEqual(cisco_cleanup.removed["snmp"], 1)
        self.assertEqual(cisco_cleanup.removed["ssh"], 2)

        junos_document = JunosDocument(self.washing_config("junos.cfg"))
        junos_cleanup = junos_document.clean_management_access()
        junos_document.add_lab_account()
        junos = junos_document.render()
        self.assertNotIn("old-user", junos)
        self.assertNotIn("RADIUSSECRET", junos)
        self.assertNotIn("TACACSSECRET", junos)
        self.assertNotIn("radius-options", junos)
        self.assertNotIn("tacplus-options", junos)
        self.assertNotIn("accounting", junos)
        self.assertNotIn("community SNMPSECRET", junos)
        self.assertNotIn("ssh-known-hosts", junos)
        self.assertNotIn("authentication-order tacplus", junos)
        self.assertNotIn("telnet;", junos)
        self.assertIn("ftp;", junos)
        self.assertIn("user labadmin", junos)
        self.assertIn("authentication-key-chains", junos)
        self.assertIn("simple-password OSPFSECRET", junos)
        self.assertIn("ca-profile PROD-CA", junos)
        self.assertIn("rule-set PROD-NAT", junos)
        self.assertIn("flow-monitoring", junos)
        self.assertEqual(junos_cleanup.removed["snmp"], 1)
        self.assertGreaterEqual(junos_cleanup.removed["remote-access"], 3)

    def test_optional_washing_switches_remove_expanded_categories(self):
        """五个可选开关显式开启后才删除协议和兼容性配置。"""
        policy = load_washing_policy(FIXTURES / "washing_configs" / "all_optional.yaml")

        cisco_document = CiscoDocument(self.washing_config("iosxr.cfg"))
        cisco_document.clean_optional_features(policy)
        cisco = cisco_document.render()
        self.assertIn("username old-user", cisco)
        self.assertIn("tacacs-server", cisco)
        self.assertNotIn("key chain OSPF_KEYS", cisco)
        self.assertNotIn("authentication message-digest", cisco)
        self.assertNotIn("crypto pki", cisco)
        self.assertNotIn("hw-module", cisco)
        self.assertNotIn("service cgn", cisco)
        self.assertNotIn("flow monitor", cisco)

        junos_document = JunosDocument(self.washing_config("junos.cfg"))
        junos_document.clean_optional_features(policy)
        junos = junos_document.render()
        self.assertIn("old-user", junos)
        self.assertIn("radius-server", junos)
        self.assertNotIn("authentication-key-chains", junos)
        self.assertNotIn("simple-password OSPFSECRET", junos)
        self.assertNotIn("ca-profile PROD-CA", junos)
        self.assertNotIn("chassis {", junos)
        self.assertNotIn("rule-set PROD-NAT", junos)
        self.assertNotIn("nat-rules PROD-NAT", junos)
        self.assertNotIn("flow-monitoring", junos)
        self.assertNotIn("sampling {", junos)

        topology, config_dir = self.conversion_fixture("cross_vendor")
        with tempfile.TemporaryDirectory() as directory:
            context = convert(
                topology,
                config_dir,
                Path(directory) / "output",
                washing_policy_path=FIXTURES / "washing_configs" / "all_optional.yaml",
            )
            self.assertFalse(context.has_errors)
            self.assertTrue(context.washing_policy.protocol_authentication)
            self.assertTrue(context.washing_policy.flow_statistics)
            optional_events = [
                event for event in context.events if event["kind"] == "optional-washing"
            ]
            self.assertTrue(optional_events)
            self.assertIn("protocol_authentication", optional_events[0]["enabled"])
            authentication_events = [
                event for event in context.events if event["kind"] == "authentication"
            ]
            self.assertTrue(authentication_events)
            self.assertNotIn("pki", authentication_events[0]["removed_by_type"])

        combined = CiscoDocument(self.washing_config("iosxr.cfg"))
        combined.clean_authentication(policy)
        combined_rendered = combined.render()
        self.assertNotIn("username old-user", combined_rendered)
        self.assertNotIn("crypto pki", combined_rendered)

    def test_cisco_bundle_thresholds_are_removed_when_flattened(self):
        """Bundle 迁移到普通物理口时不遗留 minimum-active 或 LACP。"""
        document = CiscoDocument(self.washing_config("iosxr.cfg"))
        document.rename_interface_tree(
            "Bundle-Ether10",
            "GigabitEthernet0/0/0/0",
            strip_bundle=True,
        )
        rendered = document.render()
        self.assertNotIn("bundle minimum-active", rendered)
        self.assertNotIn("lacp switchover", rendered)
        self.assertIn("interface GigabitEthernet0/0/0/0", rendered)
        self.assertIn("ipv4 address 10.0.0.1 255.255.255.252", rendered)

    def test_iosxr_mlag_is_split_by_peer_and_references_are_cloned(self):
        """同一 Bundle 跨对端时分配多个物理口，全局引用同步展开。"""
        topology, config_dir = self.conversion_fixture("iosxr_mlag")
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
        topology, config_dir = self.conversion_fixture("junos_mlag")
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
        topology, config_dir = self.conversion_fixture("iosxr_duplicate_vlan")
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

    def test_iosxr_group_regex_expansion(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_regex.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertFalse(outcome.warnings)
        self.assertTrue(outcome.events)
        self.assertIn("mtu 9000", rendered)
        self.assertNotIn("group COMMON", rendered)
        self.assertNotIn("apply-group COMMON", rendered)

    def test_junos_wildcard_group_expansion(self):
        document = JunosDocument(self.fixture_config("junos_group_wildcard.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertFalse(outcome.warnings)
        self.assertTrue(outcome.events)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups COMMON", rendered)

    def test_junos_inline_blocks_are_parsed_and_expanded(self):
        source = '''groups { "EDGE GROUP" { interfaces { ge-0/0/0 { mtu 9000; } } } }
interfaces { ge-0/0/0 { unit 0; description "literal { brace; }"; } }
apply-groups "EDGE GROUP";
'''
        self.assertEqual(
            JunosDocument._expand_inline_blocks("G { mtu 9000; };"),
            "G {\nmtu 9000;\n};",
        )
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.events, ["已展开 Junos 配置组 EDGE GROUP"])
        self.assertIn("mtu 9000;", rendered)
        self.assertIn('description "literal { brace; }";', rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_inline_parser_ignores_braces_in_comments(self):
        source = '''system { /* ignored { block; } */ host-name "lab;{vmx}"; # ignored }
    services { ssh; }
}
'''
        document = JunosDocument(source)
        rendered = document.render()

        self.assertIn("system {", rendered)
        self.assertIn("/* ignored { block; } */", rendered)
        self.assertIn('host-name "lab;{vmx}";', rendered)
        self.assertIn("services {", rendered)
        self.assertIn("ssh;", rendered)

    def test_junos_inline_parser_still_rejects_unbalanced_braces(self):
        with self.assertRaisesRegex(ValueError, "出现多余右大括号"):
            JunosDocument("system { host-name lab; } }")
        with self.assertRaisesRegex(ValueError, "大括号不平衡"):
            JunosDocument("system { host-name lab;")

    def test_junos_inactive_apply_groups_are_removed_without_expansion(self):
        source = """groups {
    COMMON {
        interfaces {
            ge-0/0/0 {
                mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
inactive: apply-groups COMMON;
inactive: apply-groups MISSING;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.events)
        self.assertFalse(outcome.warnings)
        self.assertIn("groups {", rendered)
        self.assertNotIn("inactive: apply-groups", rendered)
        self.assertNotIn("MISSING", rendered)

    def test_junos_inactive_group_definition_is_removed_without_active_groups(self):
        source = """groups {
    inactive: DISABLED {
        system {
            host-name old;
        }
    }
}
inactive: apply-groups DISABLED;
system {
    host-name current;
}
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("DISABLED", rendered)
        self.assertIn("host-name current;", rendered)

    def test_junos_all_inactive_nodes_are_removed_without_group_expansion(self):
        source = """system {
    inactive: host-name old;
    host-name current;
    inactive: services {
        ssh;
    }
}
interfaces {
    inactive: ge-0/0/0 {
        unit 0;
    }
    ge-0/0/1 {
        inactive: mtu 1500;
        mtu 9000;
    }
}
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertNotIn("inactive:", rendered)
        self.assertNotIn("host-name old;", rendered)
        self.assertNotIn("ge-0/0/0", rendered)
        self.assertNotIn("mtu 1500;", rendered)
        self.assertIn("host-name current;", rendered)
        self.assertIn("mtu 9000;", rendered)

    def test_junos_inactive_nested_controls_do_not_change_expansion(self):
        source = """groups {
    COMMON {
        inactive: apply-groups MISSING;
        interfaces {
            ge-0/0/0 {
                inactive: apply-groups-except COMMON;
                mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups COMMON;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_inactive_group_is_not_expanded(self):
        source = """groups {
    inactive: DISABLED {
        interfaces {
            ge-0/0/0 {
                description disabled-group;
            }
        }
    }
    ENABLED {
        interfaces {
            ge-0/0/0 {
                mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups ENABLED;
inactive: apply-groups DISABLED;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.events, ["已展开 Junos 配置组 ENABLED"])
        self.assertNotIn("inactive: DISABLED {", rendered)
        self.assertNotIn("inactive: apply-groups DISABLED;", rendered)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("description disabled-group;", rendered)

    def test_junos_inactive_value_is_removed_before_group_merge(self):
        source = """groups {
    COMMON {
        interfaces {
            ge-0/0/0 {
                mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        inactive: mtu 1500;
        unit 0;
    }
}
apply-groups COMMON;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertNotIn("inactive: mtu 1500;", rendered)
        self.assertIn("mtu 9000;", rendered)
        self.assertFalse(outcome.conflicts)

    def test_junos_protect_prefix_is_effective_but_removed_from_output(self):
        source = """groups {
    protect: COMMON {
        interfaces {
            ge-0/0/0 {
                protect: mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
protect: apply-groups COMMON;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("protect:", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_quoted_group_names_in_list_are_fully_expanded(self):
        source = """groups {
    "BUSINESS EDGE" {
        interfaces {
            ge-0/0/0 {
                mtu 9000;
            }
        }
    }
    "LOGGING CORE" {
        system {
            syslog {
                file messages {
                    any notice;
                }
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups [ "BUSINESS EDGE" "LOGGING CORE" ];
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertEqual(
            set(outcome.events),
            {
                "已展开 Junos 配置组 BUSINESS EDGE",
                "已展开 Junos 配置组 LOGGING CORE",
            },
        )
        self.assertIn("mtu 9000;", rendered)
        self.assertIn("syslog {", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_quoted_nested_group_and_except_are_resolved(self):
        source = """groups {
    "BASE GROUP" {
        interfaces {
            <ge-*> {
                mtu 9000;
            }
        }
    }
    "EDGE GROUP" {
        apply-groups "BASE GROUP";
        interfaces {
            ge-0/0/0 {
                apply-groups-except "BASE GROUP";
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups "EDGE GROUP";
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertNotIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_inactive_interfaces_and_units_are_not_discovered(self):
        source = """interfaces {
    inactive: ge-0/0/0 {
        unit 100 {
            vlan-id 100;
            family inet;
        }
    }
    protect: ge-0/0/1 {
        protect: inactive: unit 200 {
            vlan-id 200;
            family inet;
        }
        unit 300 {
            inactive: protect: vlan-id 300;
            vlan-id 301;
            protect: family inet;
        }
    }
}
"""
        document = JunosDocument(source)
        specs = document.interface_specs()

        self.assertEqual([spec.name for spec in specs], ["ge-0/0/1.300"])
        self.assertEqual(specs[0].vlan, 301)
        self.assertEqual(document.business_interface_names(), {"ge-0/0/1.300"})
        rendered = document.render()
        self.assertIn("ge-0/0/1 {", rendered)
        self.assertIn("inactive: unit 200 {", rendered)
        self.assertIn("inactive: vlan-id 300;", rendered)
        self.assertNotIn("protect:", rendered)

    def test_full_expansion_rejects_unrelated_invalid_iosxr_group(self):
        source = """group BUSINESS
 interface 'GigabitEthernet.*'
  mtu 9000
 !
end-group
!
group TELEMETRY
 telemetry model-driven
  destination-group LAB
  apply-group MISSING
 !
end-group
!
apply-group BUSINESS TELEMETRY
!
interface GigabitEthernet0/0/0/5
!
end
"""
        document = CiscoDocument(source)
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        self.assertFalse(outcome.success)
        self.assertTrue(any("TELEMETRY" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_iosxr_expands_non_interface_group(self):
        source = """group TELEMETRY
 telemetry model-driven
  destination-group LAB
 !
end-group
!
apply-group TELEMETRY
!
end
"""
        document = CiscoDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertIn("telemetry model-driven", rendered)
        self.assertIn("destination-group LAB", rendered)
        self.assertNotIn("group TELEMETRY", rendered)
        self.assertNotIn("apply-group TELEMETRY", rendered)

    def test_full_expansion_rejects_unrelated_invalid_junos_group(self):
        source = """groups {
    BUSINESS {
        interfaces {
            <ge-*> {
                mtu 9000;
            }
        }
    }
    LOGGING {
        system {
            syslog {
                file messages {
                    any notice;
                    apply-groups MISSING;
                }
            }
        }
    }
}
interfaces {
    ge-0/0/5 {
        unit 0;
    }
}
apply-groups [ BUSINESS LOGGING ];
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(any("MISSING" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_junos_expands_non_interface_group(self):
        source = """groups {
    LOGGING {
        system {
            syslog {
                file messages {
                    any notice;
                }
            }
        }
    }
}
apply-groups LOGGING;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertTrue(outcome.success)
        self.assertNotIn("groups {", document.render())
        self.assertNotIn("apply-groups", document.render())
        self.assertIn("syslog {", document.render())

    def test_iosxr_group_conflict_precedence_and_semantic_keys(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_conflicts.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 1500", rendered)
        self.assertNotIn("mtu 8000", rendered)
        self.assertNotIn("mtu 9000", rendered)
        self.assertIn("ipv4 address 192.0.2.1", rendered)
        self.assertNotIn("ipv4 address 10.0.0.1", rendered)
        self.assertIn("ipv4 access-group GROUP-IN ingress", rendered)
        self.assertIn("ipv4 access-group EXPLICIT-OUT egress", rendered)
        self.assertGreaterEqual(len(outcome.conflicts), 3)
        self.assertTrue(all(item["winner_source"] == "explicit" for item in outcome.conflicts))

    def test_iosxr_first_group_in_list_wins(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_order.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 1111", rendered)
        self.assertNotIn("mtu 2222", rendered)
        self.assertEqual(outcome.conflicts[0]["winner_source"], "group:FIRST")

    def test_iosxr_longest_regex_and_negated_single_value(self):
        document = CiscoDocument(self.fixture_config("iosxr_group_specificity.cfg"))
        outcome = document.expand_groups(["GigabitEthernet0/0/0/5"])
        rendered = document.render()
        self.assertIn("mtu 2000", rendered)
        self.assertNotIn("mtu 1000", rendered)
        self.assertIn("description generic", rendered)
        self.assertIn("bandwidth 1000000", rendered)
        self.assertIn("no shutdown", rendered)
        self.assertNotIn("\n shutdown", rendered)
        self.assertTrue(outcome.conflicts)

    def test_junos_nested_list_and_except_precedence(self):
        document = JunosDocument(self.fixture_config("junos_group_precedence.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertIn("mtu 3333;", rendered)
        self.assertIn("mtu 2222;", rendered)
        self.assertIn("mtu 4444;", rendered)
        self.assertNotIn("mtu 1111;", rendered)
        self.assertIn("address 10.0.0.1/32;", rendered)
        self.assertIn("address 10.0.0.2/32;", rendered)
        self.assertTrue(outcome.conflicts)

    def test_junos_group_can_apply_groups_transitively(self):
        """Group 内的传递引用按本地、父 Group、子 Group 的顺序求值。"""
        source = """groups {
    BASE {
        interfaces {
            <ge-*> {
                mtu 9000;
            }
        }
    }
    REGIONAL {
        apply-groups BASE;
        interfaces {
            <ge-*> {
                mtu 8000;
            }
        }
    }
    EDGE {
        apply-groups REGIONAL;
    }
}
interfaces {
    ge-0/0/0 {
        mtu 1500;
        unit 0;
    }
    ge-0/0/1 {
        unit 0;
    }
}
apply-groups EDGE;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertIn("mtu 1500;", rendered)
        self.assertIn("mtu 8000;", rendered)
        self.assertNotIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)
        self.assertEqual(
            set(outcome.events),
            {
                "已展开 Junos 配置组 BASE",
                "已展开 Junos 配置组 EDGE",
                "已展开 Junos 配置组 REGIONAL",
            },
        )

    def test_junos_nested_group_honors_apply_groups_except(self):
        """Group 内继承的 Group 可在更深层级被 apply-groups-except 排除。"""
        source = """groups {
    BASE {
        interfaces {
            <ge-*> {
                mtu 9000;
            }
        }
    }
    EDGE {
        apply-groups BASE;
        interfaces {
            ge-0/0/0 {
                apply-groups-except BASE;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
    ge-0/0/1 {
        unit 0;
    }
}
apply-groups EDGE;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertEqual(rendered.count("mtu 9000;"), 1)
        first_interface, second_interface = rendered.split("    ge-0/0/1", maxsplit=1)
        self.assertNotIn("mtu 9000;", first_interface)
        self.assertIn("mtu 9000;", second_interface)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_undefined_apply_groups_except_rolls_back_without_apply(self):
        """只有 except 引用时也必须校验 group 是否已定义。"""
        source = """interfaces {
    ge-0/0/0 {
        apply-groups-except "MISSING GROUP";
        unit 0;
    }
}
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(
            any(
                "apply-groups-except" in warning and "MISSING GROUP" in warning
                for warning in outcome.warnings
            )
        )
        self.assertEqual(document.render(), source)

    def test_junos_selected_group_validates_apply_groups_except(self):
        """已选中 group 内的 except 引用缺失时整体回滚。"""
        source = """groups {
    EDGE {
        interfaces {
            <ge-*> {
                apply-groups-except MISSING;
                mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups EDGE;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(any("MISSING" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_junos_inactive_and_unused_group_exclusions_do_not_block_expansion(self):
        """inactive 引用会删除，未使用模板中的 except 不阻断完整展开。"""
        source = """groups {
    EDGE {
        interfaces {
            <ge-*> {
                mtu 9000;
            }
        }
    }
    UNUSED {
        apply-groups-except MISSING-IN-UNUSED;
    }
}
interfaces {
    ge-0/0/0 {
        inactive: apply-groups-except MISSING-INACTIVE;
        unit 0;
    }
}
apply-groups EDGE;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("MISSING-IN-UNUSED", rendered)
        self.assertNotIn("MISSING-INACTIVE", rendered)

    def test_junos_apply_groups_except_does_not_create_dependency(self):
        """合法 except 引用只校验存在性，不会单独选中或展开 group。"""
        source = """groups {
    EXCLUDED {
        interfaces {
            <ge-*> {
                description SHOULD-NOT-EXPAND;
            }
        }
    }
    EDGE {
        interfaces {
            <ge-*> {
                apply-groups-except EXCLUDED;
                mtu 9000;
            }
        }
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups EDGE;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertNotIn("已展开 Junos 配置组 EXCLUDED", outcome.events)
        self.assertNotIn("description SHOULD-NOT-EXPAND;", rendered)
        self.assertNotIn("groups {", rendered)

    def test_junos_nested_group_cycle_and_missing_reference_roll_back(self):
        """循环与间接未定义引用均在写回前失败并保留原配置。"""
        cycle = """groups {
    A {
        apply-groups B;
    }
    B {
        apply-groups A;
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups A;
"""
        cycle_document = JunosDocument(cycle)
        cycle_outcome = cycle_document.expand_groups([])
        self.assertFalse(cycle_outcome.success)
        self.assertTrue(any("循环引用" in warning for warning in cycle_outcome.warnings))
        self.assertEqual(cycle_document.render(), cycle)

        missing = """groups {
    A {
        apply-groups MISSING;
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups A;
"""
        missing_document = JunosDocument(missing)
        missing_outcome = missing_document.expand_groups([])
        self.assertFalse(missing_outcome.success)
        self.assertTrue(any("MISSING" in warning for warning in missing_outcome.warnings))
        self.assertEqual(missing_document.render(), missing)

    def test_junos_removes_unapplied_group_definitions_after_expansion(self):
        """活动引用完整展开后删除全部 group 定义，包括未应用模板。"""
        source = """groups {
    BASE {
        interfaces {
            <ge-*> {
                mtu 9000;
            }
        }
    }
    EDGE {
        apply-groups BASE;
    }
    UNUSED-TEMPLATE {
        apply-groups BASE;
    }
}
interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups EDGE;
"""
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)
        self.assertEqual(rendered.count("mtu 9000;"), 1)

    def test_group_expansion_rolls_back_on_unresolved_reference(self):
        source = self.fixture_config("junos_group_unresolved.cfg")
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.warnings)
        self.assertIn("apply-groups MISSING", document.render())
        self.assertIn("groups {", document.render())

    def test_undefined_root_group_fails_without_any_definitions(self):
        junos_source = """interfaces {
    ge-0/0/0 {
        unit 0;
    }
}
apply-groups MISSING;
"""
        junos = JunosDocument(junos_source)
        junos_outcome = junos.expand_groups([])
        self.assertFalse(junos_outcome.success)
        self.assertTrue(any("MISSING" in item for item in junos_outcome.warnings))
        self.assertEqual(junos.render(), junos_source)

        cisco_source = """apply-group MISSING
!
interface GigabitEthernet0/0/0/0
!
end
"""
        cisco = CiscoDocument(cisco_source)
        cisco_outcome = cisco.expand_groups([])
        self.assertFalse(cisco_outcome.success)
        self.assertTrue(any("MISSING" in item for item in cisco_outcome.warnings))
        self.assertEqual(cisco.render(), cisco_source)

    def test_unresolved_applied_group_fails_conversion_before_mapping(self):
        topology, config_dir = self.conversion_fixture("junos_unresolved")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            context = convert(topology, config_dir, output)
            self.assertTrue(context.has_errors)
            self.assertFalse((output / "configs").exists())
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any("group" in message for message in report["errors"]))


if __name__ == "__main__":
    unittest.main()
