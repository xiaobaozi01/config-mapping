"""Synthetic mapping benchmark; never reads production configuration.

Run with PYTHONPATH=src python benchmarks/mapping_hotpaths.py --nodes 23000.
Timing, allocation tracing and profiling run separately; parsing is excluded.
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

from impl.cisco.document import CiscoDocument
from impl.cisco.rules import pruning
from impl.cisco.rules.runtime import CiscoRuleVisitor, CiscoVisitorRule
from impl.juniper.document import JunosDocument
from impl.juniper.rules.runtime import JunosRuleVisitor, JunosVisitorRule


def prepare(vendor, nodes, rules, edits, case):  # noqa: C901 - scenario matrix
    """Build a fresh document and the same deterministic workload each run."""
    cisco = vendor == "cisco"
    if cisco:
        source = "hostname old\n" + "".join(
            f"interface Gi0/0/{i}\n description repeated\n mtu 9000\n!\n"
            for i in range(max(1, (nodes - 2) // 4))
        )
    else:
        source = (
            "system { host-name old; }\ninterfaces {"
            + "".join(
                f'ge-0/0/{i} {{ description "repeated"; mtu 9000; }}'
                for i in range(max(1, (nodes - 5) // 3))
            )
            + "}"
        )
    document = (CiscoDocument if cisco else JunosDocument)(source)
    base = CiscoRuleVisitor if cisco else JunosRuleVisitor
    rule_type = CiscoVisitorRule if cisco else JunosVisitorRule
    target = document.root.nodes[0]
    if not cisco:
        target = target.nodes[0]

    class Visitor(base):

        def visit_node(self, node):
            if case == "sparse":
                self.session.edits.replace_header(
                    node,
                    ("hostname " if cisco else "host-name ")
                    + str(self.session.context),
                    "benchmark",
                )

    rule = rule_type(
        "benchmark",
        visitor_factory=Visitor,
        selector=lambda doc, ctx: (target,),
        readonly=case == "readonly",
    )
    names = {f"GigabitEthernet0/0/{i}" for i in range(edits)}

    match = (
        pruning.reference_matcher(document, names)
        if cisco and hasattr(pruning, "reference_matcher")
        else lambda text: pruning.interface_references(text, names)
    )

    def execute():
        changes = 0
        for index in range(rules):
            if case in {"readonly", "noop", "sparse"}:
                changes += len(rule.apply(document, index).changes)
            elif case == "references":
                for number in range(100):
                    changes += len(match(f"interface Gi0/0/{number % 10}"))
            else:
                try:
                    with document.edit().bulk() as root:
                        container = root if cisco else root.nodes[-1]
                        for node in container.nodes[:edits]:
                            node.comment = str(index)
                        if case == "rollback":
                            raise RuntimeError("synthetic rollback")
                except RuntimeError:
                    if case != "rollback":
                        raise
        return changes

    return document, execute


def run(vendor, args, case):
    reference = None

    def sample(mode):
        nonlocal reference
        document, execute = prepare(
            vendor, args.nodes, args.rules, args.edits, case
        )
        gc.collect()
        profile = cProfile.Profile() if mode == "profile" else None
        if mode == "memory":
            tracemalloc.start()
        start = time.perf_counter()
        count = profile.runcall(execute) if profile else execute()
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1] if mode == "memory" else 0
        if mode == "memory":
            tracemalloc.stop()
        result = (hashlib.sha256(document.source.encode()).hexdigest(), count)
        if reference is None:
            reference = result
        assert reference == result, "non-deterministic output or audit count"
        return elapsed, peak, profile, document

    sample("warmup")
    timings = [sample("time")[0] for _ in range(args.repeat)]
    _, peak, _, _ = sample("memory")
    _, _, profile, document = sample("profile")
    calls = {}
    for (_, _, name), values in pstats.Stats(profile).stats.items():
        if name in {
            "tree_signature",
            "_tree_signature",
            "clone",
            "bind_parents",
            "interface_references",
            "canonical_cisco_interface",
        }:
            calls[name] = calls.get(name, 0) + values[1]
    return dict(
        vendor=vendor,
        case=case,
        nodes=sum(
            1
            for _ in document.root.walk(
                include_self=True, include_formatting=True
            )
        ),
        rules=args.rules,
        edits=args.edits,
        median_seconds=statistics.median(timings),
        peak_mib=peak / 2**20,
        calls=calls,
        output_sha256=reference[0],
        result_count=reference[1],
    )


def construction(vendor, args):
    """Measure retained ownership overhead separately from edit-time peaks."""
    times = []
    for _ in range(args.repeat + 1):
        gc.collect()
        start = time.perf_counter()
        document, execute = prepare(
            vendor, args.nodes, args.rules, args.edits, "readonly"
        )
        times.append(time.perf_counter() - start)
        del document, execute
    gc.collect()
    tracemalloc.start()
    document, execute = prepare(
        vendor, args.nodes, args.rules, args.edits, "readonly"
    )
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del document, execute
    return dict(
        vendor=vendor,
        requested_nodes=args.nodes,
        constructor_median_seconds=statistics.median(times[1:]),
        retained_mib=retained / 2**20,
        peak_mib=peak / 2**20,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--construction", action="store_true")
    parser.add_argument("--nodes", type=int, default=23000)
    parser.add_argument("--rules", type=int, default=20)
    parser.add_argument("--edits", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=[
            "readonly",
            "noop",
            "sparse",
            "bulk",
            "rollback",
            "references",
        ],
        default=[
            "readonly",
            "noop",
            "sparse",
            "bulk",
            "rollback",
            "references",
        ],
    )
    args = parser.parse_args()
    if min(args.nodes, args.rules, args.edits, args.repeat) < 1:
        parser.error("sizes and repeat must be positive")
    for vendor in ("cisco", "juniper"):
        if args.construction:
            print(json.dumps(construction(vendor, args)), flush=True)
            continue
        for case in args.cases:
            if vendor == "juniper" and case == "references":
                continue
            print(json.dumps(run(vendor, args, case)), flush=True)


if __name__ == "__main__":
    main()
