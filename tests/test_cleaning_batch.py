"""验证两轮清洗、首条命中语义及与独立参考遍历的差分结果。"""

from __future__ import annotations

import copy
import gc
import itertools
import random
import re
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config_adaptor.adaptation.cleaning_rules import (
    CleaningPoint,
    CleaningResult,
    CleaningRulesHandler,
    ConfiguredCleaningRule,
)
from config_adaptor.adaptation.models import ConversionContext, Vendor
from config_adaptor.cisco.document import CiscoDocument, CiscoNode
from config_adaptor.common.cleaning import (
    CleaningOperation,
    compile_cleaning_rules,
    matches_cleaning_path,
)
from config_adaptor.juniper.document import JunosDocument


def reference_clean(document, operations):
    """朴素两轮参考：每个节点逐条检查规则，不使用编译路径或候选缓存。"""
    counts = [0] * len(operations)
    is_cisco = isinstance(document, CiscoDocument)

    def normalized(node):
        if is_cisco:
            return node.header.strip()
        return JunosDocument._base_header(node.header).rstrip(";").strip()

    for action in ("delete", "replace"):
        def visit(node, ancestors):
            if is_cisco:
                if not node.active or node.is_formatting:
                    return
            elif not node.effective:
                return
            header = normalized(node)
            for index, operation in enumerate(operations):
                if operation.action != action:
                    continue
                if not matches_cleaning_path(ancestors, operation.path):
                    continue
                pattern = re.compile(operation.match, re.IGNORECASE)
                if not pattern.search(header):
                    continue
                counts[index] += 1
                if action == "delete":
                    node.active = False
                    return
                target = node.header if is_cisco else header
                node.header = pattern.sub(operation.value or "", target)
                if not is_cisco and not node.is_block:
                    node.header += ";"
                break
            for child in node.children or []:
                visit(child, (*ancestors, normalized(node)))

        if any(operation.action == action for operation in operations):
            for node in document.root.children or []:
                visit(node, ())
    return counts


