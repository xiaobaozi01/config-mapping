"""验证手写 Parser、Editor、遍历和回滚约定。"""

import builtins
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from impl.cisco.document import CiscoDocument
from impl.common.config_ast import (
    ConfigListener,
    ConfigNode,
    ConfigTreeWalker,
    ConfigVisitor,
)
from impl.common.config_ast.editor import EditConflictError
from impl.common.config_ast.model import tree_signature
from impl.common.domain.outcomes import InterfaceAdaptationOutcome
from impl.juniper.document import JunosDocument
from impl.juniper.rules.runtime import JunosVisitorRule, RuleContractError


@pytest.mark.parametrize(
    "document_type,source",
    [
        (
            CiscoDocument,
            "hostname A\r\ninterface Gi0\r\n\tdescription test\r\n!\r\n",
        ),
        (
            JunosDocument,
            'system { host-name "a;{b}"; /* { } */ services { ssh; } }',
        ),
        (
            JunosDocument,
            '# before\ninterfaces { protect: inactive: ge-0/0/0 { description "a\\"b"; } }',
        ),
        (JunosDocument, 'system { description "a\nb"; /* multi\n{;} */ }'),
    ],
)
def test_canonical_roundtrip(document_type, source):
    document = document_type(source)
    canonical = document.source
    assert document_type(canonical).source == canonical
    before = tree_signature(document.root)
    document.render()
    assert tree_signature(document.root) == before
    assert "\r" not in canonical


@pytest.mark.parametrize(
    "source",
    [
        "group G\n interface Gi0\n",
        "end-group\n",
        "banner motd #\nunclosed\n",
    ],
)
def test_cisco_rejects_unclosed_structures(source):
    with pytest.raises(ValueError, match="行"):
        CiscoDocument(source)


@pytest.mark.parametrize(
    "source",
    [
        "system {",
        "system }",
        "system { host-name x }",
        'system { description "unclosed; }',
        "/* unclosed",
    ],
)
def test_junos_rejects_unclosed_structures(source):
    with pytest.raises(ValueError, match="行"):
        JunosDocument(source)


def test_banner_is_opaque_and_preserved():
    source = "banner motd ^C\ninterface FAKE\nend-group\n  ! body\n^C\nhostname REAL\n"
    document = CiscoDocument(source)
    assert document.hostname() == "REAL"
    assert document.root.nodes[0].kind == "opaque"
    assert document.root.nodes[0].nodes == ()
    assert document.source == source


def test_listener_and_visitor_order_and_pruning():
    root = ConfigNode(
        "root",
        [ConfigNode("a", [ConfigNode("hidden")]), ConfigNode("b")],
        kind="root",
    )
    events = []

    class Listener(ConfigListener):

        def enter_node(self, node):
            events.append(("enter", node.header))

        def exit_node(self, node):
            events.append(("exit", node.header))

    ConfigTreeWalker().walk(Listener(), root)
    assert events == [
        ("enter", "root"),
        ("enter", "a"),
        ("enter", "hidden"),
        ("exit", "hidden"),
        ("exit", "a"),
        ("enter", "b"),
        ("exit", "b"),
        ("exit", "root"),
    ]
    visited = []

    class Visitor(ConfigVisitor):

        def should_visit(self, node):
            return node.header != "a"

        def visit_node(self, node):
            visited.append(node.header)
            return self.visit_children(node)

    root.accept(Visitor())
    assert visited == ["b"]


def test_deep_tree_walk_and_clone_do_not_recurse():
    root = ConfigNode("root", [], kind="root")
    parent = root
    for _ in range(3000):
        child = ConfigNode("nested", [])
        parent.children.append(child)
        child.parent = parent
        parent = child
    cloned = root.clone()
    assert len(list(cloned.walk())) == 3000
    ConfigTreeWalker().walk(ConfigListener(), cloned)


@pytest.mark.parametrize(
    "document_type,source,fragment",
    [
        (
            CiscoDocument,
            "interface Gi0\n description a\n description b\ninterface Gi1\n",
            "mtu 9000\n",
        ),
        (
            JunosDocument,
            "interfaces { ge-0/0/0 { description a; mtu 100; } ge-0/0/1 {} }",
            "mtu 9000;",
        ),
    ],
)
def test_editor_delete_append_move_clone_and_ownership(
    document_type, source, fragment
):
    document = document_type(source)
    container = (
        document.root
        if document_type is CiscoDocument
        else document.root.nodes[0]
    )
    first, second = container.nodes
    editor = document.edit()
    for child in first.nodes:
        editor.delete(child, "remove consecutive nodes")
    editor.append_child(first, fragment, "new child")
    editor.apply()
    assert len(first.nodes) == 1
    assert first.nodes[0].location is None
    assert first.nodes[0].parent is first
    editor = document.edit()
    cloned = editor.clone(first.nodes[0], second, "copy")
    editor.apply()
    assert cloned is not first.nodes[0]
    cloned.header = "mtu 8000"
    assert first.nodes[0].header == "mtu 9000"
    editor = document.edit()
    editor.move(cloned, first, "move")
    editor.apply()
    assert cloned.parent is first and cloned not in second.children
    with pytest.raises(EditConflictError):
        document.edit().move(first, cloned, "cycle")
    with pytest.raises(EditConflictError):
        document.edit().delete(document_type(source).root.nodes[0], "foreign")
    editor = document.edit()
    editor.delete(cloned, "detach")
    editor.apply()
    with pytest.raises(EditConflictError):
        document.edit().delete(cloned, "already removed")


