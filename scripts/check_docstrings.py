"""检查手写类和函数/方法是否满足两段式 docstring 规范。"""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL_PATTERN = re.compile(r"(?:做什么|为什么|流程)\s*[:：]")
SECTION_PATTERN = re.compile(
    r"(?m)^\s*(?:Args|Arguments|Returns|Raises|Yields|Attributes)\s*:"
)


@dataclass(frozen=True, slots=True)
class Violation:
    """保存一条 docstring 规范违规记录。

    稳定记录路径、行号、成员名和类型，便于门禁输出可定位且可重复比较。
    """

    path: str
    line: int
    name: str
    kind: str


def _member_name(node, parents):
    """构造包含类名和嵌套作用域的稳定成员名称。

    仅使用成员名会在嵌套函数中产生歧义，完整作用域可让定位结果保持唯一。
    """
    scope = [parent.name for parent in parents if hasattr(parent, "name")]
    return ".".join([*scope, node.name])


def _iter_members(tree):
    """递归遍历类、函数和嵌套函数。

    文档规范覆盖所有手写成员，统一迭代可避免遗漏构造器、属性和私有函数。
    """
    parents = []

    def visit(node):
        """递归产出当前语法树中的成员及其父级作用域。

        维护动态父级栈既保持声明顺序，也能为嵌套函数生成完整稳定名称。
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                yield child, tuple(parents)
                parents.append(child)
                yield from visit(child)
                parents.pop()

    yield from visit(tree)


def check_file(path: Path) -> list[Violation]:
    """返回单个 Python 文件中的全部 docstring 违规。

    所有 Python 源文件统一要求自然语言两段说明。
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations = []
    for node, parents in _iter_members(tree):
        name = _member_name(node, parents)
        docstring = ast.get_docstring(node, clean=False)
        if docstring is None or not docstring.strip():
            violations.append(
                Violation(str(path), node.lineno, name, "missing")
            )
            continue
        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\n\s*\n", docstring.strip())
            if paragraph.strip()
        ]
        if len(paragraphs) < 2:
            violations.append(
                Violation(str(path), node.lineno, name, "paragraphs")
            )
        if LABEL_PATTERN.search(docstring):
            violations.append(Violation(str(path), node.lineno, name, "label"))
        if SECTION_PATTERN.search(docstring):
            violations.append(
                Violation(str(path), node.lineno, name, "section")
            )
    return violations


def check_paths(paths):
    """扫描文件和目录，并汇总全部违规。

    同时接受单文件和目录，可让本地定位与全量门禁复用同一检查函数。
    """
    violations = []
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            violations.extend(check_file(path))
        elif path.is_dir():
            for source in sorted(path.rglob("*.py")):
                violations.extend(check_file(source))
    return violations


def main(arguments=None):
    """运行全量 docstring 检查并返回进程退出状态。

    默认扫描源码树，显式路径用于验证本次变更或排查单个模块。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(arguments)
    paths = [Path(value).resolve() for value in args.paths]
    if not paths:
        paths = [ROOT / "src"]
    violations = check_paths(paths)
    messages = {
        "missing": "缺少 docstring",
        "paragraphs": "docstring 少于两个说明段落",
        "label": "docstring 包含禁止的字面标签",
        "section": "docstring 包含禁止的结构化模板段落",
    }
    for item in violations:
        print(f"{item.path}:{item.line}: {item.name}: {messages[item.kind]}")
    print(f"Checked docstrings; violations {len(violations)}.")
    return bool(violations)


if __name__ == "__main__":
    raise SystemExit(main())
