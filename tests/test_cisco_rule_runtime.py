"""托管 Visitor 规则的作用范围、生命周期、失败恢复和现有注册契约。"""

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
    rules,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
    compatibility,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.support import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    InterfaceScope,
    RuleContractError,
    compatibility_interfaces_scope,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.parser import (
    ConfigurationParseError,
)
from gns3_config_adaptor.impl.common.config_ast.model import tree_signature
from gns3_config_adaptor.impl.common.domain.interface import InterfaceKind
from gns3_config_adaptor.impl.common.domain.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from gns3_config_adaptor.impl.common.domain.policies import (
    SimulationAdaptationPolicy,
)
from gns3_config_adaptor.impl.common.rules import (
    AdaptContext,
    FunctionRule,
    PruneContext,
    ValidationContext,
    execute_prune_rules,
    execute_rules,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rule_extensions"
EMPTY = AdaptContext(frozenset())


def example():
    return CiscoDocument((FIXTURES / "examples.cfg").read_text())


class MarkVisitor(CiscoRuleVisitor):

    def __init__(self, session):
        super().__init__(session)
        self.count = 0

    def visit_node(self, node):
        self.count += 1
        if node.header == "hostname OLD":
            self.session.edits.replace_header(
                node, "hostname NEW", "test marker"
            )
        return self.visit_children(node)


class RuntimeLifecycleTest(unittest.TestCase):

    def test_one_registered_rule_runs_independent_visitors_and_results_per_device(
        self,
    ):
        created = []

        def factory(session):
            visitor = MarkVisitor(session)
            created.append(visitor)
            return visitor

        rule = CiscoVisitorRule("cisco.adapt.custom", factory)
        first, second = CiscoDocument("hostname OLD\n"), CiscoDocument(
            "hostname OLD\n"
        )
        with patch.object(rules, "ADAPT_RULES", (rule,)):
            a = first.compatibility.adapt_interfaces(set())
            b = second.compatibility.adapt_interfaces(set())
            c = first.compatibility.adapt_interfaces(set())
        self.assertEqual([visitor.count for visitor in created], [1, 1, 1])
        self.assertEqual(len({id(visitor.session) for visitor in created}), 3)
        self.assertIsNot(created[0].session.result, created[1].session.result)
        self.assertEqual(a.changes[0]["rule_id"], rule.rule_id)
        self.assertEqual(b.changes[0]["rule_id"], rule.rule_id)
        self.assertEqual(c.changes, [])
        self.assertEqual(first.source, second.source)

    def test_later_function_rule_sees_managed_commit_with_fresh_contexts(self):
        document = CiscoDocument("hostname OLD\n")
        original = tree_signature(document.root)
        observed = []

        def read(doc, context):
            observed.append(
                (tree_signature(doc.root), doc.root.nodes[0].header)
            )
            return InterfaceAdaptationOutcome()

        execute_rules(
            (
                CiscoVisitorRule("managed", MarkVisitor),
                FunctionRule("function", read),
            ),
            document,
            EMPTY,
        )
        self.assertIsNot(observed[0][0], original)
        self.assertEqual(observed[0][1], "hostname NEW")
        self.assertEqual(document.revision, 1)

    def test_finish_runs_once_after_all_roots_before_single_commit(self):
        document = CiscoDocument("hostname OLD\ndescription OLD\n")
        snapshot = tree_signature(document.root)
        observed = []

        class FinishVisitor(MarkVisitor):

            def finish(self):
                observed.append(
                    (self.count, tree_signature(self.session.document.root))
                )
                self.session.edits.replace_header(
                    self.session.document.root.nodes[1],
                    "description NEW",
                    "finish",
                )

        rule = CiscoVisitorRule(
            "finish", FinishVisitor, selector=lambda doc, ctx: doc.root.nodes
        )
        result = rule.apply(document, EMPTY)
        self.assertEqual(observed, [(2, snapshot)])
        self.assertEqual(document.revision, 1)
        self.assertEqual(len(result.changes), 2)
        self.assertEqual(document.source, "hostname NEW\ndescription NEW\n")

    def test_condition_skip_does_not_construct_outcome_scope_or_visitor(self):
        def forbidden(*args):
            self.fail("skipped rule executed")

        rule = CiscoVisitorRule(
            "skip",
            forbidden,
            selector=forbidden,
            outcome_factory=forbidden,
            condition=lambda ctx: False,
        )
        self.assertEqual(execute_rules((rule,), CiscoDocument(""), EMPTY), [])

    def test_empty_scope_does_not_construct_visitor_or_finish(self):
        rule = CiscoVisitorRule(
            "empty",
            lambda session: self.fail("empty scope visited"),
            selector=lambda doc, ctx: (),
        )
        document = example()
        snapshot = tree_signature(document.root)
        self.assertEqual(rule.apply(document, EMPTY).changes, [])
        self.assertEqual(tree_signature(document.root), snapshot)

    def test_foreign_duplicate_and_overlapping_roots_fail_before_visiting(self):
        document = example()
        foreign = example()
        snapshot = tree_signature(document.root)
        for roots in (
            (foreign.root,),
            (document.root, document.root),
            (document.root, document.root.nodes[0]),
        ):
            with self.subTest(roots=len(roots)):
                rule = CiscoVisitorRule(
                    "invalid",
                    lambda session: self.fail("invalid scope visited"),
                    selector=lambda doc, ctx: roots,
                )
                with self.assertRaises(RuleContractError):
                    rule.apply(document, EMPTY)
                self.assertEqual(tree_signature(document.root), snapshot)

    def test_cached_visitor_is_rejected_on_subsequent_execution(self):
        visitors = []

        def factory(session):
            if not visitors:
                visitors.append(MarkVisitor(session))
            return visitors[0]

        rule = CiscoVisitorRule("cached", factory)
        rule.apply(CiscoDocument("hostname OLD\n"), EMPTY)
        other = CiscoDocument("hostname OLD\n")
        with self.assertRaises(RuleContractError):
            rule.apply(other, EMPTY)
        self.assertEqual(other.source, "hostname OLD\n")


class RuntimeFailureTest(unittest.TestCase):

    def test_business_error_discards_edits_changes_and_prune_sets(self):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)

        class FailVisitor(MarkVisitor):

            def finish(self):
                result = self.session.result
                result.changes.append({"action": "fake"})
                result.removed_interfaces.add("Gi0/0/0/9")
                result.removed_addresses.add("192.0.2.9")
                result.errors.append("business conflict")
                result.warnings.append("preserved warning")

        rule = CiscoVisitorRule(
            "failed", FailVisitor, outcome_factory=InterfacePruningOutcome
        )
        result = rule.apply(document, PruneContext(frozenset()))
        self.assertEqual(tree_signature(document.root), snapshot)
        self.assertEqual(result.errors, ["business conflict"])
        self.assertEqual(result.warnings, ["preserved warning"])
        self.assertEqual(
            (
                result.changes,
                result.removed_interfaces,
                result.removed_addresses,
            ),
            ([], set(), set()),
        )

    def test_reparse_failure_preserves_snapshot_and_rule_origin_is_not_reported_as_change(
        self,
    ):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)

        class InvalidVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                self.session.edits.replace_header(
                    node, "group UNTERMINATED", "invalid"
                )

        rule = CiscoVisitorRule("invalid", InvalidVisitor)
        with (
            patch.object(rules, "ADAPT_RULES", (rule,)),
            self.assertRaises(ConfigurationParseError),
        ):
            document.compatibility.adapt_interfaces(set())
        self.assertEqual(tree_signature(document.root), snapshot)
        self.assertEqual(document.revision, 0)

    def test_mapper_failure_restores_snapshot_and_clears_staged_statistics(
        self,
    ):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)
        results = []

        def outcome_factory():
            result = SimulationAdaptationOutcome()
            results.append(result)
            return result

        def broken_mapper(session, visitor, edits):
            self.assertEqual(session.document.hostname(), "NEW")
            session.result.record("added", "fake", 10)
            raise RuntimeError("mapping failed")

        rule = CiscoVisitorRule(
            "mapping",
            MarkVisitor,
            outcome_factory=outcome_factory,
            result_mapper=broken_mapper,
        )
        with self.assertRaisesRegex(RuntimeError, "mapping failed"):
            rule.apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), snapshot)
        self.assertEqual(results[0].total, 0)

    def test_mapper_business_error_restores_snapshot_and_returns_only_diagnostics(
        self,
    ):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)

        def reject(session, visitor, edits):
            session.result.changes.extend(edits.changes)
            session.result.errors.append("mapping conflict")

        result = CiscoVisitorRule(
            "reject", MarkVisitor, result_mapper=reject
        ).apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), snapshot)
        self.assertEqual(result.changes, [])
        self.assertEqual(result.errors, ["mapping conflict"])

    def test_mapper_cannot_perform_a_second_commit(self):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)

        def forbidden(session, visitor, edits):
            transaction = session.document.edit()
            transaction.replace_header(
                session.document.root.nodes[0], "hostname EXTRA", "bypass"
            )
            transaction.apply()

        with self.assertRaises(RuleContractError):
            CiscoVisitorRule(
                "bypass", MarkVisitor, result_mapper=forbidden
            ).apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), snapshot)

    def test_failed_rule_restores_its_start_without_undoing_preceding_rule(
        self,
    ):
        document = CiscoDocument("hostname OLD\n")
        CiscoVisitorRule("first", MarkVisitor).apply(document, EMPTY)
        committed = tree_signature(document.root)

        class BypassVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                transaction = self.session.document.edit()
                transaction.replace_header(node, "hostname BAD", "bypass")
                transaction.apply()

        with self.assertRaises(RuleContractError):
            CiscoVisitorRule("second", BypassVisitor).apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), committed)
        self.assertEqual(document.hostname(), "NEW")

    def test_invalid_outcome_factory_and_selector_mutation_restore_document(
        self,
    ):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)
        with self.assertRaises(RuleContractError):
            CiscoVisitorRule(
                "bad-outcome", MarkVisitor, outcome_factory=list
            ).apply(document, EMPTY)

        def selector(doc, context):
            transaction = doc.edit()
            transaction.delete(doc.root.nodes[0], "bypass")
            transaction.apply()
            return (doc.root,)

        with self.assertRaises(RuleContractError):
            CiscoVisitorRule(
                "bad-selector", MarkVisitor, selector=selector
            ).apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), snapshot)

    def test_visitor_cannot_commit_through_edit_facade(self):
        class CommitVisitor(MarkVisitor):

            def finish(self):
                self.session.edits.apply()

        document = CiscoDocument("hostname OLD\n")
        with self.assertRaises(RuleContractError):
            CiscoVisitorRule("commit", CommitVisitor).apply(document, EMPTY)
        self.assertEqual(document.hostname(), "OLD")


