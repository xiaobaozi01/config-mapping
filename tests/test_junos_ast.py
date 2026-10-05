"""验证 Junos 大括号语法的解析、行内块展开与渲染行为。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures" / "junos_ast"


def junos_config(name: str) -> str:
    """读取 Junos 解析测试配置。"""
    return (FIXTURES / name).read_text(encoding="utf-8")


class JunosAstTest(unittest.TestCase):
    """覆盖行内块、注释、hostname 与括号边界。"""

    def test_junos_inline_blocks_are_parsed_and_expanded(self):
        source = junos_config("inline_blocks.cfg")
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
        document = JunosDocument(junos_config("comments_with_braces.cfg"))
        rendered = document.render()

        self.assertIn("system {", rendered)
        self.assertIn("/* ignored { block; } */", rendered)
        self.assertIn('host-name "lab;{vmx}";', rendered)
        self.assertIn("services {", rendered)
        self.assertIn("ssh;", rendered)

    def test_junos_hostname_returns_last_active_system_value(self):
        """应跳过 inactive 语句，返回最后一条生效且已去引号的 host-name。"""
        document = JunosDocument(junos_config("hostname_last_active.cfg"))

        self.assertEqual(document.hostname(), "current-name")

    def test_junos_hostname_returns_none_without_system_block(self):
        """没有 system/host-name 时应返回 None，而不是报错。"""
        document = JunosDocument(junos_config("hostname_absent.cfg"))

        self.assertIsNone(document.hostname())

    def test_junos_inline_parser_still_rejects_unbalanced_braces(self):
        with self.assertRaisesRegex(ValueError, "出现多余右大括号"):
            JunosDocument("system { host-name lab; } }")
        with self.assertRaisesRegex(ValueError, "大括号不平衡"):
            JunosDocument("system { host-name lab;")


if __name__ == "__main__":
    unittest.main()
