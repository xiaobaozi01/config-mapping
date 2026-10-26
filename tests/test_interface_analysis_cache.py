"""验证接口分析缓存的复用、返回值隔离和任意 AST 修改后的失效。"""

from __future__ import annotations

import copy
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from config_adaptor.cisco import interfaces as cisco_interfaces
from config_adaptor.cisco.document import CiscoDocument, CiscoNode
from config_adaptor.common.interface import InterfaceKind, InterfaceSpec
from config_adaptor.juniper import interfaces as junos_interfaces
from config_adaptor.juniper.document import JunosDocument, JunosNode


CISCO = (
    "interface Gi0/0/0/1\n bundle id 10 mode active\n!\n"
    "interface Gi0/0/0/1.100 l2transport\n encapsulation dot1q 100\n!\n"
    "interface Bundle-Ether10\n ipv4 address 192.0.2.1/31\n!\n"
)
JUNOS = (
    "interfaces {"
    " ge-0/0/1 { ether-options { 802.3ad ae10; }"
    " unit 100 { vlan-id 100; family bridge; } }"
    " ae10 { unit 0 { family inet; } }"
    "}"
)


def analyze(document):
    """预热并返回三个依赖当前树的公开查询。"""
    return (
        document.interface_specs(),
        document.bundle_members(),
        document.business_interface_names(),
    )


