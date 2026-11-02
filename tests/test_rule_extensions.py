"""四类模板经真实文档入口执行；示例从不进入默认注册表。"""

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from cisco_support import replace_header

from gns3_config_adaptor.adaptation.cleaning_rules import (
    CleaningPoint,
    CleaningRulesHandler,
)
from gns3_config_adaptor.adaptation.models import Vendor
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
    rules,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces import (
    find_interface_node,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
    examples,
)
from gns3_config_adaptor.impl.common.config_ast.model import tree_signature
from gns3_config_adaptor.impl.common.domain.outcomes import (
    InterfacePruningOutcome,
)
from gns3_config_adaptor.impl.common.domain.policies import (
    SimulationAdaptationPolicy,
)
from gns3_config_adaptor.impl.common.rules import (
    AdaptContext,
    FunctionRule,
    execute_prune_rules,
    execute_rules,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rule_extensions"


def example_document(name="examples.cfg"):
    return CiscoDocument((FIXTURES / name).read_text())


class RuleTemplateTest(unittest.TestCase):

    def test_yaml_template_runs_through_existing_scoped_handler(self):
        source = (
            Path(__file__).parents[1]
            / "docs"
            / "rule-templates"
            / "cleaning.yaml"
        )
        raw = yaml.safe_load(source.read_text())["rules"][0]
        rule = CleaningRulesHandler._parse_rule(raw, set())
        handler = CleaningRulesHandler(CleaningPoint.POST_REWRITE)
        handler._rules = [rule]
        document = example_document("cleaning.cfg")
        device = SimpleNamespace(
            document=document,
            device=SimpleNamespace(vendor=Vendor.CISCO_IOSXR),
            profile=SimpleNamespace(
                simulation_adaptation=SimulationAdaptationPolicy()
            ),
            mappings=[
                SimpleNamespace(
                    role="NNI", action="rename", target_interface="Gi0/0/0/0"
                )
            ],
        )
        context = SimpleNamespace(devices={"DEMO": device}, events=[])
        context.add_event = (
            lambda kind, message, **details: context.events.append(details)
        )
        handler.process(context)
        self.assertEqual(document.render().count("RULE_DEMO_REMOVE"), 0)
        self.assertEqual(context.events[0]["rule_id"], rule.rule_id)
        context.events.clear()
        handler.process(context)
        self.assertEqual(context.events, [])

    def test_adaptation_registration_scope_report_and_idempotence(self):
        document = example_document()
        rule = examples.ADAPT_DEMO_RULE
        with patch.object(rules, "ADAPT_RULES", (rule,)):
            outcome = document.compatibility.adapt_interfaces({"Gi0/0/0/0"})
            self.assertEqual(len(outcome.changes), 1)
            self.assertEqual(outcome.changes[0]["rule_id"], rule.rule_id)
            self.assertEqual(
                document.compatibility.adapt_interfaces({"Gi0/0/0/0"}).changes,
                [],
            )
        target = find_interface_node(document, "Gi0/0/0/0")
        other = find_interface_node(document, "Gi0/0/0/1")
        bundle = find_interface_node(document, "Bundle-Ether10")
        self.assertEqual(target.nodes[0].header, "description RULE_DEMO_NEW")
        self.assertEqual(other.nodes[0].header, "description RULE_DEMO_OLD")
        self.assertEqual(bundle.nodes[0].header, "description RULE_DEMO_OLD")

    def test_conflict_keeps_whole_rule_unmodified_and_reports_origin(self):
        document = example_document("conflict.cfg")
        before = tree_signature(document.root)
        rule = examples.ADAPT_DEMO_RULE
        with patch.object(rules, "ADAPT_RULES", (rule,)):
            result = document.compatibility.adapt_interfaces(
                {"Gi0/0/0/0", "Gi0/0/0/1"}
            )
        self.assertEqual(tree_signature(document.root), before)
        self.assertEqual(result.changes, [])
        self.assertEqual(result.diagnostics[0]["rule_id"], rule.rule_id)
        self.assertIn("冲突", result.errors[0])

    def test_empty_targets_do_not_modify_or_validate_other_interfaces(self):
        document = example_document()
        before = tree_signature(document.root)
        with patch.object(
            rules,
            "ADAPT_RULES",
            (replace(examples.ADAPT_DEMO_RULE, rule_id="adapt"),),
        ):
            self.assertEqual(
                document.compatibility.adapt_interfaces(set()).changes, []
            )
        with patch.object(
            rules,
            "VALIDATION_RULES",
            (replace(examples.VALIDATE_DEMO_RULE, rule_id="validate"),),
        ):
            self.assertEqual(
                rules.validate_interfaces(document, set(), set()).errors, []
            )
        self.assertEqual(tree_signature(document.root), before)

    def test_prune_consumes_accumulated_removed_interfaces(self):
        document = example_document()
        registered = (
            FunctionRule(
                "cisco.prune.source",
                lambda doc, ctx: InterfacePruningOutcome(
                    removed_interfaces={"GigabitEthernet0/0/0/9"},
                    removed_addresses={"192.0.2.9"},
                ),
            ),
            examples.PRUNE_DEMO_RULE,
        )
        with patch.object(rules, "PRUNE_RULES", registered):
            outcome = document.pruning.prune_non_nni({"GigabitEthernet0/0/0/0"})
            self.assertEqual(document.pruning.prune_non_nni(set()).changes, [])
        self.assertNotIn("RULE_DEMO_DEPENDS_ON", document.render())
        self.assertEqual(outcome.removed_interfaces, {"GigabitEthernet0/0/0/9"})
        self.assertEqual(outcome.removed_addresses, {"192.0.2.9"})
        self.assertEqual(outcome.changes[0]["rule_id"], "cisco.prune.demo")
        untouched = example_document()
        self.assertEqual(
            examples.PRUNE_DEMO_RULE.apply(
                untouched, rules.PruneContext(frozenset())
            ).changes,
            [],
        )

    def test_simulation_modes_policy_scope_statistics_and_idempotence(self):
        rule = examples.SIMULATE_DEMO_RULE
        for mode in ("off", "compatible", "stable"):
            for enabled in (False, True):
                with self.subTest(mode=mode, enabled=enabled):
                    document = example_document()
                    policy = SimulationAdaptationPolicy(
                        mode=mode, ensure_data_interfaces_enabled=enabled
                    )
                    with patch.object(rules, "SIMULATION_RULES", (rule,)):
                        outcome = document.simulation.adapt(
                            policy, {"Gi0/0/0/0"}
                        )
                        self.assertEqual(
                            outcome.total, int(mode != "off" and enabled)
                        )
                        self.assertEqual(
                            document.simulation.adapt(
                                policy, {"Gi0/0/0/0"}
                            ).total,
                            0,
                        )
                    if outcome.total:
                        self.assertEqual(
                            outcome.rule_changes, {rule.rule_id: 1}
                        )
                    self.assertIn(
                        "shutdown",
                        [
                            node.header
                            for node in find_interface_node(
                                document, "Gi0/0/0/1"
                            ).nodes
                        ],
                    )

    def test_validation_is_read_only_with_ancestor_path_and_rule_id(self):
        document = example_document()
        before = tree_signature(document.root)
        rule = examples.VALIDATE_DEMO_RULE
        with patch.object(rules, "VALIDATION_RULES", (rule,)):
            outcome = rules.validate_interfaces(document, {"Gi0/0/0/0"}, set())
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("interface GigabitEthernet0/0/0/0.100", outcome.errors[0])
        self.assertEqual(outcome.diagnostics[0]["rule_id"], rule.rule_id)
        self.assertEqual(tree_signature(document.root), before)


class MigratedRulesTest(unittest.TestCase):

    def test_missing_parent_multiple_subinterfaces_move_once_and_preserve_end(
        self,
    ):
        document = example_document("ethertype.cfg")
        outcome = document.compatibility.adapt_interfaces({"Gi0/0/0/0"})
        self.assertFalse(outcome.errors)
        self.assertEqual(len(outcome.changes), 2)
        self.assertEqual(
            {item["rule_id"] for item in outcome.changes},
            {"cisco.adapt.ethertype"},
        )
        self.assertEqual(document.render().count("tunneling ethertype"), 1)
        self.assertEqual(document.render().strip().splitlines()[-1], "end")
        self.assertEqual(
            document.compatibility.adapt_interfaces({"Gi0/0/0/0"}).changes, []
        )
        self.assertEqual(
            CiscoDocument(document.render()).render(), document.render()
        )

    def test_conflicting_siblings_leave_ast_unchanged(self):
        document = example_document("ethertype.cfg")
        source = find_interface_node(document, "Gi0/0/0/0.200")
        replace_header(
            document,
            source.nodes[0],
            source.nodes[0].header.replace("0x9100", "0x9200"),
        )
        before = tree_signature(document.root)
        result = rules.compatibility.ETHERTYPE_RULE.apply(
            document, AdaptContext(frozenset({"Gi0/0/0/0"}))
        )
        self.assertTrue(result.errors)
        self.assertEqual(result.changes, [])
        self.assertEqual(tree_signature(document.root), before)

    def test_nested_aggregation_removal_preserves_members_bundles_and_outside_scope(
        self,
    ):
        document = example_document("aggregation.cfg")
        outcome = document.compatibility.adapt_interfaces(
            {"Gi0/0/0/0", "Gi0/0/0/1", "BVI100"}
        )
        self.assertFalse(outcome.errors)
        self.assertEqual(len(outcome.changes), 4)
        target = find_interface_node(document, "Gi0/0/0/0")
        self.assertNotIn(
            "lacp", " ".join(node.header for node in target.walk())
        )
        self.assertIn(
            "description keep", [node.header for node in target.walk()]
        )
        member = find_interface_node(document, "Gi0/0/0/1")
        self.assertIn(
            "bundle id 10 mode active", [node.header for node in member.nodes]
        )
        outside = find_interface_node(document, "Gi0/0/0/2")
        self.assertIn(
            "lacp period short", [node.header for node in outside.nodes]
        )
        self.assertEqual(
            document.compatibility.adapt_interfaces(
                {"Gi0/0/0/0", "BVI100"}
            ).changes,
            [],
        )


class RegistryValidationTest(unittest.TestCase):

    def test_default_registries_are_unique_and_examples_are_not_enabled(self):
        for registry in (
            rules.PRUNE_RULES,
            rules.ADAPT_RULES,
            rules.SIMULATION_RULES,
            rules.VALIDATION_RULES,
        ):
            ids = [rule.rule_id for rule in registry]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertFalse(any("demo" in rule_id for rule_id in ids))
        self.assertEqual(
            rules.VALIDATION_RULES[: len(rules.COMPATIBILITY_VALIDATION_RULES)],
            rules.COMPATIBILITY_VALIDATION_RULES,
        )

    def test_duplicate_and_empty_ids_fail_before_any_execution(self):
        calls = []
        first = FunctionRule(
            "duplicate", lambda doc, ctx: calls.append("called")
        )
        for execute in (execute_rules, execute_prune_rules):
            for invalid in (
                FunctionRule("duplicate", first.action),
                FunctionRule("", first.action),
            ):
                with self.assertRaises(ValueError):
                    execute(
                        (first, invalid), None, rules.PruneContext(frozenset())
                    )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
