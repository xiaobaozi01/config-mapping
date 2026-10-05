"""命令行参数定义和进程退出码处理。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .service import convert


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器；这里只定义输入，不执行任何转换。"""
    parser = argparse.ArgumentParser(description="将 IOS XR/Junos 配置适配到 GNS3 XRv9000/vMX")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("convert", help="执行拓扑和配置转换")
    command.add_argument("--topology", type=Path, required=True, help="输入 Excel 拓扑")
    command.add_argument("--config-dir", type=Path, required=True, help="原始配置文件目录")
    command.add_argument("--output-dir", type=Path, required=True, help="输出目录")
    command.add_argument("--profiles", type=Path, help="镜像接口配置 YAML")
    command.add_argument("--rules", type=Path, help="可选清洗规则 YAML")
    command.add_argument("--washing-policy", type=Path, help="group 模式及扩展清洗开关 YAML")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行一次转换，并用 0/2 表示成功或失败。"""
    args = _parser().parse_args(argv)
    try:
        context = convert(
            topology_path=args.topology,
            config_dir=args.config_dir,
            output_dir=args.output_dir,
            profiles_path=args.profiles,
            rules_path=args.rules,
            washing_policy_path=args.washing_policy,
        )
    except Exception as exc:  # CLI 是最外层边界，只向操作者显示简洁错误。
        print(f"转换失败: {exc}", file=sys.stderr)
        return 2
    if context.has_errors:
        print(f"转换失败，详情见 {args.output_dir / 'report.json'}", file=sys.stderr)
        return 2
    print(f"转换完成: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
