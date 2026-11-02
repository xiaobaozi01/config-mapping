"""Incremental tracking must preserve transactional and direct-write behavior."""

import gc
import weakref
from copy import deepcopy
from unittest.mock import patch

import pytest

from impl.cisco.document import CiscoDocument
from impl.cisco.rules.runtime import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    RuleContractError,
)
from impl.common.config_ast.editor import EditConflictError
from impl.common.config_ast.model import (
    ConfigNode,
    SourceLocation,
    tree_signature,
)
from impl.juniper.document import JunosDocument
from impl.juniper.rules.runtime import JunosRuleVisitor, JunosVisitorRule


@pytest.fixture(params=["cisco", "juniper"])
def document(request):
    return (
        CiscoDocument("hostname old\ninterface Gi0\n mtu 9000\n!\n")
        if request.param == "cisco"
        else JunosDocument(
            "system { host-name old; } interfaces { ge-0/0/0 { mtu 9000; } }"
        )
    )


def test_nested_independent_rollback_then_outer_rollback(document):
    source, revision = document.source, document.revision
    with pytest.raises(RuntimeError, match="outer"):
        with document.edit().scope(independent=True):
            with document.edit().bulk() as root:
                root.nodes[0].comment = "outer change"
            outer = document.source
            with pytest.raises(RuntimeError, match="inner"):
                with document.edit().scope(independent=True):
                    with document.edit().bulk() as root:
                        root.nodes[-1].active = False
                    raise RuntimeError("inner")
            assert document.source == outer
            with document.edit().bulk() as root:
                root.nodes[0].comment = "after inner rollback"
            raise RuntimeError("outer")
    assert document.source == source
    assert document.revision == revision
    assert document._edit_scope is None
    assert not document._tracker.journal


def test_nested_independent_rollback_can_commit_outer(document):
    with document.edit().scope(independent=True):
        with document.edit().bulk() as root:
            root.nodes[0].comment = "keep"
        with pytest.raises(RuntimeError):
            with document.edit().scope(independent=True):
                root.nodes[0].comment = "discard"
                raise RuntimeError()
        assert document.root.nodes[0].comment == "keep"
    assert document.root.nodes[0].comment == "keep"
    assert document.revision == 1


def test_root_replacement_and_nested_rollback(document):
    original = document.source
    with pytest.raises(RuntimeError):
        with document.edit().scope(independent=True):
            document.edit().replace_root(document.root.clone())
            with document.edit().bulk() as root:
                root.nodes[0].comment = "replacement"
            with pytest.raises(RuntimeError):
                with document.edit().scope(independent=True):
                    document.edit().replace_root(document.root.clone())
                    raise RuntimeError()
            raise RuntimeError()
    assert document.source == original
    assert document.revision == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("header", "different"),
        ("kind", "group"),
        ("comment", "comment"),
        ("active", False),
        ("origin", "inherited"),
        ("rank", (0, 1)),
        ("declared_in_source", True),
        ("location", SourceLocation(123)),
        ("parent", None),
    ],
)
def test_all_fields_are_reversible(document, field, value):
    before = tree_signature(document.root)
    node = document.root.nodes[-1]
    with pytest.raises(RuntimeError):
        with document.edit().bulk():
            setattr(node, field, value)
            raise RuntimeError()
    assert tree_signature(document.root) == before
    with pytest.raises(EditConflictError):
        document.edit().delete(node, "stale")


def mutate_list(children, operation):
    if operation == "append":
        children.append(children[0].clone())
    elif operation == "extend":
        children.extend([children[0].clone(), children[-1].clone()])
    elif operation == "insert":
        children.insert(-1, children[0].clone())
    elif operation == "pop":
        children.pop()
    elif operation == "remove":
        children.remove(children[0])
    elif operation == "clear":
        children.clear()
    elif operation == "item":
        children[-1] = children[0].clone()
    elif operation == "slice":
        children[1:0] = [children[0].clone()]
    elif operation == "stepped":
        children[::-1] = list(children)
    elif operation == "delete":
        del children[::-2]
    elif operation == "reverse":
        children.reverse()
    elif operation == "sort":
        children.sort(key=lambda node: node.header, reverse=True)
    elif operation == "iadd":
        children += [children[0].clone()]
    elif operation == "imul":
        children *= 0
    else:
        raise AssertionError(operation)


