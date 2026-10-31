"""真实裁剪规则统一会话、原子提交和语义事件映射的回归。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from impl.cisco import rules
from impl.cisco.document import CiscoDocument
from impl.cisco.antlr.parsing import ConfigurationParseError
from impl.cisco.rules import pruning
from impl.cisco.rules.support import CiscoVisitorRule
from impl.common.outcomes import InterfacePruningOutcome
from impl.common.rules import PruneContext

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
    members = PruneContext(frozenset(), frozenset({REMOVED}), remove_non_nni=False)
    return (
        (pruning.NON_NNI_RULE, FULL_SOURCE, full),
        (pruning.MEMBER_SOURCES_RULE, MEMBER_SOURCE, members),
        (pruning.TRACK_DEPENDENCIES_RULE, TRACK_SOURCE, members),
        (pruning.INTERFACE_BINDINGS_RULE, BINDING_SOURCE, members),
    )


class ManagedPruningFailureTest(unittest.TestCase):
    def test_each_real_prune_rule_rolls_back_mapper_failure_and_clears_staged_results(self):
        for rule, source, context in prune_cases():
            with self.subTest(rule=rule.rule_id):
                document = CiscoDocument(source)
                snapshot = document.snapshot
                staged = InterfacePruningOutcome()

                def reject(session, visitor, edits):
                    self.assertGreater(edits.total, 0)
                    self.assertIsNot(document.snapshot, snapshot)
                    pruning.pruning_result(session, visitor, edits)
                    self.assertTrue(session.result.changes)
                    raise RuntimeError("mapping failed")

                failing = replace(rule, outcome_factory=lambda: staged, result_mapper=reject)
                with self.assertRaisesRegex(RuntimeError, "mapping failed"):
                    failing.apply(document, context)
                self.assertIs(document.snapshot, snapshot)
                self.assertEqual(document.revision, 0)
                self.assertEqual((staged.changes, staged.removed_interfaces, staged.removed_addresses), ([], set(), set()))

    def test_each_real_prune_rule_discards_results_if_candidate_reparse_fails(self):
        for rule, source, context in prune_cases():
            with self.subTest(rule=rule.rule_id):
                document = CiscoDocument(source)
                snapshot = document.snapshot
                staged = InterfacePruningOutcome()
                registered = replace(rule, outcome_factory=lambda: staged)
                with patch("impl.cisco.antlr.rewriting.CiscoSnapshot", side_effect=ConfigurationParseError("invalid")):
                    with self.assertRaises(ConfigurationParseError):
                        registered.apply(document, context)
                self.assertIs(document.snapshot, snapshot)
                self.assertEqual((staged.changes, staged.removed_interfaces, staged.removed_addresses), ([], set(), set()))

    def test_bgp_analysis_error_discards_full_prune_and_deleted_object_sets(self):
        source = FULL_SOURCE + "router bgp 65001\n neighbor 192.0.2.3\n  use neighbor-group MISSING\n"
        document = CiscoDocument(source)
        snapshot = document.snapshot
        result = document.prune_non_nni({"Gi0/0/0/0"})
        self.assertTrue(any("缺失的 neighbor-group MISSING" in error for error in result.errors))
        self.assertIs(document.snapshot, snapshot)
        self.assertEqual((result.changes, result.removed_interfaces, result.removed_addresses), ([], set(), set()))
        self.assertEqual(result.diagnostics[0]["rule_id"], "cisco.prune.non_nni")

    def test_unknown_track_consumer_cancels_other_valid_track_deletions(self):
        source = TRACK_SOURCE + (
            "track UNKNOWN\n type line-protocol state\n  interface Gi0/0/0/2\n"
            "custom-service FOO\n track object UNKNOWN\n"
        )
        document = CiscoDocument(source)
        snapshot = document.snapshot
        context = PruneContext(frozenset(), frozenset({REMOVED}))
        result = pruning.TRACK_DEPENDENCIES_RULE.apply(document, context)
        self.assertTrue(any("custom-service FOO / track object UNKNOWN" in error for error in result.errors))
        self.assertEqual(result.changes, [])
        self.assertIs(document.snapshot, snapshot)

    def test_bindings_error_discards_known_binding_deletions_in_same_rule(self):
        document = CiscoDocument(BINDING_SOURCE + "custom-service FOO\n bind-interface Gi0/0/0/2\n")
        snapshot = document.snapshot
        result = pruning.INTERFACE_BINDINGS_RULE.apply(document, PruneContext(frozenset(), frozenset({REMOVED})))
        self.assertTrue(result.errors)
        self.assertEqual(result.changes, [])
        self.assertIs(document.snapshot, snapshot)


class ManagedPruningResultTest(unittest.TestCase):
    def test_full_prune_keeps_business_reports_and_does_not_share_device_state(self):
        first, second = CiscoDocument(FULL_SOURCE), CiscoDocument(FULL_SOURCE)
        lost = first.prune_non_nni({"Gi0/0/0/0"})
        retained = second.prune_non_nni({"Gi0/0/0/2"})
        self.assertEqual(lost.removed_interfaces, {REMOVED})
        self.assertEqual(lost.removed_addresses, {"192.0.2.1"})
        self.assertEqual(len(lost.changes), 2)
        self.assertTrue(all(change["action"] == "remove" for change in lost.changes))
        self.assertTrue(all(change["rule_id"] == "cisco.prune.non_nni" for change in lost.changes))
        self.assertEqual(retained.removed_interfaces, {"GigabitEthernet0/0/0/0"})
        self.assertEqual(retained.removed_addresses, set())
        self.assertIn("neighbor 192.0.2.2", second.source)
        self.assertEqual(first.prune_non_nni({"Gi0/0/0/0"}).changes, [])
        self.assertEqual(second.prune_non_nni({"Gi0/0/0/2"}).changes, [])

    def test_later_managed_rules_receive_removed_sets_and_committed_snapshot(self):
        document = CiscoDocument(FULL_SOURCE + TRACK_SOURCE + BINDING_SOURCE)
        original = document.snapshot
        observed = []

        def observe(context):
            observed.append((context.removed_interfaces, context.removed_addresses, document.snapshot))
            return True

        track = replace(pruning.TRACK_DEPENDENCIES_RULE, condition=observe)
        registered = (pruning.NON_NNI_RULE, track, pruning.INTERFACE_BINDINGS_RULE)
        with patch.object(rules, "PRUNE_RULES", registered):
            result = document.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors)
        self.assertEqual(observed[0][:2], (frozenset({REMOVED}), frozenset({"192.0.2.1"})))
        self.assertIsNot(observed[0][2], original)
        self.assertNotIn("track LOST", document.source)
        self.assertNotIn("cost 99", document.source)
        self.assertEqual({item["rule_id"] for item in result.changes}, {rule.rule_id for rule in registered})

    def test_ancestor_delete_preserves_covered_child_business_event_without_overlap(self):
        class NestedVisitor(pruning.PruningVisitor):
            def visitConfiguration(self, root):
                parent = root.nodes[0]
                self.record(parent.nodes[0], (parent.header, parent.nodes[0].header), "child", {REMOVED})
                self.record(parent, (parent.header,), "parent", {REMOVED})

        document = CiscoDocument(BINDING_SOURCE)
        rule = CiscoVisitorRule(
            "cisco.prune.nested", NestedVisitor,
            outcome_factory=InterfacePruningOutcome, result_mapper=pruning.pruning_result,
        )
        with patch.object(rules, "PRUNE_RULES", (rule,)):
            result = document.prune_non_nni(set())
        self.assertEqual(document.source, "")
        self.assertEqual(document.revision, 1)
        self.assertEqual([event["reason"] for event in result.changes], ["child", "parent"])
        self.assertTrue(all(event["rule_id"] == rule.rule_id for event in result.changes))


if __name__ == "__main__":
    unittest.main()
