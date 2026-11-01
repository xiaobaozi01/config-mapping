"""核心路由实验按源身份裁剪 Cisco 接入配置的语义回归。"""

from pathlib import Path
import unittest

from impl.cisco.document import CiscoDocument


FIXTURES = Path(__file__).parent / "fixtures" / "core_pruning"


def document(name):
    return CiscoDocument((FIXTURES / name).read_text())


class CiscoPruningTest(unittest.TestCase):
    def test_mixed_bridge_domain_keeps_original_bvi_and_nni(self):
        doc = document("iosxr_mixed.cfg")
        result = doc.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors)
        output = doc.render()
        self.assertIn("interface Bundle-Ether10.200 l2transport", output)
        self.assertIn("rewrite ingress tag pop 1 symmetric", output)
        self.assertIn("interface GigabitEthernet0/0/0/1", output)
        self.assertIn("bridge-domain MIXED", output)
        self.assertIn("routed interface BVI10", output)
        self.assertIn("interface BVI10", output)
        self.assertNotIn("BVI20", output)
        self.assertNotIn("bridge-domain ACCESS", output)
        self.assertNotIn("0/0/0/2", output)
        self.assertIn("vrf SHARED", output)
        self.assertIn("65000:1", output)
        self.assertFalse(doc.validate_interfaces(set(), result.removed_interfaces).errors)

    def test_bgp_source_dependencies_remove_whole_sessions_and_templates(self):
        doc = document("iosxr_mixed.cfg")
        result = doc.prune_non_nni({"GigabitEthernet0/0/0/0"})
        self.assertFalse(result.errors)
        output = doc.render()
        for peer in ("192.0.2.2", "192.0.2.3", "192.0.2.4", "2001:db8::2"):
            self.assertNotIn(f"neighbor {peer}", output)
        self.assertNotIn("neighbor-group ACCESS", output)
        self.assertNotIn("neighbor-group INHERITED", output)
        self.assertNotIn("session-group ACCESS-SESSION", output)
        self.assertIn("neighbor 10.0.0.2", output)
        self.assertIn("update-source Loopback0", output)
        # 同地址仍存在于 Loopback，不能误删该邻居。
        self.assertIn("neighbor 10.0.0.3", output)
        self.assertIn("neighbor 10.0.0.4", output)
        self.assertNotIn("neighbor 10.0.0.5", output)
        self.assertIn("use session-group SAME", output)
        self.assertNotIn("neighbor-group SAME", output)
        self.assertNotIn("1.1.1.1", result.removed_addresses)

    def test_explicit_static_route_and_protocol_interface_are_removed(self):
        doc = document("iosxr_mixed.cfg")
        result = doc.prune_non_nni({"GigabitEthernet0/0/0/0"})
        self.assertFalse(result.errors)
        output = doc.render()
        self.assertNotIn("203.0.113.0/24", output)
        self.assertNotIn("cost 99", output)
        self.assertIn("198.51.100.0/24 Bundle-Ether10 10.0.0.2", output)
        self.assertIn("interface Bundle-Ether10", output)

    def test_unknown_dependency_fails_with_path_and_alias_resolved_interface(self):
        doc = document("iosxr_unknown.cfg")
        result = doc.prune_non_nni({"GigabitEthernet0/0/0/0"})
        self.assertFalse(result.errors)
        self.assertTrue(
            any(
                "custom-service FOO / bind-interface" in warning
                and "GigabitEthernet0/0/0/2" in warning
                for warning in result.warnings
            )
        )
        self.assertIn("custom-service FOO", doc.render())
        self.assertIn("description GigabitEthernet0/0/0/2", doc.render())
        self.assertEqual(
            {
                (item.path, item.interface)
                for item in result.deferred_interface_references
            },
            {
                (
                    (
                        "custom-service FOO",
                        "bind-interface Gi0/0/0/2",
                    ),
                    "GigabitEthernet0/0/0/2",
                )
            },
        )
        validation = doc.validate_interfaces(
            set(),
            result.removed_interfaces,
            result.deferred_interface_references,
        )
        self.assertFalse(validation.errors)

    def test_deleted_source_name_reused_as_nni_target_has_no_stale_configuration(self):
        doc = document("iosxr_collision.cfg")
        result = doc.prune_non_nni({"GigabitEthernet0/0/0/7"})
        self.assertFalse(result.errors)
        doc.rename_interface_tree("Gi0/0/0/7", "Gi0/0/0/0")
        doc.replace_references({"GigabitEthernet0/0/0/7": ["GigabitEthernet0/0/0/0"]})
        self.assertFalse(doc.validate_interfaces({"GigabitEthernet0/0/0/0"}, result.removed_interfaces).errors)
        output = doc.render()
        self.assertIn("REAL-NNI", output)
        self.assertIn("cost 10", output)
        self.assertNotIn("OLD-ACCESS", output)
        self.assertNotIn("cost 99", output)
        self.assertNotIn("192.0.2.1", output)
        self.assertNotIn("Gi 0/0/0/7", output)

    def test_service_bindings_removed_without_treating_domain_names_as_references(self):
        doc = document("iosxr_service_dependencies.cfg")
        outcome = doc.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(outcome.errors, outcome.errors)
        output = doc.render()
        for removed in ["REMOVED-RELAY", "monitor-session REMOVED", "interface Bundle-Ether609"]:
            self.assertNotIn(removed, output)
        for retained in ["RETAINED-RELAY", "monitor-session RETAINED", "bridge group BVI100",
                         "bridge-domain BVI100", "allow SSH"]:
            self.assertIn(retained, output)
        self.assertFalse(doc.validate_interfaces(set(), outcome.removed_interfaces).errors)

    def test_tunnel_is_preserved_but_unreferenced_line_track_is_removed(self):
        doc = document("iosxr_ambiguous_objects.cfg")
        outcome = doc.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(outcome.errors, outcome.errors)
        self.assertTrue(any("tunnel source" in warning for warning in outcome.warnings))
        validation = doc.validate_interfaces(
            set(),
            outcome.removed_interfaces,
            outcome.deferred_interface_references,
        )
        self.assertFalse(validation.errors)
        self.assertIn("interface tunnel-ip1000", doc.render())
        self.assertNotIn("track WATCH-LINK", doc.render())

    def test_duplicate_track_definitions_are_preserved_with_warning(self):
        doc = CiscoDocument(
            "interface GigabitEthernet0/0/0/1\n"
            "!\n"
            "track LOST\n type line-protocol state\n"
            "  interface GigabitEthernet0/0/0/1\n"
            "!\n"
            "track LOST\n type line-protocol state\n"
            "  interface GigabitEthernet0/0/0/1\n"
            "!\n"
            "end\n"
        )
        outcome = doc.prune_non_nni(set())
        self.assertFalse(outcome.errors)
        self.assertTrue(any("track LOST" in warning for warning in outcome.warnings))
        self.assertEqual(doc.render().count("track LOST"), 2)
        validation = doc.validate_interfaces(
            set(),
            outcome.removed_interfaces,
            outcome.deferred_interface_references,
        )
        self.assertFalse(validation.errors)

    def test_unmarked_removed_interface_reference_still_fails_validation(self):
        doc = CiscoDocument(
            "custom-service FOO\n"
            " bind-interface GigabitEthernet0/0/0/2\n"
        )
        outcome = doc.validate_interfaces(
            set(),
            {"GigabitEthernet0/0/0/2"},
        )
        self.assertTrue(outcome.errors)



if __name__ == "__main__":
    unittest.main()
