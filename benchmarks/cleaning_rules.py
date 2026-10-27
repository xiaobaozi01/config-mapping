"""合成大配置的清洗基准：旧式逐规则遍历与固定两轮执行。

运行：PYTHONPATH=src python benchmarks/cleaning_rules.py --lines 40000 --rules 100
耗时包含规则编译，不含输入解析；访问和正则搜索次数在独立 profile 中统计。
基准规则互不重叠，使新旧语义在此工作负载上产生相同 AST 和命中数。
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import pstats
import re
import statistics
import time

from config_adaptor.cisco.document import CiscoDocument
from config_adaptor.common.cleaning import CleaningOperation, matches_cleaning_path
from config_adaptor.juniper.document import JunosDocument


def configuration(vendor: str, lines: int) -> str:
    result = [] if vendor == "cisco" else ["interfaces {"]
    block_lines = 20 if vendor == "cisco" else 22
    blocks = (lines - (0 if vendor == "cisco" else 2)) // block_lines
    for index in range(blocks):
        children = [f"drop-marker-{index % 5}", f"rewrite-marker-{index % 5} old"]
        children += [f"keep-{number} original" for number in range(16)]
        if vendor == "cisco":
            result += [f"interface GigabitEthernet0/0/0/{index}"]
            result += [f" {child}" for child in children]
            result.append("!")
        else:
            result += [f" ge-0/0/{index} {{", "  unit 1 {"]
            result += [f"   {child};" for child in children]
            result += ["  }", " }"]
    if vendor == "juniper":
        result.append("}")
    result += [("!" if vendor == "cisco" else "#") + " padding"] * (lines - len(result))
    return "\n".join(result) + "\n"


def operations(vendor: str, count: int) -> list[CleaningOperation]:
    path = (r"^interface\b",) if vendor == "cisco" else ("^interfaces$", "^ge-", r"^unit\s+1$")
    return [
        CleaningOperation(path, f"^drop-marker-{index}$", "delete")
        for index in range(count // 2)
    ] + [
        CleaningOperation(path, f"^(rewrite-marker-{index}) old$", "replace", r"\g<1> new")
        for index in range(count - count // 2)
    ]


def legacy_clean(document, rules):
    """保留优化前逐规则、完整递归遍历的算法，作为性能基线。"""
    counts = []
    cisco = isinstance(document, CiscoDocument)

    def header(node):
        if cisco:
            return node.header.strip()
        return document._parser.base_header(node.header).rstrip(";").strip()

    for rule in rules:
        hits = 0
        pattern = re.compile(rule.match, re.IGNORECASE)

        def visit(node, ancestors):
            nonlocal hits
            if cisco:
                if not node.active or node.is_formatting:
                    return
            elif not document._parser.is_effective(node):
                return
            statement = header(node)
            if matches_cleaning_path(ancestors, rule.path) and pattern.search(statement):
                hits += 1
                if rule.action == "delete":
                    node.active = False
                    return
                node.header = pattern.sub(rule.value or "", node.header if cisco else statement)
                if not cisco and not node.is_block:
                    node.header += ";"
                statement = header(node)
            for child in node.children or []:
                visit(child, (*ancestors, statement))

        for node in document.root.children or []:
            visit(node, ())
        counts.append(hits)
    return counts


def batch_clean(document, rules):
    return document.apply_cleaning_rules(rules)


def run(vendor: str, line_count: int, rule_count: int, repeat: int) -> None:
    cls = CiscoDocument if vendor == "cisco" else JunosDocument
    source, rules = configuration(vendor, line_count), operations(vendor, rule_count)
    results = []
    reference = None
    for name, execute in (("legacy", legacy_clean), ("two-pass", batch_clean)):
        timings = []
        for _ in range(repeat):
            document = cls(source)
            gc.collect()
            start = time.perf_counter()
            counts = execute(document, rules)
            timings.append(time.perf_counter() - start)
            if reference is None:
                reference = (document.root, counts)
            else:
                assert document.root == reference[0] and counts == reference[1]
        document = cls(source)
        profile = cProfile.Profile()
        profile.runcall(execute, document, rules)
        stats = pstats.Stats(profile).stats
        visits = sum(
            value[1] for (filename, _, function), value in stats.items()
            if function == ("visit" if name == "legacy" else "walk")
            and (filename == __file__ if name == "legacy" else filename.endswith(f"/{vendor}/document.py"))
        )
        searches = sum(
            value[1] for (_, _, function), value in stats.items()
            if function == "<method 'search' of 're.Pattern' objects>"
        )
        elapsed = statistics.median(timings)
        results.append(elapsed)
        print(
            f"{vendor} {name}: lines={len(source.splitlines())}, rules={len(rules)}, "
            f"median={elapsed:.4f}s, node_visits={visits:,}, regex_searches={searches:,}",
            flush=True,
        )
    print(f"{vendor}: speedup={results[0] / results[1]:.2f}x; AST and per-rule counts identical", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lines", type=int, default=40_000)
    parser.add_argument("--rules", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.lines < 24 or arguments.rules < 10 or arguments.repeat < 1:
        parser.error("lines >= 24, rules >= 10, repeat >= 1")
    for current_vendor in ("cisco", "juniper"):
        run(current_vendor, arguments.lines, arguments.rules, arguments.repeat)
