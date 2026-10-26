"""验证字符串解析缓存的命中、语义兼容性及 AST 修改后的实时查询。"""

from __future__ import annotations

import copy
import gc
import unittest
import weakref

from config_adaptor.cisco import document as cisco
from config_adaptor.common.interface import InterfaceKind
from config_adaptor.juniper import document as junos


class DocumentCacheTest(unittest.TestCase):
    def test_deepcopied_documents_have_independent_empty_parse_caches(self):
        for cls, raw in [
            (cisco.CiscoDocument, " Gi 0/0/0/1 "),
            (junos.JunosDocument, " ge-0/0/1 "),
        ]:
            with self.subTest(vendor=cls.vendor):
                original = cls("")
                original.resolve_interface(raw)
                original.interface_kind(raw)
                cloned = copy.deepcopy(original)
                self.assertIsNot(cloned.root, original.root)
                for name, parser in vars(original._parser).items():
                    if callable(parser) and hasattr(parser, "cache_info"):
                        copied_parser = getattr(cloned._parser, name)
                        self.assertIsNot(copied_parser, parser)
                        self.assertEqual(copied_parser.cache_info().currsize, 0)
                self.assertEqual(cloned.resolve_interface(raw), original.resolve_interface(raw))
                self.assertEqual(cloned.interface_kind(raw), original.interface_kind(raw))
                cloned._parser.interface_kind.cache_clear()
                self.assertEqual(original._parser.interface_kind.cache_info().currsize, 1)

    def test_deepcopy_does_not_keep_original_parse_caches_alive(self):
        for cls in (cisco.CiscoDocument, junos.JunosDocument):
            with self.subTest(vendor=cls.vendor):
                original = cls("")
                original.interface_kind("future0")
                references = [weakref.ref(original), weakref.ref(original._parser)]
                references.extend(weakref.ref(value) for value in vars(original._parser).values())
                cloned = copy.deepcopy(original)
                del original
                gc.collect()
                self.assertTrue(all(reference() is None for reference in references))
                self.assertEqual(cloned.interface_kind("future0"), InterfaceKind.UNKNOWN)

    def test_module_parsers_do_not_hold_global_result_caches(self):
        for module in (cisco, junos):
            with self.subTest(module=module.__name__):
                cached_globals = [
                    name for name, value in vars(module).items()
                    if callable(value)
                    and getattr(value, "__module__", None) == module.__name__
                    and hasattr(value, "cache_info")
                ]
                self.assertEqual(cached_globals, [])

    def test_parse_caches_are_isolated_between_documents(self):
        for cls, raw in [
            (cisco.CiscoDocument, " Gi 0/0/0/1 "),
            (junos.JunosDocument, " ge-0/0/1 "),
        ]:
            with self.subTest(vendor=cls.vendor):
                first, second = cls(""), cls("")
                self.assertIsNot(first._parser, second._parser)
                for name, parser in vars(first._parser).items():
                    if callable(parser) and hasattr(parser, "cache_info"):
                        self.assertIsNot(parser, getattr(second._parser, name))
                first.resolve_interface(raw)
                first.interface_kind(raw)
                self.assertEqual(first._parser.canonical_interface.cache_info().misses, 1)
                self.assertEqual(first._parser.interface_kind.cache_info().misses, 1)
                self.assertEqual(second._parser.canonical_interface.cache_info().currsize, 0)
                self.assertEqual(second._parser.interface_kind.cache_info().currsize, 0)
                second.interface_kind(raw)
                self.assertEqual(second._parser.interface_kind.cache_info().misses, 1)
                first._parser.interface_kind.cache_clear()
                self.assertEqual(second._parser.interface_kind.cache_info().currsize, 1)

    def test_releasing_document_releases_caches_even_when_nodes_are_retained(self):
        for cls, text in [
            (cisco.CiscoDocument, "interface Gi0/0/0/1\n ipv4 address 192.0.2.1/31\n!\n"),
            (junos.JunosDocument, "interfaces { ge-0/0/1 { unit 0 { family inet; } } }"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(text)
                document.interface_specs()
                document.bundle_members()
                document.business_interface_names()
                retained_node = document._interface_nodes()[0]
                references = [
                    weakref.ref(document),
                    weakref.ref(document._parser),
                    weakref.ref(document._interface_analysis_cache),
                ]
                references.extend(
                    weakref.ref(value) for value in vars(document._parser).values()
                    if callable(value) and hasattr(value, "cache_info")
                )
                del document
                gc.collect()
                self.assertTrue(all(reference() is None for reference in references))
                self.assertTrue(retained_node.header)
                self.assertIsNotNone(retained_node.clone())

    def test_repeated_strings_hit_bounded_caches_including_non_matches(self):
        cisco_document = cisco.CiscoDocument("")
        junos_document = junos.JunosDocument("")
        cases = [
            (cisco_document._parser.canonical_interface, " Gi 0/0/0/1 l2transport ", "GigabitEthernet0/0/0/1"),
            (cisco_document._parser.canonical_interface, "", ""),
            (cisco_document._parser.interface_kind, " Gi 0/0/0/1 ", InterfaceKind.PHYSICAL),
            (cisco_document._parser.interface_kind, "Future0", InterfaceKind.UNKNOWN),
            (cisco_document._parser.interface_name, "interface Gi0/0/0/1", "GigabitEthernet0/0/0/1"),
            (cisco_document._parser.interface_name, "hostname XR", None),
            (cisco_document._parser.l2transport, "interface Gi0 l2transport", True),
            (cisco_document._parser.l2transport, "interface Gi0 l2transport extra", False),
            (cisco_document._parser.is_group, "GROUP EDGE", True),
            (cisco_document._parser.is_group, "group ", False),
            (junos_document._parser.canonical_interface, " ge-0/0/1 . 10 ", "ge-0/0/1.10"),
            (junos_document._parser.canonical_interface, "", ""),
            (junos_document._parser.interface_kind, " ge-0/0/1 ", InterfaceKind.PHYSICAL),
            (junos_document._parser.interface_kind, "future0", InterfaceKind.UNKNOWN),
            (junos_document._parser.header_prefixes, "protect: inactive: unit 10;", ("unit 10;", True)),
            (junos_document._parser.header_prefixes, "", ("", False)),
            (junos_document._parser.is_unit, "protect: unit 001;", True),
            (junos_document._parser.is_unit, "unit 1; extra", False),
            (junos_document._parser.unit_number, "protect: unit 001;", "001"),
            (junos_document._parser.vlan, "protect: vlan-id 0;", 0),
            (junos_document._parser.vlan, "vlan-tags outer 100 inner 20;", 100),
            (junos_document._parser.vlan, "vlan-id 100", None),
            (junos_document._parser.inner_vlan, "vlan-tags outer 100 inner 0;", 0),
            (junos_document._parser.inner_vlan, "vlan-id 100;", None),
        ]
        for parser, value, expected in cases:
            with self.subTest(parser=parser.__name__, value=value):
                parser.cache_clear()
                self.addCleanup(parser.cache_clear)
                self.assertEqual(parser(value), expected)
                before = parser.cache_info()
                self.assertEqual(parser(value), expected)
                after = parser.cache_info()
                self.assertEqual(after.maxsize, 32_768)
                self.assertEqual(after.hits, before.hits + 1)
                self.assertEqual(after.misses, before.misses)

    def test_junos_prefix_consumers_share_one_cached_parse(self):
        header = " protect: inactive: protect: unit 10; "
        document = junos.JunosDocument("")
        self.assertFalse(document._parser.is_effective(junos.JunosNode(header)))
        self.assertEqual(document._parser.base_header(header), "unit 10;")
        self.assertEqual(document._parser.output_header(header), "inactive: unit 10;")
        info = document._parser.header_prefixes.cache_info()
        self.assertEqual((info.misses, info.hits), (1, 2))

    def test_junos_prefix_order_repetition_and_case_are_preserved(self):
        document = junos.JunosDocument("")
        for header, base, inactive, output in [
            (" protect: protect: unit 0; ", "unit 0;", False, "unit 0;"),
            ("inactive: protect: inactive: unit 0;", "unit 0;", True, "inactive: unit 0;"),
            ("protect:inactive:unit 0;", "unit 0;", True, "inactive: unit 0;"),
            ("Inactive: unit 0;", "Inactive: unit 0;", False, "Inactive: unit 0;"),
            ("inactive:", "", True, "inactive: "),
        ]:
            with self.subTest(header=header):
                node = junos.JunosNode(header)
                self.assertEqual(node.configured_inactive, inactive)
                self.assertEqual(document._parser.base_header(header), base)
                self.assertEqual(document._parser.output_header(header), output)
                self.assertEqual(junos.JunosDocument._base_header(header), base)
                self.assertEqual(junos.JunosDocument._output_header(header), output)

    def test_cisco_cached_mode_and_name_follow_header_edits_and_clone(self):
        node = cisco.CiscoNode("interface preconfigure Gi0/0/0/1 l2transport")
        self.assertEqual(node.interface_name, "GigabitEthernet0/0/0/1")
        self.assertTrue(node.is_preconfigured_interface)
        self.assertTrue(node.l2transport)
        clone = node.clone()
        clone.header = "interface Loopback10"
        self.assertEqual(clone.interface_name, "Loopback10")
        self.assertFalse(clone.is_preconfigured_interface)
        self.assertFalse(clone.l2transport)
        self.assertTrue(node.is_preconfigured_interface)
        self.assertTrue(node.l2transport)
        node.header = "hostname XR"
        self.assertIsNone(node.interface_name)
        self.assertFalse(node.is_preconfigured_interface)
        self.assertFalse(node.l2transport)
        node.header = "group EDGE"
        self.assertTrue(cisco.CiscoDocument._is_group_node(node))
        node.header = "end-group"
        self.assertFalse(cisco.CiscoDocument._is_group_node(node))

    def test_cisco_interface_queries_follow_tree_edits_after_warming(self):
        document = cisco.CiscoDocument("interface Gi0/0/0/1\n!\n")
        original = document._interface_nodes()[0]
        self.assertEqual(document.interface_specs()[0].name, "GigabitEthernet0/0/0/1")
        clone = original.clone()
        clone.header = "interface Loopback10"
        document.root.children.append(clone)
        self.assertEqual(len(document.interface_specs()), 2)
        original.active = False
        self.assertEqual([spec.name for spec in document.interface_specs()], ["Loopback10"])
        document.root.children.remove(clone)
        self.assertEqual(document.interface_specs(), [])
        original.active = True
        self.assertEqual(document._interface_nodes(), [original])

    def test_junos_prefix_flags_follow_edits_active_state_and_comments(self):
        document = junos.JunosDocument("")
        node = junos.JunosNode("protect: inactive: unit 10;", [])
        self.assertFalse(node.effective)
        node.header = "protect: unit 10;"
        self.assertFalse(node.configured_inactive)
        self.assertTrue(node.effective)
        self.assertEqual(document._parser.output_header(node.header), "unit 10;")
        node.active = False
        self.assertFalse(node.effective)
        node.active = True
        self.assertTrue(node.effective)
        node.header = ""
        node.comment = "# standalone"
        self.assertFalse(node.effective)

    def test_junos_unit_queries_follow_header_state_and_children_edits(self):
        document = junos.JunosDocument("")
        unit = junos.JunosNode("unit 001;")
        interface = junos.JunosNode("ge-0/0/1", [unit])
        self.assertEqual(document._unit_nodes(interface), [unit])
        self.assertEqual(document._unit_number(unit), "001")
        unit.header = "protect: unit 20"
        self.assertEqual(document._unit_number(unit), "20")
        self.assertEqual(document._unit_nodes(interface), [unit])
        unit.header = "inactive: unit 20"
        self.assertEqual(document._unit_nodes(interface), [])
        unit.header = "unit 20"
        unit.active = False
        self.assertEqual(document._unit_nodes(interface), [])
        unit.active = True
        unit.header = "description unit 20;"
        self.assertEqual(document._unit_nodes(interface), [])
        added = junos.JunosNode("unit 30", [])
        interface.children.append(added)
        self.assertEqual(document._unit_nodes(interface), [added])
        interface.children.remove(added)
        self.assertEqual(document._unit_nodes(interface), [])

    def test_junos_vlan_queries_follow_header_state_and_children_edits(self):
        document = junos.JunosDocument("")
        first = junos.JunosNode("vlan-id 100;")
        tags = junos.JunosNode("protect: vlan-tags outer 200 inner 20;")
        unit = junos.JunosNode("unit 10", [first, tags])

        def check(outer, inner):
            self.assertEqual(document._find_vlan(unit), outer)
            self.assertEqual(document._find_inner_vlan(unit), inner)

        check(100, 20)
        first.header = "vlan-id 0;"
        check(0, 20)
        first.active = False
        check(200, 20)
        tags.header = "inactive: vlan-tags outer 200 inner 20;"
        check(None, None)
        tags.header = "vlan-tags outer 300 inner 30;"
        check(300, 30)
        tags.active = False
        check(None, None)
        tags.active = True
        unit.children.remove(tags)
        check(None, None)
        unit.children.append(tags)
        check(300, 30)
        clone = unit.clone()
        clone.children[-1].header = "vlan-tags outer 400 inner 40;"
        self.assertEqual(document._find_vlan(clone), 400)
        self.assertEqual(document._find_inner_vlan(clone), 40)
        check(300, 30)
        unit.children = None
        check(None, None)


if __name__ == "__main__":
    unittest.main()
