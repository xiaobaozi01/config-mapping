"""ANTLR 结构解析、原文保留和事务重写的行为契约。"""

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from antlr4 import ParseTreeWalker

from impl.cisco.antlr.generated.CiscoParser import CiscoParser
from impl.cisco.antlr.parsing import ConfigurationParseError
from impl.cisco.document import CiscoDocument
from impl.cisco.rules.support import (
    CiscoParserListener,
    CiscoParserVisitor,
    RewriteConflictError,
    select_interfaces,
)
from impl.common.interface import InterfaceKind
from impl.common.rules import AdaptContext, FunctionRule, execute_rules

FIXTURES = Path(__file__).parent / "fixtures" / "antlr_cisco"


def document(name="editing.cfg"):
    return CiscoDocument((FIXTURES / name).read_bytes().decode())


class AntlrParsingTest(unittest.TestCase):

    def test_generated_contexts_preserve_raw_text_unicode_tabs_and_crlf(self):
        source = (FIXTURES / "format.cfg").read_bytes().decode()
        config = CiscoDocument(source)
        self.assertIsInstance(config.root, CiscoParser.ConfigurationContext)
        self.assertEqual(config.source, source)
        self.assertEqual(config.render(), source)
        self.assertEqual(config.snapshot.tokens.getText(), source)
        block = next(node for node in config.root.nodes if node.interface_name)
        self.assertIsInstance(block, CiscoParser.NodeContext)
        self.assertEqual(block.nodes[0].header, "description  keep   spaces")
        self.assertEqual(block.nodes[0].indent, 8)
        self.assertEqual(block.nodes[0].path, ("interface Gi0/0/0/0",))
        self.assertIs(block.nodes[0].parentCtx, block)

    def test_banner_body_is_opaque_to_interface_and_group_analysis(self):
        config = document("banner.cfg")
        self.assertEqual(config._interface_nodes(), [])
        banner = config.root.nodes[1]
        self.assertEqual(banner.nodes, ())
        self.assertIn("end-group", banner.text)
        self.assertEqual(config.hostname(), "AFTER")
        from adaptation.cleaning_rules import CiscoBannerCleaningRule

        self.assertEqual(CiscoBannerCleaningRule().apply(config).changed, 1)
        self.assertNotIn("FAKE", config.source)
        self.assertIn("hostname AFTER", config.source)

    def test_inline_banners_and_delimiter_after_body_text(self):
        from adaptation.cleaning_rules import CiscoBannerCleaningRule

        source = "banner login #hello world#\nbanner motd ^Chello^C\nhostname AFTER\n"
        config = CiscoDocument(source)
        self.assertEqual(len(config.root.nodes), 3)
        self.assertEqual(config.render(), source)
        self.assertEqual(CiscoBannerCleaningRule().apply(config).changed, 1)
        self.assertNotIn("banner login", config.source)
        self.assertIn("banner motd ^Chello^C", config.source)
        multiline = CiscoDocument(
            "banner exec #\ninterface FAKE\nlast body text#\nhostname AFTER\n"
        )
        self.assertEqual(multiline._interface_nodes(), [])
        self.assertEqual(multiline.hostname(), "AFTER")

    def test_group_unindented_body_and_nested_comments_keep_hierarchy(self):
        source = "group G\ninterface 'Gi.*'\n mtu 9000\n!\nend-group\n!\nend\n"
        config = CiscoDocument(source)
        group = config.root.nodes[0]
        self.assertEqual(group.nodes[0].header, "interface 'Gi.*'")
        self.assertEqual(group.nodes[0].nodes[0].header, "mtu 9000")
        self.assertEqual(config.render(), source)
        config = CiscoDocument(
            "router ospf 1\n area 0\n  interface Loopback0\n   cost 10\n  ! note\n   hello-interval 5\n!\n"
        )
        interface = config.root.nodes[0].nodes[0].nodes[0]
        self.assertIn(
            "hello-interval 5", [node.header for node in interface.nodes]
        )

    def test_structural_errors_include_position_and_unknown_commands_survive(
        self,
    ):
        for source in (
            "end-group\n",
            "group G\n interface Gi0\n",
            "banner exec ^\nunclosed\n",
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(
                    ConfigurationParseError, r"行 \d+ 列 \d+"
                ),
            ):
                CiscoDocument(source)
        config = CiscoDocument(
            'future-feature "{ value }"\n nested-new-command anything\n'
        )
        self.assertIn('future-feature "{ value }"', config.render())

    def test_standard_visitor_and_listener_traverse_generated_tree(self):
        config = document()
        events = []

        class Listener(CiscoParserListener):

            def enterNode(self, context):
                events.append(("enter", context.header))

            def exitNode(self, context):
                events.append(("leave", context.header))

        ParseTreeWalker.DEFAULT.walk(Listener(), config.root)
        self.assertEqual(
            events[:3],
            [
                ("enter", "parent"),
                ("enter", "child-a"),
                ("enter", "grandchild"),
            ],
        )
        seen = []

        class Visitor(CiscoParserVisitor):

            def visitNode(self, context):
                seen.append(context.header)
                if context.header == "parent":
                    return None
                return self.visitChildren(context)

        Visitor().visit(config.root)
        self.assertEqual(seen, ["parent", "sibling", "end"])

    def test_context_views_do_not_offer_mutable_node_fields(self):
        context = document().root.nodes[0]
        for field, value in (
            ("header", "changed"),
            ("nodes", ()),
            ("active", False),
        ):
            with self.subTest(field=field), self.assertRaises(AttributeError):
                setattr(context, field, value)

    def test_group_expansion_preserves_unaffected_blocks_byte_for_byte(self):
        config = document("group_preserve.cfg")
        untouched = [
            node.text
            for node in config.root.nodes
            if node.header.startswith(("hostname", "router static"))
        ]
        self.assertTrue(
            config.expand_groups(["GigabitEthernet0/0/0/0"]).success
        )
        for fragment in untouched:
            self.assertIn(fragment, config.source)
        self.assertIn(" mtu 9000\r\n", config.source)
        self.assertIn("!! untouched comment\r\n", config.source)
        self.assertTrue(config.source.endswith("end"))
        self.assertNotIn("apply-group", config.source)
        self.assertNotIn("end-group", config.source)


