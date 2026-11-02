"""Reference extraction is independent of changing interface name sets."""

import random
import re
from copy import deepcopy
from unittest.mock import patch

from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.pruning import (
    _CiscoPruner,
    interface_references,
    reference_matcher,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax import (
    semantics,
)
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.ast import (
    CiscoNode,
)
from gns3_config_adaptor.impl.common.domain.outcomes import (
    InterfacePruningOutcome,
)


def legacy(text, names):
    if re.match(
        r"^(?:bridge\s+group|bridge-domain|description|remark)\b", text, re.I
    ):
        return set()
    index = {name.lower(): name for name in names}
    return {
        index[semantics.canonical_cisco_interface(match.group()).lower()]
        for match in semantics._CISCO_INTERFACE_TOKEN.finditer(text)
        if semantics.canonical_cisco_interface(match.group()).lower() in index
    }


def test_reference_extraction_matches_previous_behavior():
    names = {
        "GigabitEthernet0/0/1",
        "GigabitEthernet0/0/10",
        "Bundle-Ether2",
        "BVI2",
    }
    document = CiscoDocument("")
    match = reference_matcher(document, names)
    for text in [
        "interface Gi0/0/1",
        "interface gi 0/0/10",
        "interface Gi0/0/100",
        "interface Gi0/0/1.5",
        "interface be2",
        "bridge-domain BVI2",
        "bridge group BVI2",
        "description Gi0/0/1",
        "remark BE2",
        "mtu 9000",
        "hostname LAB",
        "interface Gi0/0/1 BE2 gi0/0/1",
        "interface Unknown9",
    ]:
        assert (
            match(text)
            == legacy(text, names)
            == interface_references(text, names)
        )


def test_name_changes_do_not_poison_text_cache_and_results_are_independent():
    document = CiscoDocument("")
    text = "interface Gi0/0/1"
    names = {"GigabitEthernet0/0/1"}
    first = reference_matcher(document, names)
    assert first(text) == names
    first(text).clear()
    names.clear()
    assert first(text) == {"GigabitEthernet0/0/1"}
    assert reference_matcher(document, names)(text) == set()
    assert first(text) == {"GigabitEthernet0/0/1"}
    assert document._parser.interface_references.cache_info().misses == 1


def test_text_cache_includes_nonmatches_and_is_bounded():
    document = CiscoDocument("")
    extract = document._parser.interface_references
    with patch.object(
        semantics,
        "canonical_cisco_interface",
        wraps=semantics.canonical_cisco_interface,
    ) as canonical:
        extract("interface Gi1")
        extract("interface Gi1")
        assert canonical.call_count == 1
    assert extract("hostname no-digits") == frozenset()
    assert extract("hostname no-digits") == frozenset()
    assert extract.cache_info().maxsize == 32768
    for index in range(32769):
        extract(f"hostname synthetic-{index}")
    assert extract.cache_info().currsize == 32768


def test_iterative_clean_preserves_reference_paths_and_event_order():
    randomizer = random.Random(731)
    names = {"GigabitEthernet0", "Bundle-Ether2"}
    headers = [
        "nested",
        "description Gi0",
        "remark BE2",
        "interface Gi0",
        "bind-interface Gi0",
        "mtu 9000",
        "interface BE2",
        "interface Gi01",
    ]

    def build(depth):
        return [
            CiscoNode(
                randomizer.choice(headers), build(depth - 1) if depth else None
            )
            for _ in range(randomizer.randrange(1, 4))
        ]

    def reference(worker, nodes, path=()):
        for node in nodes:
            if not node.active or node.is_formatting:
                continue
            current = (*path, node.header)
            if re.match(r"^(?:description|remark)\b", node.header, re.I):
                continue
            refs = legacy(node.header, worker.removed)
            if refs and worker._handle_removed_reference(node, current, refs):
                continue
            reference(worker, node.nodes, current)

    for _ in range(20):
        nodes = build(4)
        results = []
        for execute in (reference, lambda worker, nodes: worker.clean(nodes)):
            events, outcome = [], InterfacePruningOutcome()
            worker = _CiscoPruner(
                CiscoDocument(""),
                set(),
                outcome,
                lambda node, path, reason, refs: events.append(
                    (node.header, path, reason, refs)
                ),
            )
            worker.removed = names
            execute(worker, deepcopy(nodes))
            results.append((events, outcome))
        assert results[0] == results[1]


def test_iterative_clean_handles_deep_unknown_reference_without_recursion():
    from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.pruning import (
        _CiscoPruner,
    )
    from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.syntax.ast import (
        CiscoNode,
    )
    from gns3_config_adaptor.impl.common.domain.outcomes import (
        InterfacePruningOutcome,
    )

    node = CiscoNode("bind-interface Gi0")
    for _ in range(1500):
        node = CiscoNode("nested", [node])
    outcome = InterfacePruningOutcome()
    worker = _CiscoPruner(CiscoDocument(""), set(), outcome, lambda *args: None)
    worker.removed = {"GigabitEthernet0"}
    worker.clean([node])
    assert len(outcome.warnings) == 1
    assert len(next(iter(outcome.deferred_interface_references)).path) == 1501