class InterfaceAnalysisCacheTest(unittest.TestCase):
    def test_deepcopied_document_rebuilds_analysis_without_affecting_original(self):
        for cls, module, text, source in [
            (CiscoDocument, cisco_interfaces, CISCO, "GigabitEthernet0/0/0/1"),
            (JunosDocument, junos_interfaces, JUNOS, "ge-0/0/1"),
        ]:
            with self.subTest(vendor=cls.vendor):
                original = cls(text)
                expected = analyze(original)
                cloned = copy.deepcopy(original)
                with patch.object(module, "interface_specs", wraps=module.interface_specs) as collect:
                    cloned.business_interface_names()
                    self.assertEqual(analyze(cloned), expected)
                    collect.assert_called_once_with(cloned)
                    self.assertEqual(analyze(original), expected)
                    collect.assert_called_once_with(cloned)
                cloned.remove_interface(source, include_children=True)
                self.assertNotEqual(analyze(cloned), expected)
                self.assertEqual(analyze(original), expected)

    def test_failed_collection_is_not_cached_and_can_retry(self):
        for cls, module, text in [
            (CiscoDocument, cisco_interfaces, CISCO),
            (JunosDocument, junos_interfaces, JUNOS),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                collect = module.business_interface_names
                expected = collect(document)
                with patch.object(module, "business_interface_names", side_effect=RuntimeError("failed")):
                    with self.assertRaisesRegex(RuntimeError, "failed"):
                        document.business_interface_names()
                with patch.object(module, "business_interface_names", wraps=collect) as retry:
                    self.assertEqual(document.business_interface_names(), expected)
                    self.assertEqual(document.business_interface_names(), expected)
                    retry.assert_called_once_with(document)

    def test_repeated_queries_and_business_analysis_reuse_specs(self):
        for cls, module, text in [
            (CiscoDocument, cisco_interfaces, CISCO),
            (JunosDocument, junos_interfaces, JUNOS),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                with (
                    patch.object(module, "interface_specs", wraps=module.interface_specs) as specs,
                    patch.object(module, "bundle_members", wraps=module.bundle_members) as members,
                    patch.object(module, "business_interface_names", wraps=module.business_interface_names) as business,
                ):
                    expected = analyze(document)
                    self.assertEqual(analyze(document), expected)
                    specs.assert_called_once_with(document)
                    members.assert_called_once_with(document)
                    business.assert_called_once_with(document)
                    parent = expected[0][0].parent
                    document.logical_names_under(parent)
                    specs.assert_called_once_with(document)

    def test_returned_containers_and_specs_cannot_mutate_cached_values(self):
        for cls, text in [(CiscoDocument, CISCO), (JunosDocument, JUNOS)]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                expected = analyze(document)
                specs, members, business = analyze(document)
                self.assertIsNot(specs[0], expected[0][0])
                specs[0].name = "corrupted"
                specs[0].parent = "other-parent"
                specs[0].unit = "999"
                specs[0].vlan = 4094
                specs[0].kind = InterfaceKind.UNKNOWN
                specs[0].inner_vlan = 4093
                specs.clear()
                members.clear()
                business.clear()
                self.assertEqual(analyze(document), expected)

    def test_empty_results_are_cached(self):
        for cls, module in [(CiscoDocument, cisco_interfaces), (JunosDocument, junos_interfaces)]:
            with self.subTest(vendor=cls.vendor):
                document = cls("")
                with patch.object(module, "business_interface_names", wraps=module.business_interface_names) as collect:
                    self.assertEqual(document.business_interface_names(), set())
                    self.assertEqual(document.business_interface_names(), set())
                    collect.assert_called_once_with(document)

    def test_caches_are_isolated_per_document(self):
        for cls, text in [(CiscoDocument, CISCO), (JunosDocument, JUNOS)]:
            with self.subTest(vendor=cls.vendor):
                populated = cls(text)
                empty = cls("")
                expected = analyze(populated)
                self.assertEqual(analyze(empty), ([], {}, set()))
                self.assertEqual(analyze(populated), expected)

    def test_cisco_direct_header_active_and_child_list_edits(self):
        document = CiscoDocument(CISCO)
        analyze(document)
        parent, sub = document._interface_nodes()[:2]
        parent.children[0].header = "bundle id 20 mode active"
        self.assertEqual(document.bundle_members(), {"GigabitEthernet0/0/0/1": "Bundle-Ether20"})
        sub.children[0].header = "encapsulation dot1q 200 second-dot1q 30"
        spec = next(spec for spec in document.interface_specs() if spec.unit == "100")
        self.assertEqual((spec.vlan, spec.inner_vlan), (200, 30))
        sub.header = "interface Gi0/0/0/1.100"
        self.assertNotIn(sub.interface_name, document.business_interface_names())
        child = CiscoNode("ipv4 address 198.51.100.1/31")
        sub.children.append(child)
        self.assertIn(sub.interface_name, document.business_interface_names())
        child.active = False
        self.assertNotIn(sub.interface_name, document.business_interface_names())
        child.active = True
        sub.children.remove(child)
        self.assertNotIn(sub.interface_name, document.business_interface_names())
        parent.active = False
        self.assertEqual(document.bundle_members(), {})
        self.assertNotIn(parent.interface_name, {spec.name for spec in document.interface_specs()})
        parent.active = True
        self.assertTrue(document.bundle_members())

    def test_junos_direct_header_active_and_child_list_edits(self):
        document = JunosDocument(JUNOS)
        analyze(document)
        parent = document._interface_nodes()[0]
        options, unit = parent.children
        options.children[0].header = "802.3ad ae20;"
        self.assertEqual(document.bundle_members(), {"ge-0/0/1": "ae20"})
        unit.children[0].header = "vlan-tags outer 200 inner 30;"
        spec = document.interface_specs()[0]
        self.assertEqual((spec.vlan, spec.inner_vlan), (200, 30))
        unit.header = "protect: unit 200"
        self.assertEqual(document.interface_specs()[0].name, "ge-0/0/1.200")
        unit.children.pop()
        self.assertNotIn("ge-0/0/1.200", document.business_interface_names())
        unit.children.append(JunosNode("family inet", []))
        self.assertIn("ge-0/0/1.200", document.business_interface_names())
        unit.children[-1].active = False
        self.assertNotIn("ge-0/0/1.200", document.business_interface_names())
        parent.header = "inactive: ge-0/0/1"
        self.assertEqual(document.bundle_members(), {})
        self.assertEqual([spec.name for spec in document.interface_specs()], ["ae10.0"])
        parent.header = "ge-0/0/1"
        self.assertTrue(document.bundle_members())

    def test_external_reference_edits_invalidate_business_analysis(self):
        for cls, text, name, replacement in [
            (CiscoDocument, "interface Gi0/0/0/1\n!\nrouter ospf 1\n interface GigabitEthernet0/0/0/1\n!\n", "GigabitEthernet0/0/0/1", "interface GigabitEthernet0/0/0/2"),
            (JunosDocument, "interfaces { ge-0/0/1 { unit 0; } } protocols { ospf { interface ge-0/0/1.0; } }", "ge-0/0/1.0", "interface ge-0/0/2.0;"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                self.assertIn(name, document.business_interface_names())
                if cls is CiscoDocument:
                    reference = document.root.children[2].children[0]
                else:
                    reference = document._top_block("protocols").children[0].children[0]
                reference.active = False
                self.assertNotIn(name, document.business_interface_names())
                reference.active = True
                self.assertIn(name, document.business_interface_names())
                reference.header = replacement
                self.assertNotIn(name, document.business_interface_names())

    def test_rename_clone_and_remove_invalidate_all_queries(self):
        for cls, text, source, target, clone_name in [
            (CiscoDocument, CISCO, "GigabitEthernet0/0/0/1", "GigabitEthernet0/0/0/2", "GigabitEthernet0/0/0/3"),
            (JunosDocument, JUNOS, "ge-0/0/1", "ge-0/0/2", "ge-0/0/3"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                analyze(document)
                document.rename_interface_tree(source, target)
                specs, members, business = analyze(document)
                self.assertNotIn(source, {spec.parent for spec in specs})
                self.assertIn(target, members)
                document.clone_interface_tree(target, clone_name)
                specs, members, business = analyze(document)
                self.assertIn(clone_name, {spec.parent for spec in specs})
                self.assertIn(clone_name, members)
                document.remove_interface(target, include_children=True)
                specs, members, business = analyze(document)
                self.assertNotIn(target, {spec.parent for spec in specs})
                self.assertNotIn(target, members)
                self.assertFalse(any(name == target or name.startswith(target + ".") for name in business))

    def test_cleaning_rule_replacements_invalidate_queries(self):
        for cls, text in [(CiscoDocument, CISCO), (JunosDocument, JUNOS)]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                analyze(document)
                if cls is CiscoDocument:
                    document.apply_cleaning_rule(("interface.*",), r"bundle\s+id\s+10", "replace", "bundle id 20")
                    expected = {"GigabitEthernet0/0/0/1": "Bundle-Ether20"}
                else:
                    document.apply_cleaning_rule(("interfaces", "ge-0/0/1", "ether-options"), r"802\.3ad\s+ae10", "replace", "802.3ad ae20")
                    expected = {"ge-0/0/1": "ae20"}
                self.assertEqual(document.bundle_members(), expected)

    def test_reference_rewrite_invalidates_business_query(self):
        for cls, text, source, target in [
            (CiscoDocument, "interface Gi0/0/0/1\n!\ninterface Gi0/0/0/2\n!\nrouter ospf 1\n interface GigabitEthernet0/0/0/1\n!\n", "GigabitEthernet0/0/0/1", "GigabitEthernet0/0/0/2"),
            (JunosDocument, "interfaces { ge-0/0/1 { unit 0; } ge-0/0/2 { unit 0; } } protocols { ospf { interface ge-0/0/1.0; } }", "ge-0/0/1.0", "ge-0/0/2.0"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                self.assertEqual(document.business_interface_names(), {source})
                document.replace_references({source: [target]})
                self.assertEqual(document.business_interface_names(), {target})

    def test_junos_leaf_and_empty_block_changes_invalidate_specs(self):
        document = JunosDocument("interfaces { ge-0/0/1 { unit 0; } }")
        parent = document._interface_nodes()[0]
        self.assertEqual(document.interface_specs()[0].name, "ge-0/0/1.0")
        parent.children = None
        self.assertEqual(document.interface_specs(), [])
        parent.children = []
        self.assertEqual(document.interface_specs()[0].name, "ge-0/0/1")

    def test_interface_kind_uses_cached_pure_lookup_without_module_dispatch(self):
        for cls, raw, canonical in [
            (CiscoDocument, " Gi 0/0/0/1 ", "GigabitEthernet0/0/0/1"),
            (JunosDocument, " ge-0/0/1 ", "ge-0/0/1"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls("")
                with patch.object(document, "_interfaces", side_effect=AssertionError("unexpected dispatch")):
                    self.assertEqual(document.interface_kind(raw), InterfaceKind.PHYSICAL)
                    self.assertEqual(document.interface_kind(canonical), InterfaceKind.PHYSICAL)
                    self.assertEqual(document.interface_kind("future0"), InterfaceKind.UNKNOWN)


class InterfaceCacheDifferentialTest(unittest.TestCase):
    def assert_matches_uncached(self, document):
        """同时绕过整树缓存和字符串 LRU，避免内嵌查询仍命中缓存。"""
        actual = analyze(document)
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                document._interface_analysis_cache,
                "read",
                side_effect=lambda root, query, collect, copy_result: copy_result(collect()),
            ))
            for name, parser in vars(document._parser).items():
                if callable(parser) and hasattr(parser, "cache_info"):
                    stack.enter_context(patch.object(document._parser, name, parser.__wrapped__))
            self.assertEqual(actual, analyze(document))
        return actual

    @staticmethod
    def all_nodes(document):
        pending = [document.root]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(reversed(node.children or []))

    def test_statement_order_changes_vlan_priority(self):
        for cls, text in [
            (CiscoDocument, "interface Gi0/0/0/1.1\n encapsulation dot1q 100\n encapsulation dot1q 200\n!\n"),
            (JunosDocument, "interfaces { ge-0/0/1 { unit 1 { vlan-id 100; vlan-id 200; } } }"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                self.assertEqual(self.assert_matches_uncached(document)[0][0].vlan, 100)
                parent = document._interface_nodes()[0]
                container = parent if cls is CiscoDocument else document._unit_nodes(parent)[0]
                container.children.reverse()
                self.assertEqual(self.assert_matches_uncached(document)[0][0].vlan, 200)

    def test_same_preorder_headers_with_different_parentage_invalidate_business(self):
        for cls, text, name in [
            (CiscoDocument, "interface Gi0/0/0/1\n nested\n ipv4 address 192.0.2.1/31\n!\n", "GigabitEthernet0/0/0/1"),
            (JunosDocument, "interfaces { ge-0/0/1 { unit 1 { nested {} family inet; } } }", "ge-0/0/1.1"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                parent = document._interface_nodes()[0]
                container = parent if cls is CiscoDocument else document._unit_nodes(parent)[0]
                nested, business = container.children
                nested.active = False
                self.assertIn(name, self.assert_matches_uncached(document)[2])
                headers = [node.header for node in self.all_nodes(document)]
                nested.children.append(business)
                container.children.remove(business)
                self.assertEqual(headers, [node.header for node in self.all_nodes(document)])
                self.assertNotIn(name, self.assert_matches_uncached(document)[2])

    def test_gateway_reference_changes_refresh_specs_and_business(self):
        for cls, text, old, new in [
            (
                CiscoDocument,
                "interface Gi0/0/0/1.1 l2transport\n encapsulation dot1q 100\n!\n"
                "interface BVI1000\n!\ninterface BVI2000\n!\nl2vpn\n bridge group B\n"
                "  bridge-domain D\n   interface GigabitEthernet0/0/0/1.1\n"
                "   routed interface BVI1000\n!\n",
                "BVI1000", "BVI2000",
            ),
            (
                JunosDocument,
                "interfaces { ge-0/0/1 { unit 1 { vlan-id 100; family bridge; } }"
                " irb { unit 1000; unit 2000; } }"
                " bridge-domains { D { interface ge-0/0/1.1; routing-interface irb.1000; } }",
                "irb.1000", "irb.2000",
            ),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                before = self.assert_matches_uncached(document)
                self.assertIn(old, before[2])
                self.assertNotIn(new, before[2])
                reference = next(
                    node for node in self.all_nodes(document)
                    if node.header.startswith(("routed interface ", "routing-interface "))
                )
                reference.header = reference.header.replace(old, new)
                after = self.assert_matches_uncached(document)
                self.assertNotIn(old, after[2])
                self.assertIn(new, after[2])
                if cls is CiscoDocument:
                    specs = {spec.name: spec for spec in after[0]}
                    self.assertIsNone(specs[old].inner_vlan)
                    self.assertEqual(specs[new].inner_vlan, 100)

    def test_comment_presence_changes_effective_business_subtree(self):
        document = JunosDocument("interfaces { ge-0/0/1 { unit 1; } }")
        parent = document._interface_nodes()[0]
        subtree = JunosNode("", [JunosNode("family inet;")])
        parent.children.append(subtree)
        for comment, expected in [
            (None, {"ge-0/0/1.1"}),
            ("", set()),
            ("# standalone", set()),
            ("# different comment", set()),
            (None, {"ge-0/0/1.1"}),
        ]:
            with self.subTest(comment=comment):
                subtree.comment = comment
                self.assertEqual(self.assert_matches_uncached(document)[2], expected)


class JunosActiveVlanIndexTest(unittest.TestCase):
    def test_parent_scan_runs_once_and_duplicate_parents_and_units_keep_first(self):
        document = JunosDocument(
            "interfaces {"
            " ge-0/0/1 {"
            " unit 1 { family ethernet-switching { vlan members BLUE; } }"
            " unit 1 { family ethernet-switching { vlan members RED; } }"
            " unit 2 { family bridge; }"
            " }"
            " ge-0/0/1 { unit 1 { family ethernet-switching { vlan members GREEN; } } }"
            "}"
        )
        specs = [
            InterfaceSpec(f"ge-0/0/1.{unit}", "ge-0/0/1", str(unit), 100 + unit, InterfaceKind.PHYSICAL)
            for unit in (1, 2)
        ]
        with patch.object(document, "_interface_nodes", wraps=document._interface_nodes) as scan:
            vlans, names = junos_interfaces._collect_active_vlans(document, {spec.name for spec in specs}, specs)
            scan.assert_called_once_with()
        self.assertEqual(vlans, {101, 102})
        self.assertEqual(names, {"BLUE"})


if __name__ == "__main__":
    unittest.main()
