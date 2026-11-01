"""物理父口和 unit 引用完整改写，接口定义不重复迁移。"""

import unittest
from pathlib import Path

from adaptation.interfaces import ReferenceRewriteHandler
from adaptation.models import (
    ConversionContext,
    Device,
    DeviceContext,
    Link,
    TopologyWorkbook,
    Vendor,
)
from adaptation.nni import NNIHandler
from adaptation.pipeline import build_default_pipeline
from adaptation.profiles import load_profiles
from impl.juniper.document import JunosDocument

FIXTURES = Path(__file__).parent / "fixtures" / "reference_rewrite"


def nni_context():
    device = Device("A", Vendor.JUNIPER_JUNOS, None, 2)
    peer = Device("P", Vendor.HUAWEI, None, 3)
    path = FIXTURES / "junos_nni.cfg"
    document = JunosDocument(path.read_text())
    profile = load_profiles(None)[Vendor.JUNIPER_JUNOS]
    topology = TopologyWorkbook(
        Path("unused.xlsx"),
        None,
        "devices",
        "links",
        {},
        {},
        [device, peer],
        [
            Link(2, "link-2", "A", "ge-0/0/7", "P", "Ethernet0"),
            Link(3, "link-3", "A", "ge-0/0/8", "P", "Ethernet1"),
        ],
    )
    return ConversionContext(
        topology,
        {"A": DeviceContext(device, path, document, profile)},
        external_devices={"P"},
    )


class JunosReferenceRewriteTest(unittest.TestCase):

    def test_reference_rewriter_is_owned_by_reference_service(self):
        from impl.juniper import document, references

        self.assertFalse(hasattr(document, "_ReferenceRewriter"))
        self.assertEqual(
            references._ReferenceRewriter.__module__, references.__name__
        )

    def test_nni_generates_parent_and_unit_mappings(self):
        context = nni_context()
        NNIHandler().process(context)
        self.assertFalse(context.has_errors, context.errors)
        replacements = context.devices["A"].replacement_map
        self.assertEqual(replacements["ge-0/0/7"], ["ge-0/0/0"])
        self.assertEqual(replacements["ge-0/0/7.0"], ["ge-0/0/0.0"])
        self.assertEqual(replacements["ge-0/0/8"], ["ge-0/0/1"])
        self.assertEqual(replacements["ge-0/0/8.100"], ["ge-0/0/1.100"])
        ReferenceRewriteHandler().process(context)
        output = context.devices["A"].document.render()
        self.assertIn("unnumbered-address ge-0/0/0.0;", output)
        self.assertIn("underlying-interface ge-0/0/0;", output)
        self.assertIn("inactive: interface ge-0/0/1.100;", output)
        self.assertNotIn("ge-0/0/7", output)
        self.assertNotIn("ge-0/0/8", output)

    def test_full_pipeline_rewrites_interface_internal_and_external_references(
        self,
    ):
        context = nni_context()
        build_default_pipeline().execute(context)
        device = context.devices["A"]
        self.assertFalse(context.has_errors, context.errors + device.errors)
        output = device.document.render()
        self.assertIn("unnumbered-address ge-0/0/0.0;", output)
        self.assertIn("underlying-interface ge-0/0/0;", output)
        self.assertIn("interface ge-0/0/0.0;", output)
        self.assertNotIn("ge-0/0/7", output)
        self.assertNotIn("ge-0/0/8", output)

    def test_definitions_are_not_rewritten_but_contents_and_cos_are(self):
        document = JunosDocument(
            (FIXTURES / "junos_definition_and_references.cfg").read_text()
        )
        document.replace_references(
            {
                "ge-0/0/0": ["ge-0/0/1"],
                "ge-0/0/0.0": ["ge-0/0/1.0"],
                "ge-0/0/1": ["ge-0/0/2"],
                "ge-0/0/1.0": ["ge-0/0/2.0"],
            }
        )
        definitions = document._interfaces_block().nodes
        self.assertEqual(
            [node.header for node in definitions],
            ["ge-0/0/1", "ge-0/0/2", "demux0", "inactive: ge-0/0/0"],
        )
        first = document.writer.effective(definitions[0], document._parser)
        self.assertIn("unnumbered-address ge-0/0/1.0;", first)
        self.assertNotIn("unnumbered-address ge-0/0/2.0;", first)
        output = document.render()
        self.assertIn("underlying-interface ge-0/0/1;", output)
        self.assertIn("underlying-interface ge-0/0/1.0;", output)
        cos = next(
            node
            for node in document.root.nodes
            if node.header == "class-of-service"
        )
        self.assertEqual(cos.nodes[0].nodes[0].header, "ge-0/0/1")
        self.assertIn("# ge-0/0/0.0 is a comment", output)

    def test_one_to_many_internal_references_preserve_subtrees(self):
        document = JunosDocument(
            (FIXTURES / "junos_definition_and_references.cfg").read_text()
        )
        document.replace_references(
            {"ge-0/0/0.0": ["ge-0/0/3.0", "ge-0/0/4.0"]}
        )
        output = document.render()
        self.assertIn("unnumbered-address ge-0/0/3.0;", output)
        self.assertIn("unnumbered-address ge-0/0/4.0;", output)
        self.assertIn("underlying-interface ge-0/0/3.0;", output)
        self.assertIn("underlying-interface ge-0/0/4.0;", output)
        self.assertEqual(output.count("description TARGET-ONE;"), 1)

    def test_deleted_interface_subtree_is_not_resurrected_by_references(self):
        document = JunosDocument(
            (FIXTURES / "junos_definition_and_references.cfg").read_text()
        )
        node = document._interfaces_block().nodes[0]
        transaction = document.edit()
        transaction.delete(node, "test deletion")
        transaction.apply()
        document.replace_references({"ge-0/0/0.0": ["ge-0/0/5.0"]})
        self.assertIsNone(document._find_interface_node("ge-0/0/0"))
        self.assertIn("inactive: ge-0/0/0", document.source)


if __name__ == "__main__":
    unittest.main()
