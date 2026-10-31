from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.check_docstrings import check_file


class DocstringCheckerTest(TestCase):
    def test_missing_docstrings_are_reported_for_all_member_kinds(self):
        source = """class Example:
    def __init__(self):
        self.value = 1

    @property
    def value(self):
        return 1

    def __repr__(self):
        return "Example()"

    def _helper(self):
        return 1
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            violations = check_file(path)

        self.assertEqual(len(violations), 5)

    def test_single_paragraph_is_reported(self):
        source = '''class Example:
    """Stores one value."""
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            violations = check_file(path)

        self.assertEqual(
            [violation.kind for violation in violations], ["paragraphs"]
        )

    def test_literal_labels_are_rejected(self):
        source = '''def execute():
    """做什么：执行转换。

    为什么：保证结果一致。
    """
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            violations = check_file(path)

        self.assertEqual(
            [violation.kind for violation in violations], ["label"]
        )

    def test_structured_template_sections_are_rejected(self):
        source = '''def execute(value):
    """执行转换并返回结果。

    Args:
        value: 输入值。
    """
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            violations = check_file(path)

        self.assertEqual(
            [violation.kind for violation in violations], ["section"]
        )

    def test_two_paragraph_docstring_is_accepted(self):
        source = '''def execute():
    """执行转换并返回稳定结果。

    先完成校验再写入，避免失败时留下部分输出。
    """
    return 1
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            violations = check_file(path)

        self.assertEqual(violations, [])

    def test_generated_antlr_files_are_excluded(self):
        with TemporaryDirectory() as directory:
            generated = (
                Path(directory)
                / "impl"
                / "cisco"
                / "antlr"
                / "generated"
                / "parser.py"
            )
            generated.parent.mkdir(parents=True)
            generated.write_text("class Generated:\n    pass\n", encoding="utf-8")

            violations = check_file(generated)

        self.assertEqual(violations, [])
