"""验证 Junos groups/apply-groups 展开、优先级、inactive/protect 与回滚边界。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config_adaptor.common.policies import WashingPolicy
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures" / "group_configs"


def group_config(name: str) -> str:
    """读取 Junos group 展开测试配置。"""
    return (FIXTURES / name).read_text(encoding="utf-8")


class JunosGroupExpansionTest(unittest.TestCase):
    """覆盖 Junos group 的静态展开、冲突与失败回滚。"""

    def test_vmx_prunes_re1_before_expanding_other_groups(self):
        document = JunosDocument(
            "groups { re0 { system { host-name primary; } } "
            "re1 { system { host-name standby; } } "
            "re10 { system { domain-name example.net; } } }\n"
            "apply-groups [ re0 re1 re10 ];\n"
        )

        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertIn("host-name primary;", rendered)
        self.assertIn("domain-name example.net;", rendered)
        self.assertNotIn("host-name standby;", rendered)
        self.assertNotIn("re1", rendered)
        self.assertIn("已裁剪 Junos 配置组 re1 及其引用", outcome.events)

    def test_vmx_prunes_re1_only_and_nested_references(self):
        document = JunosDocument(
            "groups { re0 { system { host-name primary; apply-groups re1; } } "
            "re1 { system { host-name standby; } } }\n"
            "system { apply-groups-except re1; }\n"
            "apply-groups re0;\n"
        )

        outcome = document.expand_groups([])

        self.assertTrue(outcome.success)
        self.assertIn("host-name primary;", document.render())
        self.assertNotIn("standby", document.render())
        self.assertNotIn("apply-groups", document.render())
        self.assertNotIn("re1", document.render())

        only_re1 = JunosDocument(
            "groups { re1 { system { host-name standby; } } }\n"
            "apply-groups re1;\n"
        )
        self.assertTrue(only_re1.expand_groups([]).success)
        self.assertEqual(only_re1.render().strip(), "")

        unused_re1 = JunosDocument(
            "groups { re1 { system { host-name standby; } } }\n"
        )
        self.assertTrue(unused_re1.expand_groups([]).success)
        self.assertEqual(unused_re1.render().strip(), "")

    def test_vmx_re1_pruning_rolls_back_if_other_group_is_invalid(self):
        source = (
            "groups { re1 { system { host-name standby; } } }\n"
            "apply-groups [ re1 MISSING ];\n"
        )
        document = JunosDocument(source)
        original_rendered = document.render()

        outcome = document.expand_groups([])

        self.assertFalse(outcome.success)
        self.assertEqual(document.render(), original_rendered)
        self.assertNotIn("已裁剪 Junos 配置组 re1 及其引用", outcome.events)

    def test_junos_re1_pruning_can_be_disabled_by_policy(self):
        document = JunosDocument(
            "groups { re1 { system { host-name standby; } } }\n"
            "apply-groups re1;\n"
        )

        outcome = document.expand_groups(
            [], policy=WashingPolicy(junos_excluded_groups=())
        )

        self.assertTrue(outcome.success)
        self.assertIn("host-name standby;", document.render())

    def test_junos_wildcard_group_expansion(self):
        document = JunosDocument(group_config("junos_group_wildcard.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertFalse(outcome.warnings)
        self.assertTrue(outcome.events)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups COMMON", rendered)

    def test_group_payload_upgrades_matching_leaf_interfaces_to_blocks(self):
        document = JunosDocument(
            "groups { RSVP { protocols { rsvp { interface <*> { "
            "aggregate; link-protection; } } } } }\n"
            "protocols { rsvp { apply-groups RSVP; "
            "interface xe-9/0/0.0; interface ae1.0; } }\n"
        )

        outcome = document.expand_groups([])

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.identity_fallbacks, 4)
        self.assertEqual(
            document.render(),
            "protocols {\n"
            "    rsvp {\n"
            "        interface xe-9/0/0.0 {\n"
            "            aggregate;\n"
            "            link-protection;\n"
            "        }\n"
            "        interface ae1.0 {\n"
            "            aggregate;\n"
            "            link-protection;\n"
            "        }\n"
            "    }\n"
            "}\n",
        )

    def test_group_block_matches_existing_block_and_only_matching_leaf(self):
        document = JunosDocument(
            "groups { G { protocols { rsvp { interface <xe-*> { aggregate; } } } } }\n"
            "protocols { rsvp { apply-groups G; "
            "interface xe-9/0/0.0 { link-protection; } "
            "interface xe-9/0/1.0; interface ae1.0; } }\n"
        )

        self.assertTrue(document.expand_groups([]).success)
        rendered = document.render()
        self.assertEqual(rendered.count("aggregate;"), 2)
        self.assertIn("interface xe-9/0/0.0 {\n", rendered)
        self.assertIn("link-protection;\n", rendered)
        self.assertIn("interface xe-9/0/1.0 {\n", rendered)
        self.assertIn("interface ae1.0;\n", rendered)

    def test_comment_only_group_block_does_not_erase_matching_leaf(self):
        document = JunosDocument(
            "groups { G { protocols { rsvp { interface <*> { # note\n"
            "} } } } }\n"
            "protocols { rsvp { apply-groups G; interface xe-9/0/0.0; } }\n"
        )

        self.assertTrue(document.expand_groups([]).success)
        self.assertIn("interface xe-9/0/0.0;\n", document.render())

    def test_group_without_matching_descendant_keeps_original_leaf(self):
        document = JunosDocument(
            "groups { G { protocols { rsvp { interface <*> { "
            "apply-groups H; } } } } H { system { host-name lab; } } }\n"
            "protocols { rsvp { apply-groups G; interface xe-9/0/0.0; } }\n"
        )

        self.assertTrue(document.expand_groups([]).success)
        self.assertEqual(
            document.render(),
            "protocols {\n    rsvp {\n        interface xe-9/0/0.0;\n    }\n}\n",
        )

    def test_junos_multiple_group_containers_share_dependency_index(self):
        document = JunosDocument(group_config("junos_multiple_containers_shared_index.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)
        self.assertEqual(
            set(outcome.events),
            {
                "已展开 Junos 配置组 BASE",
                "已展开 Junos 配置组 EDGE",
            },
        )

    def test_junos_multiple_group_containers_normalize_without_application(self):
        document = JunosDocument(group_config("junos_multiple_containers_normalize.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertEqual(rendered.count("groups {"), 1)
        self.assertIn("FIRST {", rendered)
        self.assertIn("SECOND {", rendered)

    def test_junos_duplicate_group_definitions_merge_their_payload(self):
        document = JunosDocument(group_config("junos_duplicate_definitions_merge.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertIn("mtu 9000;", rendered)
        self.assertIn("host-name lab;", rendered)
        self.assertNotIn("groups {", rendered)

    def test_full_expansion_rejects_unrelated_invalid_junos_group(self):
        source = group_config("junos_invalid_unrelated_group.cfg")
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(any("MISSING" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_junos_expands_non_interface_group(self):
        document = JunosDocument(group_config("junos_non_interface_group.cfg"))
        outcome = document.expand_groups([])
        self.assertTrue(outcome.success)
        self.assertNotIn("groups {", document.render())
        self.assertNotIn("apply-groups", document.render())
        self.assertIn("syslog {", document.render())

    def test_junos_nested_list_and_except_precedence(self):
        document = JunosDocument(group_config("junos_group_precedence.cfg"))
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
        document = JunosDocument(group_config("junos_group_transitive.cfg"))
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
        document = JunosDocument(group_config("junos_nested_group_except.cfg"))
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
        source = group_config("junos_undefined_except.cfg")
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

    def test_junos_reports_every_undefined_group_exclusion(self):
        """一次报告同一语句中的全部未定义 except，方便集中修复。"""
        source = group_config("junos_every_undefined_exclusion.cfg")
        document = JunosDocument(source)
        outcome = document.expand_groups([])

        self.assertFalse(outcome.success)
        self.assertEqual(len(outcome.warnings), 2)
        self.assertTrue(any("MISSING-A" in warning for warning in outcome.warnings))
        self.assertTrue(any("MISSING-B" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_junos_selected_group_validates_apply_groups_except(self):
        """已选中 group 内的 except 引用缺失时整体回滚。"""
        source = group_config("junos_selected_group_validates_except.cfg")
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(any("MISSING" in warning for warning in outcome.warnings))
        self.assertEqual(document.render(), source)

    def test_junos_inactive_and_unused_group_exclusions_do_not_block_expansion(self):
        """inactive 引用会删除，未使用模板中的 except 不阻断完整展开。"""
        document = JunosDocument(group_config("junos_inactive_unused_exclusions.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("MISSING-IN-UNUSED", rendered)
        self.assertNotIn("MISSING-INACTIVE", rendered)

    def test_junos_apply_groups_except_does_not_create_dependency(self):
        """合法 except 引用只校验存在性，不会单独选中或展开 group。"""
        document = JunosDocument(group_config("junos_except_no_dependency.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertNotIn("已展开 Junos 配置组 EXCLUDED", outcome.events)
        self.assertNotIn("description SHOULD-NOT-EXPAND;", rendered)
        self.assertNotIn("groups {", rendered)

    def test_junos_nested_group_cycle_and_missing_reference_roll_back(self):
        """循环与间接未定义引用均在写回前失败并保留原配置。"""
        cycle = group_config("junos_group_cycle.cfg")
        cycle_document = JunosDocument(cycle)
        cycle_outcome = cycle_document.expand_groups([])
        self.assertFalse(cycle_outcome.success)
        self.assertTrue(any("循环引用" in warning for warning in cycle_outcome.warnings))
        self.assertEqual(cycle_document.render(), cycle)

        missing = group_config("junos_group_missing_reference.cfg")
        missing_document = JunosDocument(missing)
        missing_outcome = missing_document.expand_groups([])
        self.assertFalse(missing_outcome.success)
        self.assertTrue(any("MISSING" in warning for warning in missing_outcome.warnings))
        self.assertEqual(missing_document.render(), missing)

    def test_junos_removes_unapplied_group_definitions_after_expansion(self):
        """活动引用完整展开后删除全部 group 定义，包括未应用模板。"""
        document = JunosDocument(group_config("junos_unapplied_definitions_removed.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()
        self.assertTrue(outcome.success)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)
        self.assertEqual(rendered.count("mtu 9000;"), 1)

    def test_group_expansion_rolls_back_on_unresolved_reference(self):
        source = group_config("junos_group_unresolved.cfg")
        document = JunosDocument(source)
        outcome = document.expand_groups([])
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.warnings)
        self.assertIn("apply-groups MISSING", document.render())
        self.assertIn("groups {", document.render())

    def test_undefined_root_group_fails_without_any_definitions(self):
        junos_source = group_config("junos_undefined_root_group.cfg")
        junos = JunosDocument(junos_source)
        junos_outcome = junos.expand_groups([])
        self.assertFalse(junos_outcome.success)
        self.assertTrue(any("MISSING" in item for item in junos_outcome.warnings))
        self.assertEqual(junos.render(), junos_source)

    def test_junos_inactive_apply_groups_are_removed_without_expansion(self):
        document = JunosDocument(group_config("junos_inactive_apply_groups.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.events)
        self.assertFalse(outcome.warnings)
        self.assertIn("groups {", rendered)
        self.assertNotIn("inactive: apply-groups", rendered)
        self.assertNotIn("MISSING", rendered)

    def test_junos_inactive_group_definition_is_removed_without_active_groups(self):
        document = JunosDocument(group_config("junos_inactive_group_definition.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("DISABLED", rendered)
        self.assertIn("host-name current;", rendered)

    def test_junos_all_inactive_nodes_are_removed_without_group_expansion(self):
        document = JunosDocument(group_config("junos_all_inactive_nodes.cfg"))
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
        document = JunosDocument(group_config("junos_inactive_nested_controls.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_inactive_group_is_not_expanded(self):
        document = JunosDocument(group_config("junos_inactive_group_not_expanded.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.events, ["已展开 Junos 配置组 ENABLED"])
        self.assertNotIn("inactive: DISABLED {", rendered)
        self.assertNotIn("inactive: apply-groups DISABLED;", rendered)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("description disabled-group;", rendered)

    def test_junos_inactive_value_is_removed_before_group_merge(self):
        document = JunosDocument(group_config("junos_inactive_value_before_merge.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertNotIn("inactive: mtu 1500;", rendered)
        self.assertIn("mtu 9000;", rendered)
        self.assertFalse(outcome.conflicts)

    def test_junos_protect_prefix_is_effective_but_removed_from_output(self):
        document = JunosDocument(group_config("junos_protect_prefix.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertIn("mtu 9000;", rendered)
        self.assertNotIn("protect:", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)

    def test_junos_quoted_group_names_in_list_are_fully_expanded(self):
        document = JunosDocument(group_config("junos_quoted_group_names_list.cfg"))
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
        document = JunosDocument(group_config("junos_quoted_nested_group_except.cfg"))
        outcome = document.expand_groups([])
        rendered = document.render()

        self.assertTrue(outcome.success)
        self.assertFalse(outcome.warnings)
        self.assertNotIn("mtu 9000;", rendered)
        self.assertNotIn("groups {", rendered)
        self.assertNotIn("apply-groups", rendered)


if __name__ == "__main__":
    unittest.main()
