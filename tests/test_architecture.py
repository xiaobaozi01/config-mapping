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
                "SourceSemanticValidationHandler",
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
        from impl.cisco.rules import support
        from impl.cisco.syntax import rewrite_helpers

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
                self.assertFalse(
                    (
                        project_root / "src" / "impl" / vendor / "rules" / image
                    ).exists()
                )

        from impl.common.support.config_paths import config_path

        for vendor, image in locations:
            with self.subTest(vendor=vendor, helper=True):
                self.assertEqual(
                    config_path("rules", vendor, image),
                    project_root / "config" / "rules" / vendor / image,
                )


class InterfacePackageLayoutTest(unittest.TestCase):

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
                    self.assertEqual(
                        getattr(interfaces, name).__module__,
                        interfaces.__name__,
                    )
        self.assertIsNone(
            importlib.util.find_spec("adaptation.core_interfaces")
        )

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
        self.assertIsNone(
            importlib.util.find_spec("adaptation.cleaning_scopes")
        )


class NoCliLayoutTest(unittest.TestCase):

    def test_adaptation_has_no_cli_module_or_console_script(self):
        project_root = Path(__file__).parents[1]
        self.assertFalse(
            (project_root / "src" / "adaptation" / "cli.py").exists()
        )
        self.assertFalse(
            (project_root / "src" / "adaptation" / "__main__.py").exists()
        )
        payload = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "config-adaptor", payload.get("project", {}).get("scripts", {})
        )


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
            "impl.cisco.operations.declarative_cleaning",
            "impl.cisco.operations.management_cleaning",
            "impl.cisco.operations.compatibility",
            "impl.cisco.operations.pruning",
            "impl.cisco.operations.references",
            "impl.cisco.operations.simulation",
            "impl.cisco.operations.validation",
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
            "impl.juniper.operations.declarative_cleaning",
            "impl.juniper.operations.management_cleaning",
            "impl.juniper.operations.compatibility",
            "impl.juniper.operations.pruning",
            "impl.juniper.operations.references",
            "impl.juniper.operations.simulation",
            "impl.juniper.operations.validation",
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


class HeaderParserOwnershipTest(unittest.TestCase):
    def test_header_parsers_are_owned_by_syntax_modules(self):
        from impl.cisco import CiscoDocument
        from impl.cisco.syntax.header_parser import _CiscoHeaderParser
        from impl.juniper import JunosDocument
        from impl.juniper.syntax.header_parser import _JunosHeaderParser

        self.assertEqual(
            _CiscoHeaderParser.__module__,
            "impl.cisco.syntax.header_parser",
        )
        self.assertEqual(
            _JunosHeaderParser.__module__,
            "impl.juniper.syntax.header_parser",
        )
        self.assertIsInstance(CiscoDocument("")._parser, _CiscoHeaderParser)
        self.assertIsInstance(JunosDocument("")._parser, _JunosHeaderParser)

    def test_header_parser_classes_are_not_defined_in_document(self):
        project_root = Path(__file__).parents[1]
        blocked = {
            "cisco": "_CiscoHeaderParser",
            "juniper": "_JunosHeaderParser",
        }
        for vendor, class_name in blocked.items():
            path = project_root / "src" / "impl" / vendor / "document.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            }
            with self.subTest(vendor=vendor):
                self.assertNotIn(class_name, names)


class CapabilityProtocolLayoutTest(unittest.TestCase):

    def test_capability_protocols_are_importable(self):
        from impl.common.domain import capabilities

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
        from impl.common.domain.capabilities import InterfaceAnalysis

        self.assertFalse(hasattr(InterfaceAnalysis, "rename"))
        self.assertFalse(hasattr(InterfaceAnalysis, "clone"))
        self.assertFalse(hasattr(InterfaceAnalysis, "remove"))

    def test_validation_protocol_has_no_editing_methods(self):
        from impl.common.domain.capabilities import ValidationOperations

        self.assertFalse(hasattr(ValidationOperations, "edit"))
        self.assertFalse(hasattr(ValidationOperations, "apply_cleaning_rules"))


class CleaningModuleLayoutTest(unittest.TestCase):
    def test_cleaning_modules_have_distinct_responsibilities(self):
        from impl.cisco.operations import (
            cleaning as cisco_cleaning,
        )
        from impl.cisco.operations import (
            declarative_cleaning as cisco_declarative,
        )
        from impl.cisco.operations import (
            management_cleaning as cisco_management,
        )
        from impl.juniper.operations import (
            cleaning as juniper_cleaning,
        )
        from impl.juniper.operations import (
            declarative_cleaning as juniper_declarative,
        )
        from impl.juniper.operations import (
            management_cleaning as juniper_management,
        )

        self.assertTrue(hasattr(cisco_cleaning, "CiscoCleaningOperations"))
        self.assertFalse(hasattr(cisco_cleaning, "_MANAGEMENT_TOP_LEVEL"))
        self.assertTrue(
            hasattr(cisco_management, "clean_management_access")
        )
        self.assertTrue(
            hasattr(cisco_declarative, "apply_cleaning_rules")
        )
        self.assertIsNone(
            importlib.util.find_spec(
                "impl.cisco.operations.configuration_cleaning"
            )
        )

        self.assertTrue(hasattr(juniper_cleaning, "JunosCleaningOperations"))
        self.assertFalse(hasattr(juniper_cleaning, "_disable_matching"))
        self.assertTrue(
            hasattr(juniper_management, "clean_management_access")
        )
        self.assertTrue(
            hasattr(juniper_declarative, "apply_cleaning_rules")
        )
        self.assertIsNone(
            importlib.util.find_spec(
                "impl.juniper.operations.configuration_cleaning"
            )
        )


