"""Run the normal conversion entry point and export aggregate timing only.

Inputs and detailed reports stay in the caller's environment. Each run uses a
fresh output directory so archiving previous outputs does not distort timings.
"""

import argparse
import cProfile
import json
import statistics
import sys
import tempfile
from pathlib import Path

from gns3_config_adaptor.adaptation.service import _write_text, convert


def sample(args, warmup):
    directory = Path(
        tempfile.mkdtemp(
            prefix="warmup-" if warmup else "run-", dir=args.output
        )
    )
    profile = cProfile.Profile() if args.profile else None
    try:
        if profile is None:
            convert(
                args.topology,
                args.configs,
                directory,
                args.profiles,
                strict_input_validation=args.strict_input_validation,
                cache_budget_bytes=args.cache_budget_mib * 2**20,
            )
        else:
            profile.runcall(
                convert,
                args.topology,
                args.configs,
                directory,
                args.profiles,
                strict_input_validation=args.strict_input_validation,
                cache_budget_bytes=args.cache_budget_mib * 2**20,
            )
    except Exception:
        print(
            "Conversion failed; inspect the local report files.",
            file=sys.stderr,
        )
    finally:
        if profile is not None:
            profile.dump_stats(str(directory / "profile.pstat"))
    metrics = directory / "performance.json"
    if not metrics.is_file():
        return {
            "status": "failed",
            "metrics_available": False,
            "warmup": warmup,
        }
    result = json.loads(metrics.read_text())
    result["warmup"] = warmup
    return result


def summarize(rows, repeat, profiled):
    measured = [row for row in rows if not row["warmup"]]
    valid = len(measured) == repeat and all(
        row.get("status") == "success"
        and "total_seconds" in row
        and row.get("profiler_enabled") is False
        and row.get("tracing_enabled") is False
        for row in measured
    )
    valid = (
        valid
        and all(row.get("status") == "success" for row in rows)
        and not profiled
    )
    median = (
        statistics.median(row["total_seconds"] for row in measured)
        if valid
        else None
    )
    eligible = valid and repeat >= 3 and any(row["warmup"] for row in rows)
    return {
        "schema_version": 1,
        "target_seconds": 600,
        "measured_median_seconds": median,
        "eligible_for_acceptance": bool(eligible),
        "meets_target": median <= 600 if eligible else None,
        "runs": rows,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--configs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--cache-budget-mib", type=int, default=2048)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--strict-input-validation", action="store_true")
    args = parser.parse_args()
    if args.repeat < 1 or args.warmup < 0 or args.cache_budget_mib < 0:
        parser.error(
            "repeat must be positive; warmup and cache budget must be nonnegative"
        )
    if args.profile and (args.repeat != 1 or args.warmup != 0):
        parser.error("profile mode requires --repeat 1 --warmup 0")
    output, configs, topology = (
        args.output.resolve(),
        args.configs.resolve(),
        args.topology.resolve(),
    )
    if (
        output.is_relative_to(configs)
        or configs.is_relative_to(output)
        or topology.is_relative_to(output)
    ):
        parser.error("output must be separate from input directories")
    args.output.mkdir(parents=True, exist_ok=True)
    return args


def main():
    args = parse_args()
    rows = [sample(args, True) for _ in range(args.warmup)]
    rows.extend(sample(args, False) for _ in range(args.repeat))
    summary = summarize(rows, args.repeat, args.profile)
    text = json.dumps(summary, indent=2) + "\n"
    _write_text(args.output, "performance-summary.json", text)
    print(text, end="")
    return int(any(row["status"] != "success" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
