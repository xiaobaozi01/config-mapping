"""验证目标镜像模拟参数的适配范围与幂等性。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config_adaptor.adaptation.models import Vendor
from config_adaptor.adaptation.profiles import load_profiles
from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.common.policies import SimulationAdaptationPolicy
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def simulation_config(name: str) -> str:
    """读取模拟参数适配测试配置。"""
    return (FIXTURES / "simulation_adaptation" / name).read_text(encoding="utf-8")


class SimulationAdaptationTest(unittest.TestCase):
    def test_iosxr_simulation_adaptation_is_scoped_and_idempotent(self):
        """只调整映射目标口，并放宽已存在的激进 BFD 参数。"""
        policy = SimulationAdaptationPolicy()
        document = CiscoDocument(simulation_config("iosxr.cfg"))
        first = document.adapt_to_simulation(policy, {"GigabitEthernet0/0/0/0"})
        rendered = document.render()

        self.assertNotIn("carrier-delay", rendered)
        self.assertEqual(rendered.count("speed 10000"), 1)
        self.assertEqual(rendered.count("\n shutdown"), 1)
        self.assertEqual(rendered.count("no shutdown"), 1)
        self.assertNotIn("bfd minimum-interval 50", rendered)
        self.assertNotIn("bfd minimum-interval 100", rendered)
        self.assertEqual(rendered.count("bfd minimum-interval 300"), 2)
        self.assertNotIn("bfd multiplier 1", rendered)
        self.assertNotIn("bfd multiplier 2", rendered)
        self.assertEqual(rendered.count("bfd multiplier 3"), 2)
        self.assertGreater(first.total, 0)

        second = document.adapt_to_simulation(policy, {"GigabitEthernet0/0/0/0"})
        self.assertEqual(second.total, 0)

    def test_junos_simulation_adaptation_is_scoped_and_idempotent(self):
        """Junos 目标口清除物理属性，非目标口保持不变。"""
        policy = SimulationAdaptationPolicy()
        document = JunosDocument(simulation_config("junos.cfg"))
        first = document.adapt_to_simulation(policy, {"ge-0/0/0"})
        rendered = document.render()

        self.assertNotIn("gigether-options", rendered)
        self.assertEqual(rendered.count("speed 10g;"), 1)
        self.assertEqual(rendered.count("disable;"), 1)
        self.assertIn("minimum-interval 300;", rendered)
        self.assertIn("minimum-receive-interval 300;", rendered)
        self.assertIn("multiplier 3;", rendered)
        self.assertGreater(first.total, 0)

        second = document.adapt_to_simulation(policy, {"ge-0/0/0"})
        self.assertEqual(second.total, 0)

    def test_compatible_simulation_adaptation_preserves_bfd_values(self):
        """compatible 模式只做接口兼容，不改协议稳定性参数。"""
        policy = SimulationAdaptationPolicy(mode="compatible")
        document = CiscoDocument(simulation_config("iosxr.cfg"))
        document.adapt_to_simulation(policy, {"GigabitEthernet0/0/0/0"})
        rendered = document.render()
        self.assertIn("bfd minimum-interval 50", rendered)
        self.assertIn("bfd multiplier 2", rendered)

    def test_simulation_adaptation_profile_and_off_mode(self):
        """示例 Profile 可加载，off 模式严格保持文档不变。"""
        profiles = load_profiles(PROJECT_ROOT / "config" / "image_profiles.yaml")
        cisco_profile = profiles[Vendor.CISCO_IOSXR]
        self.assertEqual(cisco_profile.image, "xrv9000")
        self.assertEqual(cisco_profile.simulation_adaptation.mode, "stable")
        self.assertEqual(cisco_profile.simulation_adaptation.bfd_minimum_interval_ms, 300)
        self.assertIs(cisco_profile.param_adjustment, cisco_profile.simulation_adaptation)

        source = simulation_config("iosxr.cfg")
        document = CiscoDocument(source)
        outcome = document.adapt_to_simulation(
            SimulationAdaptationPolicy(mode="off"),
            {"GigabitEthernet0/0/0/0"},
        )
        self.assertEqual(outcome.total, 0)
        self.assertEqual(document.render(), source)

        with tempfile.TemporaryDirectory() as directory:
            legacy_profile = Path(directory) / "legacy-profile.yaml"
            legacy_profile.write_text(
                "profiles:\n"
                "  cisco_iosxr:\n"
                "    param_adjustment:\n"
                "      mode: off\n",
                encoding="utf-8",
            )
            legacy = load_profiles(legacy_profile)
            self.assertEqual(legacy[Vendor.CISCO_IOSXR].simulation_adaptation.mode, "off")


if __name__ == "__main__":
    unittest.main()
