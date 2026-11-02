"""真实裁剪规则统一会话、原子提交和语义事件映射的回归。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
    rules,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
    pruning,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.support import (
    CiscoVisitorRule,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.parser import (
    ConfigurationParseError,
)
from gns3_config_adaptor.impl.common.config_ast.model import tree_signature
from gns3_config_adaptor.impl.common.domain.outcomes import (
    InterfacePruningOutcome,
)
from gns3_config_adaptor.impl.common.rules import PruneContext

REMOVED = "GigabitEthernet0/0/0/2"
FULL_SOURCE = (
    "interface Gi0/0/0/0\n description retained\n"
    "interface Gi0/0/0/2\n ipv4 address 192.0.2.1/24\n"
    "router bgp 65000\n neighbor 192.0.2.2\n  update-source Gi0/0/0/2\n"
)
TRACK_SOURCE = (
    "track LOST\n type line-protocol state\n  interface Gi0/0/0/2\n"
    "router vrrp\n interface Gi0/0/0/0\n  address-family ipv4\n   vrrp 1\n    track object LOST 10\n"
)
MEMBER_SOURCE = (
    "router bgp 65000\n neighbor 192.0.2.2\n  update-source Gi0/0/0/2\n"
    "monitor-session LOST\n destination interface Gi0/0/0/2\n"
)
BINDING_SOURCE = "router ospf 1\n interface Gi0/0/0/2\n  cost 99\n"


def prune_cases():
    full = PruneContext(frozenset({"Gi0/0/0/0"}))
    members = PruneContext(
        frozenset(), frozenset({REMOVED}), remove_non_nni=False
    )
    return (
        (pruning.NON_NNI_RULE, FULL_SOURCE, full),
        (pruning.MEMBER_SOURCES_RULE, MEMBER_SOURCE, members),
        (pruning.TRACK_DEPENDENCIES_RULE, TRACK_SOURCE, members),
        (pruning.INTERFACE_BINDINGS_RULE, BINDING_SOURCE, members),
    )


class ManagedPruningFailureTest(unittest.TestCase):

    def test_each_real_prune_rule_rolls_back_mapper_failure_and_clears_staged_results(
        self,
    ):
        for rule, source, context in prune_cases():
            with self.subTest(rule=rule.rule_id):
                document = CiscoDocument(source)
                snapshot = tree_signature(document.root)
                staged = InterfacePruningOutcome()

                def reject(session, visitor, edits):
                    self.assertGreater(edits.total, 0)
                    self.assertNotEqual(tree_signature(document.root), snapshot)
                    pruning.pruning_result(session, visitor, edits)
                    self.assertTrue(session.result.changes)
                    raise RuntimeError("mapping failed")

                failing = replace(
                    rule, outcome_factory=lambda: staged, result_mapper=reject
                )
                with self.assertRaisesRegex(RuntimeError, "mapping failed"):
                    failing.apply(document, context)
                self.assertEqual(tree_signature(document.root), snapshot)
                self.assertEqual(document.revision, 0)
                self.assertEqual(
                    (
                        staged.changes,
                        staged.removed_interfaces,
                        staged.removed_addresses,
                    ),
                    ([], set(), set()),
                )

    def test_each_real_prune_rule_discards_results_if_candidate_reparse_fails(
        self,
    ):
        for rule, source, context in prune_cases():
            with self.subTest(rule=rule.rule_id):
                document = CiscoDocument(source)
                snapshot = tree_signature(document.root)
                staged = InterfacePruningOutcome()
                registered = replace(rule, outcome_factory=lambda: staged)
                with patch(
                    "gns3_config_adaptor.impl.common.config_ast.editor.ConfigEditor._execute",
                    side_effect=ConfigurationParseError("invalid"),
                ):
                    with self.assertRaises(ConfigurationParseError):
                        registered.apply(document, context)
                self.assertEqual(tree_signature(document.root), snapshot)
                self.assertEqual(
                    (
                        staged.changes,
                        staged.removed_interfaces,
                        staged.removed_addresses,
                    ),
                    ([], set(), set()),
                )

    def test_bgp_ambiguity_keeps_input_but_does_not_block_other_pruning(self):
        source = (
            FULL_SOURCE
            + "router bgp 65001\n neighbor 192.0.2.3\n  use neighbor-group MISSING\n"
        )
        document = CiscoDocument(source)
        snapshot = tree_signature(document.root)
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors)
        self.assertNotEqual(tree_signature(document.root), snapshot)
        self.assertEqual(result.removed_interfaces, {REMOVED})
        self.assertEqual(result.removed_addresses, {"192.0.2.1"})
        self.assertTrue(result.changes)
        self.assertTrue(result.warnings)
        self.assertNotIn("neighbor 192.0.2.2", document.render())
        self.assertIn("neighbor 192.0.2.3", document.render())

    def test_unknown_track_consumer_is_preserved_with_warning(self):
        source = TRACK_SOURCE + (
            "track UNKNOWN\n type line-protocol state\n  interface Gi0/0/0/2\n"
            "custom-service FOO\n track object UNKNOWN\n"
        )
        document = CiscoDocument(source)
        context = PruneContext(frozenset(), frozenset({REMOVED}))
        result = pruning.TRACK_DEPENDENCIES_RULE.apply(document, context)
        self.assertFalse(result.errors)
        self.assertTrue(
            any(
                "custom-service FOO / track object UNKNOWN" in warning
                for warning in result.warnings
            )
        )
        self.assertTrue(result.changes)
        output = document.render()
        self.assertNotIn("track LOST", output)
        self.assertIn("track UNKNOWN", output)
        self.assertIn("custom-service FOO", output)
        validation = document.validation.validate_interfaces(
            set(),
            {REMOVED},
            result.deferred_interface_references,
        )
        self.assertFalse(validation.errors)

    def test_unknown_binding_is_preserved_with_warning(self):
        document = CiscoDocument(
            BINDING_SOURCE + "custom-service FOO\n bind-interface Gi0/0/0/2\n"
        )
        result = pruning.INTERFACE_BINDINGS_RULE.apply(
            document, PruneContext(frozenset(), frozenset({REMOVED}))
        )
        self.assertFalse(result.errors)
        self.assertTrue(result.warnings)
        self.assertTrue(result.changes)
        output = document.render()
        self.assertNotIn("cost 99", output)
        self.assertIn("custom-service FOO", output)
        validation = document.validation.validate_interfaces(
            set(),
            {REMOVED},
            result.deferred_interface_references,
        )
        self.assertFalse(validation.errors)


class ManagedPruningResultTest(unittest.TestCase):

    def test_deletion_triggers_share_interface_dependency_cascade(self):
        full = CiscoDocument(FULL_SOURCE)
        member = CiscoDocument(MEMBER_SOURCE)
        with patch.object(pruning, "InterfaceDependencyCascade") as cascade:
            pruning.NON_NNI_RULE.apply(
                full,
                PruneContext(frozenset({"Gi0/0/0/0"})),
            )
            pruning.MEMBER_SOURCES_RULE.apply(
                member,
                PruneContext(
                    frozenset(),
                    frozenset({REMOVED}),
                    remove_non_nni=False,
                ),
            )

        self.assertEqual(cascade.call_count, 2)
        self.assertEqual(cascade.return_value.run.call_count, 2)

    def test_full_prune_keeps_business_reports_and_does_not_share_device_state(
        self,
    ):
        first, second = CiscoDocument(FULL_SOURCE), CiscoDocument(FULL_SOURCE)
        lost = first.pruning.prune_non_nni({"Gi0/0/0/0"})
        retained = second.pruning.prune_non_nni({"Gi0/0/0/2"})
        self.assertEqual(lost.removed_interfaces, {REMOVED})
        self.assertEqual(lost.removed_addresses, {"192.0.2.1"})
        self.assertEqual(len(lost.changes), 2)
        self.assertTrue(
            all(change["action"] == "remove" for change in lost.changes)
        )
        self.assertTrue(
            all(
                change["rule_id"] == "cisco.prune.non_nni"
                for change in lost.changes
            )
        )
        self.assertEqual(
            retained.removed_interfaces, {"GigabitEthernet0/0/0/0"}
        )
        self.assertEqual(retained.removed_addresses, set())
        self.assertIn("neighbor 192.0.2.2", second.source)
        self.assertEqual(first.pruning.prune_non_nni({"Gi0/0/0/0"}).changes, [])
        self.assertEqual(
            second.pruning.prune_non_nni({"Gi0/0/0/2"}).changes, []
        )

    def test_later_managed_rules_receive_removed_sets_and_committed_snapshot(
        self,
    ):
        document = CiscoDocument(FULL_SOURCE + TRACK_SOURCE + BINDING_SOURCE)
        original = tree_signature(document.root)
        observed = []

        def observe(context):
            observed.append(
                (
                    context.removed_interfaces,
                    context.removed_addresses,
                    tree_signature(document.root),
                )
            )
            return True

        track = replace(pruning.TRACK_DEPENDENCIES_RULE, condition=observe)
        registered = (
            pruning.NON_NNI_RULE,
            track,
            pruning.INTERFACE_BINDINGS_RULE,
        )
        with patch.object(rules, "PRUNE_RULES", registered):
            result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors)
        self.assertEqual(
            observed[0][:2], (frozenset({REMOVED}), frozenset({"192.0.2.1"}))
        )
        self.assertIsNot(observed[0][2], original)
        self.assertNotIn("track LOST", document.source)
        self.assertNotIn("cost 99", document.source)
        self.assertEqual(
            {item["rule_id"] for item in result.changes},
            {rule.rule_id for rule in registered},
        )

    def test_ancestor_delete_preserves_covered_child_business_event_without_overlap(
        self,
    ):
        class NestedVisitor(pruning.PruningVisitor):

            def visit_configuration(self, root):
                parent = root.nodes[0]
                self.record(
                    parent.nodes[0],
                    (parent.header, parent.nodes[0].header),
                    "child",
                    {REMOVED},
                )
                self.record(parent, (parent.header,), "parent", {REMOVED})

        document = CiscoDocument(BINDING_SOURCE)
        rule = CiscoVisitorRule(
            "cisco.prune.nested",
            NestedVisitor,
            outcome_factory=InterfacePruningOutcome,
            result_mapper=pruning.pruning_result,
        )
        with patch.object(rules, "PRUNE_RULES", (rule,)):
            result = document.pruning.prune_non_nni(set())
        self.assertEqual(document.source, "")
        self.assertEqual(document.revision, 1)
        self.assertEqual(
            [event["reason"] for event in result.changes], ["child", "parent"]
        )
        self.assertTrue(
            all(event["rule_id"] == rule.rule_id for event in result.changes)
        )


if __name__ == "__main__":
    unittest.main()
