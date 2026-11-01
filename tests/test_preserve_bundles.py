"""物理拓扑只列成员口时，逻辑聚合业务完整保留。"""

import unittest
from pathlib import Path

from adaptation.factory import parse_document
from adaptation.models import (
    ConversionContext,
    Device,
    DeviceContext,
    Link,
    TopologyWorkbook,
    Vendor,
)
from adaptation.pipeline import build_default_pipeline
from adaptation.profiles import load_profiles

FIXTURES = Path(__file__).parent / "fixtures" / "preserve_bundles"


def context_for(vendor, split_peer=False, filename_override=None):
    filename = "iosxr.cfg" if vendor == Vendor.CISCO_IOSXR else "junos.cfg"
    filename = filename_override or filename
    ports = (
        ["Hu0/0/0/0", "Hu0/0/0/1"]
        if vendor == Vendor.CISCO_IOSXR
        else ["ge-0/0/10", "ge-0/0/11"]
    )
    devices = [
        Device(name, vendor, None, row + 2)
        for row, name in enumerate(["A", "B", "C"])
    ]
    links = [
        Link(
            index + 2,
            f"link-{index + 2}",
            "A",
            port,
            "C" if split_peer and index else "B",
            port,
        )
        for index, port in enumerate(ports)
    ]
    topology = TopologyWorkbook(
        Path("unused"),
        None,
        "",
        "",
        {},
        {},
        devices,
        links,
    )
    profiles = load_profiles(None)
    loaded = {
        device.name: DeviceContext(
            device,
            FIXTURES / filename,
            parse_document(vendor.value, (FIXTURES / filename).read_text()),
            profiles[vendor],
        )
        for device in devices
    }
    return ConversionContext(topology, loaded)


def _context_with_unbundled_peer_member(member_mode="on"):
    context = context_for(Vendor.CISCO_IOSXR)
    third_a = "HundredGigE0/0/0/2"
    third_b = "HundredGigE0/0/0/2"
    context.topology.links.append(Link(4, "link-4", "A", third_a, "B", third_b))
    a_path = FIXTURES / "iosxr.cfg"
    a_source = a_path.read_text().replace(
        "interface Bundle-Ether10\n",
        "interface HundredGigE0/0/0/2\n"
        f" bundle id 10 mode {member_mode}\n"
        "!\n"
        "interface Bundle-Ether10\n",
        1,
    )
    context.devices["A"].document = parse_document(
        Vendor.CISCO_IOSXR.value, a_source
    )
    b_source = a_path.read_text().replace(
        "interface HundredGigE0/0/0/1\n" " bundle id 10 mode on\n" "!\n",
        "interface HundredGigE0/0/0/1\n"
        f" bundle id 10 mode {member_mode}\n"
        "!\n"
        "interface HundredGigE0/0/0/2\n"
        " description INDEPENDENT\n"
        "!\n",
        1,
    )
    context.devices["B"].document = parse_document(
        Vendor.CISCO_IOSXR.value, b_source
    )
    return context


def _context_with_junos_member_business():
    context = context_for(Vendor.JUNIPER_JUNOS)
    source = (
        (FIXTURES / "junos.cfg")
        .read_text()
        .replace(
            "    ge-0/0/10 { gigether-options { 802.3ad ae10; speed 100g; } }",
            "    ge-0/0/10 {\n"
            "        gigether-options { 802.3ad ae10; speed 100g; }\n"
            "        unit 200 { family inet { address 192.0.2.10/31; } }\n"
            "    }",
            1,
        )
    )
    context.devices["A"].document = parse_document(
        Vendor.JUNIPER_JUNOS.value, source
    )
    return context


