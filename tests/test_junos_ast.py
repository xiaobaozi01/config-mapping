"""验证 Junos 大括号语法的解析、行内块展开与渲染行为。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document import (
    JunosDocument,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.support.empty_blocks import (
    EmptyBlockPolicy,
)

FIXTURES = Path(__file__).parent / "fixtures" / "junos_ast"


def junos_config(name: str) -> str:
    """读取 Junos 解析测试配置。"""
    return (FIXTURES / name).read_text(encoding="utf-8")


def delete(document, context):
    """通过文本事务删除一个 Junos Context。"""
    transaction = document.edit()
    transaction.delete(context, "test setup")
    transaction.apply()


class JunosAstTest(unittest.TestCase):
    """覆盖行内块、注释、hostname 与括号边界。"""

    def test_junos_inline_blocks_are_parsed_and_expanded(self):
        source = junos_config("inline_blocks.cfg")
        document = JunosDocument(source)
        outcome = document.groups.expand([])
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

    def test_junos_selector_configuration_preserves_tree_on_reparse(self):
        document = JunosDocument(junos_config("selectors_with_slashes.cfg"))
        groups = document.root.nodes[1]
        self.assertEqual(document.root.nodes[0].header, "version 21.2R3-S7.7")
        self.assertEqual(
            [node.header for node in groups.nodes],
            ["re0", "re1", "Exclude_RED_Link", "arp-150k", "COS_Group_Untrust"],
        )
        interfaces = groups.nodes[3].nodes[0]
        self.assertEqual(
            [node.header for node in interfaces.nodes], ["<ae*>", "<*/*/*>"]
        )
        self.assertIn(
            "arp police-150k",
            [node.header for node in interfaces.nodes[1].walk()],
        )
        rendered = document.render()
        reparsed = JunosDocument(rendered)
        self.assertEqual(
            [
                (node.kind, node.header, node.path)
                for node in document.root.walk()
            ],
            [
                (node.kind, node.header, node.path)
                for node in reparsed.root.walk()
            ],
        )
        self.assertEqual(reparsed.render(), rendered)

    def test_junos_selectors_keep_comments_quotes_and_optional_semicolons(self):
        for selector in ("<*/*/*>", "<ae*>", "<*>", "<ge-[0-9]/*/*>"):
            for ending in (";", ""):
                with self.subTest(selector=selector, ending=ending):
                    document = JunosDocument(
                        f"interfaces {{\n{selector} /* selector note */\n{{\n"
                        "unit <*> {\n"
                        f'description "literal /* and */"{ending} # leaf }} ;\n'
                        "}\n}\n} # tail\n"
                    )
                    interface = document.root.nodes[0].nodes[0]
                    self.assertEqual(interface.header, selector)
                    self.assertEqual(interface.comment, "/* selector note */")
                    self.assertEqual(interface.nodes[0].header, "unit <*>")
                    leaf = interface.nodes[0].nodes[0]
                    self.assertEqual(
                        leaf.header, 'description "literal /* and */"'
                    )
                    self.assertEqual(leaf.comment, "# leaf } ;")
                    self.assertEqual(document.root.nodes[0].comment, "# tail")
                    rendered = document.render()
                    self.assertEqual(JunosDocument(rendered).render(), rendered)

    def test_junos_selector_does_not_hide_unclosed_real_comment(self):
        for comment in ("/* unclosed", "/* <*/*/*>"):
            with self.subTest(comment=comment):
                with self.assertRaisesRegex(
                    ValueError, "unclosed quote or comment"
                ):
                    JunosDocument(
                        f"interfaces {{ <*/*/*> {{ unit <*>; }} }} {comment}"
                    )

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
        system = document.root.nodes[0]
        delete(document, system.nodes[2].nodes[0])

        rendered = document.render()
        self.assertNotIn("login", rendered)
        self.assertNotIn("services", rendered)
        self.assertIn("host-name lab;", rendered)
        self.assertIn("ge-0/0/0;", rendered)

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
        interface = document.root.nodes[0].nodes[0].nodes[0].nodes[0]
        delete(document, interface)

        rendered = document.render()
        self.assertNotIn("interface ae0.0", rendered)
        self.assertIn("area 0.0.0.0;", rendered)
        self.assertIn("group CORE {", rendered)

    def test_render_omits_deleted_blocks_and_generated_empty_containers(self):
        document = JunosDocument(
            "system { host-name lab; services { ssh; } }\n"
        )
        system = document.root.nodes[0]
        delete(document, system.nodes[1])

        rendered = document.render()
        self.assertNotIn("services", rendered)
        self.assertIn("host-name lab;", rendered)

    def test_comment_does_not_activate_or_rewrite_interface(self):
        source = (
            "interfaces {\n ge-0/0/1 {\n  description UNUSED;\n }\n}\n"
            "# ge-0/0/1\n/* ge-0/0/1 */\n"
        )
        document = JunosDocument(source)

        self.assertEqual(document.root.nodes[1].comment, "# ge-0/0/1")
        self.assertEqual(
            document.interfaces.analysis.business_interface_names(), set()
        )
        self.assertEqual(
            document.cleaning.apply_rule((), "^#|^/\\*", "delete", None), 0
        )
        document.references.replace({"ge-0/0/1": ["ge-0/0/2"]})
        self.assertIn("# ge-0/0/1\n/* ge-0/0/1 */\n", document.render())

    def test_inactive_references_follow_interface_mapping(self):
        document = JunosDocument(
            "protocols {\n ospf {\n  area 0 {\n"
            "   interface ge-1/0/1.0;\n"
            "   inactive: interface ge-1/0/2.0;\n"
            "   inactive: interface ge-1/0/1.0 {\n    passive;\n   }\n"
            "  }\n"
            "  inactive: area 1 {\n   interface ge-1/0/2.0;\n  }\n"
            " }\n}\n"
        )

        document.references.replace(
            {
                "ge-1/0/1.0": ["ge-0/0/1.0"],
                "ge-1/0/2.0": ["ge-0/0/2.0", "ge-0/0/3.0"],
            }
        )
        rendered = document.render()

        self.assertNotIn("ge-1/0/", rendered)
        self.assertIn("    interface ge-0/0/1.0;\n", rendered)
        self.assertIn("inactive: interface ge-0/0/1.0 {\n", rendered)
        self.assertIn("inactive: interface ge-0/0/2.0;\n", rendered)
        self.assertIn("inactive: interface ge-0/0/3.0;\n", rendered)
        self.assertIn("inactive: area 1 {", rendered)
        inactive_area = rendered.split("inactive: area 1 {", 1)[1]
        self.assertIn("interface ge-0/0/2.0;", inactive_area)
        self.assertIn("interface ge-0/0/3.0;", inactive_area)

    def test_inline_comment_stays_with_statement_and_quoted_hash_is_literal(
        self,
    ):
        document = JunosDocument(
            'system {\n host-name "lab#1"; # host note\n'
            " services {\n  ssh; # access note\n }\n}\n"
        )
        system = document.root.nodes[0]

        self.assertEqual(system.nodes[0].header, 'host-name "lab#1"')
        self.assertEqual(system.nodes[0].comment, "# host note")
        self.assertIn('host-name "lab#1"; # host note\n', document.render())
        delete(document, system.nodes[1].nodes[0])
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
        document = JunosDocument(
            "security { # note\n policies {\n  policy P;\n }\n}\n"
        )

        self.assertIn("security { # note", document.render())
        self.assertIn("policy P;", document.render())

    def test_empty_security_is_omitted_but_nonempty_security_is_kept(self):
        document = JunosDocument(
            "security {\n ssh-known-hosts {\n host peer {\n rsa-key KEY;\n }\n }\n}\n"
        )
        self.assertIn("security {", document.render())

        document.cleaning.clean_management_access()
        self.assertNotIn("security", document.render())

    def test_empty_block_policy_keeps_independent_family_statement(self):
        document = JunosDocument(
            "interfaces { ge-0/0/0 { unit 0 { family inet { } } } }\n"
        )

        self.assertIn("family inet;", document.render())

    def test_empty_block_policy_can_keep_braces(self):
        document = JunosDocument("security {\n}\n")
        policy = EmptyBlockPolicy("semicolon", ((("security",), "braces"),))

        with patch(
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document.vmx_empty_block_policy",
            return_value=policy,
        ):
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
                    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document.vmx_empty_block_policy",
                    return_value=policy,
                ):
                    self.assertEqual(document.render(), rendered)

    def test_generated_empty_block_ignores_yaml_default(self):
        document = JunosDocument("")
        transaction = document.edit()
        transaction.append_child(
            document.root, "generated {\n    # note\n}\n", "generate"
        )
        transaction.apply()
        policy = EmptyBlockPolicy("braces", ())

        with patch(
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document.vmx_empty_block_policy",
            return_value=policy,
        ):
            self.assertEqual(document.render(), "\n")

    def test_group_expansion_keeps_comments_outside_group_definition(self):
        document = JunosDocument(
            "groups { G { system { host-name lab; } } }\n"
            "apply-groups G;\n# top note\n"
            "system { # block note\n    services {\n        # nested note\n        ssh;\n    }\n}\n"
        )

        self.assertTrue(document.groups.expand([]).success)
        rendered = document.render()
        self.assertIn("# top note\n", rendered)
        self.assertIn("# block note\n", rendered)
        self.assertIn("# nested note\n", rendered)
        self.assertIn("host-name lab;\n", rendered)

    def test_junos_inline_parser_still_rejects_unbalanced_braces(self):
        with self.assertRaisesRegex(ValueError, "第 1 行出现多余右大括号"):
            JunosDocument("system { host-name lab; } }")
        with self.assertRaisesRegex(ValueError, "大括号不平衡"):
            JunosDocument("system { host-name lab;")

    def test_extra_closing_brace_reports_original_line_after_inline_expansion(
        self,
    ):
        source = (
            "system { host-name lab; services { ssh; } }\n"
            "# 注释中的 } 不参与配对\n"
            "interfaces { ge-0/0/0 { mtu 9000; } }\n"
            "};\n"
        )

        with self.assertRaisesRegex(
            ValueError, "Junos 配置第 4 行出现多余右大括号"
        ):
            JunosDocument(source)


if __name__ == "__main__":
    unittest.main()
