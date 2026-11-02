"""Verify JSONL output snapshots and summarize paired benchmark timings."""

import argparse
import json
from pathlib import Path


def read(path):
    rows = [
        json.loads(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]
    result = {}
    for row in rows:
        key = (
            (row["vendor"], row["case"])
            if "vendor" in row
            else (row["fixture"], row["protected"])
        )
        if key in result:
            raise ValueError(f"Duplicate result: {key}")
        result[key] = row
    if not result:
        raise ValueError("No benchmark results")
    return result


def compare(before, after):
    if before.keys() != after.keys():
        raise ValueError("Workload sets differ")
    fields = (
        "nodes",
        "rules",
        "edits",
        "output_sha256",
        "result_count",
        "source",
        "render",
        "tree",
        "outcome",
        "interfaces",
    )
    for key, old in before.items():
        new = after[key]
        for field in fields:
            if field in old and old[field] != new.get(field):
                raise ValueError(f"Mismatch: {key}, {field}")
        print(
            f"{key}: {old['median_seconds']:.6f}s -> {new['median_seconds']:.6f}s "
            f"({old['median_seconds'] / new['median_seconds']:.2f}x)"
        )
    print(f"All {len(before)} output/result snapshots match.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline")
    parser.add_argument("optimized")
    args = parser.parse_args()
    compare(read(args.baseline), read(args.optimized))


if __name__ == "__main__":
    main()
