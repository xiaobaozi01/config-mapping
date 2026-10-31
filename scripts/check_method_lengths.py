"""检查 Python 函数最多包含 50 个非空、非注释、非 docstring 物理行。

直接运行检查工作区相对 HEAD 的修改；也可传入文件路径检查文件内全部函数。
方法签名、代码和嵌套函数计入，空白、注释和 docstring 不计入。
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


def _own_docstring_lines(node):
    """返回单个函数自身 docstring 占用的物理行号。"""
    if not getattr(node, "body", None):
        return set()
    first = node.body[0]
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return set()
    return set(range(first.lineno, first.end_lineno + 1))


def _docstring_lines(node):
    """返回函数及其嵌套函数中全部 docstring 占用的行号。"""
    lines = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.update(_own_docstring_lines(child))
    return lines


def git_output(*arguments):
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    )


def changed_files():
    tracked = git_output(
        "diff", "--name-only", "HEAD", "--", "*.py"
    ).splitlines()
    untracked = git_output(
        "ls-files", "--others", "--exclude-standard", "--", "*.py"
    ).splitlines()
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
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
        and not lines[token.start[0] - 1][: token.start[1]].strip()
    }
    previous = set()
    if changed_only:
        try:
            previous = function_sources(
                git_output("show", f"HEAD:{path.relative_to(ROOT)}")
            )
        except subprocess.CalledProcessError:
            pass
    counts = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_source_segment(source, node) in previous:
            continue
        docstring_lines = _docstring_lines(node)
        count = sum(
            bool(lines[number - 1].strip())
            and number not in comments
            and number not in docstring_lines
            for number in range(node.lineno, node.end_lineno + 1)
        )
        try:
            display_path = str(path.relative_to(ROOT))
        except ValueError:
            display_path = str(path)
        counts.append((display_path, node.lineno, node.name, count))
    return counts


def main(arguments):
    paths = (
        [Path(name).resolve() for name in arguments]
        if arguments
        else [ROOT / name for name in changed_files()]
    )
    counts = [
        entry
        for path in paths
        if path.is_file()
        for entry in check_file(path, changed_only=not arguments)
    ]
    violations = [entry for entry in counts if entry[3] > LIMIT]
    for path, line, name, count in violations:
        print(f"{path}:{line}: {name}: {count} > {LIMIT}")
    maximum = max((entry[3] for entry in counts), default=0)
    print(
        f"Checked {len(counts)} functions; maximum {maximum} lines; violations {len(violations)}."
    )
    return bool(violations)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