def test_editor_failed_batch_restores_tree_and_rejects_old_references():
    document = CiscoDocument("interface Gi0\n description old\n")
    parent = document.root.nodes[0]
    original = document.source
    editor = document.edit()
    editor.delete(parent, "parent")
    editor.delete(parent.nodes[0], "now detached")
    with pytest.raises(EditConflictError):
        editor.apply()
    assert document.source == original
    with pytest.raises(EditConflictError):
        document.edit().delete(parent, "old reference")


def test_editor_preview_is_repeatable_and_detached():
    document = JunosDocument("system { host-name old; }")
    editor = document.edit()
    editor.replace_header(
        document.root.nodes[0].nodes[0], "host-name new", "rename"
    )
    before = document.source
    first = editor.preview()
    assert editor.preview() == first
    assert document.source == before
    editor.apply()
    assert document.source == first


@pytest.mark.parametrize("readonly,raises", [(False, True), (True, False)])
def test_junos_document_action_failure_restores_configuration(readonly, raises):
    document = JunosDocument("system { host-name old; }")
    original = document.source
    node = document.root.nodes[0].nodes[0]

    def action(doc, context):
        doc.root.nodes[0].nodes[0].header = "host-name changed"
        if raises:
            raise RuntimeError("failed")
        return InterfaceAdaptationOutcome()

    rule = JunosVisitorRule("test", document_action=action, readonly=readonly)
    with pytest.raises((RuntimeError, RuleContractError)):
        rule.apply(document, None)
    assert document.source == original
    with pytest.raises(EditConflictError):
        document.edit().delete(node, "stale")


def test_parsers_do_not_import_external_runtime_or_launch_processes():
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("antlr4"):
            raise AssertionError("external parser runtime imported")
        return original_import(name, *args, **kwargs)

    with (
        patch("builtins.__import__", guarded),
        patch(
            "subprocess.Popen", side_effect=AssertionError("external process")
        ),
    ):
        cisco = CiscoDocument("hostname A\n")
        junos = JunosDocument("system { host-name B; }")
        assert cisco.hostname() == "A" and junos.hostname() == "B"
        assert deepcopy(cisco).source == cisco.source
    assert not list(Path("src").rglob("*.g4"))


def test_queued_moves_cannot_create_cycles():
    document = CiscoDocument("interface Gi0\ninterface Gi1\n")
    first, second = document.root.nodes
    editor = document.edit()
    editor.move(first, second, "first")
    editor.move(second, first, "second")
    original = document.source
    with pytest.raises(EditConflictError):
        editor.apply()
    assert document.source == original


def test_insert_after_preserves_declaration_order():
    document = CiscoDocument("hostname A\n")
    editor = document.edit()
    editor.insert_after(document.root.nodes[0], "one\n", "first")
    editor.insert_after(document.root.nodes[0], "two\n", "second")
    editor.apply()
    assert document.source == "hostname A\none\ntwo\n"


def test_cisco_cache_tracks_mutable_headers():
    document = CiscoDocument("interface Gi0/0/0/0\n")
    assert document.interface_specs()[0].name == "GigabitEthernet0/0/0/0"
    document.root.nodes[0].header = "interface Loopback0"
    assert document.interface_specs()[0].name == "Loopback0"


def test_banner_body_preserves_trailing_spaces_and_fragment_whitespace():
    source = "banner motd #first  \n  second  \n#\n"
    document = CiscoDocument(source)
    assert document.source == source
    editor = document.edit()
    editor.append_child(
        document.root, "banner login #\n  body  \n#\n", "banner"
    )
    editor.apply()
    assert "  body  \n" in document.source


def test_source_positions_are_input_positions():
    cisco = CiscoDocument("interface Gi0\n  description x\n")
    junos = JunosDocument("system {\n    host-name x;\n}\n")
    assert (
        cisco.root.nodes[0].nodes[0].location.line,
        cisco.root.nodes[0].nodes[0].location.column,
    ) == (2, 3)
    assert (
        junos.root.nodes[0].nodes[0].location.line,
        junos.root.nodes[0].nodes[0].location.column,
    ) == (2, 5)
