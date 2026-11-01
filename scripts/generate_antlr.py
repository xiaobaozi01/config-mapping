"""用本地 ANTLR 4.13.2 JAR 重现 Python 生成文件；运行程序不需要此工具。"""

import argparse
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAMMARS = {
    "cisco": ROOT / "src/impl/cisco/antlr",
    "juniper": ROOT / "src/impl/juniper/antlr",
}
VERSION = "4.13.2"


def generate_one(
    jar: Path, vendor: str, grammar_root: Path, check: bool
) -> None:
    """生成或检查一个厂商的 grammar 和 Python 目标代码。

    隔离临时输出可避免失败生成污染仓库，并保证两个厂商使用完全相同的工具版本。
    """
    names = sorted(path.name for path in grammar_root.glob("*.g4"))
    with tempfile.TemporaryDirectory() as directory:
        command = [
            "java",
            "-jar",
            str(jar.resolve()),
            "-Dlanguage=Python3",
            "-visitor",
            "-listener",
            "-o",
            directory,
        ]
        subprocess.run([*command, *names], cwd=grammar_root, check=True)
        generated = sorted(Path(directory).glob("*.py"))
        target = grammar_root / "generated"
        if not generated or any(
            f"ANTLR {VERSION}" not in source.read_text() for source in generated
        ):
            raise SystemExit(f"需要 ANTLR {VERSION} 生成工具")
        for source in generated:
            output = target / source.name
            if check:
                if (
                    not output.exists()
                    or output.read_bytes() != source.read_bytes()
                ):
                    raise SystemExit(f"生成文件不一致: {output}")
            else:
                target.mkdir(parents=True, exist_ok=True)
                output.write_bytes(source.read_bytes())
        expected = {source.name for source in generated} | {"__init__.py"}
        if check and {path.name for path in target.glob("*.py")} != expected:
            raise SystemExit("生成目录包含过期文件")
    print(
        f"{vendor} ANTLR generated sources "
        f"{'verified' if check else 'updated'}."
    )


def generate(jar: Path, check: bool, vendor: str = "all") -> None:
    """生成或检查所选厂商的 ANTLR grammar。

    默认处理两个厂商，同时保留单厂商入口以便定位生成差异。
    """
    vendors = GRAMMARS if vendor == "all" else {vendor: GRAMMARS[vendor]}
    for vendor, directory in vendors.items():
        generate_one(jar, vendor, directory, check)


def main():
    """解析命令行参数并执行 ANTLR 生成或检查。

    统一入口让本地更新和 CI 一致性检查复用同一条代码路径。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--vendor",
        choices=("all", "cisco", "juniper"),
        default="all",
    )
    arguments = parser.parse_args()
    generate(arguments.jar, arguments.check, arguments.vendor)


if __name__ == "__main__":
    main()