class RuntimeReadonlyTest(unittest.TestCase):

    def test_readonly_never_creates_transaction_and_keeps_all_diagnostics(self):
        class CheckVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                self.session.result.errors.append(node.header)

        document = CiscoDocument("hostname A\nhostname B\n")
        with patch.object(
            document,
            "edit",
            side_effect=AssertionError("readonly transaction"),
        ):
            result = CiscoVisitorRule(
                "check", CheckVisitor, readonly=True
            ).apply(document, EMPTY)
        self.assertEqual(result.errors, ["hostname A", "hostname B"])
        self.assertEqual(document.revision, 0)

    def test_readonly_edit_access_and_unbacked_change_report_are_contract_errors(
        self,
    ):
        class EditVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                self.session.edits.delete(node, "not allowed")

        class FakeChangeVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                self.session.result.changes.append({"action": "fake"})

        for visitor in (EditVisitor, FakeChangeVisitor):
            with self.subTest(visitor=visitor.__name__):
                document = CiscoDocument("hostname OLD\n")
                with self.assertRaises(RuleContractError):
                    CiscoVisitorRule("readonly", visitor, readonly=True).apply(
                        document, EMPTY
                    )
                self.assertEqual(document.source, "hostname OLD\n")

    def test_readonly_direct_write_restores_snapshot(self):
        class DirectWriteVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                self.session.document.references.replace(
                    {"Gi0/0/0/0": ["Gi0/0/0/1"]}
                )

        document = CiscoDocument("router ospf 1\n interface Gi0/0/0/0\n")
        snapshot = tree_signature(document.root)
        with self.assertRaises(RuleContractError):
            CiscoVisitorRule(
                "readonly", DirectWriteVisitor, readonly=True
            ).apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), snapshot)


