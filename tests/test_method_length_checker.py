from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.check_method_lengths import check_file


class MethodLengthCheckerTest(TestCase):
    def test_docstrings_do_not_count_toward_method_length(self):
        docstring = "\n".join(f"    line {index}" for index in range(60))
        source = f'''def documented():
    """Long behavior explanation.

{docstring}
    """
    return 1
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            counts = check_file(path)

        self.assertEqual(counts[0][3], 2)

    def test_nested_docstrings_do_not_count_toward_outer_method(self):
        docstring = "\n".join(f"        detail {index}" for index in range(60))
        source = f'''def outer():
    """Coordinate the nested operation.

    Keep orchestration separate from implementation.
    """
    def inner():
        """Perform the nested operation.

{docstring}
        """
        return 1

    return inner()
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            counts = check_file(path)

        outer = next(entry for entry in counts if entry[2] == "outer")
        self.assertEqual(outer[3], 4)
