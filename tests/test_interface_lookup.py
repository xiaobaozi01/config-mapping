"""Indexed queries must remain equivalent during live edits and rollback."""

import gc
import random
import weakref
from copy import deepcopy

import pytest

from impl.cisco.document import CiscoDocument
from impl.cisco.operations.interfaces import access as cisco
from impl.cisco.operations.interfaces.analysis import (
    logical_names_under as cisco_logical,
)
from impl.common.support.interface_lookup import interface_lookup
from impl.common.support.optimization_cache import optimization_scope
from impl.juniper.document import JunosDocument
from impl.juniper.operations.interfaces import access as junos
from impl.juniper.operations.interfaces.analysis import (
    logical_names_under as junos_logical,
)


@pytest.fixture(params=["cisco", "juniper"])
def case(request):
    if request.param == "cisco":
        return (
            CiscoDocument(
                "hostname R\ninterface Gi0\n mtu 9000\n!\ninterface Gi0.001\n!\ninterface Gi1\n!\ninterface Gi1\n!\n"
            ),
            cisco,
            cisco_logical,
        )
    return (
        JunosDocument(
            "system {} interfaces { ge-0/0/0 { unit 001; } ge-0/0/1 {} ge-0/0/1 {} inactive: ge-0/0/2 {} }"
        ),
        junos,
        junos_logical,
    )


def verify(case):
    document, access, logical = case
    actual = access.interface_nodes(document)
    names = (
        [node.interface_name for node in actual]
        if access is cisco
        else [access.interface_name(document, node) for node in actual]
    )
    for name in [*names, "missing99"]:
        node = access.find_interface_node(document, name)
        indexed = logical(document, name)
        with optimization_scope(0):
            assert access.find_interface_node(document, name) is node
            assert logical(document, name) == indexed
    actual.clear()
    assert access.interface_nodes(document)
    with optimization_scope(0):
        expected = access.interface_nodes(document)
    assert access.interface_nodes(document) == expected


def test_duplicate_order_logicals_and_root_replacement(case):
    doc, access, _ = case
    with optimization_scope() as pool:
        verify(case)
        original = access.interface_nodes(doc)[0]
        with doc.edit().bulk():
            original.active = False
        verify(case)
        doc.edit().replace_root(doc.root.clone())
        verify(case)
        assert original not in access.interface_nodes(doc)
        assert pool.stats["interfaces"]["builds"] >= 2


def test_scalar_changes_are_incremental_and_payloads_do_not_rebuild(case):
    doc, access, _ = case
    with optimization_scope() as pool:
        verify(case)
        builds = pool.stats["interfaces"]["builds"]
        node = access.interface_nodes(doc)[0]
        for number in range(20):
            node.header = (
                f"interface Gi{number + 100}"
                if access is cisco
                else f"ge-0/0/{number + 100}"
            )
            verify(case)
        node.rank = (0, 0)
        node.location = None
        node.children[0].comment = "payload" if node.children else None
        verify(case)
        assert pool.stats["interfaces"]["builds"] == builds
        assert pool.stats["interfaces"]["updates"] >= 20


def test_append_delete_and_pending_add_remove_do_not_rebuild(case):
    doc, access, _ = case
    with optimization_scope() as pool:
        verify(case)
        builds = pool.stats["interfaces"]["builds"]
        container = doc.root if access is cisco else junos.interfaces_block(doc)
        template = access.interface_nodes(doc)[0]
        for i in range(20):
            clone = template.clone()
            clone.header = (
                f"interface Gi{100 + i}"
                if access is cisco
                else f"ge-0/0/{100+i}"
            )
            with doc.edit().bulk():
                container.children.append(clone)
                verify(case)
                container.children.remove(clone)
                verify(case)
                container.children.append(clone)
                container.children.remove(clone)
            verify(case)
        assert pool.stats["interfaces"]["builds"] == builds


def test_bulk_slice_reorder_and_unit_updates_match_reference(case):
    doc, access, _ = case
    with optimization_scope():
        verify(case)
        container = doc.root if access is cisco else junos.interfaces_block(doc)
        with doc.edit().bulk():
            container.children.reverse()
            verify(case)
            container.children = list(reversed(container.children))
            verify(case)
        if access is junos:
            node = access.interface_nodes(doc)[0]
            unit = node.children[0]
            unit.header = "inactive: unit 99"
            verify(case)
            unit.header = "protect: unit 007"
            verify(case)
            unit.active = False
            verify(case)


