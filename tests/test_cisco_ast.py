"""验证 IOS XR 持久 AST 的层级、编辑与渲染行为。"""

from __future__ import annotations

import unittest

from config_adaptor.models import WashingPolicy
from config_adaptor.parsers.cisco_iosxr import CiscoDocument, CiscoNode


class CiscoAstTest(unittest.TestCase):
    """覆盖多级缩进和 ``!`` 边界组成的树结构。"""

    def test_group_accepts_unindented_children_until_end_group(self):
        """Group 内的无缩进命令不应被误判为新顶层块。"""
        source = (
            "group COMMON\n"
            "interface 'GigabitEthernet.*'\n"
            " mtu 9000\n"
            "end-group\n"
            "!\n"
            "hostname XR\n"
            "!\n"
            "end\n"
        )

        document = CiscoDocument(source)
        group = document.root.children[0]

        self.assertEqual(group.header, "group COMMON")
        self.assertEqual(group.children[0].header, "interface 'GigabitEthernet.*'")
        self.assertEqual(group.children[0].children[0].header, "mtu 9000")
        self.assertEqual(document.root.children[1].header, "end-group")
        self.assertEqual(document.root.children[3].header, "hostname XR")

    def test_hostname_returns_last_active_top_level_value(self):
        """多条 hostname 时应返回文档顺序中最后一条生效值。"""
        document = CiscoDocument(
            "hostname OLD\n"
            "!\n"
            "hostname CURRENT\n"
            "!\n"
            "end\n"
        )

        self.assertEqual(document.hostname(), "CURRENT")

    def test_hostname_returns_none_when_absent(self):
        """没有 hostname 命令时应返回 None，而不是报错。"""
        document = CiscoDocument("interface GigabitEthernet0/0/0/0\n!\nend\n")

        self.assertIsNone(document.hostname())

    def test_empty_interface_receives_inherited_group_configuration(self):
        """空接口仍是配置块，应能接收根层 group 的继承配置。"""
        source = (
            "group COMMON\n"
            " interface 'GigabitEthernet.*'\n"
            "  mtu 9000\n"
            " !\n"
            "end-group\n"
            "!\n"
            "apply-group COMMON\n"
            "!\n"
            "interface GigabitEthernet0/0/0/5\n"
            "!\n"
            "end\n"
        )

        document = CiscoDocument(source)
        interface = next(
            node
            for node in document.root.children
            if node.header.startswith("interface ")
        )

        self.assertTrue(interface.is_block)
        outcome = document.expand_groups([])

        self.assertTrue(outcome.success)
        self.assertIn(" mtu 9000\n", document.render())

    def test_indented_bang_closes_only_its_own_ast_level(self):
        """缩进 ``!`` 应位于被关闭层级的父节点下，并且不接管后续命令。"""
        source = (
            "router ospf CORE\n"
            " area 0\n"
            "  interface GigabitEthernet0/0/0/0\n"
            "   bfd fast-detect\n"
            "   !\n"
            "  !\n"
            " !\n"
            "!\n"
            "end\n"
        )

        document = CiscoDocument(source)
        block = document.root.children[0]
        area = block.children[0]
        interface = area.children[0]

        self.assertIsInstance(block, CiscoNode)
        self.assertIsInstance(area, CiscoNode)
        self.assertEqual(area.header, "area 0")
        self.assertEqual(interface.header, "interface GigabitEthernet0/0/0/0")
        self.assertEqual(interface.children[0].header, "bfd fast-detect")
        self.assertEqual(interface.children[1].header, "!")
        self.assertEqual(area.children[1].header, "!")
        self.assertEqual(block.children[1].header, "!")
        self.assertEqual(document.render(), source)

    def test_nested_cleanup_removes_complete_subtree(self):
        """清理嵌套认证模式时应连同秘钥子树删除，不影响同级业务命令。"""
        document = CiscoDocument(
            "router ospf CORE\n"
            " area 0\n"
            "  interface GigabitEthernet0/0/0/0\n"
            "   authentication\n"
            "    key-chain PROD\n"
            "   cost 10\n"
            "  !\n"
            " !\n"
            "!\n"
            "end\n"
        )

        outcome = document.clean_optional_features(
            WashingPolicy(protocol_authentication=True)
        )
        rendered = document.render()

        self.assertEqual(outcome.removed["protocol-auth-reference"], 2)
        self.assertNotIn("authentication", rendered)
        self.assertNotIn("key-chain PROD", rendered)
        self.assertIn("   cost 10", rendered)

    def test_reference_replacement_preserves_nested_subtrees(self):
        """深层接口引用一对多展开时，每个副本都应保留原节点的 children。"""
        document = CiscoDocument(
            "l2vpn\n"
            " bridge group CORE\n"
            "  bridge-domain CUSTOMER\n"
            "   interface Bundle-Ether10.100\n"
            "    static-mac-address aaaa.bbbb.cccc\n"
            "   !\n"
            "  !\n"
            " !\n"
            "!\n"
            "end\n"
        )

        document.replace_references(
            {
                "Bundle-Ether10.100": [
                    "GigabitEthernet0/0/0/0.100",
                    "GigabitEthernet0/0/0/1.100",
                ]
            }
        )
        rendered = document.render()

        self.assertEqual(rendered.count("static-mac-address aaaa.bbbb.cccc"), 2)
        self.assertIn("interface GigabitEthernet0/0/0/0.100", rendered)
        self.assertIn("interface GigabitEthernet0/0/0/1.100", rendered)
        self.assertNotIn("interface Bundle-Ether10.100", rendered)


if __name__ == "__main__":
    unittest.main()