class CiscoCapabilityObjectTest(unittest.TestCase):

    def test_document_exposes_narrow_capabilities(self):
        from impl.cisco import CiscoDocument

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
        from impl.cisco import CiscoDocument

        document = CiscoDocument("")
        first = document.interfaces
        second = document.interfaces
        self.assertIsNot(first, second)
        self.assertEqual(document.__dict__.get("_interfaces"), None)


class JuniperCapabilityObjectTest(unittest.TestCase):

    def test_document_exposes_narrow_capabilities(self):
        from impl.juniper import JunosDocument

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
        from impl.juniper import JunosDocument

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
        import inspect

        from impl.cisco.operations.bundles import CiscoBundleOperations
        from impl.cisco.operations.cleaning import CiscoCleaningOperations
        from impl.cisco.operations.compatibility import (
            CiscoCompatibilityOperations,
        )
        from impl.cisco.operations.groups import CiscoGroupOperations
        from impl.cisco.operations.interfaces import (
            CiscoInterfaceAnalysis,
            CiscoInterfaceTransform,
        )
        from impl.cisco.operations.pruning import CiscoPruningOperations
        from impl.cisco.operations.references import CiscoReferenceOperations
        from impl.cisco.operations.simulation import (
            CiscoSimulationOperations,
        )
        from impl.cisco.operations.validation import (
            CiscoValidationOperations,
        )
        from impl.common.domain import capabilities
        from impl.juniper.operations.bundles import JunosBundleOperations
        from impl.juniper.operations.cleaning import JunosCleaningOperations
        from impl.juniper.operations.compatibility import (
            JunosCompatibilityOperations,
        )
        from impl.juniper.operations.groups import JunosGroupOperations
        from impl.juniper.operations.interfaces import (
            JunosInterfaceAnalysis,
            JunosInterfaceTransform,
        )
        from impl.juniper.operations.pruning import JunosPruningOperations
        from impl.juniper.operations.references import JunosReferenceOperations
        from impl.juniper.operations.simulation import (
            JunosSimulationOperations,
        )
        from impl.juniper.operations.validation import (
            JunosValidationOperations,
        )

        pairs = (
            (capabilities.InterfaceAnalysis, CiscoInterfaceAnalysis),
            (capabilities.InterfaceAnalysis, JunosInterfaceAnalysis),
            (capabilities.InterfaceTransform, CiscoInterfaceTransform),
            (capabilities.InterfaceTransform, JunosInterfaceTransform),
            (capabilities.BundleOperations, CiscoBundleOperations),
            (capabilities.BundleOperations, JunosBundleOperations),
            (capabilities.GroupOperations, CiscoGroupOperations),
            (capabilities.GroupOperations, JunosGroupOperations),
            (capabilities.CleaningOperations, CiscoCleaningOperations),
            (capabilities.CleaningOperations, JunosCleaningOperations),
            (capabilities.PruningOperations, CiscoPruningOperations),
            (capabilities.PruningOperations, JunosPruningOperations),
            (
                capabilities.CompatibilityOperations,
                CiscoCompatibilityOperations,
            ),
            (
                capabilities.CompatibilityOperations,
                JunosCompatibilityOperations,
            ),
            (capabilities.ValidationOperations, CiscoValidationOperations),
            (capabilities.ValidationOperations, JunosValidationOperations),
            (capabilities.ReferenceOperations, CiscoReferenceOperations),
            (capabilities.ReferenceOperations, JunosReferenceOperations),
            (capabilities.SimulationOperations, CiscoSimulationOperations),
            (capabilities.SimulationOperations, JunosSimulationOperations),
        )
        for protocol, implementation in pairs:
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
        from impl.cisco.operations import compatibility as cisco_compat
        from impl.cisco.operations import pruning as cisco_pruning
        from impl.cisco.operations import simulation as cisco_simulation
        from impl.cisco.operations import validation as cisco_validation
        from impl.cisco.rules import compatibility as cisco_rule_compat
        from impl.cisco.rules import pruning as cisco_rule_pruning
        from impl.cisco.rules import simulation as cisco_rule_simulation
        from impl.juniper import rules as junos_rule_root
        from impl.juniper.operations import compatibility as junos_compat
        from impl.juniper.operations import pruning as junos_pruning
        from impl.juniper.operations import simulation as junos_simulation
        from impl.juniper.operations import validation as junos_validation
        from impl.juniper.rules import pruning as junos_rule_pruning
        from impl.juniper.rules import simulation as junos_rule_simulation

        pairs = (
            (cisco_pruning, cisco_rule_pruning, "CiscoPruningOperations"),
            (cisco_compat, cisco_rule_compat, "CiscoCompatibilityOperations"),
            (cisco_validation, cisco_rule_compat, "CiscoValidationOperations"),
            (
                cisco_simulation,
                cisco_rule_simulation,
                "CiscoSimulationOperations",
            ),
            (junos_pruning, junos_rule_pruning, "JunosPruningOperations"),
            (junos_compat, junos_rule_root, "JunosCompatibilityOperations"),
            (junos_validation, junos_rule_root, "JunosValidationOperations"),
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
        from impl.cisco import CiscoDocument
        from impl.juniper import JunosDocument

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
        source_root = Path(__file__).parents[1] / "src" / "adaptation"
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
        from impl.cisco import CiscoDocument
        from impl.juniper import JunosDocument

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
        from impl.common import VendorConfiguration
        from impl.common.domain.capabilities import VendorDocument

        self.assertIs(VendorConfiguration, VendorDocument)


class InterfaceModuleLayoutTest(unittest.TestCase):
    def test_interface_packages_have_required_modules(self):
        modules = (
            "impl.cisco.operations.interfaces.access",
            "impl.cisco.operations.interfaces.analysis",
            "impl.cisco.operations.interfaces.business",
            "impl.cisco.operations.interfaces.capabilities",
            "impl.cisco.operations.interfaces.transform",
            "impl.juniper.operations.interfaces.access",
            "impl.juniper.operations.interfaces.analysis",
            "impl.juniper.operations.interfaces.business",
            "impl.juniper.operations.interfaces.capabilities",
            "impl.juniper.operations.interfaces.transform",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_interface_single_files_are_removed(self):
        project_root = Path(__file__).parents[1]
        for vendor in ("cisco", "juniper"):
            with self.subTest(vendor=vendor):
                path = (
                    project_root
                    / "src"
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
        for vendor in ("cisco", "juniper"):
            package = (
                project_root
                / "src"
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
        from impl.cisco.operations.interfaces import (
            CiscoInterfaceAnalysis,
            CiscoInterfaceTransform,
        )
        from impl.juniper.operations.interfaces import (
            JunosInterfaceAnalysis,
            JunosInterfaceTransform,
        )

        self.assertEqual(
            CiscoInterfaceAnalysis.__module__,
            "impl.cisco.operations.interfaces.analysis",
        )
        self.assertEqual(
            CiscoInterfaceTransform.__module__,
            "impl.cisco.operations.interfaces.transform",
        )
        self.assertEqual(
            JunosInterfaceAnalysis.__module__,
            "impl.juniper.operations.interfaces.analysis",
        )
        self.assertEqual(
            JunosInterfaceTransform.__module__,
            "impl.juniper.operations.interfaces.transform",
        )


class GroupModuleLayoutTest(unittest.TestCase):
    def test_group_packages_have_required_modules(self):
        modules = (
            "impl.cisco.operations.groups.models",
            "impl.cisco.operations.groups.selectors",
            "impl.cisco.operations.groups.definitions",
            "impl.cisco.operations.groups.merger",
            "impl.cisco.operations.groups.expander",
            "impl.cisco.operations.groups.publishing",
            "impl.cisco.operations.groups.capabilities",
            "impl.juniper.operations.groups.models",
            "impl.juniper.operations.groups.selectors",
            "impl.juniper.operations.groups.definitions",
            "impl.juniper.operations.groups.merger",
            "impl.juniper.operations.groups.expander",
            "impl.juniper.operations.groups.capabilities",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_group_single_files_are_removed(self):
        project_root = Path(__file__).parents[1]
        for vendor in ("cisco", "juniper"):
            with self.subTest(vendor=vendor):
                path = (
                    project_root
                    / "src"
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
        for vendor in ("cisco", "juniper"):
            package = (
                project_root
                / "src"
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
        from impl.cisco.operations.groups import (
            CiscoGroupExpander,
            CiscoGroupOperations,
        )
        from impl.juniper.operations.groups import (
            JunosGroupExpander,
            JunosGroupOperations,
        )

        self.assertEqual(
            CiscoGroupExpander.__module__,
            "impl.cisco.operations.groups.expander",
        )
        self.assertEqual(
            CiscoGroupOperations.__module__,
            "impl.cisco.operations.groups.capabilities",
        )
        self.assertEqual(
            JunosGroupExpander.__module__,
            "impl.juniper.operations.groups.expander",
        )
        self.assertEqual(
            JunosGroupOperations.__module__,
            "impl.juniper.operations.groups.capabilities",
        )


if __name__ == "__main__":
    unittest.main()