@pytest.mark.parametrize(
    "operation",
    [
        "append",
        "extend",
        "insert",
        "pop",
        "remove",
        "clear",
        "item",
        "slice",
        "stepped",
        "delete",
        "reverse",
        "sort",
        "iadd",
        "imul",
    ],
)
def test_children_mutations_commit_and_rollback(document, operation):
    before = tree_signature(document.root)
    with pytest.raises(RuntimeError):
        with document.edit().bulk() as root:
            mutate_list(root.children, operation)
            raise RuntimeError()
    assert tree_signature(document.root) == before
    with document.edit().bulk() as root:
        mutate_list(root.children, operation)
    assert all(node.parent is document.root for node in document.root.children)
    assert document._tracker.edges == set(
        document.root.walk(include_self=True, include_formatting=True)
    )


def test_noop_scope_and_net_zero_edits_keep_revision(document):
    before = document.revision
    with document.edit().bulk() as root:
        root.children += []
        root.children *= 1
        root.children.reverse()
        root.children.reverse()
        old = root.nodes[0].comment
        root.nodes[0].comment = "temporary"
        root.nodes[0].comment = old
    assert document.revision == before


def test_direct_writes_refresh_warm_queries_without_tree_changed(document):
    specs = document.interfaces.analysis.interface_specs()
    assert specs
    node = next(
        node
        for node in document.root.nodes
        if node.header.startswith("interface")
    )
    if isinstance(document, JunosDocument):
        node = node.nodes[0]
    node.active = False
    assert not document.interfaces.analysis.interface_specs()
    node.active = True
    assert document.interfaces.analysis.interface_specs() == specs


def test_shared_and_cyclic_edges_are_rejected_and_restored(document):
    before = document.source
    for insert in (
        lambda root: root.children.append(root),
        lambda root: root.children.append(root.children[0]),
        lambda root: root.children.__imul__(2),
    ):
        with pytest.raises(ValueError, match=r"cycle|shared"):
            with document.edit().bulk() as root:
                insert(root)
        assert document.source == before


def test_successful_rules_and_warm_queries_do_not_scan_or_clone(document):
    cisco = isinstance(document, CiscoDocument)
    visitor_type = CiscoRuleVisitor if cisco else JunosRuleVisitor
    rule_type = CiscoVisitorRule if cisco else JunosVisitorRule

    class Visitor(visitor_type):

        def visit_node(self, node):
            self.session.edits.replace_header(
                node, "hostname new" if cisco else "host-name new", "rename"
            )

    target = document.root.nodes[0]
    if not cisco:
        target = target.nodes[0]
    rule = rule_type(
        "test", visitor_factory=Visitor, selector=lambda doc, ctx: (target,)
    )
    document.interfaces.analysis.interface_specs()
    with (
        patch.object(ConfigNode, "clone", side_effect=AssertionError("clone")),
        patch.object(ConfigNode, "walk", side_effect=AssertionError("walk")),
        patch(
            "impl.common.config_ast.model.tree_signature",
            side_effect=AssertionError("signature"),
        ),
    ):
        rule.apply(document, None)
        # No interface mutations: warm a query after the new revision, then
        # verify repeated cache reads without any AST traversal.
    document.interfaces.analysis.interface_specs()
    with patch.object(ConfigNode, "walk", side_effect=AssertionError("walk")):
        document.interfaces.analysis.interface_specs()


