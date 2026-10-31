# Python Code Style

This document defines the enforceable Python style baseline for this repository.
The rules follow the Google Python Style Guide where practical, with explicit
project exceptions recorded below.

## Scope

- Apply the rules to handwritten Python code under `src/`.
- Exclude `src/impl/cisco/antlr/generated/` because those files are generated
  from the ANTLR grammars.
- Apply the same formatting and lint rules to Python tests and repository
  scripts where the checks reference them.

## Formatting

- Format Python source with Pyink using an 80-character line length.
- Use four spaces per indentation level.
- Keep imports grouped and sorted, with one import per line.
- Use Pyink's selected quote style consistently; the formatter is authoritative
  for mechanical layout.

## Functions and Methods

- Every function, method, nested function, and async function must contain at
  most 50 non-blank, non-comment physical lines.
- Count the complete function signature and code; exclude docstrings, blank
  lines, and comments.
- Count nested function code as part of the enclosing function.
- Do not satisfy the limit by deleting explanations, collapsing unrelated
  statements, or hiding behavior behind excessively generic helpers.
- Every function and method must have a cyclomatic complexity of at most 10.
  Refactor functions that exceed this threshold before merging.

Run the repository checker with:

```console
python scripts/check_method_lengths.py
```

Run all checks before committing changes.
When checking the complete handwritten source tree, pass the source files to
the checker:

```console
python scripts/check_method_lengths.py $(rg --files src -g '*.py')
```

## Static Checks

Ruff enforces the following Google-aligned rule groups:

- `E`, `F`, and `W`: syntax, import, and baseline style errors.
- `I`: deterministic import ordering.
- `N`: Python naming conventions.
- `UP`: syntax that can be simplified for the supported Python version.
- `B`: common logic errors.
- `SIM`: clear simplifications that preserve behavior.
- `RUF`: selected correctness and consistency checks.
- `C901`: cyclomatic complexity must not exceed 10.

Pyink enforces the 80-character source limit. Ruff's visual-width `E501` check is
not enabled separately because it counts CJK characters as two display columns
and disagrees with the Google formatter for Chinese messages.

Project-specific exceptions:

- `RUF001`, `RUF002`, and `RUF003` are ignored because Chinese comments,
  messages, and documentation are intentional.
- ANTLR callback methods keep their framework-required camelCase names.
- `UNKNOWN` and the existing `InvariantViolation` public name are retained for
  domain and API compatibility.

## Documentation

- Every handwritten class, function, method, nested function, constructor,
  property, and magic method must have a docstring.
- The first paragraph explains the concrete behavior, result, or side effect.
- The second paragraph explains why the implementation uses that approach,
  including its business reason, invariant, safety boundary, or ordering.
- Write the two aspects as natural prose without literal field labels such as
  "做什么：", "为什么：", or "流程：".
- Do not use empty descriptions such as "返回结果", "处理数据", or
  "初始化对象".

Run the docstring checker with:

```console
python scripts/check_docstrings.py
```

## Type Annotations

- Add type annotations to public interfaces and repository tooling.
- Prefer built-in generic types such as `list[str]` and `dict[str, int]`.
- Import abstract collection types from `collections.abc`.

## Verification Commands

```console
pyink --check --line-length 80 src
ruff check src
pytest -q
python scripts/check_method_lengths.py
```
