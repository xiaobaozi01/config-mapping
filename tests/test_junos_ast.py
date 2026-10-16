"""验证 Junos 大括号语法的解析、行内块展开与渲染行为。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from config_adaptor.juniper.document import JunosDocument, JunosNode
from config_adaptor.juniper.empty_blocks import EmptyBlockPolicy


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

    def test_render_omits_configured_empty_containers(self):
        document = JunosDocument(
            "system {\n"
            "    host-name lab;\n"
            "    services {\n\n    }\n"
            "    login {\n        user old;\n    }\n"
            "}\n"
            "interfaces {\n    ge-0/0/0 {\n    }\n}\n"
        )
        system = document.root.children[0]
        system.children[2].children[0].active = False

        self.assertEqual(
            document.render(),
            "system {\n"
            "    host-name lab;\n"
            "}\n"
            "interfaces {\n"
            "    ge-0/0/0;\n"
            "}\n",
        )

    def test_render_preserves_active_ancestors_of_deleted_leaf(self):
        document = JunosDocument(
            "protocols {\n"
            "    ospf {\n"
            "        area 0.0.0.0 {\n"
            "            interface ae0.0;\n"
            "        }\n"
            "    }\n"
            "    bgp {\n        group CORE {\n            type internal;\n        }\n    }\n"
            "}\n"
        )
        document.root.children[0].children[0].children[0].children[0].active = False

        self.assertEqual(
            document.render(),
            "protocols {\n"
            "    ospf {\n        area 0.0.0.0;\n    }\n"
            "    bgp {\n        group CORE {\n            type internal;\n        }\n    }\n"
            "}\n",
        )

    def test_render_omits_deleted_blocks_and_generated_empty_containers(self):
        document = JunosDocument(
            "system { host-name lab; services { ssh; } }\n"
        )
        system = document.root.children[0]
        system.children[1].active = False
        system.children.append(JunosNode("generated", []))

        self.assertEqual(
            document.render(),
            "system {\n    host-name lab;\n}\n",
        )

    def test_comment_does_not_activate_or_rewrite_interface(self):
        source = (
            "interfaces {\n ge-0/0/1 {\n  description UNUSED;\n }\n}\n"
            "# ge-0/0/1\n/* ge-0/0/1 */\n"
        )
        document = JunosDocument(source)

        self.assertEqual(document.root.children[1].comment, "# ge-0/0/1")
        self.assertEqual(document.business_interface_names(), set())
        self.assertEqual(document.apply_cleaning_rule((), "^#|^/\\*", "delete", None), 0)
        document.replace_references({"ge-0/0/1": ["ge-0/0/2"]})
        self.assertIn("# ge-0/0/1\n/* ge-0/0/1 */\n", document.render())

    def test_inline_comment_stays_with_statement_and_quoted_hash_is_literal(self):
        document = JunosDocument(
            'system {\n host-name "lab#1"; # host note\n'
            ' services {\n  ssh; # access note\n }\n}\n'
        )
        system = document.root.children[0]

        self.assertEqual(system.children[0].header, 'host-name "lab#1";')
        self.assertEqual(system.children[0].comment, "# host note")
        self.assertIn('host-name "lab#1"; # host note\n', document.render())
        system.children[1].children[0].active = False
        self.assertNotIn("access note", document.render())
        self.assertNotIn("services;\n", document.render())

    def test_standalone_comment_does_not_keep_omitted_block(self):
        document = JunosDocument("system {\n services {\n  # note\n }\n}\n")

        self.assertNotIn("services", document.render())
        self.assertNotIn("# note", document.render())

    def test_inline_comment_does_not_keep_omitted_block(self):
        document = JunosDocument("security { # note\n}\n")

        self.assertNotIn("security", document.render())
        self.assertNotIn("# note", document.render())

    def test_omitted_block_with_configuration_keeps_its_comments(self):
        document = JunosDocument("security { # note\n policies {\n  policy P;\n }\n}\n")

        self.assertIn("security { # note", document.render())
        self.assertIn("policy P;", document.render())

    def test_empty_security_is_omitted_but_nonempty_security_is_kept(self):
        document = JunosDocument(
            "security {\n ssh-known-hosts {\n host peer {\n rsa-key KEY;\n }\n }\n}\n"
        )
        self.assertIn("security {", document.render())

        document.clean_management_access()
        self.assertNotIn("security", document.render())

    def test_empty_block_policy_keeps_independent_family_statement(self):
        document = JunosDocument(
            "interfaces { ge-0/0/0 { unit 0 { family inet { } } } }\n"
        )

        self.assertIn("family inet;", document.render())

    def test_empty_block_policy_can_keep_braces(self):
        document = JunosDocument("security {\n}\n")
        policy = EmptyBlockPolicy("semicolon", ((("security",), "braces"),))

        with patch("config_adaptor.juniper.document.vmx_empty_block_policy", return_value=policy):
            self.assertEqual(document.render(), "security {\n}\n")

    def test_empty_block_policy_controls_shape_when_only_comments_remain(self):
        document = JunosDocument("security { # header note\n # inner note\n}\n")
        expected = {
            "omit": "\n",
            "semicolon": "# inner note\nsecurity; # header note\n",
            "braces": "security { # header note\n    # inner note\n}\n",
        }
        for action, rendered in expected.items():
            with self.subTest(action=action):
                policy = EmptyBlockPolicy(action, ())
                with patch(
                    "config_adaptor.juniper.document.vmx_empty_block_policy",
                    return_value=policy,
                ):
                    self.assertEqual(document.render(), rendered)

    def test_generated_empty_block_ignores_yaml_default(self):
        document = JunosDocument("")
        document.root.children.append(JunosNode("generated", [JunosNode("", comment="# note")]))
        policy = EmptyBlockPolicy("braces", ())

        with patch("config_adaptor.juniper.document.vmx_empty_block_policy", return_value=policy):
            self.assertEqual(document.render(), "\n")

    def test_group_expansion_keeps_comments_outside_group_definition(self):
        document = JunosDocument(
            "groups { G { system { host-name lab; } } }\n"
            "apply-groups G;\n# top note\n"
            "system { # block note\n    services {\n        # nested note\n        ssh;\n    }\n}\n"
        )

        self.assertTrue(document.expand_groups([]).success)
        rendered = document.render()
        self.assertIn("# top note\n", rendered)
        self.assertIn("system { # block note\n", rendered)
        self.assertIn("# nested note\n", rendered)
        self.assertIn("host-name lab;\n", rendered)

    def test_junos_inline_parser_still_rejects_unbalanced_braces(self):
        with self.assertRaisesRegex(ValueError, "第 1 行出现多余右大括号"):
            JunosDocument("system { host-name lab; } }")
        with self.assertRaisesRegex(ValueError, "大括号不平衡"):
            JunosDocument("system { host-name lab;")

    def test_extra_closing_brace_reports_original_line_after_inline_expansion(self):
        source = (
            "system { host-name lab; services { ssh; } }\n"
            "# 注释中的 } 不参与配对\n"
            "interfaces { ge-0/0/0 { mtu 9000; } }\n"
            "};\n"
        )

        with self.assertRaisesRegex(ValueError, "Junos 配置第 4 行出现多余右大括号"):
            JunosDocument(source)


if __name__ == "__main__":
    unittest.main()