class MigratedScopeTest(unittest.TestCase):

    def test_interface_scope_and_compatibility_gateway_scope_keep_distinct_semantics(
        self,
    ):
        document = example()
        transaction = document.edit()
        transaction.append_child(
            document.root,
            "interface BVI100\n mtu 1000\n mtu 2000\n",
            "add gateway",
        )
        transaction.apply()
        scope = InterfaceScope(
            "target_interfaces", True, frozenset({InterfaceKind.PHYSICAL})
        )
        selected = scope(document, AdaptContext(frozenset({"Gi0/0/0/0.100"})))
        self.assertEqual(
            [node.interface_name for node in selected],
            ["GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/0.100"],
        )
        self.assertEqual(scope(document, EMPTY), ())
        gateways = compatibility_interfaces_scope(
            document, ValidationContext(frozenset())
        )
        self.assertEqual([node.interface_name for node in gateways], ["BVI100"])
        self.assertTrue(
            compatibility.SCALAR_VALUES_RULE.apply(
                document, ValidationContext(frozenset())
            ).errors
        )

    def test_bfd_keeps_global_descendant_scope_and_excludes_top_level_commands(
        self,
    ):
        source = "bfd minimum-interval 10\nrouter ospf 1\n bfd minimum-interval 10\ninterface Gi0/0/0/1\n bfd multiplier 1\n"
        document = CiscoDocument(source)
        policy = SimulationAdaptationPolicy(
            mode="stable", ensure_data_interfaces_enabled=False
        )
        result = document.simulation.adapt(policy, set())
        self.assertEqual(
            document.root.nodes[0].header, "bfd minimum-interval 10"
        )
        self.assertEqual(
            result.replaced, {"bfd-minimum-interval": 1, "bfd-multiplier": 1}
        )
        self.assertEqual(result.rule_changes, {"cisco.simulate.bfd": 2})
        self.assertEqual(document.simulation.adapt(policy, set()).total, 0)
        compatible = CiscoDocument(source)
        self.assertEqual(
            compatible.simulation.adapt(
                replace(policy, mode="compatible"), set()
            ).total,
            0,
        )
        self.assertEqual(compatible.source, source)

    def test_simulation_requires_result_mapping_and_rolls_back_if_missing(self):
        document = CiscoDocument("hostname OLD\n")
        snapshot = tree_signature(document.root)
        rule = CiscoVisitorRule(
            "missing-map",
            MarkVisitor,
            outcome_factory=SimulationAdaptationOutcome,
        )
        with self.assertRaises(RuleContractError):
            rule.apply(document, EMPTY)
        self.assertEqual(tree_signature(document.root), snapshot)

    def test_managed_prune_and_function_rule_share_accumulated_context(self):
        observed = []

        class RemoveVisitor(CiscoRuleVisitor):

            def visit_node(self, node):
                self.session.edits.delete(node, "remove source")
                self.session.result.removed_interfaces.add(
                    "GigabitEthernet0/0/0/0"
                )
                self.session.result.removed_addresses.add("192.0.2.1")

        def observe(document, context):
            observed.append(context)
            return InterfacePruningOutcome()

        registered = (
            CiscoVisitorRule(
                "managed",
                RemoveVisitor,
                outcome_factory=InterfacePruningOutcome,
            ),
            FunctionRule("function", observe),
        )
        execute_prune_rules(
            registered,
            CiscoDocument("interface Gi0/0/0/0\n"),
            PruneContext(frozenset()),
        )
        self.assertEqual(
            observed[0].removed_interfaces,
            frozenset({"GigabitEthernet0/0/0/0"}),
        )
        self.assertEqual(
            observed[0].removed_addresses, frozenset({"192.0.2.1"})
        )

    def test_all_builtin_registries_use_managed_rules_and_named_conditions(
        self,
    ):
        for registry in (
            rules.PRUNE_RULES,
            rules.ADAPT_RULES,
            rules.COMPATIBILITY_VALIDATION_RULES,
            rules.SIMULATION_RULES,
            rules.VALIDATION_RULES,
        ):
            self.assertTrue(
                all(isinstance(rule, CiscoVisitorRule) for rule in registry)
            )
            self.assertTrue(
                all(rule.condition.__name__ != "<lambda>" for rule in registry)
            )
        self.assertEqual(
            rules.VALIDATION_RULES[:-1], rules.COMPATIBILITY_VALIDATION_RULES
        )


if __name__ == "__main__":
    unittest.main()
