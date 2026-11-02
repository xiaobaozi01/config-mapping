"""Compare complete public conversion fixtures with new caches off and on."""

import argparse
import hashlib
import json
import statistics
import tempfile
from pathlib import Path

from openpyxl import load_workbook

from gns3_config_adaptor.adaptation.service import convert


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def snapshot(directory):
    report = json.loads((directory / "report.json").read_text())
    for name in ("events", "errors", "warnings", "group_conflicts"):
        report[name] = sorted(report[name], key=repr)
    configs = {
        path.name: path.read_text()
        for path in sorted((directory / "configs").glob("*.cfg"))
    }
    topology = directory / "topology-adapted.xlsx"
    cells = []
    if topology.exists():
        workbook = load_workbook(topology)
        cells = [(sheet.title, list(sheet.values)) for sheet in workbook]
        workbook.close()
    return digest(
        dict(
            report=report,
            configs=configs,
            cells=cells,
            mappings=json.loads(
                (directory / "interface-mapping.json").read_text()
            ),
        )
    )


def record_stages(stages, records):
    totals = {}
    for stage in records:
        name = stage["stage"]
        totals[name] = totals.get(name, 0) + stage["seconds"]
    for name, seconds in totals.items():
        stages.setdefault(name, []).append(seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    references = (
        {
            row["fixture"]: row["result_sha256"]
            for line in args.reference.read_text().splitlines()
            if (row := json.loads(line))
        }
        if args.reference
        else {}
    )
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    for topology in sorted(fixtures.rglob("topology.xlsx")):
        configs = topology.parent / "configs"
        if not configs.is_dir():
            continue
        reference = references.get(topology.parent.name)
        for budget in ((None,) if args.legacy else (0, 2 * 1024**3)):
            times, peaks, stages = [], [], {}
            with tempfile.TemporaryDirectory() as temporary:
                for run in range(args.repeat + 1):
                    output = Path(temporary) / str(run)
                    options = (
                        {} if budget is None else {"cache_budget_bytes": budget}
                    )
                    context = convert(topology, configs, output, **options)
                    actual = snapshot(output)
                    if reference is None:
                        reference = actual
                    assert reference == actual, topology.parent.name
                    metrics = context.performance.snapshot()
                    cache = metrics.get("cache", {})
                    if budget is not None:
                        assert cache["closed"] and cache["entries"] == 0
                    if run:
                        times.append(metrics["total_seconds"])
                        peaks.append(cache.get("estimated_peak_bytes", 0))
                        record_stages(stages, metrics["stages"])
            print(
                json.dumps(
                    dict(
                        fixture=topology.parent.name,
                        budget_bytes=budget,
                        median_seconds=statistics.median(times),
                        samples_seconds=times,
                        stages_median_seconds={
                            name: statistics.median(seconds)
                            for name, seconds in stages.items()
                        },
                        estimated_cache_peak=max(peaks),
                        cache=cache,
                        result_sha256=reference,
                    )
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
