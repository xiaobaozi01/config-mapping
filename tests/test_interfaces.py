"""验证接口识别、业务闭包分析与 UNI 迁移判定。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures" / "interfaces"


def interface_config(name: str) -> str:
    """读取接口业务分析测试配置。"""
    return (FIXTURES / name).read_text(encoding="utf-8")


class CiscoInterfaceBusinessTest(unittest.TestCase):
    def test_unknown_l2_attachment_does_not_activate_gateway(self):
        """未知接口即使带二层业务，也不能间接触发 BVI 的 UNI 迁移。"""
        document = CiscoDocument(interface_config("cisco_unknown_l2_attachment.cfg"))

        business = document.business_interface_names()
        self.assertNotIn("FutureVirtual0.500", business)
        self.assertNotIn("BVI500", business)

    def test_bvi_number_is_not_used_as_vlan(self):
        """BVI 编号不参与 VLAN 推断，业务标签只能来自显式广播域关系。"""
        document = CiscoDocument(interface_config("cisco_bvi_vlan_inference.cfg"))

        specs = {spec.name: spec for spec in document.interface_specs()}
        self.assertIsNone(specs["BVI300"].vlan)
        self.assertEqual(specs["BVI300"].inner_vlan, 100)
        self.assertIsNone(specs["BVI400"].vlan)
        self.assertIsNone(specs["BVI400"].inner_vlan)
        business = document.business_interface_names()
        self.assertIn("BVI300", business)
        self.assertNotIn("BVI400", business)


class JunosInterfaceClassificationTest(unittest.TestCase):
    def test_junos_virtual_and_unknown_interface_classification(self):
        """Junos 虚拟前缀与未知前缀不会落入 physical。"""
        document = JunosDocument(interface_config("junos_virtual_unknown_classification.cfg"))
        kinds = {spec.parent: spec.kind for spec in document.interface_specs()}
        self.assertEqual(kinds["ge-0/0/0"], "physical")
        self.assertEqual(kinds["gr-0/0/0"], "virtual")
        self.assertEqual(kinds["future0"], "unknown")
        business = document.business_interface_names()
        self.assertNotIn("gr-0/0/0.0", business)
        self.assertNotIn("future0.0", business)

    def test_junos_inactive_interfaces_and_units_are_not_discovered(self):
        document = JunosDocument(interface_config("junos_inactive_interfaces_units.cfg"))
        specs = document.interface_specs()

        self.assertEqual([spec.name for spec in specs], ["ge-0/0/1.300"])
        self.assertEqual(specs[0].vlan, 301)
        self.assertEqual(document.business_interface_names(), {"ge-0/0/1.300"})
        rendered = document.render()
        self.assertIn("ge-0/0/1 {", rendered)
        self.assertIn("inactive: unit 200 {", rendered)
        self.assertIn("inactive: vlan-id 300;", rendered)
        self.assertNotIn("protect:", rendered)


if __name__ == "__main__":
    unittest.main()
