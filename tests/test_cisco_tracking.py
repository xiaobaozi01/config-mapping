"""接口、track 对象和 VRRP 跟踪语句的多级裁剪。"""

from cisco_support import insert_before, replace_header
from pathlib import Path
import unittest
from unittest.mock import patch

from impl.cisco import rules as cisco_rules

from impl.cisco.document import CiscoDocument
from adaptation.models import Vendor
from adaptation.pipeline import build_default_pipeline
from test_preserve_bundles import context_for

FIXTURE = Path(__file__).parent / "fixtures" / "core_pruning" / "iosxr_track_vrrp.cfg"


def load_document():
    return CiscoDocument(FIXTURE.read_text())


def top_node(document, header):
    return next((node for node in document.root.nodes if node.header == header), None)


class CiscoTrackingTest(unittest.TestCase):
    def test_removed_interface_removes_track_and_all_vrrp_uses_only(self):
        document = load_document()
        outcome = document.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(outcome.errors, outcome.errors)
        output = document.render()
        self.assertIsNone(top_node(document, "track LOST-LINK"))
        self.assertNotIn("track object LOST-LINK 30", output)
        self.assertNotIn("track object LOST-LINK 20", output)
        self.assertNotIn("track object LOST-LINK\n", output)
        self.assertIn("track object LOST-LINK-EXTRA 10", output)
        self.assertIn("track LOST-LINK-EXTRA\n", output)
        for retained in ["vrrp 110", "vrrp 111", "vrrp 112", "priority 110",
                         "address 192.0.2.73", "address 2001:db8:40::1"]:
            self.assertIn(retained, output)
        changes = [change for change in outcome.changes if "track" in change["reason"]]
        self.assertEqual(len(changes), 4)
        self.assertEqual({change["rule_id"] for change in changes}, {"cisco.prune.track_dependencies"})
        self.assertTrue(all(change["source_interfaces"] == ["TenGigE0/0/0/67"] for change in changes))
        self.assertFalse(document.validate_interfaces(set(), outcome.removed_interfaces).errors)
        self.assertEqual(document.prune_non_nni({"Gi0/0/0/0"}).changes, [])

    def test_monitored_interface_rename_keeps_tracking_and_vrrp(self):
        document = load_document()
        outcome = document.prune_non_nni({"Gi0/0/0/0", "Te0/0/0/67"})
        self.assertFalse(outcome.errors, outcome.errors)
        document.rename_interface_tree("Te0/0/0/67", "Gi0/0/0/2")
        document.replace_references({"TenGigE0/0/0/67": ["GigabitEthernet0/0/0/2"]})
        output = document.render()
        self.assertIn("interface GigabitEthernet0/0/0/2", output)
        self.assertIn("track LOST-LINK\n", output)
        self.assertIn("track object LOST-LINK 30", output)
        self.assertIn("track object LOST-LINK 20", output)
        self.assertNotIn("Te 0/0/0/67", output)
        self.assertFalse(document.validate_interfaces(set(), outcome.removed_interfaces | {"TenGigE0/0/0/67"}).errors)

    def test_member_removal_cleanup_also_cascades_track_references(self):
        document = load_document()
        document.remove_interface("Te0/0/0/67")
        outcome = document.prune_interface_dependencies({"TenGigE0/0/0/67"})
        self.assertFalse(outcome.errors, outcome.errors)
        self.assertIsNone(top_node(document, "track LOST-LINK"))
        output = document.render()
        self.assertNotIn("track object LOST-LINK 30", output)
        self.assertIn("track object LOST-LINK-EXTRA 10", output)
        self.assertTrue(all(change["rule_id"] == "cisco.prune.track_dependencies" for change in outcome.changes))

    def test_unknown_consumer_keeps_entire_track_chain_and_reports_path(self):
        document = load_document()
        extra = CiscoDocument("custom-service FOO\n track object LOST-LINK\n!\n")
        insert_before(document, document.root.nodes[0], extra.source)
        outcome = document.prune_non_nni({"Gi0/0/0/0"})
        self.assertTrue(any("custom-service FOO / track object LOST-LINK" in error for error in outcome.errors))
        self.assertTrue(top_node(document, "track LOST-LINK").active)
        self.assertIn("track object LOST-LINK 30", document.render())

    def test_unsupported_track_type_and_duplicate_definitions_are_not_deleted(self):
        for duplicate in [False, True]:
            with self.subTest(duplicate=duplicate):
                document = load_document()
                node = top_node(document, "track LOST-LINK")
                if duplicate:
                    insert_before(document, document.root.nodes[0], node.text)
                else:
                    replace_header(document, node.nodes[0], "type unsupported-state")
                outcome = document.prune_non_nni({"Gi0/0/0/0"})
                self.assertTrue(outcome.errors)
                self.assertTrue(node.active)
                self.assertIn("track object LOST-LINK 30", document.render())

    def test_deleted_vrrp_interface_removes_its_whole_binding(self):
        document = load_document()
        outcome = document.prune_non_nni(set())
        self.assertFalse(outcome.errors, outcome.errors)
        self.assertNotIn("vrrp 110", document.render())
        self.assertNotIn("track object LOST-LINK", document.render())
        self.assertFalse(document.validate_interfaces(set(), outcome.removed_interfaces).errors)

    def test_full_pipeline_cascades_when_nni_removes_redundant_member(self):
        context = context_for(Vendor.CISCO_IOSXR, filename_override="iosxr_member_track.cfg")
        build_default_pipeline().execute(context)
        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        output = device.document.render()
        self.assertNotIn("track REDUNDANT-MEMBER", output)
        self.assertNotIn("track object REDUNDANT-MEMBER", output)
        self.assertIn("vrrp 110", output)
        self.assertIn("priority 110", output)
        self.assertIn("address 10.0.100.254", output)
        self.assertTrue(any(event["kind"] == "redundant-member-cleanup" for event in context.events))


    def test_removing_registered_track_rule_disables_both_entrypoints(self):
        rules = tuple(rule for rule in cisco_rules.PRUNE_RULES if rule.rule_id != "cisco.prune.track_dependencies")
        for member_cleanup in [False, True]:
            with self.subTest(member_cleanup=member_cleanup):
                document = load_document()
                if member_cleanup:
                    document.remove_interface("Te0/0/0/67")
                with patch.object(cisco_rules, "PRUNE_RULES", rules):
                    if member_cleanup:
                        outcome = document.prune_interface_dependencies({"TenGigE0/0/0/67"})
                    else:
                        outcome = document.prune_non_nni({"Gi0/0/0/0"})
                self.assertTrue(outcome.errors)
                self.assertTrue(top_node(document, "track LOST-LINK").active)
                self.assertIn("track object LOST-LINK 30", document.render())
                self.assertNotIn("cisco.prune.track_dependencies", {item["rule_id"] for item in outcome.changes})



if __name__ == "__main__":
    unittest.main()
