"""固定清洗与接口语义规则之间的范围和开关边界。"""

import unittest
from types import SimpleNamespace

from cleaning_support import clean_hardware
from adaptation.cleaning_rules import CleaningRulesHandler
from adaptation.cleaning_rules import resolve_cleaning_scope
from impl.cisco.document import CiscoDocument
from impl.common.cleaning import CleaningOperation
from impl.common.policies import SimulationAdaptationPolicy
from impl.juniper.document import JunosDocument


class CleaningScopeTest(unittest.TestCase):
    def test_explicit_interface_set_and_empty_scope_in_both_vendors(self):
        cases = [
            (CiscoDocument, "interface Gi0/0/0/0\n speed 1000\n!\ninterface Gi0/0/0/1\n speed 1000\n!\n", "GigabitEthernet0/0/0/0", (r"^interface\b",)),
            (JunosDocument, "interfaces { ge-0/0/0 { speed 1g; } ge-0/0/1 { speed 1g; } }", "ge-0/0/0", ("^interfaces$", "*")),
        ]
        for factory, source, interface, path in cases:
            for names, expected in [(None, 2), (frozenset(), 0), (frozenset({interface}), 1)]:
                with self.subTest(vendor=factory.vendor, names=names):
                    document = factory(source)
                    operation = CleaningOperation(path, "^speed", "delete", interface_names=names)
                    self.assertEqual(document.apply_cleaning_rules([operation]), [expected])
                    self.assertEqual(document.render().count("speed"), 2 - expected)

    def test_scope_loading_defaults_and_rejects_invalid_values(self):
        raw = dict(id="test", vendor="cisco_iosxr", enable=True, category="test",
                   path=[], match="test", action="delete", reason="test")
        self.assertEqual(CleaningRulesHandler._parse_rule(raw, set()).scope, "document")
        for scope in ["compatibility_interfaces", "simulation_interfaces"]:
            self.assertEqual(CleaningRulesHandler._parse_rule(dict(raw, scope=scope), set()).scope, scope)
        for scope in ["unknown", None, [], 1]:
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "scope"):
                CleaningRulesHandler._parse_rule(dict(raw, scope=scope), set())

    def test_compatibility_parent_closure_preserves_bundle_and_non_target(self):
        document = CiscoDocument(
            "interface Gi0/0/0/0\n no speed 1000\n!\n"
            "interface Gi0/0/0/0.100\n speed 1000\n!\n"
            "interface Gi0/0/0/0.200\n speed 1000\n!\n"
            "interface Bundle-Ether10\n speed 1000\n!\n"
            "interface Gi0/0/0/5\n speed 1000\n!\n"
            "interface BVI10\n speed 1000\n!\n"
        )
        events = clean_hardware(document, {"Gi0/0/0/0.100", "Bundle-Ether10", "BVI10"},
                                SimulationAdaptationPolicy(mode="off"))
        self.assertEqual(sum(event["hits"] for event in events), 4)
        self.assertEqual(document.render().count("speed"), 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(clean_hardware(document, {"Gi0/0/0/0.100"}), [])

    def test_simulation_policy_and_positive_command_difference(self):
        for mode in ["off", "compatible", "stable"]:
            for enabled in [False, True]:
                with self.subTest(mode=mode, enabled=enabled):
                    document = CiscoDocument(
                        "interface Bundle-Ether10\n speed 1000\n no fec standard\n!\n"
                    )
                    policy = SimulationAdaptationPolicy(mode=mode, remove_physical_interface_knobs=enabled)
                    clean_hardware(document, {"Bundle-Ether10"}, policy)
                    self.assertEqual("speed 1000" in document.render(), mode == "off" or not enabled)
                    self.assertIn("no fec standard", document.render())

    def test_hardware_is_not_deleted_by_adapt_or_simulation_fallback(self):
        document = CiscoDocument("interface Gi0/0/0/0\n speed 1000\n!\n")
        document.adapt_interfaces({"Gi0/0/0/0"})
        document.adapt_to_simulation(SimulationAdaptationPolicy(), {"Gi0/0/0/0"})
        self.assertIn("speed 1000", document.render())
        validation = document.validate_interfaces({"Gi0/0/0/0"}, set())
        self.assertTrue(any("残留不兼容属性" in error for error in validation.errors))

    def test_junos_cleanup_preserves_aggregation_and_non_option_unit_attributes(self):
        document = JunosDocument(
            "interfaces { ge-0/0/0 { speed 1g; ether-options { 802.3ad ae10; "
            "fec fec91; nested { clocking internal; } } unit 0 { speed keep; } } "
            "ge-0/0/1 { speed 1g; } ae10 { aggregated-ether-options { lacp { active; } } } }"
        )
        clean_hardware(document, {"ge-0/0/0"})
        document.adapt_to_simulation(SimulationAdaptationPolicy(), {"ge-0/0/0"})
        output = document.render()
        for retained in ["802.3ad ae10", "speed keep", "speed 1g", "active;"]:
            self.assertIn(retained, output)
        self.assertNotIn("fec fec91", output)
        self.assertNotIn("clocking internal", output)

    def test_junos_off_mode_preserves_hardware_and_empty_options(self):
        document = JunosDocument("interfaces { ge-0/0/0 { speed 1g; ether-options {} } }")
        source = document.render()
        policy = SimulationAdaptationPolicy(mode="off")
        self.assertEqual(clean_hardware(document, {"ge-0/0/0"}, policy), [])
        document.adapt_to_simulation(policy, {"ge-0/0/0"})
        self.assertEqual(document.render(), source)

    def test_named_scope_cannot_be_used_without_device_context(self):
        rule = next(rule for rule in CleaningRulesHandler._configured_rules() if rule.scope != "document")
        with self.assertRaisesRegex(ValueError, "上下文"):
            rule.apply(CiscoDocument("hostname R1\n"))
        with self.assertRaisesRegex(ValueError, "范围"):
            resolve_cleaning_scope("unknown", SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
