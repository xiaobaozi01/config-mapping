"""Synthetic interface lookup and semantic matching baselines.

Timing, profiling and allocation tracking run separately. No private inputs are
read; the same script works against the pre-index revision through PYTHONPATH.
"""

import argparse
import cProfile
import gc
import hashlib
import json
import pstats
import statistics
import time
import tracemalloc
from contextlib import nullcontext

from impl.cisco.document import CiscoDocument
from impl.cisco.operations.interfaces.access import (
    find_interface_node as cisco_find,
)
from impl.common.semantic_identity import (
    MergeKind,
    SemanticResolver,
    SemanticRule,
    StatementContext,
)
from impl.juniper.document import JunosDocument
from impl.juniper.operations.interfaces.access import (
    find_interface_node as junos_find,
)

try:
    from impl.common.support.optimization_cache import optimization_scope
except ImportError:

    def optimization_scope(_budget):
        return nullcontext(None)


def prepare(args, case, vendor):
    if case.startswith("semantic"):
        rules = tuple(
            SemanticRule(
                str(i),
                "leaf",
                ("**",),
                (f"command-{i}", "{value:word}"),
                (str(i),),
                MergeKind.REPLACE,
            )
            for i in range(100)
        )
        resolver = SemanticResolver(rules)
        contexts = [
            StatementContext(
                "synthetic", ("root",), f"command-{i % 100} value-{i}"
            )
            for i in range(args.statements)
        ]

        def run():
            total = sum(int(resolver.resolve(ctx).matched) for ctx in contexts)
            if case == "semantic_hot":
                for _ in range(args.hot_passes - 1):
                    total += sum(
                        int(resolver.resolve(ctx).matched) for ctx in contexts
                    )
            return total

        return None, run
    cisco = vendor == "cisco"
    source = (
        "".join(
            f"interface Gi0/0/{i}\n description repeated\n mtu 9000\n!\n"
            for i in range(args.interfaces)
        )
        if cisco
        else "interfaces {"
        + "".join(
            f"ge-0/0/{i} {{ unit 0 {{ description repeated; }} }}"
            for i in range(args.interfaces)
        )
        + "}"
    )
    document = (CiscoDocument if cisco else JunosDocument)(source)
    find = cisco_find if cisco else junos_find
    name = (lambda i: f"Gi0/0/{i}") if cisco else (lambda i: f"ge-0/0/{i}")

    def run():
        total = 0
        for i in range(args.queries if case == "lookup" else args.edits):
            index = i % args.interfaces
            node = find(document, name(index))
            total += node is not None
            if case == "mixed":
                changed = name(index + args.interfaces)
                editor = document.edit()
                editor.replace_header(
                    node,
                    f"interface {changed}" if cisco else changed,
                    "benchmark rename",
                )
                editor.apply()
                total += find(document, changed) is not None
                editor = document.edit()
                editor.replace_header(
                    node,
                    f"interface {name(index)}" if cisco else name(index),
                    "benchmark restore",
                )
                editor.apply()
                total += find(document, name(index)) is not None
            elif case == "churn":
                container = (
                    document.root if cisco else document.root.children[0]
                )
                editor = document.edit()
                cloned = editor.clone(node, container, "benchmark clone")
                changed = name(index + args.interfaces)
                cloned.header = f"interface {changed}" if cisco else changed
                editor.apply()
                total += find(document, changed) is cloned
                editor = document.edit()
                editor.delete(cloned, "benchmark delete")
                editor.apply()
                total += find(document, name(index)) is node

        return total

    return document, run


def sample(args, case, vendor, mode):
    gc.collect()
    with optimization_scope(args.cache_budget_mib * 2**20) as cache:
        document, run = prepare(args, case, vendor)
        profile = cProfile.Profile() if mode == "profile" else None
        if mode == "memory":
            tracemalloc.start()
        start = time.perf_counter()
        total = profile.runcall(run) if profile else run()
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1] if mode == "memory" else 0
        if mode == "memory":
            tracemalloc.stop()
        digest = hashlib.sha256(
            (document.source if document else str(total)).encode()
        ).hexdigest()
        calls = {}
        if profile:
            for (_, _, name), stats in pstats.Stats(profile).stats.items():
                if name in {
                    "interface_nodes",
                    "_resolve_uncached",
                    "match_rule",
                    "match_prepared",
                    "tokenize",
                }:
                    calls[name] = calls.get(name, 0) + stats[1]
        cleanup = time.perf_counter()
        if cache is not None:
            cache.close()
        elapsed += time.perf_counter() - cleanup
        metrics = cache.snapshot() if cache is not None else {}
    return elapsed, peak, digest, total, calls, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interfaces", type=int, default=5750)
    parser.add_argument("--queries", type=int, default=2000)
    parser.add_argument("--edits", type=int, default=100)
    parser.add_argument("--statements", type=int, default=18000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--hot-passes", type=int, default=2)
    parser.add_argument("--cache-budget-mib", type=int, default=2048)
    parser.add_argument(
        "--cases",
        nargs="+",
        default=["lookup", "mixed", "churn", "semantic_cold", "semantic_hot"],
        choices=["lookup", "mixed", "churn", "semantic_cold", "semantic_hot"],
    )
    args = parser.parse_args()
    if (
        min(
            args.interfaces,
            args.queries,
            args.edits,
            args.statements,
            args.repeat,
            args.hot_passes,
        )
        < 1
        or args.cache_budget_mib < 0
    ):
        parser.error("sizes must be positive and budget nonnegative")
    for case in args.cases:
        for vendor in (
            ("synthetic",)
            if case.startswith("semantic")
            else ("cisco", "juniper")
        ):
            sample(args, case, vendor, "warmup")
            times = [
                sample(args, case, vendor, "time") for _ in range(args.repeat)
            ]
            memory = sample(args, case, vendor, "memory")
            profiled = sample(args, case, vendor, "profile")
            assert all(row[2:4] == profiled[2:4] for row in [*times, memory])
            print(
                json.dumps(
                    dict(
                        case=case,
                        vendor=vendor,
                        interfaces=args.interfaces,
                        queries=args.queries,
                        edits=args.edits,
                        statements=args.statements,
                        hot_passes=args.hot_passes,
                        budget_mib=args.cache_budget_mib,
                        median_seconds=statistics.median(r[0] for r in times),
                        peak_mib=memory[1] / 2**20,
                        output_sha256=profiled[2],
                        result_count=profiled[3],
                        calls=profiled[4],
                        cache=profiled[5],
                    )
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
