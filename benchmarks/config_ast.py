"""测量手写解析、遍历、序列化和回滚复制的性能。"""

import argparse
import json
import time
import tracemalloc

from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.document import (
    CiscoDocument,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document import (
    JunosDocument,
)


def measure(action):
    """返回一次操作的耗时和峰值分配内存。"""
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
    """分别测量两种配置格式的各个处理阶段。"""
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
