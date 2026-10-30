"""NNI 分配不能覆盖跳过链路，接口别名不能绕过目标唯一性校验。"""

from pathlib import Path
import tempfile
import unittest

from config_adaptor.adaptation.core_interfaces import NonNNICleanupHandler
from config_adaptor.adaptation.models import (
    ConversionContext, Device, DeviceContext, Link, TopologyWorkbook, Vendor,
)
from config_adaptor.adaptation.nni import NNIHandler
from config_adaptor.adaptation.factory import parse_document
from config_adaptor.adaptation.profiles import load_profiles
from config_adaptor.adaptation.topology import TopologyPreflightHandler


FIXTURES = Path(__file__).parent / "fixtures" / "interface_compatibility"


def context_for(fixture, active_sources, skipped_source, vendor=Vendor.CISCO_IOSXR):
    device = Device("A", vendor, None, 2)
    peer = Device("P", Vendor.HUAWEI, None, 3)
    unavailable = Device("X", Vendor.CISCO_IOSXR, None, 4)
    links = [
        Link(index + 2, "A", source, "P", f"Ethernet{index}")
        for index, source in enumerate(active_sources)
    ]
    links.append(Link(len(links) + 2, "A", skipped_source, "X", "Ethernet0"))
    config_path = FIXTURES / fixture
    profile = load_profiles(None)[vendor]
    document = parse_document(vendor.value, config_path.read_text())
    topology = TopologyWorkbook(
        Path("unused.xlsx"), None, "devices", "links", {}, {},
        [device, peer, unavailable], links,
    )
    context = ConversionContext(
        topology, {"A": DeviceContext(device, config_path, document, profile)},
        external_devices={"P"},
    )
    TopologyPreflightHandler().process(context)
    NonNNICleanupHandler().process(context)
    return context


class NniTargetSafetyTest(unittest.TestCase):
    def test_skipped_physical_target_is_not_overwritten(self):
        context = context_for("iosxr_skip_target.cfg", ["Gi0/0/0/7"], "Gi0/0/0/0")
        NNIHandler().process(context)
        self.assertFalse(context.has_errors, context.errors)
        self.assertEqual(context.topology.links[0].a_interface, "GigabitEthernet0/0/0/1")
        output = context.devices["A"].document.render()
        self.assertIn("interface GigabitEthernet0/0/0/0\n description SKIPPED-NNI", output)
        self.assertIn("interface GigabitEthernet0/0/0/1\n description ACTIVE-SEVEN", output)

    def test_skipped_bundle_reserves_every_member_target(self):
        for skipped in ("Gi0/0/0/0", "BE10"):
            with self.subTest(skipped=skipped):
                context = context_for("iosxr_skip_bundle.cfg", ["Gi0/0/0/7"], skipped)
                NNIHandler().process(context)
                self.assertFalse(context.has_errors, context.errors)
                self.assertEqual(context.topology.links[0].a_interface, "GigabitEthernet0/0/0/2")
                output = context.devices["A"].document.render()
                self.assertIn("interface Bundle-Ether10", output)
                self.assertEqual(output.count("bundle id 10 mode active"), 2)

    def test_shared_active_and_skipped_source_fails_before_migration(self):
        cases = [
            ("iosxr_skip_target.cfg", "Gi0/0/0/7", "Gi0/0/0/7"),
            ("iosxr_skip_bundle.cfg", "Gi0/0/0/1", "Gi0/0/0/0"),
        ]
        for fixture, active, skipped in cases:
            with self.subTest(active=active, skipped=skipped):
                context = context_for(fixture, [active], skipped)
                before = context.devices["A"].document.render()
                NNIHandler().process(context)
                self.assertTrue(context.has_errors)
                self.assertIn("同时属于活动链路和跳过链路", "\n".join(context.errors))
                self.assertEqual(context.devices["A"].document.render(), before)
                self.assertEqual(context.topology.links[0].a_interface, active)

    def test_junos_skipped_aggregate_members_are_not_reused(self):
        context = context_for(
            "junos_skip_bundle.cfg", ["ge-0/0/7"], "ge-0/0/0", Vendor.JUNIPER_JUNOS
        )
        NNIHandler().process(context)
        self.assertFalse(context.has_errors, context.errors)
        self.assertEqual(context.topology.links[0].a_interface, "ge-0/0/2")
        output = context.devices["A"].document.render()
        self.assertEqual(output.count("802.3ad ae10;"), 2)
        self.assertIn("description SKIPPED-BUNDLE;", output)

    def test_capacity_uses_remaining_targets_and_does_not_apply_partial_plans(self):
        context = context_for(
            "iosxr_skip_target.cfg", ["Gi0/0/0/6", "Gi0/0/0/7"], "Gi0/0/0/0"
        )
        context.devices["A"].profile.interfaces = [
            f"GigabitEthernet0/0/0/{index}" for index in range(5)
        ]
        before = context.devices["A"].document.render()
        NNIHandler().process(context)
        self.assertTrue(context.has_errors)
        self.assertIn("可用物理接口数 1", "\n".join(context.errors))
        self.assertEqual(context.devices["A"].document.render(), before)
        self.assertEqual([link.a_interface for link in context.topology.links[:2]], ["Gi0/0/0/6", "Gi0/0/0/7"])

    def test_programmatic_alias_targets_fail_before_migration(self):
        context = context_for("iosxr_skip_target.cfg", ["Gi0/0/0/7"], "Gi0/0/0/0")
        context.devices["A"].profile.interfaces[1] = "Gi0/0/0/0"
        before = context.devices["A"].document.render()
        NNIHandler().process(context)
        self.assertTrue(context.has_errors)
        self.assertIn("规范化后存在重复项", "\n".join(context.errors))
        self.assertEqual(context.devices["A"].document.render(), before)


class ImageProfileCanonicalizationTest(unittest.TestCase):
    def profile(self, vendor, interfaces):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.yaml"
            path.write_text(f"profiles:\n  {vendor.value}:\n    interfaces: {interfaces!r}\n")
            return load_profiles(path)[vendor]

    def test_cisco_aliases_and_junos_whitespace_normalize(self):
        cisco = self.profile(Vendor.CISCO_IOSXR, [f"Gi 0/0/0/{index}" for index in range(4)])
        self.assertEqual(cisco.interfaces, [f"GigabitEthernet0/0/0/{index}" for index in range(4)])
        junos = self.profile(Vendor.JUNIPER_JUNOS, [f" ge-0/0/{index} " for index in range(4)])
        self.assertEqual(junos.interfaces, [f"ge-0/0/{index}" for index in range(4)])

    def test_canonical_duplicates_are_rejected_including_reserved_ports(self):
        cases = [
            (Vendor.CISCO_IOSXR, ["Gi0/0/0/0", "Gi0/0/0/1", "Gi0/0/0/2", "GigabitEthernet0/0/0/0"]),
            (Vendor.JUNIPER_JUNOS, ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2", " ge-0/0/0 "]),
        ]
        for vendor, interfaces in cases:
            with self.subTest(vendor=vendor), self.assertRaisesRegex(ValueError, "规范化后存在重复项"):
                self.profile(vendor, interfaces)


if __name__ == "__main__":
    unittest.main()
