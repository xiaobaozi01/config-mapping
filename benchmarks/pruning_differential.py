"""Compare full pruning outputs using public, repository-owned fixtures only.

Run this identical script with PYTHONPATH pointing at each revision's src.
No production configuration, identifiers or paths are read or printed.
"""

import argparse
import hashlib
import json
import statistics
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from impl.cisco.document import CiscoDocument
from impl.common.config_ast.model import tree_signature
from impl.juniper.document import JunosDocument


def canonical(value):
    if is_dataclass(value):
        return canonical(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True).encode()
    ).hexdigest()


def run(path, repeat, protect):
    cls = CiscoDocument if path.name.startswith("iosxr") else JunosDocument
    source = path.read_text()
    times, reference = [], None
    for _ in range(repeat + 1):
        document = cls(source)
        specs = document.interfaces.analysis.interface_specs()
        protected = {specs[0].parent} if specs and protect else set()
        started = time.perf_counter()
        result = document.pruning.prune_non_nni(protected)
        elapsed = time.perf_counter() - started
        outcome = canonical(result)
        # Existing BGP diagnostics enumerate identity-based sets, so their
        # top-level event order already varies between unchanged executions.
        # Preserve multiplicity and all nested paths/payload ordering.
        for field in ("changes", "warnings", "errors"):
            if field in outcome:
                outcome[field] = sorted(outcome[field], key=repr)
        snapshot = dict(
            source=digest(document.source),
            render=digest(document.render()),
            tree=digest(tree_signature(document.root)),
            outcome=digest(outcome),
            interfaces=digest(document.interfaces.analysis.interface_specs()),
        )
        if reference is None:
            reference = snapshot
        assert snapshot == reference, (
            path.name,
            {key for key in snapshot if snapshot[key] != reference[key]},
        )
        times.append(elapsed)
    return dict(
        fixture=path.name,
        protected=protect,
        median_seconds=statistics.median(times[1:]),
        **reference,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    paths = sorted((fixtures / "core_pruning").glob("*.cfg"))
    paths += sorted((fixtures / "interfaces").glob("junos_nni_pruning*.cfg"))
    for path in paths:
        for protected in (False, True):
            print(json.dumps(run(path, args.repeat, protected)), flush=True)


if __name__ == "__main__":
    main()
