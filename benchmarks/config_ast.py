"""Measure handwritten parsing, traversal, serialization, and rollback copies."""

import argparse
import json
import time
import tracemalloc

from impl.cisco.document import CiscoDocument
from impl.juniper.document import JunosDocument


def measure(action):
    """Return elapsed seconds and peak allocated memory for one action."""
    tracemalloc.start()
    started = time.perf_counter()
    value = action()
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, {
        "seconds": round(seconds, 3),
        "peak_mib": round(peak / 2**20, 2),
    }


def benchmark(blocks):
    """Measure each phase independently for both configuration formats."""
    sources = {
        CiscoDocument: "".join(
            f"interface Gi0/0/{i}/0\n description port {i}\n mtu 9000\n!\n"
            for i in range(blocks)
        ),
        JunosDocument: "interfaces {\n"
        + "".join(
            f'ge-0/0/{i} {{\n description "port {i}";\n mtu 9000;\n}}\n'
            for i in range(blocks)
        )
        + "}\n",
    }
    results = {}
    for document_type, source in sources.items():
        document, parsing = measure(
            lambda document_type=document_type, source=source: document_type(
                source
            )
        )
        count, walking = measure(
            lambda document=document: sum(1 for _ in document.root.walk())
        )
        _, writing = measure(lambda document=document: document.source)
        backup, copying = measure(document.root.clone)
        assert len(backup.children) == len(document.root.children)
        results[document_type.vendor] = dict(
            lines=len(source.splitlines()),
            nodes=count,
            parse=parsing,
            walk=walking,
            write=writing,
            rule_backup=copying,
        )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks", type=int, default=25000)
    print(json.dumps(benchmark(parser.parse_args().blocks), indent=2))
