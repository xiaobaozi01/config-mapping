"""验证重构后可独立测试的规划器与流水线边界。"""

from __future__ import annotations

import ast
import importlib.util
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from adaptation.models import Link
from adaptation.nni import plan_nni_components
from adaptation.pipeline import ConversionPipeline, build_default_pipeline
from impl.cisco import CiscoDocument
from impl.common.domain.contracts import VendorConfiguration
from impl.juniper import JunosDocument


class NniPlanningTest(unittest.TestCase):
    def test_same_bundle_and_peer_are_grouped_without_mutating_links(self):
        links = [
            Link(2, "link-2", "A", "Gi0", "B", "Gi0"),
            Link(3, "link-3", "A", "Gi1", "B", "Gi1"),
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
        self.assertFalse(plan.warnings)
        self.assertTrue(all(link.active for link in links))

    def test_same_bundle_across_peers_remains_separate_mlag_components(self):
        links = [
            Link(2, "link-2", "A", "Gi0", "B", "Gi0"),
            Link(3, "link-3", "A", "Gi1", "C", "Gi0"),
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
        self.assertFalse(plan.warnings)

    def test_unbundled_peer_link_does_not_expand_confirmed_bundle_subgroup(self):
        links = [
            Link(2, "link-2", "A", "Gi0", "B", "Gi0"),
            Link(3, "link-3", "A", "Gi1", "B", "Gi1"),
            Link(4, "link-4", "A", "Gi2", "B", "Gi2"),
        ]
        bundles = {
            (2, "A"): "Bundle-Ether10",
            (3, "A"): "Bundle-Ether10",
            (4, "A"): "Bundle-Ether10",
            (2, "B"): "Bundle-Ether20",
            (3, "B"): "Bundle-Ether20",
            (4, "B"): None,
        }

        plan = plan_nni_components(links, bundles)

        self.assertEqual(plan.component_rows, {2: (2, 3), 4: (4,)})
        self.assertEqual(plan.redundant_rows, {3: 2})
        self.assertFalse(plan.warnings)

    def test_local_bundle_connected_to_multiple_peer_bundles_is_rejected(self):
        links = [
            Link(2, "link-2", "A", "Gi0", "B", "Gi0"),
            Link(3, "link-3", "A", "Gi1", "B", "Gi1"),
        ]
        bundles = {
            (2, "A"): "Bundle-Ether10",
            (3, "A"): "Bundle-Ether10",
            (2, "B"): "Bundle-Ether20",
            (3, "B"): "Bundle-Ether30",
        }

        plan = plan_nni_components(links, bundles)

        self.assertEqual(plan.component_rows, {2: (2,), 3: (3,)})
        self.assertFalse(plan.redundant_rows)
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("Bundle-Ether10", plan.warnings[0])
        self.assertIn("Bundle-Ether20", plan.warnings[0])
        self.assertIn("Bundle-Ether30", plan.warnings[0])

    def test_uncompressible_member_remains_a_single_link_component(self):
        links = [
            Link(2, "link-2", "A", "Gi0", "B", "Gi0"),
            Link(3, "link-3", "A", "Gi1", "B", "Gi1"),
            Link(4, "link-4", "A", "Gi2", "B", "Gi2"),
        ]
        bundles = {
            (row, device): bundle
            for row in (2, 3, 4)
            for device, bundle in (
                ("A", "Bundle-Ether10"),
                ("B", "Bundle-Ether20"),
            )
        }

        plan = plan_nni_components(
            links,
            bundles,
            uncompressible_rows=frozenset({4}),
        )

        self.assertEqual(plan.component_rows, {2: (2, 3), 4: (4,)})
        self.assertEqual(plan.redundant_rows, {3: 2})


class ConversionPipelineTest(unittest.TestCase):
    def test_default_pipeline_cleans_before_mapping_and_validates_last(self):
        self.assertEqual(
            [type(stage).__name__ for stage in build_default_pipeline().stages],
            [
                "CleaningRulesHandler", "TopologyPreflightHandler", "GroupExpansionHandler",
                "PreconfiguredInterfaceCleanupHandler", "SourceSemanticValidationHandler",
                "InterfaceClassificationHandler",
                "NonNNICleanupHandler", "NNIHandler", "ReferenceRewriteHandler",
                "CleaningRulesHandler", "InterfaceCompatibilityHandler",
                "SimulationAdaptationHandler", "FinalInterfaceValidationHandler",
            ],
        )

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
    def test_vendor_implementations_live_under_impl(self):
        source_root = Path(__file__).parents[1] / "src"

        self.assertFalse((source_root / "config_adaptor").exists())
        self.assertIsNone(importlib.util.find_spec("config_adaptor"))
        for package in ("adaptation", "impl"):
            self.assertIsNotNone(importlib.util.find_spec(package))
        for package in ("cisco", "common", "juniper"):
            self.assertFalse((source_root / package).exists())
            self.assertIsNotNone(importlib.util.find_spec(f"impl.{package}"))

    def test_lower_level_packages_do_not_import_orchestration(self):
        package_root = Path(__file__).parents[1] / "src"
        forbidden = {
            "common": {"adaptation", "cisco", "juniper"},
            "cisco": {"adaptation", "juniper"},
            "juniper": {"adaptation", "cisco"},
        }

        for package, blocked in forbidden.items():
            for source_path in (package_root / "impl" / package).rglob("*.py"):
                tree = ast.parse(source_path.read_text(encoding="utf-8"))
                imported = {
                    node.module.removeprefix("impl.").split(".", 1)[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module
                }
                imported.update(
                    alias.name.removeprefix("impl.").split(".", 1)[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                )
                self.assertFalse(
                    imported & blocked,
                    f"{source_path} 违反依赖方向: {sorted(imported & blocked)}",
                )

    def test_cisco_rewrite_helpers_have_a_non_rule_owner(self):
        from impl.cisco.syntax import rewrite_helpers
        from impl.cisco.rules import support

        for name in ("append_top_level", "delete_matching"):
            with self.subTest(name=name):
                helper = getattr(rewrite_helpers, name)
                self.assertEqual(helper.__module__, rewrite_helpers.__name__)
                self.assertIs(getattr(support, name), helper)


class ProjectConfigLayoutTest(unittest.TestCase):
    def test_vendor_rule_yaml_lives_under_project_config(self):
        project_root = Path(__file__).parents[1]
        locations = (
            ("cisco", "xrv9000"),
            ("juniper", "vmx"),
        )
        for vendor, image in locations:
            with self.subTest(vendor=vendor):
                target = project_root / "config" / "rules" / vendor / image
                self.assertTrue((target / "manifest.yaml").is_file())
                self.assertFalse((project_root / "src" / "impl" / vendor / "rules" / image).exists())

        from impl.common.support.config_paths import config_path

        for vendor, image in locations:
            with self.subTest(vendor=vendor, helper=True):
                self.assertEqual(
                    config_path("rules", vendor, image),
                    project_root / "config" / "rules" / vendor / image,
                )


class InterfaceModuleLayoutTest(unittest.TestCase):
    def test_interface_handlers_are_owned_by_interfaces_module(self):
        from adaptation import interfaces

        for name in (
            "NonNNICleanupHandler",
            "InterfaceCompatibilityHandler",
            "FinalInterfaceValidationHandler",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(interfaces, name), name)
                if hasattr(interfaces, name):
                    self.assertEqual(getattr(interfaces, name).__module__, interfaces.__name__)
        self.assertIsNone(importlib.util.find_spec("adaptation.core_interfaces"))

    def test_interface_outcome_reporting_is_owned_by_interfaces_module(self):
        from adaptation import interfaces

        for name in ("_report", "_report_diagnostics"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(interfaces, name), name)
                if hasattr(interfaces, name):
                    self.assertEqual(
                        getattr(interfaces, name).__module__,
                        interfaces.__name__,
                    )


class CleaningTargetModuleLayoutTest(unittest.TestCase):
    def test_cleaning_target_helpers_are_owned_by_cleaning_rules(self):
        from adaptation import cleaning_rules

        self.assertTrue(hasattr(cleaning_rules, "target_parents"))
        self.assertEqual(
            cleaning_rules.target_parents.__module__,
            cleaning_rules.__name__,
        )
        self.assertFalse(hasattr(cleaning_rules, "CLEANING_SCOPES"))
        self.assertFalse(hasattr(cleaning_rules, "resolve_cleaning_scope"))
        self.assertIsNone(importlib.util.find_spec("adaptation.cleaning_scopes"))


class NoCliLayoutTest(unittest.TestCase):
    def test_adaptation_has_no_cli_module_or_console_script(self):
        project_root = Path(__file__).parents[1]
        self.assertFalse((project_root / "src" / "adaptation" / "cli.py").exists())
        self.assertFalse((project_root / "src" / "adaptation" / "__main__.py").exists())
        payload = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("config-adaptor", payload.get("project", {}).get("scripts", {}))


class ImplementationPackageLayoutTest(unittest.TestCase):
    def test_target_packages_are_importable(self):
        modules = (
            "impl.common.domain.contracts",
            "impl.common.domain.interface",
            "impl.common.domain.outcomes",
            "impl.common.domain.policies",
            "impl.common.rules.engine",
            "impl.common.rules.cleaning",
            "impl.common.rules.empty_blocks",
            "impl.common.support.config_paths",
            "impl.common.support.errors",
            "impl.common.support.interface_cache",
            "impl.common.support.lab_account",
            "impl.cisco.syntax.ast",
            "impl.cisco.syntax.parser",
            "impl.cisco.syntax.writer",
            "impl.cisco.syntax.semantics",
            "impl.cisco.syntax.rewrite_helpers",
            "impl.cisco.operations.groups",
            "impl.cisco.operations.interfaces",
            "impl.cisco.operations.bundles",
            "impl.cisco.operations.cleaning",
            "impl.cisco.operations.configuration_cleaning",
            "impl.cisco.operations.references",
            "impl.cisco.support.clone_constraints",
            "impl.cisco.support.empty_blocks",
            "impl.cisco.support.identity",
            "impl.juniper.syntax.ast",
            "impl.juniper.syntax.parser",
            "impl.juniper.syntax.writer",
            "impl.juniper.operations.groups",
            "impl.juniper.operations.interfaces",
            "impl.juniper.operations.bundles",
            "impl.juniper.operations.cleaning",
            "impl.juniper.operations.configuration_cleaning",
            "impl.juniper.operations.references",
            "impl.juniper.support.clone_constraints",
            "impl.juniper.support.empty_blocks",
            "impl.juniper.support.identity",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_old_flat_modules_are_removed(self):
        modules = (
            "impl.common.contracts",
            "impl.common.interface",
            "impl.common.outcomes",
            "impl.common.policies",
            "impl.common.cleaning",
            "impl.common.empty_blocks",
            "impl.common.config_paths",
            "impl.common.errors",
            "impl.common.interface_cache",
            "impl.common.lab_account",
            "impl.cisco.ast",
            "impl.cisco.parser",
            "impl.cisco.writer",
            "impl.cisco.semantics",
            "impl.cisco.rewrite_helpers",
            "impl.cisco.groups",
            "impl.cisco.interfaces",
            "impl.cisco.bundles",
            "impl.cisco.cleaning",
            "impl.cisco.configuration_cleaning",
            "impl.cisco.references",
            "impl.cisco.clone_constraints",
            "impl.cisco.empty_blocks",
            "impl.cisco.identity",
            "impl.juniper.ast",
            "impl.juniper.parser",
            "impl.juniper.writer",
            "impl.juniper.groups",
            "impl.juniper.interfaces",
            "impl.juniper.bundles",
            "impl.juniper.cleaning",
            "impl.juniper.configuration_cleaning",
            "impl.juniper.references",
            "impl.juniper.clone_constraints",
            "impl.juniper.empty_blocks",
            "impl.juniper.identity",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec(module))


if __name__ == "__main__":
    unittest.main()
