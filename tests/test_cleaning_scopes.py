"""固定清洗与接口语义规则之间的范围和开关边界。"""

import unittest

from cleaning_support import clean_hardware

from gns3_config_adaptor.adaptation.cleaning_rules import CleaningRulesHandler
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.common.domain.policies import (
    SimulationAdaptationPolicy,
)
from gns3_config_adaptor.impl.common.rules.cleaning import CleaningOperation
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document import (
    JunosDocument,
)


class CleaningScopeTest(unittest.TestCase):

    def test_explicit_interface_set_and_empty_scope_in_both_vendors(self):
        cases = [
            (
                CiscoDocument,
                "interface Gi0/0/0/0\n speed 1000\n!\ninterface Gi0/0/0/1\n speed 1000\n!\n",
                "GigabitEthernet0/0/0/0",
                (r"^interface\b",),
            ),
            (
                JunosDocument,
                "interfaces { ge-0/0/0 { speed 1g; } ge-0/0/1 { speed 1g; } }",
                "ge-0/0/0",
                ("^interfaces$", "*"),
            ),
        ]
        for factory, source, interface, path in cases:
            for names, expected in [
                (None, 2),
                (frozenset(), 0),
                (frozenset({interface}), 1),
            ]:
                with self.subTest(vendor=factory.vendor, names=names):
                    document = factory(source)
                    operation = CleaningOperation(
                        path, "^speed", "delete", interface_names=names
                    )
                    self.assertEqual(
                        document.cleaning.apply_rules([operation]), [expected]
                    )
                    self.assertEqual(
                        document.render().count("speed"), 2 - expected
                    )

    def test_configured_rule_rejects_scope_field(self):
        raw = dict(
            id="test",
            vendor="cisco_iosxr",
            enable=True,
            category="test",
            path=[],
            match="test",
            action="delete",
            reason="test",
        )
        rule = CleaningRulesHandler._parse_rule(raw, set())
        self.assertFalse(hasattr(rule, "scope"))
        with self.assertRaisesRegex(ValueError, "未知字段"):
            CleaningRulesHandler._parse_rule(
                dict(raw, scope="compatibility_interfaces"),
                set(),
            )

    def test_hardware_cleanup_applies_to_all_remaining_interfaces(self):
        document = CiscoDocument(
            "interface Gi0/0/0/0\n no speed 1000\n!\n"
            "interface Gi0/0/0/0.100\n speed 1000\n!\n"
            "interface Gi0/0/0/0.200\n speed 1000\n!\n"
            "interface Bundle-Ether10\n speed 1000\n!\n"
            "interface Gi0/0/0/5\n speed 1000\n!\n"
            "interface BVI10\n speed 1000\n!\n"
        )
        events = clean_hardware(
            document,
            {"Gi0/0/0/0.100"},
            SimulationAdaptationPolicy(mode="off"),
        )
        self.assertEqual(sum(event["hits"] for event in events), 6)
        self.assertEqual(document.render().count("speed"), 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(clean_hardware(document, {"Gi0/0/0/0.100"}), [])

    def test_hardware_cleanup_ignores_simulation_policy(self):
        for mode in ["off", "compatible", "stable"]:
            for enabled in [False, True]:
                with self.subTest(mode=mode, enabled=enabled):
                    document = CiscoDocument(
                        "interface Bundle-Ether10\n speed 1000\n no fec standard\n!\n"
                    )
                    policy = SimulationAdaptationPolicy(
                        mode=mode, remove_physical_interface_knobs=enabled
                    )
                    clean_hardware(document, {"Bundle-Ether10"}, policy)
                    self.assertNotIn("speed 1000", document.render())
                    self.assertNotIn("no fec standard", document.render())

    def test_hardware_is_not_deleted_by_adapt_or_simulation_fallback(self):
        document = CiscoDocument("interface Gi0/0/0/0\n speed 1000\n!\n")
        document.compatibility.adapt_interfaces({"Gi0/0/0/0"})
        document.simulation.adapt(SimulationAdaptationPolicy(), {"Gi0/0/0/0"})
        self.assertIn("speed 1000", document.render())
        validation = document.validation.validate_interfaces(
            {"Gi0/0/0/0"}, set()
        )
        self.assertTrue(
            any("残留不兼容属性" in error for error in validation.errors)
        )

    def test_junos_cleanup_preserves_aggregation_and_non_option_unit_attributes(
        self,
    ):
        document = JunosDocument(
            "interfaces { ge-0/0/0 { speed 1g; ether-options { 802.3ad ae10; "
            "fec fec91; nested { clocking internal; } } unit 0 { speed keep; } } "
            "ge-0/0/1 { speed 1g; } ae10 { aggregated-ether-options { lacp { active; } } } }"
        )
        clean_hardware(document, {"ge-0/0/0"})
        document.simulation.adapt(SimulationAdaptationPolicy(), {"ge-0/0/0"})
        output = document.render()
        for retained in ["802.3ad ae10", "speed keep", "active;"]:
            self.assertIn(retained, output)
        self.assertNotIn("speed 1g", output)
        self.assertNotIn("fec fec91", output)
        self.assertNotIn("clocking internal", output)

    def test_junos_hardware_cleanup_ignores_off_mode(self):
        document = JunosDocument(
            "interfaces { ge-0/0/0 { speed 1g; ether-options {} } }"
        )
        policy = SimulationAdaptationPolicy(mode="off")
        events = clean_hardware(document, {"ge-0/0/0"}, policy)
        self.assertEqual(sum(event["hits"] for event in events), 1)
        self.assertNotIn("speed 1g", document.render())
        self.assertIn("ether-options;", document.render())
        document.simulation.adapt(policy, {"ge-0/0/0"})
        self.assertIn("ether-options;", document.render())


if __name__ == "__main__":
    unittest.main()
