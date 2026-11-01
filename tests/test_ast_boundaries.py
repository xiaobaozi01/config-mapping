"""校验 AST、Document、编辑和输出职责边界。"""

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from impl.cisco.document import CiscoDocument
from impl.cisco.rules.runtime import CiscoRuleVisitor, CiscoVisitorRule
from impl.common.config_ast.editor import EditConflictError
from impl.common.config_ast.model import ConfigNode, tree_signature
from impl.common.domain.outcomes import InterfaceAdaptationOutcome
from impl.juniper.document import JunosDocument
from impl.juniper.rules.runtime import JunosVisitorRule


@pytest.mark.parametrize("vendor", ["cisco", "juniper"])
def test_nodes_have_no_document_or_output_dependencies(vendor):
    module = ast.parse(
        Path(f"src/impl/{vendor}/syntax/ast.py").read_text()
    )
    imports = [
        node.module
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
    ]
    assert not any(
        name and name.split(".")[-1] in {"document", "writer", "editor"}
        for name in imports
    )
    document = (
        CiscoDocument("hostname A\n")
        if vendor == "cisco"
        else JunosDocument("system { host-name A; }")
    )
    assert not hasattr(document.root, "text")
    assert document.writer.subtree(document.root) == document.source


@pytest.mark.parametrize(
    "document_type,source",
    [
        (CiscoDocument, "interface Loopback0\n ! note\n!\n"),
        (JunosDocument, "protect: system { services { ssh; } }"),
    ],
)
def test_render_never_edits_or_clones(document_type, source):
    document = document_type(source)
    before, revision = tree_signature(document.root), document.revision
    with (
        patch.object(
            document, "edit", side_effect=AssertionError("output used editor")
        ),
        patch.object(
            ConfigNode,
            "clone",
            side_effect=AssertionError("output copied tree"),
        ),
    ):
        assert document.render() == document.render()
    assert tree_signature(document.root) == before
    assert document.revision == revision


def test_junos_queries_never_enter_editing_scopes():
    document = JunosDocument(
        "interfaces { ge-0/0/0 { unit 0 { family inet { address 192.0.2.1/30; } } } }"
    )
    with patch.object(
        document, "edit", side_effect=AssertionError("query used editor")
    ):
        assert document.interfaces.analysis.interface_specs()
        document.interfaces.analysis.bundle_members()
        document.interfaces.analysis.business_interface_names()
        document.interfaces.analysis.logical_names_under("ge-0/0/0")
        document.interfaces.analysis.interface_has_business("ge-0/0/0")
        document.interfaces.analysis.bundle_binding_is_ambiguous(
            "ge-0/0/0"
        )


@pytest.mark.parametrize("vendor", ["cisco", "juniper"])
def test_managed_rule_creates_exactly_one_rollback_copy(vendor):
    document = (
        CiscoDocument("hostname old\n")
        if vendor == "cisco"
        else JunosDocument("system { host-name old; }")
    )
    original_clone = ConfigNode.clone

    class Rename(CiscoRuleVisitor):

        def visit_node(self, node):
            self.session.edits.replace_header(node, "hostname new", "rename")

    def action(doc, context):
        with doc.edit().bulk() as root:
            root.nodes[0].nodes[0].header = "host-name new"
        editor = doc.edit()
        editor.append_child(
            doc.root.nodes[0], "domain-name example.test;", "append"
        )
        editor.apply()
        return InterfaceAdaptationOutcome()

    rule = (
        CiscoVisitorRule("rename", Rename)
        if vendor == "cisco"
        else JunosVisitorRule("rename", document_action=action)
    )
    with patch.object(
        ConfigNode, "clone", autospec=True, side_effect=original_clone
    ) as clone:
        rule.apply(document, None)
    assert clone.call_count == 1
    assert document.hostname() == "new"
    assert document._edit_scope is None


def test_bulk_error_restores_tree_cache_and_revision():
    document = JunosDocument("interfaces { ge-0/0/0 {} }")
    original = document.source
    document.interfaces.analysis.interface_specs()
    with pytest.raises(RuntimeError, match="fail"):
        with document.edit().bulk() as root:
            root.nodes[0].nodes[0].header = "ge-0/0/9"
            raise RuntimeError("fail")
    assert document.source == original
    assert document.interfaces.analysis.interface_specs()[0].name == "ge-0/0/0"
    assert document.revision == 0


def test_bulk_success_reconnects_new_nodes():
    document = JunosDocument("system {}")
    new = document.parse_fragment("host-name lab;").nodes[0].clone()
    with document.edit().bulk() as root:
        root.nodes[0].children.append(new)
    assert new.parent is document.root.nodes[0]
    assert new.path == ("system",)
    assert document.revision == 1


def test_swallowed_nested_error_cannot_publish_partial_changes():
    document = CiscoDocument("hostname old\n")
    with pytest.raises(EditConflictError):
        with document.edit().bulk():
            try:
                with document.edit().bulk() as root:
                    root.nodes[0].header = "hostname new"
                    raise RuntimeError("nested")
            except RuntimeError:
                pass
    assert document.hostname() == "old"
    assert document._edit_scope is None


def test_bulk_structural_failure_restores_original_tree():
    document = JunosDocument("system { host-name old; }")
    before = document.source
    with pytest.raises(ValueError, match=r"cycle|shared"):
        with document.edit().bulk() as root:
            root.nodes[0].children.append(root)
    assert document.source == before


def test_no_new_edits_after_explicit_rollback_in_active_scope():
    document = CiscoDocument("hostname old\n")
    with document.edit().scope() as scope:
        scope.rollback()
        with pytest.raises(EditConflictError):
            with document.edit().bulk():
                pass


def test_readonly_rule_rejects_parent_link_corruption():
    document = CiscoDocument("interface Gi0\n description old\n")
    original = document.source

    class Corrupt(CiscoRuleVisitor):

        def visit_node(self, node):
            node.parent = None

    with pytest.raises(RuntimeError, match="Editor"):
        CiscoVisitorRule("readonly", Corrupt, readonly=True).apply(
            document, None
        )
    assert document.source == original
    assert document.root.nodes[0].parent is document.root


def test_root_replacement_requires_a_detached_configuration_root():
    document = JunosDocument("system { host-name old; }")
    original = document.source
    with pytest.raises(EditConflictError):
        document.edit().replace_root(document.root.nodes[0].clone())
    assert document.source == original
    root = document.parse_fragment("system { host-name new; }")
    document.edit().replace_root(root)
    assert document.hostname() == "new"
    assert document.root.nodes[0].parent is document.root
