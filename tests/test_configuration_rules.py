"""规则扩展、诊断收集和只读校验的行为约束。"""

import copy
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from impl.cisco import rules as cisco_rules
from impl.cisco.document import CiscoDocument
from impl.cisco.rules.support import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    simulation_result,
)
from impl.common.outcomes import (
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationAdaptationPolicy
from impl.common.rules import (
    AdaptContext,
    FunctionRule,
    PruneContext,
    execute_prune_rules,
    execute_rules,
)
from impl.juniper.document import JunosDocument

FIXTURES = Path(__file__).parent / "fixtures"


class SimulationMarkerVisitor(CiscoRuleVisitor):

    def visitConfiguration(self, root):
        self.session.edits.append_child(
            root,
            "description custom-simulation",
            "custom rule",
            category="custom",
        )


class ConfigurationRuleTest(unittest.TestCase):

    def test_new_rule_only_needs_registration_and_reports_its_id(self):
        rule = CiscoVisitorRule(
            "cisco.simulate.custom",
            SimulationMarkerVisitor,
            outcome_factory=SimulationAdaptationOutcome,
            result_mapper=simulation_result,
        )
        document = CiscoDocument("hostname R1\n")
        with patch.object(cisco_rules, "SIMULATION_RULES", (rule,)):
            result = document.adapt_to_simulation(
                SimulationAdaptationPolicy(), set()
            )
        off_document = CiscoDocument("hostname R1\n")
        with patch.object(cisco_rules, "SIMULATION_RULES", (rule,)):
            off_document.adapt_to_simulation(
                SimulationAdaptationPolicy(mode="off"), set()
            )
        self.assertNotIn("custom-simulation", off_document.render())
        self.assertIn("custom-simulation", document.render())
        self.assertEqual(result.rule_changes, {"cisco.simulate.custom": 1})

    def test_conditions_order_and_immutable_interface_context(self):
        calls = []
        rules = (
            FunctionRule("first", lambda doc, ctx: calls.append("first")),
            FunctionRule(
                "skip", lambda doc, ctx: calls.append("skip"), lambda ctx: False
            ),
            FunctionRule("last", lambda doc, ctx: calls.append("last")),
        )
        context = AdaptContext(frozenset({"Gi0/0/0/0"}))
        self.assertEqual(
            [name for name, _ in execute_rules(rules, None, context)],
            ["first", "last"],
        )
        self.assertEqual(calls, ["first", "last"])
        with self.assertRaises(FrozenInstanceError):
            context.target_interfaces = frozenset()

    def test_validation_collects_all_rules_after_an_error(self):
        class FirstVisitor(CiscoRuleVisitor):

            def visitConfiguration(self, root):
                self.session.result.errors.append("first-error")

        class LastVisitor(CiscoRuleVisitor):

            def visitConfiguration(self, root):
                self.session.result.errors.append("last-error")

        rules = (
            CiscoVisitorRule("first", FirstVisitor, readonly=True),
            CiscoVisitorRule("last", LastVisitor, readonly=True),
        )
        with patch.object(cisco_rules, "VALIDATION_RULES", rules):
            result = CiscoDocument("hostname R1\n").validate_interfaces(
                set(), set()
            )
        self.assertEqual(result.errors, ["first-error", "last-error"])
        self.assertEqual(
            [item["rule_id"] for item in result.diagnostics], ["first", "last"]
        )

    def test_vendor_validation_is_read_only_and_keeps_full_checks(self):
        cases = [
            (
                CiscoDocument,
                "interface_compatibility/iosxr_invalid.cfg",
                {"GigabitEthernet0/0/0/0"},
            ),
            (JunosDocument, "", {"ge-0/0/0"}),
        ]
        for factory, fixture, targets in cases:
            source = (
                (FIXTURES / fixture).read_text()
                if fixture
                else (
                    "interfaces { ge-0/0/0 { unit 0 { family inet; family bridge; } unit 0; } }"
                )
            )
            with self.subTest(vendor=factory.vendor):
                document = factory(source)
                before = (
                    document.snapshot
                    if isinstance(document, CiscoDocument)
                    else copy.deepcopy(document.root)
                )
                result = document.validate_interfaces(targets, set())
                self.assertTrue(result.errors)
                self.assertTrue(result.diagnostics)
                self.assertEqual(
                    document.snapshot
                    if isinstance(document, CiscoDocument)
                    else document.root,
                    before,
                )

    def test_off_mode_and_disabled_enable_policy_do_not_run_builtin_rules(self):
        document = CiscoDocument(
            "interface Gi0/0/0/0\n shutdown\n speed 10000\n!\n"
        )
        source = document.render()
        result = document.adapt_to_simulation(
            SimulationAdaptationPolicy(mode="off"), {"Gi0/0/0/0"}
        )
        self.assertEqual(document.render(), source)
        self.assertEqual(result.rule_changes, {})
        result = document.adapt_to_simulation(
            SimulationAdaptationPolicy(
                mode="compatible", ensure_data_interfaces_enabled=False
            ),
            {"Gi0/0/0/0"},
        )
        self.assertEqual(result.total, 0)
        self.assertEqual(document.render(), source)

    def test_prune_results_are_passed_to_next_rule_as_immutable_context(self):
        observed = []
        rules = (
            FunctionRule(
                "remove",
                lambda doc, ctx: InterfacePruningOutcome(
                    removed_interfaces={"Gi0/0/0/1"},
                    removed_addresses={"192.0.2.1"},
                ),
            ),
            FunctionRule(
                "dependencies",
                lambda doc, ctx: observed.append(ctx)
                or InterfacePruningOutcome(),
                lambda ctx: "Gi0/0/0/1" in ctx.removed_interfaces,
            ),
        )
        original = PruneContext(frozenset({"Gi0/0/0/0"}))
        results = execute_prune_rules(rules, None, original)
        self.assertEqual(
            [name for name, _ in results], ["remove", "dependencies"]
        )
        self.assertEqual(original.removed_interfaces, frozenset())
        self.assertEqual(
            observed[0].removed_interfaces, frozenset({"Gi0/0/0/1"})
        )
        self.assertEqual(
            observed[0].removed_addresses, frozenset({"192.0.2.1"})
        )
        self.assertEqual(
            observed[0].protected_parents, original.protected_parents
        )
        with self.assertRaises(FrozenInstanceError):
            observed[0].removed_interfaces = frozenset()


if __name__ == "__main__":
    unittest.main()