class PreserveBundleTest(unittest.TestCase):

    def test_cisco_main_subinterfaces_and_protocols_survive_with_one_member(
        self,
    ):
        context = context_for(Vendor.CISCO_IOSXR)
        build_default_pipeline().execute(context)
        self.assertFalse(
            context.has_errors, context.errors + context.devices["A"].errors
        )
        device = context.devices["A"]
        output = device.document.render()
        self.assertIn("interface GigabitEthernet0/0/0/0", output)
        self.assertIn("bundle id 10 mode on", output)
        self.assertIn("bundle minimum-active links 1", output)
        self.assertNotIn("minimum-active bandwidth", output)
        self.assertIn("interface Bundle-Ether10\n", output)
        self.assertTrue(
            any("显式 MAC" in warning for warning in device.warnings)
        )
        self.assertIn("ipv4 address 10.0.0.1 255.255.255.252", output)
        self.assertIn("interface Bundle-Ether10.100", output)
        self.assertIn("encapsulation dot1q 100", output)
        self.assertIn("  interface Bundle-Ether10.100", output)
        self.assertNotIn("HundredGigE", output)
        self.assertNotIn("Bundle-Ether10", device.replacement_map)
        self.assertEqual(sum(link.active for link in context.topology.links), 1)
        self.assertTrue(
            any(mapping.action == "preserve" for mapping in device.mappings)
        )
        self.assertTrue(
            any(
                event["kind"] == "bundle-adaptation" for event in context.events
            )
        )

    def test_unbundled_peer_member_remains_a_separate_mixed_link(self):
        context = _context_with_unbundled_peer_member()
        third_a = "HundredGigE0/0/0/2"
        third_b = "HundredGigE0/0/0/2"

        build_default_pipeline().execute(context)

        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        self.assertTrue(
            any(
                "非对称混合链路" in warning and "HundredGigE0/0/0/2" in warning
                for warning in context.warnings
            )
        )
        self.assertEqual(
            [link.row for link in context.topology.links if link.active],
            [2, 4],
        )
        self.assertEqual(len(device.document.interfaces.analysis.bundle_members()), 2)
        self.assertEqual(len(context.devices["B"].document.interfaces.analysis.bundle_members()), 1)
        self.assertTrue(
            any(
                mapping.source_interface == third_a
                and mapping.action == "map-member"
                for mapping in device.mappings
            )
        )
        self.assertTrue(
            any(
                mapping.source_interface == third_b and mapping.action == "map"
                for mapping in context.devices["B"].mappings
            )
        )

    def test_lacp_member_with_unbundled_peer_is_preserved_with_warning(self):
        context = _context_with_unbundled_peer_member(member_mode="active")

        build_default_pipeline().execute(context)

        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        self.assertTrue(
            any("非对称混合链路" in warning for warning in context.warnings)
        )
        self.assertFalse(
            any(
                "第 3 行" in warning and "协商方式不兼容" in warning
                for warning in context.warnings
            )
        )
        self.assertIn("bundle id 10 mode active", device.document.render())

    def test_passive_member_with_unbundled_peer_is_preserved_with_warning(self):
        context = _context_with_unbundled_peer_member(member_mode="passive")

        build_default_pipeline().execute(context)

        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        self.assertTrue(
            any("非对称混合链路" in warning for warning in context.warnings)
        )
        self.assertIn("bundle id 10 mode passive", device.document.render())

    def test_junos_binding_and_ae_units_survive_stable_simulation(self):
        context = context_for(Vendor.JUNIPER_JUNOS)
        build_default_pipeline().execute(context)
        self.assertFalse(
            context.has_errors, context.errors + context.devices["A"].errors
        )
        output = context.devices["A"].document.render()
        for text in [
            "802.3ad ae10;",
            "minimum-links 1;",
            "ae10 {",
            "address 10.0.0.1/30;",
            "interface ae10.0;",
            "interface ae10.100;",
        ]:
            self.assertIn(text, output)
        self.assertNotIn("minimum-bandwidth", output)
        self.assertNotIn("speed 100g", output)
        self.assertEqual(
            context.devices["A"].document.interfaces.analysis.bundle_members(), {"ge-0/0/0": "ae10"}
        )

    def test_lacp_is_preserved_without_enforcing_peer_mode_consistency(self):
        context = context_for(
            Vendor.CISCO_IOSXR, filename_override="iosxr_lacp.cfg"
        )
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors, context.errors)
        self.assertIn(
            "bundle id 10 mode active", context.devices["A"].document.render()
        )
        context = context_for(Vendor.CISCO_IOSXR)
        context.devices["B"].document = parse_document(
            Vendor.CISCO_IOSXR.value, (FIXTURES / "iosxr_lacp.cfg").read_text()
        )
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors, context.errors)
        self.assertTrue(
            any("协商方式不兼容" in warning for warning in context.warnings)
        )
        self.assertIn(
            "bundle id 10 mode on", context.devices["A"].document.render()
        )
        self.assertIn(
            "bundle id 10 mode active",
            context.devices["B"].document.render(),
        )

    def test_cisco_member_business_is_preserved_without_compression(self):
        context = context_for(
            Vendor.CISCO_IOSXR,
            filename_override="iosxr_member_business.cfg",
        )

        build_default_pipeline().execute(context)

        self.assertFalse(context.has_errors, context.errors)
        self.assertEqual(
            [link.row for link in context.topology.links if link.active],
            [2, 3],
        )
        self.assertEqual(len(context.devices["A"].document.interfaces.analysis.bundle_members()), 2)
        self.assertTrue(
            any(
                "独立业务" in warning
                for warning in context.warnings + context.devices["A"].warnings
            )
        )
        self.assertIn(
            "ipv4 address 192.0.2.1 255.255.255.0",
            context.devices["A"].document.render(),
        )

    def test_conflicting_bundle_binding_fails_before_migration(self):
        context = context_for(
            Vendor.CISCO_IOSXR,
            filename_override="iosxr_conflicting_binding.cfg",
        )

        build_default_pipeline().execute(context)

        self.assertTrue(context.has_errors)
        self.assertFalse(
            any(mapping.action == "map-member" for mapping in context.mappings)
        )

    def test_junos_member_business_is_preserved_without_compression(self):
        context = _context_with_junos_member_business()

        build_default_pipeline().execute(context)

        self.assertFalse(context.has_errors, context.errors)
        self.assertEqual(
            [link.row for link in context.topology.links if link.active],
            [2, 3],
        )
        self.assertEqual(len(context.devices["A"].document.interfaces.analysis.bundle_members()), 2)
        self.assertTrue(
            any(
                "独立业务" in warning
                for warning in context.warnings + context.devices["A"].warnings
            )
        )
        self.assertIn(
            "address 192.0.2.10/31;",
            context.devices["A"].document.render(),
        )

    def test_business_bundle_without_members_is_deleted_as_non_nni(self):
        cases = [
            (
                Vendor.CISCO_IOSXR,
                ["HundredGigE0/0/0/0", "HundredGigE0/0/0/1"],
                "Bundle-Ether10",
                "interface Bundle-Ether10\n",
            ),
            (
                Vendor.JUNIPER_JUNOS,
                ["ge-0/0/10", "ge-0/0/11"],
                "ae10",
                "ae10 {",
            ),
        ]
        for vendor, members, bundle, bundle_header in cases:
            with self.subTest(vendor=vendor):
                filename = (
                    "iosxr.cfg" if vendor == Vendor.CISCO_IOSXR else "junos.cfg"
                )
                document = parse_document(
                    vendor.value,
                    (FIXTURES / filename).read_text(),
                )
                for member in members:
                    document.interfaces.transform.remove(member)

                outcome = document.pruning.prune_non_nni(set())

                self.assertFalse(outcome.errors, outcome.errors)
                self.assertIn(bundle, outcome.removed_interfaces)
                self.assertNotIn(bundle_header, document.render())

    def test_junos_lacp_modes_are_preserved_without_consistency_validation(
        self,
    ):
        context = context_for(
            Vendor.JUNIPER_JUNOS, filename_override="junos_conflicting_lacp.cfg"
        )
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors, context.errors)
        rendered = context.devices["A"].document.render()
        self.assertIn("active;", rendered)
        self.assertIn("passive;", rendered)

    def test_same_physical_port_cannot_be_copied_to_two_targets(self):
        context = context_for(Vendor.CISCO_IOSXR)
        context.topology.links[1].a_interface = context.topology.links[
            0
        ].a_interface
        build_default_pipeline().execute(context)
        self.assertTrue(context.has_errors)
        self.assertTrue(any("重复占用" in error for error in context.errors))

    def test_cross_peer_bundle_is_rejected_instead_of_copying_business(self):
        for vendor in [Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS]:
            with self.subTest(vendor=vendor):
                context = context_for(vendor, True)
                build_default_pipeline().execute(context)
                self.assertTrue(context.has_errors)
                self.assertTrue(
                    any("MC-LAG" in error for error in context.errors)
                )
                self.assertFalse(
                    any(
                        mapping.action in {"map-member", "preserve"}
                        for mapping in context.mappings
                    )
                )

    def test_unknown_removed_physical_reference_is_deferred_through_pipeline(
        self,
    ):
        context = context_for(Vendor.CISCO_IOSXR)
        document = context.devices["A"].document
        transaction = document.edit()
        terminal = next(
            node for node in document.root.nodes if node.header.lower() == "end"
        )
        transaction.insert_before(
            terminal,
            "interface GigabitEthernet0/0/0/99\n"
            " ipv4 address 203.0.113.1 255.255.255.0\n"
            "!\n"
            "custom-service FOO\n"
            " bind-interface GigabitEthernet0/0/0/99\n"
            "!\n",
            "add deferred reference regression",
        )
        transaction.apply()

        build_default_pipeline().execute(context)

        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        self.assertTrue(device.deferred_interface_references)
        self.assertIn("custom-service FOO", device.document.render())

    def test_redundant_member_references_are_pruned_at_nni_removal(self):
        cases = [
            (
                Vendor.CISCO_IOSXR,
                "iosxr_member_references.cfg",
                "HundredGigE0/0/0/1",
            ),
            (Vendor.JUNIPER_JUNOS, "junos_member_references.cfg", "ge-0/0/11"),
        ]
        for vendor, fixture, removed in cases:
            with self.subTest(vendor=vendor):
                context = context_for(vendor, filename_override=fixture)
                build_default_pipeline().execute(context)
                device = context.devices["A"]
                self.assertFalse(
                    context.has_errors, context.errors + device.errors
                )
                self.assertNotIn(removed, device.document.render())
                self.assertTrue(
                    any(
                        event["kind"] == "redundant-member-cleanup"
                        for event in context.events
                    )
                )
                self.assertIn(removed, device.removed_interfaces)
                self.assertTrue(
                    any(
                        mapping.action == "remove"
                        and mapping.source_interface == removed
                        for mapping in device.mappings
                    )
                )

    def test_deleted_member_name_reused_as_target_does_not_keep_old_bindings(
        self,
    ):
        context = context_for(
            Vendor.JUNIPER_JUNOS,
            filename_override="junos_member_references.cfg",
        )
        for device in context.devices.values():
            source = device.config_path.read_text().replace(
                "ge-0/0/11", "ge-0/0/0"
            )
            device.document = parse_document(Vendor.JUNIPER_JUNOS.value, source)
        context.topology.links[1].a_interface = "ge-0/0/0"
        context.topology.links[1].z_interface = "ge-0/0/0"
        build_default_pipeline().execute(context)
        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        self.assertIn("ge-0/0/0", device.document.interfaces.analysis.bundle_members())
        output = device.document.render()
        self.assertNotIn("interface ge-0/0/0;", output)
        self.assertIn("interface ae10.0;", output)


if __name__ == "__main__":
    unittest.main()
