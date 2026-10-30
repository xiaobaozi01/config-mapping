"""物理拓扑只列成员口时，逻辑聚合业务完整保留。"""
from pathlib import Path
import unittest
from config_adaptor.adaptation.factory import parse_document
from config_adaptor.adaptation.models import ConversionContext, Device, DeviceContext, Link, TopologyWorkbook, Vendor
from config_adaptor.adaptation.profiles import load_profiles
from config_adaptor.adaptation.pipeline import build_default_pipeline

FIXTURES = Path(__file__).parent / 'fixtures' / 'preserve_bundles'

def context_for(vendor, split_peer=False, filename_override=None):
    filename = 'iosxr.cfg' if vendor == Vendor.CISCO_IOSXR else 'junos.cfg'
    filename = filename_override or filename
    ports = ['Hu0/0/0/0', 'Hu0/0/0/1'] if vendor == Vendor.CISCO_IOSXR else ['ge-0/0/10', 'ge-0/0/11']
    devices = [Device(name, vendor, None, row+2) for row, name in enumerate(['A', 'B', 'C'])]
    topology = TopologyWorkbook(Path('unused'), None, '', '', {}, {}, devices, [Link(i+2, 'A', port, 'C' if split_peer and i else 'B', port) for i, port in enumerate(ports)])
    profiles = load_profiles(None)
    loaded = {device.name: DeviceContext(device, FIXTURES/filename, parse_document(vendor.value, (FIXTURES/filename).read_text()), profiles[vendor]) for device in devices}
    return ConversionContext(topology, loaded)

class PreserveBundleTest(unittest.TestCase):
    def test_cisco_main_subinterfaces_and_protocols_survive_with_one_member(self):
        context = context_for(Vendor.CISCO_IOSXR)
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors, context.errors + context.devices['A'].errors)
        device = context.devices['A']
        output = device.document.render()
        self.assertIn('interface GigabitEthernet0/0/0/0', output)
        self.assertIn('bundle id 10 mode on', output)
        self.assertIn('bundle minimum-active links 1', output)
        self.assertNotIn('minimum-active bandwidth', output)
        self.assertIn('interface Bundle-Ether10\n', output)
        self.assertTrue(any('显式 MAC' in warning for warning in device.warnings))
        self.assertIn('ipv4 address 10.0.0.1 255.255.255.252', output)
        self.assertIn('interface Bundle-Ether10.100', output)
        self.assertIn('encapsulation dot1q 100', output)
        self.assertIn('  interface Bundle-Ether10.100', output)
        self.assertNotIn('HundredGigE', output)
        self.assertNotIn('Bundle-Ether10', device.replacement_map)
        self.assertEqual(sum(link.active for link in context.topology.links), 1)
        self.assertTrue(any(mapping.action == 'preserve' for mapping in device.mappings))
        self.assertTrue(any(event['kind'] == 'bundle-adaptation' for event in context.events))

    def test_junos_binding_and_ae_units_survive_stable_simulation(self):
        context = context_for(Vendor.JUNIPER_JUNOS)
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors, context.errors + context.devices['A'].errors)
        output = context.devices['A'].document.render()
        for text in ['802.3ad ae10;', 'minimum-links 1;', 'ae10 {', 'address 10.0.0.1/30;', 'interface ae10.0;', 'interface ae10.100;']:
            self.assertIn(text, output)
        self.assertNotIn('minimum-bandwidth', output)
        self.assertNotIn('speed 100g', output)
        self.assertEqual(context.devices['A'].document.bundle_members(), {'ge-0/0/0': 'ae10'})

    def test_lacp_is_preserved_and_static_lacp_mismatch_fails(self):
        context = context_for(Vendor.CISCO_IOSXR, filename_override='iosxr_lacp.cfg')
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors, context.errors)
        self.assertIn('bundle id 10 mode active', context.devices['A'].document.render())
        context = context_for(Vendor.CISCO_IOSXR)
        context.devices['B'].document = parse_document(Vendor.CISCO_IOSXR.value, (FIXTURES/'iosxr_lacp.cfg').read_text())
        build_default_pipeline().execute(context)
        self.assertTrue(context.has_errors)
        self.assertTrue(any('协商方式不兼容' in error for error in context.errors))

    def test_member_business_and_conflicting_binding_fail_before_migration(self):
        for filename in ['iosxr_member_business.cfg', 'iosxr_conflicting_binding.cfg']:
            with self.subTest(filename=filename):
                context = context_for(Vendor.CISCO_IOSXR, filename_override=filename)
                build_default_pipeline().execute(context)
                self.assertTrue(context.has_errors)
                self.assertFalse(any(mapping.action == 'map-member' for mapping in context.mappings))

    def test_orphan_business_bundle_is_not_deleted_as_non_nni(self):
        context = context_for(Vendor.CISCO_IOSXR)
        doc = context.devices['A'].document
        for name in ['Hu0/0/0/0', 'Hu0/0/0/1']:
            doc.remove_interface(name)
        build_default_pipeline().execute(context)
        self.assertTrue(context.has_errors)
        self.assertTrue(any('归属不明' in error for error in context.devices['A'].errors))
        self.assertIn('interface Bundle-Ether10', doc.render())

    def test_conflicting_junos_lacp_modes_fail_before_mapping(self):
        context = context_for(Vendor.JUNIPER_JUNOS, filename_override='junos_conflicting_lacp.cfg')
        build_default_pipeline().execute(context)
        self.assertTrue(context.has_errors)
        self.assertFalse(any(mapping.action == 'map-member' for mapping in context.mappings))

    def test_same_physical_port_cannot_be_copied_to_two_targets(self):
        context = context_for(Vendor.CISCO_IOSXR)
        context.topology.links[1].a_interface = context.topology.links[0].a_interface
        build_default_pipeline().execute(context)
        self.assertTrue(context.has_errors)
        self.assertTrue(any('重复占用' in error for error in context.errors))

    def test_cross_peer_bundle_is_rejected_instead_of_copying_business(self):
        for vendor in [Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS]:
            with self.subTest(vendor=vendor):
                context = context_for(vendor, True)
                build_default_pipeline().execute(context)
                self.assertTrue(context.has_errors)
                self.assertTrue(any('MC-LAG' in error for error in context.errors))
                self.assertFalse(any(mapping.action in {'map-member', 'preserve'} for mapping in context.mappings))

if __name__ == '__main__':
    unittest.main()
