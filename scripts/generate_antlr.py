"""用本地 ANTLR 4.13.2 JAR 重现 Python 生成文件；运行程序不需要此工具。"""

import argparse
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAMMARS = ROOT / "src/impl/cisco/antlr"
VERSION = "4.13.2"


def generate(jar: Path, check: bool) -> None:
    with tempfile.TemporaryDirectory() as directory:
        command = ["java", "-jar", str(jar.resolve()), "-Dlanguage=Python3", "-visitor", "-listener", "-o", directory]
        subprocess.run([*command, "CiscoLexer.g4", "CiscoParser.g4"], cwd=GRAMMARS, check=True)
        generated = sorted(Path(directory).glob("*.py"))
        target = GRAMMARS / "generated"
        if not generated or any(f"ANTLR {VERSION}" not in source.read_text() for source in generated):
            raise SystemExit(f"需要 ANTLR {VERSION} 生成工具")
        for source in generated:
            output = target / source.name
            if check:
                if not output.exists() or output.read_bytes() != source.read_bytes():
                    raise SystemExit(f"生成文件不一致: {output}")
            else:
                target.mkdir(parents=True, exist_ok=True)
                output.write_bytes(source.read_bytes())
        expected = {source.name for source in generated} | {"__init__.py"}
        if check and {path.name for path in target.glob("*.py")} != expected:
            raise SystemExit("生成目录包含过期文件")
    print("ANTLR generated sources verified." if check else "ANTLR generated sources updated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    generate(arguments.jar, arguments.check)


if __name__ == "__main__":
    main()
