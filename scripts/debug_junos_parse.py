"""直接运行或在 IDE 中调试 JuniperParser.parse()。

默认解析下方示例，也可通过命令行参数传入本地配置文件。
"""

import argparse
import sys
from pathlib import Path

SAMPLE = """system {
    host-name lab # 主机名
    xxx *
    services {
        ssh;
    }
}
"""


def main():
    """读取配置并调用解析器，打印解析得到的节点树。

    在 parse 调用处打断点后单步进入，可观察 buffer、stack 等局部变量。
    """
    # 直接运行脚本时也能找到 src，无需配置 PYTHONPATH。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.parser import (
        JuniperParser,
    )

    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("config", nargs="?", type=Path, help="本地配置文件")
    args = arguments.parse_args()
    text = args.config.read_text(encoding="utf-8") if args.config else SAMPLE

    # 在下面这一行打断点，单步进入 parse()。
    root = JuniperParser().parse(text)

    pending = [(root, 0)]
    while pending:
        node, depth = pending.pop()
        indent = "    " * depth
        print(f"{indent}{node.kind}: {node.header!r}")
        if node.comment:
            print(f"{indent}    comment: {node.comment!r}")
        pending.extend((child, depth + 1) for child in reversed(node.nodes))


if __name__ == "__main__":
    main()
