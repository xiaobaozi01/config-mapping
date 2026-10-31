"""检查本次新增、修改的 Python 函数，最多 50 个非空、非纯注释物理行。

直接运行检查工作区相对 HEAD 的修改；也可传入文件路径检查文件内全部函数。
文档字符串计入，嵌套函数的代码也计入外层。
"""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 50


def git_output(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)


def changed_files():
    tracked = git_output("diff", "--name-only", "HEAD", "--", "*.py").splitlines()
    untracked = git_output("ls-files", "--others", "--exclude-standard", "--", "*.py").splitlines()
    return sorted(set(tracked + untracked))


def function_sources(source):
    return {
        ast.get_source_segment(source, node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def check_file(path, *, changed_only=False):
    if "impl/cisco/antlr/generated" in path.as_posix():
        return []
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    comments = {
        token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and not lines[token.start[0] - 1][:token.start[1]].strip()
    }
    previous = set()
    if changed_only:
        try:
            previous = function_sources(git_output("show", f"HEAD:{path.relative_to(ROOT)}"))
        except subprocess.CalledProcessError:
            pass
    counts = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_source_segment(source, node) in previous:
            continue
        count = sum(
            bool(lines[number - 1].strip()) and number not in comments
            for number in range(node.lineno, node.end_lineno + 1)
        )
        counts.append((str(path.relative_to(ROOT)), node.lineno, node.name, count))
    return counts


def main(arguments):
    paths = [Path(name).resolve() for name in arguments] if arguments else [ROOT / name for name in changed_files()]
    counts = [
        entry for path in paths if path.is_file()
        for entry in check_file(path, changed_only=not arguments)
    ]
    violations = [entry for entry in counts if entry[3] > LIMIT]
    for path, line, name, count in violations:
        print(f"{path}:{line}: {name}: {count} > {LIMIT}")
    maximum = max((entry[3] for entry in counts), default=0)
    print(f"Checked {len(counts)} functions; maximum {maximum} lines; violations {len(violations)}.")
    return bool(violations)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