class CleaningBatchTest(unittest.TestCase):
    def test_delete_precedes_replace_and_counts_follow_original_input_order(self):
        operations = [
            CleaningOperation((), "^A$", "replace", "B"),
            CleaningOperation((), "^B$", "delete"),
            CleaningOperation((), "^B$", "replace", "C"),
            CleaningOperation((), "^OLD$", "delete"),
        ]
        for cls, source, expected in [
            (CiscoDocument, "A\nB\nOLD\n", "B\n"),
            (JunosDocument, "A; B; OLD;", "B;\n"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                self.assertEqual(document.apply_cleaning_rules(operations), [1, 1, 0, 1])
                self.assertEqual(document.render(), expected)

    def test_parent_delete_skips_children_even_when_child_rule_is_earlier(self):
        operations = [
            CleaningOperation(("**",), "^child$", "delete"),
            CleaningOperation((), "^parent$", "delete"),
            CleaningOperation((), "^parent$", "delete"),
        ]
        for cls, source in [
            (CiscoDocument, "parent\n child\n"),
            (JunosDocument, "parent { child; }"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                self.assertEqual(document.apply_cleaning_rules(operations), [0, 1, 0])
                parent = document.root.children[0]
                self.assertFalse(parent.active)
                self.assertTrue(parent.children[0].active)

    def test_replacement_stops_on_first_match_even_when_value_is_unchanged(self):
        for cls, source in [(CiscoDocument, "A\n"), (JunosDocument, "A;")]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                counts = document.apply_cleaning_rules([
                    CleaningOperation((), "^A$", "replace", "A"),
                    CleaningOperation((), "^A$", "replace", "B"),
                ])
                self.assertEqual(counts, [1, 0])
                self.assertIn("A", document.render())
                self.assertNotIn("B", document.render())

    def test_separate_batches_recompute_candidates_for_their_own_rules(self):
        for cls, source, expected in [
            (CiscoDocument, "A\n", "C\n"),
            (JunosDocument, "A;", "C;\n"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                self.assertEqual(document.apply_cleaning_rules([
                    CleaningOperation((), "^A$", "replace", "B"),
                ]), [1])
                self.assertEqual(document.apply_cleaning_rules([
                    CleaningOperation((), "^B$", "replace", "C"),
                ]), [1])
                self.assertEqual(document.render(), expected)

    def test_phase_caches_release_without_waiting_for_cyclic_gc(self):
        for cls, source, module in [
            (CiscoDocument, "A\nOLD\n", "config_adaptor.cisco.document"),
            (JunosDocument, "A; OLD;", "config_adaptor.juniper.document"),
        ]:
            with self.subTest(vendor=cls.vendor):
                references = []

                def track_compilation(operations):
                    phases = compile_cleaning_rules(operations)
                    references.extend(weakref.ref(phase) for phase in phases)
                    return phases

                enabled = gc.isenabled()
                gc.disable()
                try:
                    document = cls(source)
                    parser = weakref.ref(document._parser)
                    with patch(f"{module}.compile_cleaning_rules", side_effect=track_compilation):
                        self.assertEqual(document.apply_cleaning_rules([
                            CleaningOperation((), "^OLD$", "delete"),
                            CleaningOperation((), "^A$", "replace", "B"),
                        ]), [1, 1])
                    self.assertEqual(len(references), 2)
                    self.assertTrue(all(reference() is None for reference in references))
                    del document
                    self.assertIsNone(parser())
                finally:
                    if enabled:
                        gc.enable()

    def test_replaced_parent_provides_new_path_for_children(self):
        operations = [
            CleaningOperation((), "^old$", "replace", "new"),
            CleaningOperation(("^old$",), "^child$", "replace", "wrong"),
            CleaningOperation(("^new$",), "^child$", "replace", "changed"),
        ]
        for cls, source in [
            (CiscoDocument, "old\n child\nold\n child\n"),
            (JunosDocument, "old { child; } old { child; }"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                self.assertEqual(document.apply_cleaning_rules(operations), [2, 0, 2])
                self.assertEqual(document.render().count("changed"), 2)
                self.assertNotIn("wrong", document.render())

    def test_formatting_inactive_protect_and_leaf_semicolons(self):
        operations = [CleaningOperation(("**",), "old", "replace", "new")]
        cisco = CiscoDocument("! old comment\n\nparent\n old\n!\n")
        self.assertEqual(cisco.apply_cleaning_rules(operations), [1])
        self.assertIn("! old comment", cisco.render())
        self.assertIn(" new\n", cisco.render())

        junos = JunosDocument(
            "inactive: old { old; } protect: old { protect: old; } # old comment\n"
        )
        self.assertEqual(junos.apply_cleaning_rules(operations), [2])
        self.assertEqual(junos.root.children[0].header, "inactive: old")
        self.assertEqual(junos.root.children[0].children[0].header, "old;")
        self.assertEqual(junos.root.children[1].header, "new")
        self.assertEqual(junos.root.children[1].children[0].header, "new;")
        self.assertIn("# old comment", junos.render())

    def test_invalid_batch_is_rejected_before_mutating_the_tree(self):
        for cls, source in [(CiscoDocument, "old\n"), (JunosDocument, "old;")]:
            for action in ("warn", "mask", "unknown"):
                with self.subTest(vendor=cls.vendor, action=action):
                    document = cls(source)
                    before = copy.deepcopy(document.root)
                    with self.assertRaisesRegex(ValueError, "不支持的清洗动作"):
                        document.apply_cleaning_rules([
                            CleaningOperation((), "old", "delete"),
                            CleaningOperation((), "old", action),
                        ])
                    self.assertEqual(document.root, before)
            document = cls(source)
            with self.assertRaisesRegex(ValueError, "必须提供 value"):
                document.apply_cleaning_rules([CleaningOperation((), "old", "replace")])
            self.assertTrue(document.root.children[0].active)
            self.assertEqual(document.apply_cleaning_rules([]), [])

    def test_invalid_replacement_templates_fail_before_delete_even_without_a_match(self):
        for cls, source in [(CiscoDocument, "OLD\nA\n"), (JunosDocument, "OLD; A;")]:
            for template in (r"\2", r"\g<missing>", r"\q", "trailing\\"):
                for pattern in ("^A$", "^never-matches$"):
                    with self.subTest(vendor=cls.vendor, template=template, pattern=pattern):
                        document = cls(source)
                        before = copy.deepcopy(document.root)
                        with self.assertRaisesRegex(ValueError, "第 2 条.*无效替换模板"):
                            document.apply_cleaning_rules([
                                CleaningOperation((), "^OLD$", "delete"),
                                CleaningOperation((), pattern, "replace", template),
                            ])
                        self.assertEqual(document.root, before)

    def test_non_string_replacement_fails_before_mutation(self):
        for cls, source in [(CiscoDocument, "OLD\nA\n"), (JunosDocument, "OLD; A;")]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                before = copy.deepcopy(document.root)
                with self.assertRaisesRegex(ValueError, "value 必须是字符串"):
                    document.apply_cleaning_rules([
                        CleaningOperation((), "^OLD$", "delete"),
                        CleaningOperation((), "^A$", "replace", 123),
                    ])
                self.assertEqual(document.root, before)

    def test_valid_replacement_backreferences_remain_supported(self):
        for cls, source, expected in [
            (CiscoDocument, "A\n", "A-new\n"),
            (JunosDocument, "A;", "A-new;\n"),
        ]:
            for pattern, template in [("(A)", r"\1-new"), ("(?P<name>A)", r"\g<name>-new")]:
                with self.subTest(vendor=cls.vendor, template=template):
                    document = cls(source)
                    self.assertEqual(document.apply_cleaning_rules([
                        CleaningOperation((), pattern, "replace", template),
                    ]), [1])
                    self.assertEqual(document.render(), expected)

    def test_single_rule_compatibility_and_empty_replacement(self):
        for cls, source, expected in [
            (CiscoDocument, "old-value\n", "-value"),
            (JunosDocument, "old-value;", "-value;"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                self.assertEqual(document.apply_cleaning_rule((), "old", "replace", ""), 1)
                self.assertEqual(document.root.children[0].header, expected)

    def test_single_rule_preserves_legacy_none_as_empty_replacement(self):
        for cls, source, expected in [
            (CiscoDocument, "old-value\n", "-value"),
            (JunosDocument, "old-value;", "-value;"),
        ]:
            with self.subTest(vendor=cls.vendor):
                document = cls(source)
                self.assertEqual(document.apply_cleaning_rule((), "old", "replace", None), 1)
                self.assertEqual(document.root.children[0].header, expected)

    def test_one_hundred_rules_visit_each_node_only_twice(self):
        operations = [
            CleaningOperation((), f"^missing-{index}$", action, "replacement")
            for index in range(50) for action in ("delete", "replace")
        ]
        cisco = CiscoDocument("\n".join(f"node-{index}" for index in range(20)))
        visited = []
        original = CiscoNode.is_formatting.fget
        with patch.object(CiscoNode, "is_formatting", property(
            lambda node: visited.append(node) or original(node)
        )):
            self.assertEqual(cisco.apply_cleaning_rules(operations), [0] * 100)
        self.assertEqual(len(visited), 40)

        junos = JunosDocument(" ".join(f"node-{index};" for index in range(20)))
        with patch.object(junos._parser, "is_effective", wraps=junos._parser.is_effective) as visit:
            self.assertEqual(junos.apply_cleaning_rules(operations), [0] * 100)
            self.assertEqual(visit.call_count, 40)


class CompiledCleaningPathTest(unittest.TestCase):
    def test_compiled_paths_match_existing_wildcard_regex_semantics(self):
        paths = [()]
        ancestors = [()]
        for depth in range(1, 4):
            paths.extend(itertools.product(("*", "**", "^a$", "b"), repeat=depth))
            ancestors.extend(itertools.product(("a", "B", "other"), repeat=depth))
        for path in paths:
            phase = compile_cleaning_rules([CleaningOperation(path, ".", "delete")])[0]
            for current in ancestors:
                with self.subTest(path=path, ancestors=current):
                    self.assertEqual(bool(phase.candidates(current)), matches_cleaning_path(current, path))


class CleaningBatchDifferentialTest(unittest.TestCase):
    def test_fixtures_and_seeded_mixed_rules_match_uncached_two_pass_reference(self):
        fixtures = Path(__file__).parent / "fixtures"
        cases = [
            (CiscoDocument, fixtures / "washing_configs/iosxr.cfg"),
            (CiscoDocument, fixtures / "type_constraints/iosxr.cfg"),
            (JunosDocument, fixtures / "washing_configs/junos.cfg"),
            (JunosDocument, fixtures / "type_constraints/junos.cfg"),
            (JunosDocument, fixtures / "interfaces/junos_inactive_interfaces_units.cfg"),
        ]
        configured = CleaningRulesHandler._configured_rules()
        randomizer = random.Random(20261027)
        for cls, path in cases:
            text = path.read_text(encoding="utf-8")
            native = [
                CleaningOperation(rule.path, rule.match, rule.action, rule.value)
                for rule in configured if rule.vendor == cls.vendor and rule.enabled
            ]
            for iteration in range(12):
                operations = native + [
                    CleaningOperation(
                        randomizer.choice(((), ("**",), ("*",), ("^interfaces$", "**"), ("^interface",))),
                        randomizer.choice(("^description", "authentication", "old", "^mtu", "^unit")),
                        randomizer.choice(("delete", "replace")),
                        randomizer.choice(("changed", "", r"\g<0>-changed")),
                    )
                    for _ in range(12)
                ]
                randomizer.shuffle(operations)
                with self.subTest(vendor=cls.vendor, fixture=path.name, iteration=iteration):
                    actual, expected = cls(text), cls(text)
                    self.assertEqual(actual.apply_cleaning_rules(operations), reference_clean(expected, operations))
                    self.assertEqual(actual.root, expected.root)
                    self.assertEqual(actual.render(), expected.render())


class CleaningBatchHandlerTest(unittest.TestCase):
    @staticmethod
    def rule(rule_id, action, match, value=None, **changes):
        fields = dict(
            rule_id=rule_id, vendor=Vendor.CISCO_IOSXR.value, enable=True,
            category="test", path=(), match=match, action=action, reason="test", value=value,
        )
        fields.update(changes)
        return ConfiguredCleaningRule(**fields)

    @staticmethod
    def context(document):
        return ConversionContext(topology=None, devices={
            "R1": SimpleNamespace(
                document=document, device=SimpleNamespace(vendor=Vendor.CISCO_IOSXR), errors=[],
            ),
        })

    def test_handler_submits_one_batch_and_audits_phase_order_before_code_rules(self):
        document = CiscoDocument("A\nOLD\n")
        handler = CleaningRulesHandler(CleaningPoint.POST_REWRITE)
        code = SimpleNamespace(
            enabled=True, point=CleaningPoint.POST_REWRITE, vendor=document.vendor,
            rule_id="code", category="test", action="transform", reason="test",
            apply=Mock(return_value=CleaningResult(changed=1)),
        )
        handler._rules = [
            self.rule("replace", "replace", "^A$", "B"),
            self.rule("disabled", "delete", "A", enable=False),
            self.rule("other-vendor", "delete", "A", vendor=Vendor.JUNIPER_JUNOS.value),
            self.rule("delete", "delete", "^OLD$"),
            self.rule("no-cascade", "replace", "^B$", "C"),
            code,
        ]
        context = self.context(document)
        with (
            patch.object(document, "apply_cleaning_rules", wraps=document.apply_cleaning_rules) as bulk,
            patch.object(document, "apply_cleaning_rule", side_effect=AssertionError("single-rule dispatch")),
        ):
            handler.process(context)
            bulk.assert_called_once()
        self.assertEqual(document.render(), "B\n")
        self.assertEqual([event["rule_id"] for event in context.events], ["delete", "replace", "code"])
        self.assertEqual([event["hits"] for event in context.events], [1, 1, 1])
        code.apply.assert_called_once_with(document)

    def test_code_rule_input_errors_still_short_circuit(self):
        handler = CleaningRulesHandler(CleaningPoint.PRE_ANALYSIS)
        context = self.context(CiscoDocument("banner exec ^\nunterminated\n"))
        handler.process(context)
        self.assertEqual(len(context.errors), 1)
        self.assertEqual(context.events[0]["kind"], "cleaning-rule-error")
        self.assertEqual(context.events[0]["rule_id"], "cisco.remove.banner")

    def test_yaml_rejects_removed_actions_and_default_rules_contain_no_warn(self):
        self.assertTrue(all(rule.action in {"delete", "replace"} for rule in CleaningRulesHandler._configured_rules()))
        for action in ("warn", "mask"):
            raw = dict(
                id="test", vendor="cisco_iosxr", enable=True, category="test",
                path=[], match=".", action=action, reason="test",
            )
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "不支持的动作"):
                CleaningRulesHandler._parse_rule(raw, set())

    def test_yaml_rejects_invalid_templates_with_rule_id_before_execution(self):
        for template in (r"\2", r"\g<missing>", r"\q", "trailing\\"):
            with self.subTest(template=template):
                raw = dict(
                    id="invalid.template", vendor="cisco_iosxr", enable=True, category="test",
                    path=[], match="^A$", action="replace", value=template, reason="test",
                )
                with self.assertRaisesRegex(ValueError, r"cisco\.invalid\.template.*无效替换模板"):
                    CleaningRulesHandler._parse_rule(raw, set())

    def test_invalid_programmatic_template_does_not_mutate_document_in_handler(self):
        document = CiscoDocument("OLD\nA\n")
        before = copy.deepcopy(document.root)
        handler = CleaningRulesHandler(CleaningPoint.POST_REWRITE)
        handler._rules = [
            self.rule("delete", "delete", "^OLD$"),
            self.rule("invalid", "replace", "^A$", r"\2"),
        ]
        context = self.context(document)
        with self.assertRaisesRegex(ValueError, "无效替换模板"):
            handler.process(context)
        self.assertEqual(document.root, before)
        self.assertEqual(context.events, [])


if __name__ == "__main__":
    unittest.main()
