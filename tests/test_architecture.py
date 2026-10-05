"""验证重构后可独立测试的规划器与流水线边界。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from config_adaptor.adaptation.models import Link
from config_adaptor.adaptation.nni import plan_nni_components
from config_adaptor.adaptation.pipeline import ConversionPipeline
from config_adaptor.adaptation.uni import allocate_uni_vlans
from config_adaptor.cisco import CiscoDocument
from config_adaptor.common.contracts import VendorConfiguration
from config_adaptor.common.interface import InterfaceKind, InterfaceSpec
from config_adaptor.juniper import JunosDocument


class NniPlanningTest(unittest.TestCase):
    def test_same_bundle_and_peer_are_grouped_without_mutating_links(self):
        links = [
            Link(2, "A", "Gi0", "B", "Gi0"),
            Link(3, "A", "Gi1", "B", "Gi1"),
        ]
        bundles = {
            (2, "A"): "Bundle-Ether10",
            (3, "A"): "Bundle-Ether10",
            (2, "B"): "Bundle-Ether20",
            (3, "B"): "Bundle-Ether20",
        }

        plan = plan_nni_components(links, bundles)

        self.assertEqual(plan.component_rows, {2: (2, 3)})
        self.assertEqual(plan.redundant_rows, {3: 2})
        self.assertFalse(plan.errors)
        self.assertTrue(all(link.active for link in links))

    def test_same_bundle_across_peers_remains_separate_mlag_components(self):
        links = [
            Link(2, "A", "Gi0", "B", "Gi0"),
            Link(3, "A", "Gi1", "C", "Gi0"),
        ]
        bundles = {
            (2, "A"): "Bundle-Ether10",
            (3, "A"): "Bundle-Ether10",
            (2, "B"): None,
            (3, "C"): None,
        }

        plan = plan_nni_components(links, bundles)

        self.assertEqual(plan.component_rows, {2: (2,), 3: (3,)})
        self.assertFalse(plan.redundant_rows)
        self.assertFalse(plan.errors)


class UniVlanAllocationTest(unittest.TestCase):
    def test_duplicate_vlan_keeps_first_and_reassigns_second(self):
        specs = [
            InterfaceSpec("Gi0.100", "Gi0", "100", 100, InterfaceKind.PHYSICAL),
            InterfaceSpec("Gi1.100", "Gi1", "100", 100, InterfaceKind.PHYSICAL),
            InterfaceSpec("Gi2", "Gi2", None, None, InterfaceKind.PHYSICAL),
        ]

        allocated = allocate_uni_vlans(specs)

        self.assertEqual(
            allocated,
            {"Gi0.100": 100, "Gi1.100": 2, "Gi2": 3},
        )


class ConversionPipelineTest(unittest.TestCase):
    def test_pipeline_stops_after_first_error(self):
        calls: list[str] = []

        class Stage:
            def __init__(self, name: str, fail: bool = False):
                self.name = name
                self.fail = fail

            def process(self, context) -> None:
                calls.append(self.name)
                if self.fail:
                    context.has_errors = True

        context = SimpleNamespace(has_errors=False)
        pipeline = ConversionPipeline([Stage("first"), Stage("fail", True), Stage("last")])

        pipeline.execute(context)

        self.assertEqual(calls, ["first", "fail"])


class VendorConfigurationContractTest(unittest.TestCase):
    def test_both_vendor_documents_implement_the_application_port(self):
        self.assertIn(VendorConfiguration, CiscoDocument.__mro__)
        self.assertIn(VendorConfiguration, JunosDocument.__mro__)
        self.assertIsInstance(CiscoDocument("end\n"), VendorConfiguration)
        self.assertIsInstance(JunosDocument(""), VendorConfiguration)


class PackageDependencyTest(unittest.TestCase):
    def test_lower_level_packages_do_not_import_orchestration(self):
        package_root = Path(__file__).parents[1] / "src" / "config_adaptor"
        forbidden = {
            "common": {"adaptation", "cisco", "juniper"},
            "cisco": {"adaptation", "juniper"},
            "juniper": {"adaptation", "cisco"},
        }

        for package, blocked in forbidden.items():
            for source_path in (package_root / package).rglob("*.py"):
                tree = ast.parse(source_path.read_text(encoding="utf-8"))
                imported = {
                    node.module.split(".", 1)[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module
                }
                imported.update(
                    alias.name.removeprefix("config_adaptor.").split(".", 1)[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                )
                self.assertFalse(
                    imported & blocked,
                    f"{source_path} 违反依赖方向: {sorted(imported & blocked)}",
                )


if __name__ == "__main__":
    unittest.main()