class AntlrRewriteTest(unittest.TestCase):

    def test_header_replacement_preserves_children_and_untouched_bytes(self):
        config = document("format.cfg")
        before = config.source
        block = next(node for node in config.root.nodes if node.interface_name)
        transaction = config.rewrite()
        transaction.replace_header(block, "interface Gi0/0/0/1", "rename")
        self.assertEqual(config.source, before)
        result = transaction.commit()
        self.assertEqual(result.total, 1)
        self.assertEqual(config.revision, 1)
        self.assertEqual(
            config.source,
            before.replace("interface Gi0/0/0/0", "interface Gi0/0/0/1"),
        )
        self.assertEqual(
            config.interface_specs()[0].name, "GigabitEthernet0/0/0/1"
        )

    def test_adjacent_deletes_and_whole_subtree_replacement(self):
        config = document()
        parent, sibling, end = config.root.nodes
        transaction = config.rewrite()
        transaction.delete(parent.nodes[0], "remove first child subtree")
        transaction.delete(parent.nodes[1], "remove adjacent sibling")
        transaction.replace_subtree(
            sibling, "replacement\n nested\n", "replace subtree"
        )
        self.assertEqual(transaction.commit().total, 3)
        self.assertEqual(config.source, "parent\nreplacement\n nested\nend\n")

    def test_insertion_order_is_declaration_order(self):
        config = CiscoDocument("a\nend\n")
        a = config.root.nodes[0]
        transaction = config.rewrite()
        transaction.insert_before(a, "before1", "first")
        transaction.insert_before(a, "before2", "second")
        transaction.insert_after(a, "after1", "third")
        transaction.insert_after(a, "after2", "fourth")
        transaction.append_child(a, "child1", "fifth")
        transaction.append_child(a, "child2", "sixth")
        transaction.commit()
        self.assertEqual(
            config.source,
            "before1\nbefore2\na\n child1\n child2\nafter1\nafter2\nend\n",
        )

    def test_append_child_and_move_reindent_and_reparse(self):
        config = document()
        parent, sibling, _ = config.root.nodes
        source = parent.nodes[0]
        transaction = config.rewrite()
        transaction.delete(source, "move source")
        transaction.append_child(sibling, source.text, "move target")
        transaction.commit()
        self.assertEqual(
            [node.header for node in config.root.nodes[0].nodes], ["child-b"]
        )
        moved = config.root.nodes[1].nodes[0]
        self.assertEqual(moved.header, "child-a")
        self.assertEqual(moved.nodes[0].header, "grandchild")
        self.assertEqual(moved.path, ("sibling",))

    def test_adjacent_move_into_empty_target_is_not_an_overlap(self):
        config = CiscoDocument("parent\n nested\n ipv4 address 192.0.2.1/31\n")
        nested, source = config.root.nodes[0].nodes
        transaction = config.rewrite()
        transaction.delete(source, "move source")
        transaction.append_child(nested, source.text, "move target")
        transaction.commit()
        nested = config.root.nodes[0].nodes[0]
        self.assertEqual(nested.nodes[0].header, "ipv4 address 192.0.2.1/31")
        self.assertEqual(nested.nodes[0].path, ("parent", "nested"))

    def test_empty_unknown_block_mode_survives_commit_and_deepcopy(self):
        from impl.common.empty_blocks import EmptyBlockPolicy

        config = CiscoDocument("custom-block\n child\ntrack object LEAF\n")
        transaction = config.rewrite()
        transaction.delete(config.root.nodes[0].nodes[0], "empty block")
        transaction.commit()
        self.assertTrue(config.root.nodes[0].is_block)
        self.assertFalse(config.root.nodes[1].is_block)
        with patch(
            "impl.cisco.document.xrv9000_empty_block_policy",
            return_value=EmptyBlockPolicy("omit", ()),
        ):
            self.assertEqual(config.render(), "track object LEAF\n")
            self.assertEqual(copy.deepcopy(config).render(), config.render())
        self.assertEqual(config.source, "custom-block\ntrack object LEAF\n")

    def test_same_eof_insert_order_without_original_final_newline(self):
        config = CiscoDocument("hostname BEFORE")
        transaction = config.rewrite()
        transaction.append_child(config.root, "hostname FIRST", "first")
        transaction.append_child(config.root, "hostname SECOND", "second")
        transaction.commit()
        self.assertEqual(
            config.source, "hostname BEFORE\nhostname FIRST\nhostname SECOND\n"
        )

    def test_overlap_and_foreign_contexts_are_rejected_without_mutation(self):
        config = document()
        snapshot = config.snapshot
        transaction = config.rewrite()
        transaction.delete(config.root.nodes[0], "ancestor")
        with self.assertRaises(RewriteConflictError):
            transaction.replace_header(
                config.root.nodes[0].nodes[0], "changed", "descendant"
            )
        with self.assertRaises(RewriteConflictError):
            transaction.delete(document().root.nodes[0], "foreign")
        self.assertIs(config.snapshot, snapshot)
        for action in ("delete", "replace_header", "replace_subtree"):
            with (
                self.subTest(action=action),
                self.assertRaises(RewriteConflictError),
            ):
                getattr(config.rewrite(), action)(
                    config.root,
                    *(["changed"] if action != "delete" else []),
                    "root",
                )

    def test_stale_context_transaction_and_reuse_are_rejected(self):
        config = document()
        old_context = config.root.nodes[0]
        old_transaction = config.rewrite()
        fresh = config.rewrite()
        fresh.replace_header(old_context, "changed", "fresh")
        fresh.commit()
        with self.assertRaises(RewriteConflictError):
            old_transaction.commit()
        with self.assertRaises(RewriteConflictError):
            config.rewrite().delete(old_context, "stale context")
        with self.assertRaises(RewriteConflictError):
            fresh.commit()

    def test_candidate_parse_failure_does_not_publish_or_report(self):
        config = document()
        snapshot = config.snapshot
        transaction = config.rewrite()
        transaction.delete(config.root.nodes[0].nodes[0], "valid edit")
        transaction.replace_header(
            config.root.nodes[1], "group UNTERMINATED", "invalid candidate"
        )
        with self.assertRaises(ConfigurationParseError):
            transaction.commit()
        self.assertIs(config.snapshot, snapshot)
        self.assertEqual(config.revision, 0)

    def test_commit_failure_keeps_source_even_after_rewriter_ran(self):
        config = document()
        snapshot = config.snapshot
        transaction = config.rewrite()
        transaction.replace_header(config.root.nodes[0], "changed", "test")
        with patch(
            "impl.cisco.antlr.rewriting.CiscoSnapshot",
            side_effect=RuntimeError("failure"),
        ):
            with self.assertRaises(RuntimeError):
                transaction.commit()
        self.assertIs(config.snapshot, snapshot)
        self.assertEqual(transaction.commit().total, 1)

    def test_noops_keep_snapshot_and_revision(self):
        config = document()
        snapshot = config.snapshot
        transaction = config.rewrite()
        transaction.replace_header(
            config.root.nodes[0], "parent", "same header"
        )
        transaction.replace_subtree(
            config.root.nodes[1], config.root.nodes[1].text, "same subtree"
        )
        transaction.append_child(config.root, "", "empty")
        self.assertEqual(transaction.commit().total, 0)
        self.assertIs(config.snapshot, snapshot)
        self.assertEqual(config.revision, 0)

    def test_noop_header_keeps_original_trailing_spaces(self):
        config = document("format.cfg")
        snapshot = config.snapshot
        description = next(
            node
            for node in config.root.walk()
            if node.header.startswith("description")
        )
        transaction = config.rewrite()
        transaction.replace_header(description, description.header, "same")
        self.assertEqual(transaction.commit().total, 0)
        self.assertIs(config.snapshot, snapshot)
        with self.assertRaises(ValueError):
            config.rewrite().replace_subtree(description, description.text, "")

    def test_insert_rejects_large_foreign_context_before_indexing(self):
        config = CiscoDocument("hostname X\n")
        foreign = document().root.nodes[-1]
        for method in ("insert_before", "insert_after"):
            with (
                self.subTest(method=method),
                self.assertRaises(RewriteConflictError),
            ):
                getattr(config.rewrite(), method)(foreign, "new", "foreign")

    def test_new_nodes_are_visited_only_by_subsequent_rules(self):
        config = CiscoDocument("hostname BEFORE\n")
        observations = []

        def insert(document, context):
            transaction = document.rewrite()
            for node in document.root.nodes:
                observations.append(node.header)
                transaction.insert_after(node, "description INSERTED", "insert")
            return transaction.commit()

        def observe(document, context):
            observations.extend(node.header for node in document.root.nodes)
            return None

        execute_rules(
            (FunctionRule("insert", insert), FunctionRule("observe", observe)),
            config,
            AdaptContext(frozenset()),
        )
        self.assertEqual(
            observations,
            ["hostname BEFORE", "hostname BEFORE", "description INSERTED"],
        )

    def test_scope_normalizes_aliases_and_excludes_unspecified_kinds(self):
        config = CiscoDocument(
            "interface Gi0/0/0/0\n!\ninterface Gi0/0/0/0.100\n!\ninterface Bundle-Ether10\n!\ninterface BVI10\n"
        )
        kinds = frozenset({InterfaceKind.PHYSICAL})
        selected = select_interfaces(
            config, {"Gi0/0/0/0.100"}, include_subinterfaces=True, kinds=kinds
        )
        self.assertEqual(
            [node.interface_name for node in selected],
            ["GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/0.100"],
        )
        self.assertEqual(
            select_interfaces(
                config, set(), include_subinterfaces=True, kinds=kinds
            ),
            (),
        )
        self.assertEqual(
            select_interfaces(
                config,
                {"Bundle-Ether10", "BVI10"},
                include_subinterfaces=True,
                kinds=kinds,
            ),
            (),
        )
        self.assertEqual(
            len(
                select_interfaces(
                    config,
                    {"Gi0/0/0/0"},
                    include_subinterfaces=False,
                    kinds=kinds,
                )
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
