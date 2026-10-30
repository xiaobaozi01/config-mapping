"""Junos 剪枝必须遵循 routing-instance 与 BGP 显式覆盖的边界。"""

from pathlib import Path
import unittest

from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures" / "core_pruning"


def load(name):
    return JunosDocument((FIXTURES / name).read_text())


class JunosPruningScopeTest(unittest.TestCase):
    def test_bgp_sources_cannot_borrow_identical_addresses_from_other_instances(self):
        document = load("junos_scoped_addresses.cfg")
        outcome = document.prune_non_nni({"ge-0/0/0", "ge-0/0/2"})
        self.assertFalse(outcome.errors, outcome.errors)
        rendered = document.render()
        for kept in ("DEFAULT-KEEP", "VRF-KEEP", "neighbor 192.0.2.2", "neighbor 198.51.100.3"):
            self.assertIn(kept, rendered)
        for removed in ("DEFAULT-LOST", "VRF-LOST", "neighbor 198.51.100.2", "neighbor 192.0.2.3"):
            self.assertNotIn(removed, rendered)
        self.assertFalse(document.validate_interfaces(set(), outcome.removed_interfaces).errors)

    def test_equal_vlan_ids_and_names_in_distinct_instances_do_not_keep_customer_gateways(self):
        document = load("junos_scoped_vlans.cfg")
        outcome = document.prune_non_nni({"ge-0/0/0", "ge-0/0/2"})
        self.assertFalse(outcome.errors, outcome.errors)
        rendered = document.render()
        self.assertIn("CORE-BD", rendered)
        self.assertIn("routing-interface irb.100", rendered)
        self.assertIn("l3-interface irb.300", rendered)
        self.assertNotIn("CUSTOMER-BD", rendered)
        self.assertNotIn("irb.200", rendered)
        self.assertNotIn("irb.400", rendered)
        specs = {spec.name for spec in document.interface_specs()}
        self.assertTrue({"irb.100", "irb.300"} <= specs)
        self.assertFalse({"irb.200", "irb.400"} & specs)

    def test_explicit_attachment_members_override_vlan_hints_within_same_scope(self):
        document = load("junos_explicit_domain_members.cfg")
        outcome = document.prune_non_nni({"ge-0/0/0"})
        self.assertFalse(outcome.errors, outcome.errors)
        rendered = document.render()
        self.assertIn("NNI-DOMAIN", rendered)
        self.assertNotIn("ACCESS-DOMAIN", rendered)
        self.assertNotIn("unit 200", rendered)

    def test_neighbor_explicit_source_override_survives_invalid_group_default(self):
        document = load("junos_bgp_source_overrides.cfg")
        outcome = document.prune_non_nni({"ge-0/0/0"})
        self.assertFalse(outcome.errors, outcome.errors)
        rendered = document.render()
        for kept in ("MIXED-SOURCES", "peer-as 65001", "export CORE-POLICY", "neighbor 10.0.0.2", "neighbor 10.0.0.3", "local-address 1.1.1.1"):
            self.assertIn(kept, rendered)
        for removed in ("ALL-LOST", "neighbor 192.0.2.2", "neighbor 192.0.2.3", "neighbor 192.0.2.4", "neighbor 192.0.2.5", "allow 192.0.2.0/24", "local-address 192.0.2.1"):
            self.assertNotIn(removed, rendered)
        self.assertFalse(document.validate_interfaces(set(), outcome.removed_interfaces).errors)


if __name__ == "__main__":
    unittest.main()
