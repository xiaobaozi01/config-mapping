"""Candidate filtering and LRU must preserve complete legacy matching results."""

import gc
import random
import weakref
from pathlib import Path

import pytest

from gns3_config_adaptor.impl.common.semantic_identity import (
    MergeKind,
    SemanticResolver,
    SemanticRule,
    StatementContext,
)
from gns3_config_adaptor.impl.common.semantic_identity.matcher import (
    match_rule,
    rule_specificity,
)
from gns3_config_adaptor.impl.common.support.optimization_cache import (
    optimization_scope,
)


class Reference(SemanticResolver):

    def _matches(self, context):
        return [
            (rule_specificity(rule), rule, captures)
            for rule in self._rules
            if (captures := match_rule(rule, context)) is not None
        ]


class Counted(SemanticResolver):

    def __init__(self, rules, **kwargs):
        super().__init__(rules, **kwargs)
        self.calls = 0

    def _resolve_uncached(self, context):
        self.calls += 1
        return super()._resolve_uncached(context)


def rule(
    name="x",
    statement=("set", "{value:word}"),
    path=("**",),
    kind="leaf",
    identity=("{value}",),
):
    return SemanticRule(
        name, kind, path, statement, identity, MergeKind.REPLACE
    )


def outcome(resolver, context):
    try:
        return resolver._resolve_uncached(context)
    except ValueError as error:
        return type(error), str(error)


def test_lru_crosses_old_capacity_without_clearing_hot_results():
    resolver = Counted((rule(),))
    contexts = [
        StatementContext("test", ("root",), f"set {i}") for i in range(18000)
    ]
    with optimization_scope(64 * 2**20) as cache:
        for context in contexts:
            resolver.resolve(context)
        assert resolver.calls == 18000
        for context in contexts[:1000]:
            resolver.resolve(context)
        assert resolver.calls == 18000
        assert cache.stats["semantic"]["hits"] == 1000
    assert not cache.entries
    with optimization_scope(64 * 2**20):
        resolver.resolve(contexts[0])
    assert resolver.calls == 18001


def test_candidate_filter_preserves_capture_first_empty_and_ambiguity():
    rules = (
        rule("literal"),
        rule("capture", ("{verb:word}", "{value:word}")),
        rule("rest", ("{value...}",)),
        rule("block", ("container", "{value:word}"), kind="block"),
        rule("tie-one", ("tie", "{value:word}")),
        rule("tie-two", ("tie", "{value:word}")),
    )
    optimized, reference = SemanticResolver(rules), Reference(rules)
    for text in (
        "",
        "set 123",
        "other words",
        'set "quoted value"',
        "tie v",
        "container x",
    ):
        for kind in ("leaf", "block", "unknown"):
            context = StatementContext("test", ("nested", "root"), text, kind)
            assert outcome(optimized, context) == outcome(reference, context)


def test_path_first_errors_are_not_suppressed_by_literal_prefilter():
    rules = (rule("bad", ("unrelated", "{value}"), ("{name:unsupported}",)),)
    context = StatementContext("test", ("root",), "set v")
    actual = outcome(SemanticResolver(rules), context)
    assert actual == outcome(Reference(rules), context)
    assert actual[0] is ValueError


def test_same_rule_ids_and_different_packs_do_not_share_results():
    first = SemanticResolver((rule(identity=("one",)),))
    second = SemanticResolver((rule(identity=("two",)),))
    context = StatementContext("test", (), "set value")
    with optimization_scope():
        assert first.resolve(context).identity == ("one",)
        assert second.resolve(context).identity == ("two",)
        assert first.resolve(context).identity == ("one",)


def test_task_release_drops_input_and_static_resolvers_do_not_retain_it():
    class Sensitive(str):
        pass

    resolver = SemanticResolver((rule(),), cache_budget_bytes=0)
    text = Sensitive("set sensitive-value")
    weak = weakref.ref(text)
    with optimization_scope() as cache:
        context = StatementContext("test", (), text)
        resolver.resolve(context)
        del context, text
        assert weak() is not None
    gc.collect()
    assert weak() is None
    assert not resolver._cache.entries and not cache.entries


@pytest.mark.parametrize("budget", [0, 512, 4096])
def test_budget_pressure_never_changes_results_or_caches_exceptions(budget):
    rules = (rule(), rule("tie", statement=("set", "{value:word}")))
    optimized, reference = Counted(rules), Reference(rules)
    with optimization_scope(budget) as cache:
        for i in range(100):
            ctx = StatementContext("test", (), f"set {i}")
            assert outcome(optimized, ctx) == outcome(reference, ctx)
            with pytest.raises(ValueError):
                optimized.resolve(ctx)
        assert cache.snapshot()["estimated_peak_bytes"] <= budget


def test_seeded_paths_capture_constraints_and_wildcard_backtracking():
    rules = tuple(
        rule(
            f"r{i}",
            (f"cmd{i}", "{value:uint}"),
            ("**", "group {name:word}", "**"),
        )
        for i in range(30)
    )
    rules = (
        *rules,
        rule("shared", ("{name:word}", "{value...}"), ("group {name:word}",)),
    )
    actual, expected = SemanticResolver(rules), Reference(rules)
    randomizer = random.Random(83)
    for _ in range(1000):
        name = randomizer.randrange(35)
        ctx = StatementContext(
            "test",
            ("root", f"group g{name}", "child"),
            f"cmd{name} {randomizer.randrange(100)}",
        )
        assert outcome(actual, ctx) == outcome(expected, ctx)


def test_pack_fixture_contexts_match_unfiltered_reference():
    from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
        CiscoDocument,
    )
    from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.support.identity import (
        _resolver as cisco_resolver,
    )
    from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document import (
        JunosDocument,
    )
    from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.support.identity import (
        _resolver as junos_resolver,
    )

    root = Path(__file__).parent / "fixtures" / "semantic_identity"
    for path in root.glob("*.cfg"):
        cisco = path.name.startswith(("iosxr", "cisco"))
        doc = (CiscoDocument if cisco else JunosDocument)(path.read_text())
        optimized = (cisco_resolver if cisco else junos_resolver)()
        expected = Reference(optimized._rules)
        for node in doc.root.walk():
            ctx = StatementContext(
                doc.vendor,
                node.path,
                node.header,
                "block" if node.is_block else "leaf",
            )
            assert outcome(optimized, ctx) == outcome(expected, ctx)
