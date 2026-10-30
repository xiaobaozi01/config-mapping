"""M-LAG 一对多只能复制可等价表达的接口配置和引用。"""

from pathlib import Path
import unittest

from config_adaptor.adaptation.factory import parse_document
from config_adaptor.adaptation.models import (
    ConversionContext, Device, DeviceContext, Link, TopologyWorkbook, Vendor,
)
from config_adaptor.adaptation.nni import NNIHandler
from config_adaptor.adaptation.profiles import load_profiles
from config_adaptor.adaptation.service import prepare_context
from config_adaptor.adaptation.pipeline import build_default_pipeline


FIXTURES = Path(__file__).parent / "fixtures"
CONFIGS = FIXTURES / "interface_compatibility"


def load(vendor, filename):
    return parse_document(vendor.value, (CONFIGS / filename).read_text())


def context_for(vendor, filename, count=2):
    name = "A"
    device = Device(name, vendor, filename, 2)
    peers = [Device(peer, Vendor.HUAWEI, None, index + 3) for index, peer in enumerate(("P", "Q"))]
    prefix = "Gi0/0/0/" if vendor == Vendor.CISCO_IOSXR else "ge-0/0/"
    links = [Link(index + 2, name, prefix + str(index), peer.name, "Ethernet0") for index, peer in enumerate(peers[:count])]
    topology = TopologyWorkbook(Path("unused.xlsx"), None, "devices", "links", {}, {}, [device, *peers], links)
    profile = load_profiles(None)[vendor]
    return ConversionContext(
        topology, {name: DeviceContext(device, CONFIGS / filename, load(vendor, filename), profile)},
        external_devices={peer.name for peer in peers},
    )


class NniCloneConstraintsTest(unittest.TestCase):
    def test_l3_addresses_on_bundle_children_fail_before_any_clone(self):
        for vendor, filename, source in (
            (Vendor.CISCO_IOSXR, "iosxr_clone_l3.cfg", "Bundle-Ether10"),
            (Vendor.JUNIPER_JUNOS, "junos_clone_l3.cfg", "ae10"),
        ):
            with self.subTest(vendor=vendor):
                context = context_for(vendor, filename)
                before = context.devices["A"].document.render()
                NNIHandler().process(context)
                self.assertTrue(context.has_errors)
                self.assertEqual(context.devices["A"].document.render(), before)
                self.assertEqual(context.devices["A"].mappings, [])
                errors = "\n".join(context.errors + context.devices["A"].errors)
                self.assertIn("设备 A", errors)
                self.assertIn(source, errors)
                self.assertIn(source, errors)
                self.assertTrue(all("target_interfaces" in event for event in context.events if event["kind"] == "nni-clone-rejected"))

    def test_bgp_single_source_reference_cannot_expand_to_multiple_targets(self):
        for vendor, filename in (
            (Vendor.CISCO_IOSXR, "iosxr_clone_source.cfg"),
            (Vendor.JUNIPER_JUNOS, "junos_clone_source.cfg"),
        ):
            with self.subTest(vendor=vendor):
                context = context_for(vendor, filename)
                before = context.devices["A"].document.render()
                NNIHandler().process(context)
                self.assertTrue(context.has_errors)
                self.assertEqual(context.devices["A"].document.render(), before)
                self.assertTrue(context.errors + context.devices["A"].errors)

    def test_one_to_one_l3_mapping_remains_allowed(self):
        for vendor, filename in (
            (Vendor.CISCO_IOSXR, "iosxr_clone_l3.cfg"),
            (Vendor.JUNIPER_JUNOS, "junos_clone_l3.cfg"),
        ):
            with self.subTest(vendor=vendor):
                context = context_for(vendor, filename, 1)
                NNIHandler().process(context)
                self.assertFalse(context.has_errors, context.devices["A"].errors)
                self.assertIn("10.0.0.1", context.devices["A"].document.render())
                self.assertIn("2001:db8::1", context.devices["A"].document.render())

    def test_empty_l3_family_is_not_unconditionally_forbidden(self):
        document = load(Vendor.JUNIPER_JUNOS, "junos_clone_source.cfg")
        protocols = next(node for node in document.root.children if node.header == "protocols")
        protocols.active = False
        outcome = document.validate_nni_clone("ae10", {"ge-0/0/0", "ge-0/0/1"})
        self.assertEqual(outcome.errors, [])

    def test_cross_peer_bundles_are_rejected_without_cloning_even_for_l2(self):
        for vendor, filename in (
            (Vendor.CISCO_IOSXR, "iosxr_clone_source.cfg"),
            (Vendor.JUNIPER_JUNOS, "junos_clone_l2.cfg"),
        ):
            with self.subTest(vendor=vendor):
                context = context_for(vendor, filename)
                before = context.devices["A"].document.render()
                NNIHandler().process(context)
                self.assertTrue(context.has_errors)
                self.assertEqual(context.devices["A"].document.render(), before)
                self.assertFalse(any(mapping.is_mlag_clone for mapping in context.mappings))

    def test_programmatic_subinterface_target_fails_before_clone(self):
        context = context_for(Vendor.CISCO_IOSXR, "iosxr_clone_l3.cfg", 1)
        context.devices["A"].profile.interfaces[0] += ".100"
        before = context.devices["A"].document.render()
        NNIHandler().process(context)
        self.assertTrue(context.has_errors)
        self.assertEqual(context.devices["A"].document.render(), before)
        self.assertIn("物理父口", "\n".join(context.errors + context.devices["A"].errors))


if __name__ == "__main__":
    unittest.main()
