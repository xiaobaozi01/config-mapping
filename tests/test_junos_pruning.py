"""NNI-only Junos 剪枝保留业务角色并验证依赖边界。"""

from pathlib import Path
import unittest

from config_adaptor.juniper.document import JunosDocument
from config_adaptor.juniper.pruning import prune_non_nni, validate_pruned_references


FIXTURES = Path(__file__).parent / "fixtures" / "interfaces"


def load(name: str) -> JunosDocument:
    return JunosDocument((FIXTURES / name).read_text(encoding="utf-8"))


class JunosNniPruningTest(unittest.TestCase):
    def test_protected_member_preserves_bundle_members_and_original_irb_role(self):
        document = load("junos_nni_pruning.cfg")
        outcome = prune_non_nni(document, {"ge-0/0/0", "ge-0/0/20"})
        self.assertEqual(outcome.errors, [])
        specs = {spec.name for spec in document.interface_specs()}
        self.assertTrue({"ge-0/0/0", "ge-0/0/1", "ae0.100", "irb.100", "lo0.0", "fxp0.0", "gr-0/0/0.0", "future0.0"} <= specs)
        self.assertFalse({"ge-0/0/2.200", "ge-0/0/3.300", "irb.300"} & specs)
        rendered = document.render()
        self.assertIn("routing-interface irb.100;", rendered)
        self.assertIn("MIXED {", rendered)
        self.assertIn("KEEP-UNRELATED-DOMAIN", rendered)
        self.assertNotIn("ACCESS-ONLY", rendered)
        self.assertNotIn("ge-0/0/3.300", rendered)
        self.assertIn("192.0.2.1", outcome.removed_addresses)
        self.assertIn("198.51.103.1", outcome.removed_addresses)
        self.assertNotIn("198.51.100.1", outcome.removed_addresses)

    def test_removes_owned_routes_and_bgp_sessions_retains_shared_vrf_policy(self):
        document = load("junos_nni_pruning.cfg")
        outcome = prune_non_nni(document, {"ae0", "ge-0/0/20"})
        self.assertEqual(outcome.errors, [])
        rendered = document.render()
        for deleted in ("REMOVED-SOURCE", "10.10.10.1", "10.10.10.2", "route 10.10.0.0/24", "route 10.11.0.0/24", "route 10.14.0.0/24", "scheduler-map OLD", "interface ge-0/0/2.200", "OLD-VPWS", "target:64500:999", "local 903"):
            self.assertNotIn(deleted, rendered)
        for retained in ("10.10.10.3", "10.10.10.4", "route 10.12.0.0/24", "route 10.13.0.0/24", "SHARED-VRF", "SHARED-POLICY", "vrf-target target:64500:1", "scheduler-map KEEP", "interface ge-0/0/20.0", "MIXED-VPWS", "local 901"):
            self.assertIn(retained, rendered)

    def test_unknown_active_reference_is_reported_with_path_and_interface(self):
        document = load("junos_nni_pruning_unknown.cfg")
        outcome = prune_non_nni(document, {"ge-0/0/0"})
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("forwarding-options / unknown-feature / source-interface", outcome.errors[0])
        self.assertIn("ge-0/0/1.0", outcome.errors[0])

    def test_descriptions_comments_and_inactive_references_are_not_dependencies(self):
        document = load("junos_nni_pruning_inactive.cfg")
        outcome = prune_non_nni(document, {"ge-0/0/0"})
        self.assertEqual(outcome.errors, [])
        self.assertIn("neighbor 10.0.0.1", document.render())
        self.assertIn("document ge-0/0/1.0", document.render())

    def test_vlan_membership_retains_only_connected_vlan_irb(self):
        document = load("junos_nni_pruning_vlans.cfg")
        outcome = prune_non_nni(document, {"ge-0/0/0"})
        self.assertEqual(outcome.errors, [])
        self.assertIn("l3-interface irb.10", document.render())
        self.assertNotIn("RED", document.render())
        self.assertEqual({spec.name for spec in document.interface_specs()}, {"ge-0/0/0.0", "irb.10"})

    def test_final_validation_rejects_conflicting_units_and_gateway_role(self):
        document = load("junos_nni_pruning_invalid.cfg")
        outcome = validate_pruned_references(document, set())
        self.assertEqual(len(outcome.errors), 4)
        self.assertTrue(any("二三层模式冲突" in error for error in outcome.errors))
        self.assertTrue(any("重复逻辑接口" in error for error in outcome.errors))
        self.assertTrue(any("非法网关引用" in error for error in outcome.errors))
        self.assertTrue(any("网关接口缺失" in error and "irb.999" in error for error in outcome.errors))

    def test_final_reference_validation_is_read_only(self):
        document = load("junos_nni_pruning_unknown.cfg")
        document.remove_interface("ge-0/0/1")
        before = document.render()
        outcome = validate_pruned_references(document, {"ge-0/0/1.0"})
        self.assertTrue(outcome.errors)
        self.assertEqual(document.render(), before)

    def test_final_validation_allows_nni_target_to_reuse_a_removed_source_name(self):
        document = load("junos_nni_pruning_unknown.cfg")
        outcome = validate_pruned_references(document, {"ge-0/0/1", "ge-0/0/1.0"})
        self.assertEqual(outcome.errors, [])


if __name__ == "__main__":
    unittest.main()