def test_nested_rollback_deepcopy_and_pool_teardown(case):
    doc, access, _ = case
    with optimization_scope() as pool:
        verify(case)
        before = doc.source
        with pytest.raises(RuntimeError):
            with doc.edit().scope(independent=True):
                with doc.edit().bulk():
                    access.interface_nodes(doc)[0].active = False
                verify(case)
                with pytest.raises(ValueError):
                    with doc.edit().scope(independent=True):
                        doc.edit().replace_root(doc.root.clone())
                        verify(case)
                        raise ValueError()
                verify(case)
                raise RuntimeError()
        assert doc.source == before
        verify(case)
        clone = deepcopy(doc)
        assert "_interface_lookup" not in vars(clone)
        assert all(
            a is not b
            for a, b in zip(
                access.interface_nodes(doc),
                access.interface_nodes(clone),
                strict=True,
            )
        )
        state = interface_lookup(doc)
        weak = weakref.ref(state)
        del state
    assert not pool.entries and pool.closed
    assert weak() is None
    verify(case)


def test_low_budget_falls_back_and_document_gc_releases_index():
    with optimization_scope(1) as small:
        doc = CiscoDocument("interface Gi0\n!\n")
        for _ in range(10):
            assert cisco.find_interface_node(doc, "Gi0") is not None
        assert small.snapshot()["entries"] == 0
    with optimization_scope() as pool:
        cisco.find_interface_node(doc, "Gi0")
        reference = weakref.ref(doc)
        parser = weakref.ref(doc._parser)
        retained = doc.root
        del doc
        gc.collect()
        assert reference() is None and parser() is None
        assert retained.children
        assert not pool.entries


def test_cisco_nested_dotted_prefixes_keep_exact_tree_semantics():
    doc = CiscoDocument(
        "interface Gi0\ninterface Gi0.1\ninterface Gi0.1.2\ninterface Gi0.10\n"
    )
    with optimization_scope():
        assert [
            n.interface_name
            for n in cisco.interface_tree_nodes(doc, "GigabitEthernet0.1")
        ] == ["GigabitEthernet0.1", "GigabitEthernet0.1.2"]


def test_junos_uses_first_effective_interfaces_container():
    doc = JunosDocument("interfaces { ge-0/0/0 {} } interfaces { ge-0/0/1 {} }")
    with optimization_scope():
        assert junos.find_interface_node(doc, "ge-0/0/0")
        doc.root.children[0].header = "inactive: interfaces"
        assert junos.find_interface_node(doc, "ge-0/0/0") is None
        assert junos.find_interface_node(doc, "ge-0/0/1")
        with doc.edit().bulk():
            doc.root.children.append(
                doc.parse_fragment("system {}").children[0].clone()
            )
        assert len(junos.interface_nodes(doc)) == 1


def test_seeded_mixed_mutations_remain_equivalent(case):
    doc, access, _ = case
    rng = random.Random(83)
    with optimization_scope():
        for _ in range(60):
            container = (
                doc.root if access is cisco else junos.interfaces_block(doc)
            )
            candidates = access.interface_nodes(doc)
            operation = rng.randrange(4)
            with doc.edit().bulk():
                if operation == 0:
                    container.children.reverse()
                elif operation == 1 and len(candidates) > 1:
                    container.children.remove(candidates[-1])
                elif operation == 2:
                    container.children.append(candidates[0].clone())
                else:
                    candidates[0].comment = "change"
                verify(case)
            verify(case)


def test_eviction_rebuilds_fresh_nodes_after_mutation():
    doc = CiscoDocument("interface Gi0\n!\ninterface Gi1\n!\n")
    with optimization_scope(8192) as pool:
        node = cisco.find_interface_node(doc, "Gi0")
        pool.put("pressure", (), 8000, "pressure")
        node.header = "interface Gi2"
        assert cisco.find_interface_node(doc, "Gi0") is None
        assert cisco.find_interface_node(doc, "Gi2") is node
        assert pool.stats["interfaces"]["builds"] == 2
        assert pool.snapshot()["estimated_peak_bytes"] <= 8192


def test_oversized_index_does_not_retry_build_on_every_scalar_edit():
    doc = CiscoDocument("".join(f"interface Gi{i}\n!\n" for i in range(10)))
    with optimization_scope(5000) as pool:
        node = cisco.find_interface_node(doc, "Gi0")
        for i in range(10):
            node.header = f"interface Gi{100 + i}"
            assert cisco.find_interface_node(doc, f"Gi{100+i}") is node
        assert pool.stats["interfaces"]["builds"] == 1
        assert not pool.entries
