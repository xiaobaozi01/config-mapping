"""检查 Python 函数最多包含 50 个非空、非注释、非 docstring 物理行。

直接运行检查工作区相对 HEAD 的修改；也可传入文件路径检查文件内全部函数。
方法签名、代码和嵌套函数计入，空白、注释和 docstring 不计入。
"""

from __future__ import annotations

import ast
import contextlib
import io
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 50


def _own_docstring_lines(node):
    """返回单个函数自身 docstring 占用的物理行号。

    门禁不统计文档说明，因此需要从物理行范围中精确扣除对应行。
    """
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
    """返回函数及其嵌套函数中全部 docstring 占用的行号。

    嵌套函数代码计入外层长度，其 docstring 也必须按同一规则排除。
    """
    lines = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.update(_own_docstring_lines(child))
    return lines


def git_output(*arguments):
    """执行 git 命令并返回标准输出。

    统一工作目录和文本解码可让变更文件与历史函数读取保持一致。
    """
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    )


def changed_files():
    """返回相对 HEAD 修改或新增的 Python 文件。

    同时覆盖已跟踪 diff 和未跟踪文件，避免新增模块绕过函数长度门禁。
    """
    tracked = git_output(
        "diff", "--name-only", "HEAD", "--", "*.py"
    ).splitlines()
    untracked = git_output(
        "ls-files", "--others", "--exclude-standard", "--", "*.py"
    ).splitlines()
    return sorted(set(tracked + untracked))


def function_fingerprints(source):
    """返回源文件全部函数的 AST 指纹，忽略纯格式变化。

    门禁只应复检语义修改的函数，格式化不应把历史长函数重新纳入检查。
    """
    return {
        ast.dump(node, include_attributes=False)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def check_file(path, *, changed_only=False):
    """返回一个 Python 文件中超限函数的统计记录。

    生成代码由生成一致性测试负责，手写代码则按有效物理行和复杂度独立检查。
    """
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
        with contextlib.suppress(subprocess.CalledProcessError):
            previous = function_fingerprints(
                git_output("show", f"HEAD:{path.relative_to(ROOT)}")
            )
    counts = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.dump(node, include_attributes=False) in previous:
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
    """运行函数长度检查并返回是否发现违规。

    无参数时只检查本次语义变更，显式路径则用于全量审计和发布前验收。
    """
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
