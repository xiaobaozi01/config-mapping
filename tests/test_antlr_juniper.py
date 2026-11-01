"""验证 Junos ANTLR4 结构解析、快照隔离和原文保留行为。"""

from __future__ import annotations

import unittest
from pathlib import Path

from impl.common.rules import AdaptContext
from impl.juniper.antlr.parsing import JunosConfigurationParseError
from impl.juniper.antlr.rewriting import RewriteConflictError
from impl.juniper.document import JunosDocument
from impl.juniper.rules import (
    ADAPT_RULES,
    FINAL_INTEGRITY_VALIDATION_RULES,
    PRUNE_RULES,
    SIMULATION_RULES,
    SOURCE_SEMANTIC_VALIDATION_RULES,
    VALIDATION_RULES,
)
from impl.juniper.rules.runtime import (
    JunosRuleVisitor,
    JunosVisitorRule,
    RuleContractError,
)

FIXTURES = Path(__file__).parent / "fixtures"


class JunosAntlrParsingTest(unittest.TestCase):
    """覆盖 Junos 大括号结构及不透明命令文本。"""

    def test_nested_inline_blocks_build_read_only_context_tree(self):
        source = (
            'system { host-name "lab;{vmx}"; services { ssh; } }\n'
            "interfaces { ge-0/0/0 { unit 0; } }\n"
        )

        document = JunosDocument(source)
        system = document.root.nodes[0]

        self.assertEqual(document.source, source)
        self.assertEqual(document.snapshot.tokens.getText(), source)
        self.assertEqual(document.render(), source)
        self.assertEqual(document.revision, 0)
        self.assertEqual(system.header, "system")
        self.assertEqual(system.nodes[0].header, 'host-name "lab;{vmx}";')
        self.assertEqual(system.nodes[1].nodes[0].header, "ssh;")
        interfaces = document.root.nodes[1]
        self.assertEqual(interfaces.nodes[0].path, ("interfaces",))
        self.assertEqual(
            interfaces.nodes[0].nodes[0].path, ("interfaces", "ge-0/0/0")
        )
        with self.assertRaises(AttributeError):
            system.header = "changed"

    def test_comments_and_escaped_quotes_do_not_change_structure(self):
        source = (
            "system {\n"
            "  # ignored { block; }\n"
            '  host-name "lab\\"#1"; # host note\n'
            "  /* ignored\n"
            "     { nested; }\n"
            "  */\n"
            "}\n"
        )

        document = JunosDocument(source)
        system = document.root.nodes[0]

        self.assertEqual(
            [node.header for node in system.nodes if node.header],
            ['host-name "lab\\"#1";'],
        )
        self.assertEqual(document.render(), source)

    def test_render_preserves_tabs_and_missing_final_newline(self):
        source = 'system {\n\thost-name "实验室";\n}'
        document = JunosDocument(source)

        self.assertEqual(document.render(), source)
        transaction = document.rewrite()
        transaction.replace_header(
            document.root.nodes[0].nodes[0],
            'host-name "after";',
            "rename",
        )
        transaction.commit()
        self.assertEqual(
            document.render(),
            'system {\n\thost-name "after";\n}',
        )

    def test_all_junos_fixture_configurations_parse(self):
        parsed = 0
        for path in FIXTURES.rglob("*.cfg"):
            if not (
                path.name.startswith("junos")
                or "junos_ast" in path.parts
                or any(part.startswith("junos_") for part in path.parts)
            ):
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                JunosDocument(text)
            parsed += 1
        self.assertGreaterEqual(parsed, 50)

    def test_unbalanced_blocks_report_original_line(self):
        with self.assertRaisesRegex(
            JunosConfigurationParseError,
            "第 3 行出现多余右大括号",
        ):
            JunosDocument(
                "system { host-name lab; }\n" "# comment with }\n" "}\n"
            )
        with self.assertRaisesRegex(
            JunosConfigurationParseError,
            "大括号不平衡",
        ):
            JunosDocument("system {\n host-name lab;\n")