def test_readonly_rejects_children_root_and_metadata_writes(document):
    cisco = isinstance(document, CiscoDocument)
    visitor_type = CiscoRuleVisitor if cisco else JunosRuleVisitor
    rule_type = CiscoVisitorRule if cisco else JunosVisitorRule
    before = document.source
    for mutation in (
        lambda doc: doc.root.children.clear(),
        lambda doc: setattr(doc.root, "rank", (0, 0)),
        lambda doc: setattr(doc, "root", doc.root.clone()),
    ):

        class Visitor(visitor_type):

            def visit_node(self, node):
                mutation(self.session.document)

        with pytest.raises((RuleContractError, RuntimeError), match="Editor"):
            rule_type("readonly", visitor_factory=Visitor, readonly=True).apply(
                document, None
            )
        assert document.source == before


def test_removed_subtrees_do_not_accumulate_and_copy_is_independent(document):
    node = document.root.children[-1]
    reference = weakref.ref(node)
    with document.edit().bulk() as root:
        root.children.remove(node)
    del node
    gc.collect()
    assert reference() is None
    clone = deepcopy(document)
    with clone.edit().bulk() as root:
        root.nodes[0].comment = "copy only"
    assert clone.root.nodes[0].comment != document.root.nodes[0].comment


def test_retired_children_alias_is_not_an_edge_in_the_live_tree(document):
    alias = document.root.children
    original = document.source
    with pytest.raises(RuntimeError):
        with document.edit().bulk() as root:
            root.children = []
            alias.append(root.clone())
            assert not root.children
            raise RuntimeError()
    assert document.source == original
    with document.edit().bulk() as root:
        alias = root.children
        root.children = []
        orphan = ConfigNode("orphan")
        alias.append(orphan)
    assert not document._tracker.belongs(orphan)


def test_invalid_list_indices_do_not_mutate_or_invalidate(document):
    children = document.root.children
    epoch = document.mutation_epoch
    for index in (-len(children) - 1, len(children)):
        with pytest.raises(IndexError):
            children[index] = ConfigNode("invalid")
        with pytest.raises(IndexError):
            del children[index]
    assert document.mutation_epoch == epoch


def test_failed_direct_container_writes_leave_ownership_unchanged(document):
    foreign_document = CiscoDocument("hostname foreign\n")
    foreign = foreign_document.root
    original, epoch = document.source, document.mutation_epoch
    for invalid in (foreign, None):
        new = ConfigNode("new")
        with pytest.raises((ValueError, AttributeError)):
            document.root.children.extend([new, invalid])
        assert document.source == original
        assert document.mutation_epoch == epoch
        assert new._tracker() is None
        assert not document._tracker.belongs(new)
        assert document._tracker.edges == set(
            document.root.walk(include_self=True, include_formatting=True)
        )


def test_warm_queries_have_no_signature_or_rebinding_calls(document):
    import cProfile
    import pstats

    query = document.interfaces.analysis.interface_specs
    expected = query()
    profile = cProfile.Profile()
    assert profile.runcall(query) == expected
    for (_, _, name), stats in pstats.Stats(profile).stats.items():
        if name in {
            "tree_signature",
            "_tree_signature",
            "bind_parents",
            "clone",
        }:
            assert stats[1] == 0


def test_header_validation_only_binds_detached_fragments(document):
    from impl.common.config_ast import bind_parents

    def bind_fragment(root):
        assert root is not document.root
        assert len(root.children) == 1
        return bind_parents(root)

    node = document.root.nodes[0]
    header = "hostname new"
    if isinstance(document, JunosDocument):
        node, header = node.nodes[0], "host-name new"
    with patch(
        "impl.common.config_ast.bind_parents", side_effect=bind_fragment
    ):
        editor = document.edit()
        editor.replace_header(node, header, "rename")
        editor.apply()
    assert document.hostname() == "new"


def test_untracked_protocol_nodes_never_reuse_stale_analysis():
    from impl.common.support.interface_cache import InterfaceAnalysisCache

    class PlainNode:
        __slots__ = ("header",)

        def __init__(self):
            self.header = "old"

    root = PlainNode()
    cache = InterfaceAnalysisCache()
    assert cache.read(root, "header", lambda: root.header, str) == "old"
    root.header = "new"
    assert cache.read(root, "header", lambda: root.header, str) == "new"
