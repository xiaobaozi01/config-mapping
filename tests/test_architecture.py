"""验证重构后可独立测试的规划器与流水线边界。"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from gns3_config_adaptor.adaptation.models import Link
from gns3_config_adaptor.adaptation.nni import plan_nni_components
from gns3_config_adaptor.adaptation.pipeline import (
    ConversionPipeline,
    build_default_pipeline,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    bundles as cisco_bundles,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    cleaning as cisco_cleaning,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    compatibility as cisco_compatibility,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    groups as cisco_groups,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    interfaces as cisco_interfaces,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    pruning as cisco_pruning,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    references as cisco_references,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    simulation as cisco_simulation,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
    validation as cisco_validation,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
    compatibility as cisco_rule_compatibility,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
    pruning as cisco_rule_pruning,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
    simulation as cisco_rule_simulation,
)
from gns3_config_adaptor.impl.common.domain import capabilities
from gns3_config_adaptor.impl.common.domain.contracts import VendorConfiguration
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
    JunosDocument,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
    rules as junos_rules,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    bundles as junos_bundles,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    cleaning as junos_cleaning,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    compatibility as junos_compatibility,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    groups as junos_groups,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    interfaces as junos_interfaces,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    pruning as junos_pruning,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    references as junos_references,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    simulation as junos_simulation,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
    validation as junos_validation,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.rules import (
    pruning as junos_rule_pruning,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.rules import (
    simulation as junos_rule_simulation,
)

CISCO_HANDLER_PACKAGE = "cisco_iosxrv9k_7_11_1_config_mapping_handler"
JUNIPER_HANDLER_PACKAGE = (
    "juniper_vjunos_router_23_2R1_15_config_mapping_handler"
)
VENDOR_HANDLER_PACKAGES = (CISCO_HANDLER_PACKAGE, JUNIPER_HANDLER_PACKAGE)
VENDOR_HANDLER_BY_VENDOR = {
    "cisco_iosxr": CISCO_HANDLER_PACKAGE,
    "juniper_junos": JUNIPER_HANDLER_PACKAGE,
}


def _first_imported_package(parts: list[str]) -> str:
    """返回导入目标在统一根包下的首个业务包名。"""
    if parts[:1] == ["gns3_config_adaptor"] and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else ""


IMPLEMENTATION_PACKAGE_MODULES = (
    "gns3_config_adaptor.impl.common.domain.contracts",
    "gns3_config_adaptor.impl.common.domain.interface",
    "gns3_config_adaptor.impl.common.domain.outcomes",
    "gns3_config_adaptor.impl.common.domain.policies",
    "gns3_config_adaptor.impl.common.rules.engine",
    "gns3_config_adaptor.impl.common.rules.cleaning",
    "gns3_config_adaptor.impl.common.rules.empty_blocks",
    "gns3_config_adaptor.impl.common.support.config_paths",
    "gns3_config_adaptor.impl.common.support.errors",
    "gns3_config_adaptor.impl.common.support.interface_cache",
    "gns3_config_adaptor.impl.common.support.lab_account",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.ast",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.parser",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.writer",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.semantics",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.rewrite_helpers",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.bundles",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.cleaning",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.declarative_cleaning",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.management_cleaning",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.compatibility",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.pruning",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.references",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.simulation",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.validation",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.support.empty_blocks",
    "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.support.identity",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.ast",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.parser",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.writer",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.bundles",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.cleaning",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.declarative_cleaning",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.management_cleaning",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.compatibility",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.pruning",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.references",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.simulation",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.validation",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.support.empty_blocks",
    "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.support.identity",
)

CAPABILITY_IMPLEMENTATION_PAIRS = (
    (
        capabilities.InterfaceAnalysis,
        cisco_interfaces.CiscoInterfaceAnalysis,
    ),
    (
        capabilities.InterfaceAnalysis,
        junos_interfaces.JunosInterfaceAnalysis,
    ),
    (
        capabilities.InterfaceTransform,
        cisco_interfaces.CiscoInterfaceTransform,
    ),
    (
        capabilities.InterfaceTransform,
        junos_interfaces.JunosInterfaceTransform,
    ),
    (
        capabilities.BundleOperations,
        cisco_bundles.CiscoBundleOperations,
    ),
    (
        capabilities.BundleOperations,
        junos_bundles.JunosBundleOperations,
    ),
    (
        capabilities.GroupOperations,
        cisco_groups.CiscoGroupOperations,
    ),
    (
        capabilities.GroupOperations,
        junos_groups.JunosGroupOperations,
    ),
    (
        capabilities.CleaningOperations,
        cisco_cleaning.CiscoCleaningOperations,
    ),
    (
        capabilities.CleaningOperations,
        junos_cleaning.JunosCleaningOperations,
    ),
    (
        capabilities.PruningOperations,
        cisco_pruning.CiscoPruningOperations,
    ),
    (
        capabilities.PruningOperations,
        junos_pruning.JunosPruningOperations,
    ),
    (
        capabilities.CompatibilityOperations,
        cisco_compatibility.CiscoCompatibilityOperations,
    ),
    (
        capabilities.CompatibilityOperations,
        junos_compatibility.JunosCompatibilityOperations,
    ),
    (
        capabilities.ValidationOperations,
        cisco_validation.CiscoValidationOperations,
    ),
    (
        capabilities.ValidationOperations,
        junos_validation.JunosValidationOperations,
    ),
    (
        capabilities.ReferenceOperations,
        cisco_references.CiscoReferenceOperations,
    ),
    (
        capabilities.ReferenceOperations,
        junos_references.JunosReferenceOperations,
    ),
    (
        capabilities.SimulationOperations,
        cisco_simulation.CiscoSimulationOperations,
    ),
    (
        capabilities.SimulationOperations,
        junos_simulation.JunosSimulationOperations,
    ),
)


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

    def test_unbundled_peer_link_does_not_expand_confirmed_bundle_subgroup(
        self,
    ):
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
                "CleaningRulesHandler",
                "TopologyPreflightHandler",
                "GroupExpansionHandler",
                "PreconfiguredInterfaceCleanupHandler",
                "InterfaceClassificationHandler",
                "NonNNICleanupHandler",
                "NNIHandler",
                "ReferenceRewriteHandler",
                "CleaningRulesHandler",
                "InterfaceCompatibilityHandler",
                "SimulationAdaptationHandler",
                "FinalInterfaceValidationHandler",
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
        pipeline = ConversionPipeline(
            [Stage("first"), Stage("fail", True), Stage("last")]
        )

        pipeline.execute(context)

        self.assertEqual(calls, ["first", "fail"])


class VendorConfigurationContractTest(unittest.TestCase):

    def test_both_vendor_documents_implement_the_application_port(self):
        self.assertIn(VendorConfiguration, CiscoDocument.__mro__)
        self.assertIn(VendorConfiguration, JunosDocument.__mro__)
        self.assertIsInstance(CiscoDocument("end\n"), VendorConfiguration)
        self.assertIsInstance(JunosDocument(""), VendorConfiguration)


class UnifiedPackageRootTest(unittest.TestCase):

    def test_source_packages_live_under_one_importable_root(self):
        source_root = Path(__file__).parents[1] / "src"
        root_package = source_root / "gns3_config_adaptor"

        self.assertTrue(root_package.is_dir())
        self.assertTrue((root_package / "adaptation").is_dir())
        self.assertTrue((root_package / "impl").is_dir())
        self.assertFalse((source_root / "adaptation").exists())
        self.assertFalse((source_root / "impl").exists())
        self.assertIsNone(importlib.util.find_spec("adaptation"))
        self.assertIsNone(importlib.util.find_spec("impl"))

        module = importlib.import_module("gns3_config_adaptor.adaptation")
        self.assertTrue(callable(module.convert))

    def test_source_internal_imports_are_relative(self):
        source_root = Path(__file__).parents[1] / "src"
        internal_roots = {
            "adaptation",
            "gns3_config_adaptor",
            "impl",
        }
        violations = []

        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    root = (node.module or "").split(".", 1)[0]
                    if root in internal_roots:
                        violations.append(
                            f"{source_path}:{node.lineno}: from {node.module}"
                        )
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        if root in internal_roots:
                            violations.append(
                                f"{source_path}:{node.lineno}: import "
                                f"{alias.name}"
                            )

        self.assertEqual(violations, [])

    def test_vendor_handler_packages_use_mapping_specific_names(self):
        impl = (
            Path(__file__).parents[1] / "src" / "gns3_config_adaptor" / "impl"
        )
        self.assertTrue((impl / CISCO_HANDLER_PACKAGE).is_dir())
        self.assertTrue((impl / JUNIPER_HANDLER_PACKAGE).is_dir())
        self.assertFalse((impl / "cisco").exists())
        self.assertFalse((impl / "juniper").exists())
        self.assertIsNone(
            importlib.util.find_spec("gns3_config_adaptor.impl.cisco")
        )
        self.assertIsNone(
            importlib.util.find_spec("gns3_config_adaptor.impl.juniper")
        )
        cisco_module = importlib.import_module(
            f"gns3_config_adaptor.impl.{CISCO_HANDLER_PACKAGE}"
        )
        juniper_module = importlib.import_module(
            f"gns3_config_adaptor.impl.{JUNIPER_HANDLER_PACKAGE}"
        )
        self.assertTrue(hasattr(cisco_module, "CiscoDocument"))
        self.assertTrue(hasattr(juniper_module, "JunosDocument"))

    def test_support_does_not_export_generic_config_path(self):
        from gns3_config_adaptor.impl.common import support

        self.assertFalse(hasattr(support, "config_path"))


class PackageDependencyTest(unittest.TestCase):

    def test_vendor_implementations_live_under_unified_root(self):
        source_root = Path(__file__).parents[1] / "src"

        self.assertFalse((source_root / "config_adaptor").exists())
        self.assertIsNone(importlib.util.find_spec("config_adaptor"))
        self.assertIsNone(importlib.util.find_spec("adaptation"))
        self.assertIsNone(importlib.util.find_spec("impl"))
        self.assertIsNotNone(importlib.util.find_spec("gns3_config_adaptor"))
        for package in ("adaptation", "impl"):
            self.assertIsNotNone(
                importlib.util.find_spec(f"gns3_config_adaptor.{package}")
            )
        for package in (*VENDOR_HANDLER_PACKAGES, "common"):
            self.assertFalse((source_root / package).exists())
            self.assertIsNotNone(
                importlib.util.find_spec(f"gns3_config_adaptor.impl.{package}")
            )

    def test_lower_level_packages_do_not_import_orchestration(self):
        source_root = Path(__file__).parents[1] / "src"
        package_root = source_root / "gns3_config_adaptor"
        forbidden = {
            "common": {"adaptation", *VENDOR_HANDLER_PACKAGES},
            CISCO_HANDLER_PACKAGE: {"adaptation", JUNIPER_HANDLER_PACKAGE},
            JUNIPER_HANDLER_PACKAGE: {"adaptation", CISCO_HANDLER_PACKAGE},
        }

        for package, blocked in forbidden.items():
            for source_path in (package_root / "impl" / package).rglob("*.py"):
                tree = ast.parse(source_path.read_text(encoding="utf-8"))
                current_package = list(
                    source_path.relative_to(source_root)
                    .with_suffix("")
                    .parts[:-1]
                )
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if node.level:
                            end = len(current_package) - (node.level - 1)
                            base = current_package[:end]
                            target = [
                                *base,
                                *((node.module or "").split(".")),
                            ]
                        else:
                            target = (node.module or "").split(".")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            target = alias.name.split(".")
                            imported.add(_first_imported_package(target))
                        continue
                    else:
                        continue
                    imported.add(_first_imported_package(target))
                self.assertFalse(
                    imported & blocked,
                    f"{source_path} 违反依赖方向: {sorted(imported & blocked)}",
                )

    def test_cisco_rewrite_helpers_have_a_non_rule_owner(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules import (
            support,
        )
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax import (
            rewrite_helpers,
        )

        for name in ("append_top_level", "delete_matching"):
            with self.subTest(name=name):
                helper = getattr(rewrite_helpers, name)
                self.assertEqual(helper.__module__, rewrite_helpers.__name__)
                self.assertIs(getattr(support, name), helper)


class ProjectConfigLayoutTest(unittest.TestCase):

    def test_vendor_rule_yaml_lives_under_project_config(self):
        project_root = Path(__file__).parents[1]
        locations = (
            ("cisco_iosxr", "xrv9000"),
            ("juniper_junos", "vmx"),
        )
        self.assertFalse((project_root / "config" / "rules" / "cisco").exists())
        self.assertFalse(
            (project_root / "config" / "rules" / "juniper").exists()
        )
        for vendor, image in locations:
            with self.subTest(vendor=vendor):
                target = project_root / "config" / "rules" / vendor / image
                self.assertTrue((target / "manifest.yaml").is_file())
                self.assertFalse(
                    (
                        project_root
                        / "src"
                        / "gns3_config_adaptor"
                        / "impl"
                        / VENDOR_HANDLER_BY_VENDOR[vendor]
                        / "rules"
                        / image
                    ).exists()
                )

        from gns3_config_adaptor.impl.common.support.config_paths import (
            rule_pack_dir,
        )

        for vendor, image in locations:
            with self.subTest(vendor=vendor, helper=True):
                self.assertEqual(
                    rule_pack_dir(vendor_dir=vendor, image=image),
                    project_root / "config" / "rules" / vendor / image,
                )

    def test_rule_yaml_paths_are_owned_by_config_paths(self):
        source_root = Path(__file__).parents[1] / "src"
        owner = (
            source_root
            / "gns3_config_adaptor"
            / "impl"
            / "common"
            / "support"
            / "config_paths.py"
        )
        fixed_rule_files = {
            "manifest.yaml",
            "cleaning.yaml",
            "input_normalization.yaml",
            "empty_blocks.yaml",
        }
        violations = []

        for source_path in source_root.rglob("*.py"):
            if source_path == owner:
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and node.value in fixed_rule_files
                ):
                    violations.append(
                        f"{source_path}: 固定规则文件名 {node.value}"
                    )

        self.assertEqual(violations, [])


class InterfacePackageLayoutTest(unittest.TestCase):

    def test_interface_handlers_are_owned_by_interfaces_module(self):
        from gns3_config_adaptor.adaptation import interfaces

        for name in (
            "NonNNICleanupHandler",
            "InterfaceCompatibilityHandler",
            "FinalInterfaceValidationHandler",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(interfaces, name), name)
                if hasattr(interfaces, name):
                    self.assertEqual(
                        getattr(interfaces, name).__module__,
                        interfaces.__name__,
                    )
        self.assertIsNone(
            importlib.util.find_spec(
                "gns3_config_adaptor.adaptation.core_interfaces"
            )
        )

    def test_interface_outcome_reporting_is_owned_by_interfaces_module(self):
        from gns3_config_adaptor.adaptation import interfaces

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
        from gns3_config_adaptor.adaptation import cleaning_rules

        self.assertTrue(hasattr(cleaning_rules, "target_parents"))
        self.assertEqual(
            cleaning_rules.target_parents.__module__,
            cleaning_rules.__name__,
        )
        self.assertFalse(hasattr(cleaning_rules, "CLEANING_SCOPES"))
        self.assertFalse(hasattr(cleaning_rules, "resolve_cleaning_scope"))
        self.assertIsNone(
            importlib.util.find_spec(
                "gns3_config_adaptor.adaptation.cleaning_scopes"
            )
        )


class NoCliLayoutTest(unittest.TestCase):

    def test_adaptation_has_no_cli_module_or_console_script(self):
        project_root = Path(__file__).parents[1]
        self.assertFalse(
            (
                project_root
                / "src"
                / "gns3_config_adaptor"
                / "adaptation"
                / "cli.py"
            ).exists()
        )
        self.assertFalse(
            (
                project_root
                / "src"
                / "gns3_config_adaptor"
                / "adaptation"
                / "__main__.py"
            ).exists()
        )
        payload = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "config-adaptor", payload.get("project", {}).get("scripts", {})
        )


class ImplementationPackageLayoutTest(unittest.TestCase):

    def test_target_packages_are_importable(self):
        for module in IMPLEMENTATION_PACKAGE_MODULES:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_old_flat_modules_are_removed(self):
        modules = (
            "gns3_config_adaptor.impl.common.contracts",
            "gns3_config_adaptor.impl.common.interface",
            "gns3_config_adaptor.impl.common.outcomes",
            "gns3_config_adaptor.impl.common.policies",
            "gns3_config_adaptor.impl.common.cleaning",
            "gns3_config_adaptor.impl.common.empty_blocks",
            "gns3_config_adaptor.impl.common.config_paths",
            "gns3_config_adaptor.impl.common.errors",
            "gns3_config_adaptor.impl.common.interface_cache",
            "gns3_config_adaptor.impl.common.lab_account",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.ast",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.parser",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.writer",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.semantics",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rewrite_helpers",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.groups",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.interfaces",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.bundles",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.cleaning",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.configuration_cleaning",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.references",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.clone_constraints",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.empty_blocks",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.identity",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.ast",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.parser",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.writer",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.groups",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.interfaces",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.bundles",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.cleaning",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.configuration_cleaning",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.references",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.clone_constraints",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.empty_blocks",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.identity",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec(module))


class HeaderParserOwnershipTest(unittest.TestCase):

    def test_header_parsers_are_owned_by_syntax_modules(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
            CiscoDocument,
        )
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.header_parser import (
            _CiscoHeaderParser,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
            JunosDocument,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.header_parser import (
            _JunosHeaderParser,
        )

        self.assertEqual(
            _CiscoHeaderParser.__module__,
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.header_parser",
        )
        self.assertEqual(
            _JunosHeaderParser.__module__,
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.header_parser",
        )
        self.assertIsInstance(CiscoDocument("")._parser, _CiscoHeaderParser)
        self.assertIsInstance(JunosDocument("")._parser, _JunosHeaderParser)

    def test_header_parser_classes_are_not_defined_in_document(self):
        project_root = Path(__file__).parents[1]
        blocked = {
            CISCO_HANDLER_PACKAGE: "_CiscoHeaderParser",
            JUNIPER_HANDLER_PACKAGE: "_JunosHeaderParser",
        }
        for vendor, class_name in blocked.items():
            path = (
                project_root
                / "src"
                / "gns3_config_adaptor"
                / "impl"
                / vendor
                / "document.py"
            )
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            }
            with self.subTest(vendor=vendor):
                self.assertNotIn(class_name, names)


class CapabilityProtocolLayoutTest(unittest.TestCase):

    def test_nni_clone_validation_capability_is_removed(self):
        from gns3_config_adaptor.impl.common.domain.capabilities import (
            ValidationOperations,
        )

        self.assertFalse(hasattr(ValidationOperations, "validate_nni_clone"))
        self.assertFalse(
            hasattr(
                cisco_validation.CiscoValidationOperations,
                "validate_nni_clone",
            )
        )
        self.assertFalse(
            hasattr(
                junos_validation.JunosValidationOperations,
                "validate_nni_clone",
            )
        )
        for package in VENDOR_HANDLER_PACKAGES:
            self.assertIsNone(
                importlib.util.find_spec(
                    f"gns3_config_adaptor.impl.{package}.support."
                    "clone_constraints"
                )
            )

    def test_validate_interfaces_is_not_exposed_by_validation_facade(self):
        from gns3_config_adaptor.impl.common.domain.capabilities import (
            ValidationOperations,
        )

        self.assertFalse(hasattr(ValidationOperations, "validate_interfaces"))
        self.assertFalse(
            hasattr(
                cisco_validation.CiscoValidationOperations,
                "validate_interfaces",
            )
        )
        self.assertFalse(
            hasattr(
                junos_validation.JunosValidationOperations,
                "validate_interfaces",
            )
        )

    def test_capability_protocols_are_importable(self):
        from gns3_config_adaptor.impl.common.domain import capabilities

        expected = (
            "DocumentCore",
            "VendorDocument",
            "InterfaceCapabilities",
            "InterfaceAnalysis",
            "InterfaceTransform",
            "BundleOperations",
            "GroupOperations",
            "CleaningOperations",
            "PruningOperations",
            "CompatibilityOperations",
            "ValidationOperations",
            "ReferenceOperations",
            "SimulationOperations",
        )
        self.assertEqual(set(expected), set(capabilities.__all__))

    def test_interface_protocols_do_not_contain_mutation_methods(self):
        from gns3_config_adaptor.impl.common.domain.capabilities import (
            InterfaceAnalysis,
        )

        self.assertFalse(hasattr(InterfaceAnalysis, "rename"))
        self.assertFalse(hasattr(InterfaceAnalysis, "clone"))
        self.assertFalse(hasattr(InterfaceAnalysis, "remove"))

    def test_validation_protocol_has_no_editing_methods(self):
        from gns3_config_adaptor.impl.common.domain.capabilities import (
            ValidationOperations,
        )

        self.assertFalse(hasattr(ValidationOperations, "edit"))
        self.assertFalse(hasattr(ValidationOperations, "apply_cleaning_rules"))


class CleaningModuleLayoutTest(unittest.TestCase):

    def test_cleaning_modules_have_distinct_responsibilities(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
            cleaning as cisco_cleaning,
        )
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
            declarative_cleaning as cisco_declarative,
        )
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations import (
            management_cleaning as cisco_management,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
            cleaning as juniper_cleaning,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
            declarative_cleaning as juniper_declarative,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations import (
            management_cleaning as juniper_management,
        )

        self.assertTrue(hasattr(cisco_cleaning, "CiscoCleaningOperations"))
        self.assertFalse(hasattr(cisco_cleaning, "_MANAGEMENT_TOP_LEVEL"))
        self.assertTrue(hasattr(cisco_management, "clean_management_access"))
        self.assertTrue(hasattr(cisco_declarative, "apply_cleaning_rules"))
        self.assertIsNone(
            importlib.util.find_spec(
                "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.configuration_cleaning"
            )
        )

        self.assertTrue(hasattr(juniper_cleaning, "JunosCleaningOperations"))
        self.assertFalse(hasattr(juniper_cleaning, "_disable_matching"))
        self.assertTrue(hasattr(juniper_management, "clean_management_access"))
        self.assertTrue(hasattr(juniper_declarative, "apply_cleaning_rules"))
        self.assertIsNone(
            importlib.util.find_spec(
                "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.configuration_cleaning"
            )
        )


class CiscoCapabilityObjectTest(unittest.TestCase):

    def test_document_exposes_narrow_capabilities(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
            CiscoDocument,
        )

        document = CiscoDocument("interface Gi0\n!\n")
        self.assertEqual(
            document.interfaces.analysis.resolve_interface("Gi0"),
            "GigabitEthernet0",
        )
        self.assertEqual(
            document.interfaces.analysis.interface_specs()[0].name,
            "GigabitEthernet0",
        )
        self.assertFalse(hasattr(document, "_bundles_cache"))

    def test_capability_access_does_not_create_long_lived_services(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
            CiscoDocument,
        )

        document = CiscoDocument("")
        first = document.interfaces
        second = document.interfaces
        self.assertIsNot(first, second)
        self.assertEqual(document.__dict__.get("_interfaces"), None)


class JuniperCapabilityObjectTest(unittest.TestCase):

    def test_document_exposes_narrow_capabilities(self):
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
            JunosDocument,
        )

        document = JunosDocument("interfaces { ge-0/0/0 {} }")
        self.assertEqual(
            document.interfaces.analysis.resolve_interface("ge-0/0/0"),
            "ge-0/0/0",
        )
        self.assertEqual(
            document.interfaces.analysis.interface_specs()[0].name,
            "ge-0/0/0",
        )

    def test_private_interface_helpers_are_no_longer_document_methods(self):
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
            JunosDocument,
        )

        for name in (
            "_top_block",
            "_interfaces_block",
            "_interface_nodes",
            "_unit_nodes",
            "_find_vlan",
            "_find_inner_vlan",
        ):
            self.assertFalse(hasattr(JunosDocument, name))


class CapabilityImplementationContractTest(unittest.TestCase):

    def test_vendor_capability_objects_match_protocol_methods(self):
        for protocol, implementation in CAPABILITY_IMPLEMENTATION_PAIRS:
            with self.subTest(
                protocol=protocol.__name__,
                implementation=implementation.__name__,
            ):
                required = {
                    name
                    for name, value in inspect.getmembers(protocol)
                    if not name.startswith("_") and callable(value)
                }
                provided = {
                    name
                    for name, value in inspect.getmembers(implementation)
                    if not name.startswith("_") and callable(value)
                }
                self.assertEqual(required - provided, set())

    def test_rule_backed_capability_wrappers_live_in_operations(self):
        pairs = (
            (cisco_pruning, cisco_rule_pruning, "CiscoPruningOperations"),
            (
                cisco_compatibility,
                cisco_rule_compatibility,
                "CiscoCompatibilityOperations",
            ),
            (
                cisco_validation,
                cisco_rule_compatibility,
                "CiscoValidationOperations",
            ),
            (
                cisco_simulation,
                cisco_rule_simulation,
                "CiscoSimulationOperations",
            ),
            (junos_pruning, junos_rule_pruning, "JunosPruningOperations"),
            (
                junos_compatibility,
                junos_rules,
                "JunosCompatibilityOperations",
            ),
            (
                junos_validation,
                junos_rules,
                "JunosValidationOperations",
            ),
            (
                junos_simulation,
                junos_rule_simulation,
                "JunosSimulationOperations",
            ),
        )
        for operations, rules, class_name in pairs:
            with self.subTest(class_name=class_name):
                capability = getattr(operations, class_name)
                self.assertEqual(capability.__module__, operations.__name__)
                self.assertFalse(hasattr(rules, class_name))

    def test_capability_attributes_are_created_on_demand(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
            CiscoDocument,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
            JunosDocument,
        )

        for document in (
            CiscoDocument("interface Gi0\n!\n"),
            JunosDocument("interfaces { ge-0/0/0 {} }"),
        ):
            with self.subTest(vendor=document.vendor):
                self.assertIsNot(document.interfaces, document.interfaces)
                for name in (
                    "_interfaces",
                    "_bundles",
                    "_groups",
                    "_cleaning",
                    "_pruning",
                    "_validation",
                ):
                    self.assertNotIn(name, document.__dict__)


class CapabilityCallerTest(unittest.TestCase):

    def test_adaptation_uses_capabilities_not_legacy_document_methods(self):
        source_root = (
            Path(__file__).parents[1]
            / "src"
            / "gns3_config_adaptor"
            / "adaptation"
        )
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(source_root.glob("*.py"))
        )
        for legacy_method in (
            "interface_specs",
            "prune_non_nni",
            "adapt_interfaces",
            "validate_interfaces",
            "apply_cleaning_rules",
            "adapt_to_simulation",
        ):
            self.assertNotIn(f"document.{legacy_method}(", source)


class DocumentSurfaceTest(unittest.TestCase):

    def test_documents_do_not_define_business_methods(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import (
            CiscoDocument,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler import (
            JunosDocument,
        )

        removed = {
            "expand_groups",
            "interface_specs",
            "interface_kind",
            "bundle_members",
            "business_interface_names",
            "remove_interface",
            "rename_interface_tree",
            "clone_interface_tree",
            "bundle_mode",
            "adapt_bundle",
            "prune_non_nni",
            "adapt_interfaces",
            "validate_source_semantics",
            "validate_final_integrity",
            "validate_interfaces",
            "replace_references",
            "clean_management_access",
            "add_lab_account",
            "adapt_to_simulation",
            "apply_cleaning_rules",
        }
        for document_type in (CiscoDocument, JunosDocument):
            for name in removed:
                with self.subTest(document=document_type.__name__, name=name):
                    self.assertFalse(hasattr(document_type, name))

    def test_vendor_configuration_alias_is_vendor_document(self):
        from gns3_config_adaptor.impl.common import VendorConfiguration
        from gns3_config_adaptor.impl.common.domain.capabilities import (
            VendorDocument,
        )

        self.assertIs(VendorConfiguration, VendorDocument)


class InterfaceModuleLayoutTest(unittest.TestCase):

    def test_interface_packages_have_required_modules(self):
        modules = (
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.access",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.analysis",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.business",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.capabilities",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.transform",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.access",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.analysis",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.business",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.capabilities",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.transform",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_interface_single_files_are_removed(self):
        project_root = Path(__file__).parents[1]
        for vendor in VENDOR_HANDLER_PACKAGES:
            with self.subTest(vendor=vendor):
                path = (
                    project_root
                    / "src"
                    / "gns3_config_adaptor"
                    / "impl"
                    / vendor
                    / "operations"
                    / "interfaces.py"
                )
                self.assertFalse(path.exists())

    def test_interface_submodule_dependencies_point_inward(self):
        project_root = Path(__file__).parents[1]
        forbidden = {
            "access": {"analysis", "business", "transform"},
            "business": {"analysis", "transform"},
            "analysis": {"transform"},
            "transform": {"analysis"},
        }
        for vendor in VENDOR_HANDLER_PACKAGES:
            package = (
                project_root
                / "src"
                / "gns3_config_adaptor"
                / "impl"
                / vendor
                / "operations"
                / "interfaces"
            )
            for module_name, blocked in forbidden.items():
                path = package / f"{module_name}.py"
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imported = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.level == 1
                    for alias in node.names
                }
                with self.subTest(vendor=vendor, module=module_name):
                    self.assertFalse(imported & blocked)

    def test_capability_classes_remain_compatible(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces import (
            CiscoInterfaceAnalysis,
            CiscoInterfaceTransform,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces import (
            JunosInterfaceAnalysis,
            JunosInterfaceTransform,
        )

        self.assertEqual(
            CiscoInterfaceAnalysis.__module__,
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.analysis",
        )
        self.assertEqual(
            CiscoInterfaceTransform.__module__,
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.transform",
        )
        self.assertEqual(
            JunosInterfaceAnalysis.__module__,
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.analysis",
        )
        self.assertEqual(
            JunosInterfaceTransform.__module__,
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.interfaces.transform",
        )


class GroupModuleLayoutTest(unittest.TestCase):

    def test_group_packages_have_required_modules(self):
        modules = (
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.models",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.selectors",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.definitions",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.merger",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.expander",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.publishing",
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.capabilities",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.models",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.selectors",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.definitions",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.merger",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.expander",
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.capabilities",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_group_single_files_are_removed(self):
        project_root = Path(__file__).parents[1]
        for vendor in VENDOR_HANDLER_PACKAGES:
            with self.subTest(vendor=vendor):
                path = (
                    project_root
                    / "src"
                    / "gns3_config_adaptor"
                    / "impl"
                    / vendor
                    / "operations"
                    / "groups.py"
                )
                self.assertFalse(path.exists())

    def test_group_submodule_dependencies_point_inward(self):
        project_root = Path(__file__).parents[1]
        forbidden = {
            "models": {
                "selectors",
                "definitions",
                "merger",
                "expander",
                "capabilities",
            },
            "selectors": {
                "definitions",
                "merger",
                "expander",
                "capabilities",
            },
            "definitions": {"merger", "expander", "capabilities"},
            "merger": {"definitions", "expander", "capabilities"},
            "publishing": {"definitions", "merger", "expander", "capabilities"},
        }
        for vendor in VENDOR_HANDLER_PACKAGES:
            package = (
                project_root
                / "src"
                / "gns3_config_adaptor"
                / "impl"
                / vendor
                / "operations"
                / "groups"
            )
            for module_name, blocked in forbidden.items():
                path = package / f"{module_name}.py"
                if not path.exists():
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imported = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.level == 1
                    for alias in node.names
                }
                with self.subTest(vendor=vendor, module=module_name):
                    self.assertFalse(imported & blocked)

    def test_group_capability_classes_remain_compatible(self):
        from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups import (
            CiscoGroupExpander,
            CiscoGroupOperations,
        )
        from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups import (
            JunosGroupExpander,
            JunosGroupOperations,
        )

        self.assertEqual(
            CiscoGroupExpander.__module__,
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.expander",
        )
        self.assertEqual(
            CiscoGroupOperations.__module__,
            "gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.groups.capabilities",
        )
        self.assertEqual(
            JunosGroupExpander.__module__,
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.expander",
        )
        self.assertEqual(
            JunosGroupOperations.__module__,
            "gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.operations.groups.capabilities",
        )


if __name__ == "__main__":
    unittest.main()