class JunosAntlrRewriteTest(unittest.TestCase):
    """覆盖 Junos 事务发布和旧 Context 失效。"""

    def test_header_replacement_preserves_untouched_source_and_publishes(self):
        source = (
            "system {\r\n"
            "  host-name old;\r\n"
            "}\r\n"
            "interfaces {\r\n"
            "  ge-0/0/0 { unit 0; }\r\n"
            "}\r\n"
        )
        document = JunosDocument(source)
        system = document.root.nodes[0]
        transaction = document.rewrite()
        transaction.replace_header(system.nodes[0], "host-name new;", "rename")

        self.assertEqual(document.source, source)
        self.assertEqual(transaction.preview(), source.replace("old", "new"))
        transaction.commit()

        self.assertEqual(document.revision, 1)
        self.assertEqual(document.source, source.replace("old", "new"))
        self.assertIn("ge-0/0/0 { unit 0; }", document.source)
        with self.assertRaises(RewriteConflictError):
            document.rewrite().replace_header(
                system.nodes[0], "host-name stale;", "stale"
            )

    def test_noop_commit_keeps_snapshot_and_revision(self):
        document = JunosDocument("system { host-name lab; }\n")
        snapshot = document.snapshot
        transaction = document.rewrite()
        transaction.replace_header(
            document.root.nodes[0].nodes[0],
            "host-name lab;",
            "same",
        )

        self.assertEqual(transaction.commit().total, 0)
        self.assertIs(document.snapshot, snapshot)
        self.assertEqual(document.revision, 0)

    def test_overlap_and_stale_contexts_do_not_publish(self):
        document = JunosDocument("parent { child; }\n")
        snapshot = document.snapshot
        parent = document.root.nodes[0]
        transaction = document.rewrite()
        transaction.delete(parent, "delete parent")
        with self.assertRaises(RewriteConflictError):
            transaction.replace_header(parent.nodes[0], "child new;", "overlap")
        self.assertIs(document.snapshot, snapshot)

        fresh = document.rewrite()
        fresh.replace_header(parent, "renamed", "rename")
        fresh.commit()
        with self.assertRaises(RewriteConflictError):
            document.rewrite().delete(parent, "stale")

    def test_interface_rename_preserves_untouched_sibling_source(self):
        source = (
            "interfaces {\n"
            "  ge-0/0/0 {\n"
            "    description old;\n"
            "  }\n"
            "  ge-0/0/1 { unit 0; } # keep\n"
            "}\n"
        )
        document = JunosDocument(source)

        document.rename_interface_tree("ge-0/0/0", "ge-0/0/2")

        self.assertIn(
            "  ge-0/0/1 { unit 0; } # keep\n",
            document.source,
        )
        self.assertIn("  ge-0/0/2 {", document.source)


class JunosVisitorRuntimeTest(unittest.TestCase):
    """验证 Junos Visitor 规则统一收集和提交编辑。"""

    def test_visitor_edit_is_committed_once(self):
        class RenameVisitor(JunosRuleVisitor):

            def visitStatement(self, context):
                if context.header == "old;":
                    self.session.edits.replace_header(
                        context, "new;", "visitor rename"
                    )
                return self.visitChildren(context)

        rule = JunosVisitorRule(
            "juniper.test.rename",
            visitor_factory=RenameVisitor,
        )
        document = JunosDocument("old;\n")

        result = rule.apply(document, AdaptContext(frozenset()))

        self.assertEqual(document.source, "new;\n")
        self.assertEqual(document.revision, 1)
        self.assertEqual(result.changes[0]["rule_id"], "juniper.test.rename")

    def test_readonly_visitor_cannot_request_edits(self):
        class InvalidVisitor(JunosRuleVisitor):

            def visitConfiguration(self, context):
                _ = context
                _edits = self.session.edits
                return None

        rule = JunosVisitorRule(
            "juniper.test.readonly",
            visitor_factory=InvalidVisitor,
            readonly=True,
        )
        with self.assertRaises(RuleContractError):
            rule.apply(
                JunosDocument("system { host-name lab; }\n"),
                AdaptContext(frozenset()),
            )

    def test_builtin_registries_use_visitor_rules(self):
        registries = (
            PRUNE_RULES,
            ADAPT_RULES,
            SIMULATION_RULES,
            VALIDATION_RULES,
            SOURCE_SEMANTIC_VALIDATION_RULES,
            FINAL_INTEGRITY_VALIDATION_RULES,
        )
        for registry in registries:
            for rule in registry:
                with self.subTest(rule_id=rule.rule_id):
                    self.assertIsInstance(rule, JunosVisitorRule)


if __name__ == "__main__":
    unittest.main()
