"""裁剪按 BGP 有效源进行，显式覆盖不会因模板默认源失效被误删。"""

import unittest
from pathlib import Path

from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)

FIXTURES = Path(__file__).parent / "fixtures" / "core_pruning"


def load(name):
    return CiscoDocument((FIXTURES / name).read_text())


class CiscoBgpPruningTest(unittest.TestCase):

    def test_access_only_p2p_removes_remote_binding_and_keeps_other_services(
        self,
    ):
        document = load("iosxr_p2p_pruning.cfg")
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors, result.errors)
        output = document.render()
        self.assertNotIn("p2p ACCESS-ONLY", output)
        self.assertNotIn("192.0.2.10 pw-id 100", output)
        self.assertIn("p2p MIXED", output)
        self.assertIn("interface GigabitEthernet0/0/0/0.100", output)
        self.assertNotIn("GigabitEthernet0/0/0/1.200", output)
        self.assertIn("p2p KEEP", output)
        self.assertIn("192.0.2.20 pw-id 200", output)
        self.assertIn("p2p REMOTE-ONLY", output)
        self.assertIn("192.0.2.30 pw-id 300", output)
        self.assertIn("pw-class SHARED", output)

    def test_neighbor_override_keeps_common_template_parameters(self):
        document = load("iosxr_bgp_source_overrides.cfg")
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors, result.errors)
        output = document.render()
        self.assertIn(
            "neighbor 10.0.0.2\n  use neighbor-group CORE\n  update-source Loopback0",
            output,
        )
        self.assertIn("neighbor-group CORE\n  remote-as 65001", output)
        self.assertNotIn("neighbor 192.0.2.2", output)
        self.assertNotIn("update-source Gi0/0/0/1", output)
        self.assertTrue(
            any("失效默认源" in change["reason"] for change in result.changes)
        )

    def test_nested_template_and_neighbor_overrides_keep_inherited_parameters(
        self,
    ):
        document = load("iosxr_bgp_source_overrides.cfg")
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors, result.errors)
        output = document.render()
        for retained in (
            "neighbor 10.0.0.3",
            "neighbor 10.0.0.4",
            "neighbor-group WRAPPER",
            "neighbor-group OVERRIDE",
            "session-group BAD\n  timers 15 45",
            "remote-as 65003",
        ):
            self.assertIn(retained, output)
        self.assertNotIn("neighbor 192.0.2.3", output)
        self.assertNotIn("update-source GigabitEthernet0/0/0/1", output)
        self.assertFalse(
            document.validation.validate_interfaces(
                set(), result.removed_interfaces
            ).errors
        )

    def test_inherited_ip_source_is_evaluated_in_each_actual_neighbor_vrf(self):
        document = load("iosxr_bgp_source_overrides.cfg")
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors, result.errors)
        output = document.render()
        # 192.0.2.1 只剩 BLUE 内的本地地址，缺省表邻居不能借用它。
        self.assertNotIn("neighbor 192.0.2.4", output)
        self.assertIn(
            "neighbor 10.0.0.5\n   use neighbor-group ADDRESS", output
        )
        self.assertIn(
            "neighbor-group ADDRESS\n  local-address 192.0.2.1", output
        )
        # BLUE 的地址已删，不能因 RED 仍有同地址而保留 BLUE 邻居。
        self.assertNotIn("neighbor 198.51.100.2", output)
        self.assertIn(
            "neighbor 10.0.0.6\n   use session-group RED-ADDRESS", output
        )
        self.assertIn("local-address 198.51.100.1", output)
        self.assertNotIn("neighbor 2001:db8::2", output)

    def test_multiple_inherited_sources_are_preserved_with_warning(self):
        document = load("iosxr_bgp_source_conflicts.cfg")
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors)
        self.assertTrue(
            any(
                "无法确定" in warning and "neighbor 10.0.0.2" in warning
                for warning in result.warnings
            )
        )
        self.assertIn("neighbor 10.0.0.2", document.render())

    def test_missing_duplicate_and_cyclic_templates_are_preserved_with_warnings(
        self,
    ):
        cases = {
            "missing": (
                "router bgp 65000\n neighbor 10.0.0.2\n"
                "  use neighbor-group MISSING\n!\nend\n",
                "neighbor 10.0.0.2",
            ),
            "duplicate": (
                "router bgp 65000\n"
                " neighbor-group DUP\n  update-source Loopback0\n !\n"
                " neighbor-group DUP\n  update-source Loopback1\n !\n"
                " neighbor 10.0.0.2\n  use neighbor-group DUP\n!\nend\n",
                "neighbor 10.0.0.2",
            ),
            "cycle": (
                "router bgp 65000\n"
                " neighbor-group A\n  use neighbor-group B\n !\n"
                " neighbor-group B\n  use neighbor-group A\n !\n"
                " neighbor 10.0.0.2\n  use neighbor-group A\n!\nend\n",
                "neighbor 10.0.0.2",
            ),
        }
        for name, (source, retained) in cases.items():
            with self.subTest(name=name):
                document = CiscoDocument(source)
                result = document.pruning.prune_non_nni(set())
                self.assertFalse(result.errors)
                self.assertTrue(result.warnings)
                self.assertIn(retained, document.render())

    def test_explicit_neighbor_source_overrides_different_inherited_defaults(
        self,
    ):
        document = load("iosxr_bgp_override_conflicting_defaults.cfg")
        result = document.pruning.prune_non_nni({"Gi0/0/0/0"})
        self.assertFalse(result.errors, result.errors)
        output = document.render()
        self.assertIn("neighbor 10.0.0.2", output)
        self.assertIn("remote-as 65001", output)
        self.assertIn("timers 30 90", output)


if __name__ == "__main__":
    unittest.main()
