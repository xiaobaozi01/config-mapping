"""镜像端口表不能把逻辑口、管理口或未知值当成 GNS3 物理端口。"""

from pathlib import Path
import tempfile
import unittest

import yaml

from config_adaptor.adaptation.models import Vendor
from config_adaptor.adaptation.profiles import load_profiles, normalize_data_interfaces


class ProfileDataInterfaceTest(unittest.TestCase):
    def test_physical_interface_aliases_and_supported_families_are_preserved(self):
        cisco = normalize_data_interfaces(Vendor.CISCO_IOSXR, [
            "Gi 0/0/0/0", "TenGigE0/0/0/1", "HundredGigE0/0/0/2", "Serial0/0",
        ])
        self.assertEqual(cisco, [
            "GigabitEthernet0/0/0/0", "TenGigE0/0/0/1", "HundredGigE0/0/0/2", "Serial0/0",
        ])
        junos = normalize_data_interfaces(Vendor.JUNIPER_JUNOS, [
            " ge-0/0/0 ", "xe-0/0/1", "et-0/0/2:0", "t1-0/0/3",
        ])
        self.assertEqual(junos, ["ge-0/0/0", "xe-0/0/1", "et-0/0/2:0", "t1-0/0/3"])

    def test_all_positions_require_physical_parent_including_reserved_ports(self):
        cases = {
            Vendor.CISCO_IOSXR: [
                "Gi0/0/0/0.100", "Bundle-Ether10", "BVI100", "Loopback0",
                "MgmtEth0/RP0/CPU0/0", "Tunnel-ip10", "NNI", "GigabitEthernet0/0/0/0oops",
            ],
            Vendor.JUNIPER_JUNOS: [
                "ge-0/0/0.100", "ae10", "irb.100", "lo0", "fxp0", "gr-0/0/0",
                "NNI", "ge-0/0/0oops",
            ],
        }
        for vendor, invalid_names in cases.items():
            defaults = load_profiles(None)[vendor].interfaces[:4]
            for position in range(4):
                for invalid in invalid_names:
                    with self.subTest(vendor=vendor, position=position, invalid=invalid):
                        interfaces = list(defaults)
                        interfaces[position] = invalid
                        with tempfile.TemporaryDirectory() as directory:
                            path = Path(directory) / "profiles.yaml"
                            path.write_text(yaml.safe_dump({"profiles": {vendor.value: {"interfaces": interfaces}}}))
                            with self.assertRaisesRegex(ValueError, "物理父口"):
                                load_profiles(path)


if __name__ == "__main__":
    unittest.main()
