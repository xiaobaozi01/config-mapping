"""Reference extraction is independent of changing interface name sets."""

import re
from unittest.mock import patch

from impl.cisco.document import CiscoDocument
from impl.cisco.rules.pruning import interface_references, reference_matcher
from impl.cisco.syntax import semantics


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
