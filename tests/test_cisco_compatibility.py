"""目标接口类型约束的离线验证；不代替实际 XRv9000 commit 检查。"""

from pathlib import Path
import unittest

from config_adaptor.cisco.compatibility import adapt_interfaces, validate_interfaces
from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.cisco.interfaces import clone_interface_tree, ensure_parent_interface, find_interface_node


FIXTURES = Path(__file__).parent / "fixtures" / "interface_compatibility"
TARGETS = {"GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/1.200"}


class CiscoCompatibilityTest(unittest.TestCase):
    def test_active_configuration_after_end_fails_final_validation(self):
        document = CiscoDocument((FIXTURES / "iosxr_early_end.cfg").read_text())
        outcome = validate_interfaces(document, TARGETS)
        self.assertTrue(any("end 后存在活动配置" in error for error in outcome.errors))

    def test_scalar_conflicts_fail_without_rejecting_multiple_secondary_or_ipv6_addresses(self):
        document = CiscoDocument((FIXTURES / "iosxr_scalar_conflicts.cfg").read_text())
        outcome = validate_interfaces(document, TARGETS)
        self.assertEqual(len(outcome.errors), 2)
        self.assertTrue(any("mtu 存在冲突" in error for error in outcome.errors))
        self.assertTrue(any("ipv4 address primary 存在冲突" in error for error in outcome.errors))

    def test_adaptation_preserves_business_and_unknown_commands(self):
        document = CiscoDocument((FIXTURES / "iosxr_targets.cfg").read_text())
        outcome = adapt_interfaces(document, TARGETS)
        self.assertFalse(outcome.errors)
        self.assertEqual(len(outcome.changes), 6)
        parent = find_interface_node(document, "GigabitEthernet0/0/0/0")
        sub = find_interface_node(document, "GigabitEthernet0/0/0/0.100")
        self.assertIn("ethernet dot1q tunneling ethertype 0x9100", [node.header for node in parent.children])
        self.assertNotIn("ethernet dot1q tunneling ethertype 0x9100", [node.header for node in sub.children])
        rendered = document.render()
        self.assertIn("ipv4 address 192.0.2.1", rendered)
        self.assertIn("rewrite ingress tag pop 1 symmetric", rendered)
        self.assertIn("routed interface BVI300", rendered)
        self.assertIn("unknown-platform-feature preserve-this", rendered)
        self.assertEqual(rendered.count("speed 10000"), 1)
        validation = validate_interfaces(document, TARGETS)
        self.assertFalse(validation.errors, validation.errors)
        self.assertEqual(len(validation.warnings), 1)
        self.assertIn("unknown-platform-feature", validation.warnings[0])
        self.assertEqual(adapt_interfaces(document, TARGETS).changes, [])

    def test_final_validation_detects_type_and_reference_conflicts(self):
        document = CiscoDocument((FIXTURES / "iosxr_invalid.cfg").read_text())
        outcome = validate_interfaces(document, TARGETS)
        text = "\n".join(outcome.errors)
        for fragment in ("三层地址或 VRF", "子接口封装", "父口不能配置为 l2transport", "L2 标签改写", "BVI 接口", "必须配置 l2transport", "不能引用 GigabitEthernet", "不存在的 BVI"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_ethertype_collision_is_reported_without_overwriting_source(self):
        document = CiscoDocument((FIXTURES / "iosxr_targets.cfg").read_text())
        parent = find_interface_node(document, "GigabitEthernet0/0/0/0")
        source = find_interface_node(document, "GigabitEthernet0/0/0/0.100")
        setting = next(node for node in source.children if "ethertype" in node.header)
        different = setting.clone()
        different.header = different.header.replace("0x9100", "0x9200")
        parent.children.append(different)
        outcome = adapt_interfaces(document, TARGETS)
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("冲突", outcome.errors[0])
        self.assertIn(setting, source.children)
        self.assertIn(different, parent.children)

    def test_new_parent_and_missing_clone_are_inserted_before_end(self):
        document = CiscoDocument("hostname R1\n!\nend\n")
        ensure_parent_interface(document, "GigabitEthernet0/0/0/0")
        clone_interface_tree(document, "Bundle-Ether10", "GigabitEthernet0/0/0/1")
        commands = document.render().strip().splitlines()
        self.assertEqual(commands[-1], "end")
        self.assertIn("interface GigabitEthernet0/0/0/0", commands)
        self.assertIn("interface GigabitEthernet0/0/0/1", commands)

    def test_ethertype_parent_is_created_before_end(self):
        document = CiscoDocument((FIXTURES / "iosxr_targets.cfg").read_text())
        parent = find_interface_node(document, "GigabitEthernet0/0/0/0")
        parent.active = False
        outcome = adapt_interfaces(document, TARGETS)
        self.assertFalse(outcome.errors)
        self.assertEqual(document.render().strip().splitlines()[-1], "end")
        parent = find_interface_node(document, "GigabitEthernet0/0/0/0")
        self.assertTrue(any("ethertype" in node.header for node in parent.children))


if __name__ == "__main__":
    unittest.main()
