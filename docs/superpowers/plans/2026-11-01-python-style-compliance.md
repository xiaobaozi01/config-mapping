# Python Style Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every handwritten Python function in `src/` comply with the
50-line team limit and enforce an 80-column, Google-aligned Python style
baseline without changing application behavior.

**Architecture:** Preserve public interfaces and existing behavior while
extracting cohesive helpers from oversized functions. Centralize mechanical
formatting and lint configuration in `pyproject.toml`, document the policy in
`docs/python-style.md`, and verify each module group with the existing test
suite.

**Tech Stack:** Python 3.11+, Pyink 26.5.1, Ruff 0.17.3, pytest, unittest

**Spec:** `docs/python-style.md`

**Execution status:** Implementation and verification completed on 2026-11-01.
Git commits remain intentionally pending user approval.

## Global Constraints

- Exclude `src/impl/cisco/antlr/generated/` from all checks.
- Check all 66 handwritten Python files under `src/`.
- Use an 80-character line length.
- Limit every function and method to 50 non-blank, non-comment physical lines.
- Limit every function and method to a cyclomatic complexity of 10.
- Include function signatures and nested function code, but exclude docstrings,
  comments, and blank lines from the count.
- Preserve behavior; the existing 396 tests and 3801 subtests are the baseline.
- Preserve Chinese comments, messages, and documentation.
- Keep ANTLR callback method names unchanged.

---

### Task 1: Establish Reproducible Style Checks

**Files:**
- Modify: `pyproject.toml`
- Use: `docs/python-style.md`
- Create: `.github/workflows/python-quality.yml`

**Interfaces:**
- Consumes: Existing pytest configuration and `scripts/check_method_lengths.py`.
- Produces: `pyink`, `ruff`, and `pytest` developer dependencies plus CI commands
  that run the same checks locally and remotely.

- [ ] **Step 1: Add development dependencies**

Add an optional `dev` dependency group containing `pytest`, `pyink==26.5.1`,
and `ruff==0.17.3`.

- [ ] **Step 2: Add formatter and lint configuration**

Set Pyink line length to 80. Set Ruff target version to `py311`, line length to
80, generated-code exclusion, rule selection from `docs/python-style.md`, and
the documented project exceptions.

- [ ] **Step 3: Add the quality workflow**

Create a GitHub Actions workflow that installs the project with the `dev`
extra, checks formatting, runs Ruff, checks method lengths, and runs pytest.

- [ ] **Step 4: Verify baseline commands**

Run: `pytest -q`

Expected: 396 tests pass and 3801 subtests pass before source refactoring.

- [ ] **Step 5: Commit the tooling baseline**

```bash
git add pyproject.toml .github/workflows/python-quality.yml docs/python-style.md
git commit -m "Add Python style quality checks"
```

### Task 2: Split Adaptation Pipeline Methods

**Files:**
- Modify: `src/adaptation/service.py`
- Modify: `src/adaptation/nni.py`
- Modify: `src/adaptation/groups.py`
- Modify: `src/impl/common/empty_blocks.py`
- Test: `tests/test_conversion.py`
- Test: `tests/test_architecture.py`
- Test: `tests/test_nni_target_safety.py`
- Test: `tests/test_empty_blocks.py`

**Interfaces:**
- Consumes: Existing `ConversionContext`, `DeviceContext`, `TopologyWorkbook`,
  pipeline stages, and result models.
- Produces: The same public functions and classes with unchanged signatures.

- [ ] **Step 1: Record the behavior baseline**

Run: `pytest -q tests/test_conversion.py tests/test_architecture.py tests/test_nni_target_safety.py tests/test_empty_blocks.py`

Expected: PASS.

- [ ] **Step 2: Extract `prepare_context` and `write_outputs` helpers**

In `src/adaptation/service.py`, isolate context loading and output publication
steps. Keep `prepare_context` and `write_outputs` below 50 counted lines.

- [ ] **Step 3: Extract NNI planning and allocation helpers**

In `src/adaptation/nni.py`, separate endpoint resolution, target capacity
calculation, allocation, and device-plan application. Keep `process` and
`_allocate_targets` below 50 counted lines.

- [ ] **Step 4: Extract group processing**

In `src/adaptation/groups.py`, move interface collection and report construction
out of `process` into focused private helpers.

- [ ] **Step 5: Extract empty-block policy parsing**

In `src/impl/common/empty_blocks.py`, isolate policy validation and rule
construction so `load_empty_block_policy` remains below 50 counted lines.

- [ ] **Step 6: Run focused tests**

Run: `pytest -q tests/test_conversion.py tests/test_architecture.py tests/test_nni_target_safety.py tests/test_empty_blocks.py`

Expected: PASS with the same assertion counts as Step 1.

- [ ] **Step 7: Commit the adaptation changes**

```bash
git add src/adaptation/service.py src/adaptation/nni.py src/adaptation/groups.py src/impl/common/empty_blocks.py
git commit -m "Split oversized adaptation methods"
```

### Task 3: Split Juniper Document and Interface Methods

**Files:**
- Modify: `src/impl/juniper/document.py`
- Modify: `src/impl/juniper/cleaning.py`
- Modify: `src/impl/juniper/interfaces.py`
- Test: `tests/test_junos_ast.py`
- Test: `tests/test_junos_reference_rewrite.py`
- Test: `tests/test_junos_empty_blocks.py`
- Test: `tests/test_junos_pruning.py`
- Test: `tests/test_junos_pruning_scope.py`

**Interfaces:**
- Consumes: Existing Junos node model, parser, interface classifier, and
  reference rewriter.
- Produces: Unchanged `JunosDocument` and interface-analysis behavior.

- [ ] **Step 1: Record the behavior baseline**

Run: `pytest -q tests/test_junos_ast.py tests/test_junos_reference_rewrite.py tests/test_junos_empty_blocks.py tests/test_junos_pruning.py tests/test_junos_pruning_scope.py`

Expected: PASS.

- [ ] **Step 2: Extract inline-fragment parsing**

Split `src/impl/juniper/document.py::_expand_inline_fragments` into parsing,
replacement, and span-rewrite helpers.

- [ ] **Step 3: Extract management-cleaning steps**

Split `src/impl/juniper/cleaning.py::clean_management_access` into target
discovery, statement removal, and outcome reporting helpers.

- [ ] **Step 4: Extract interface-name and domain helpers**

Split `business_interface_names`, `_activate_domain_gateways`, and
`_collect_active_vlans` into focused private functions without changing their
return types.

- [ ] **Step 5: Run focused tests**

Run: `pytest -q tests/test_junos_ast.py tests/test_junos_reference_rewrite.py tests/test_junos_empty_blocks.py tests/test_junos_pruning.py tests/test_junos_pruning_scope.py`

Expected: PASS.

- [ ] **Step 6: Commit the Juniper document changes**

```bash
git add src/impl/juniper/document.py src/impl/juniper/cleaning.py src/impl/juniper/interfaces.py
git commit -m "Split oversized Juniper document methods"
```

### Task 4: Split Juniper Group Methods

**Files:**
- Modify: `src/impl/juniper/groups.py`
- Test: `tests/test_junos_groups.py`
- Test: `tests/test_conversion.py`

**Interfaces:**
- Consumes: Existing group definitions, inheritance resolution, statement
  matching, and group-expansion outcomes.
- Produces: Unchanged group expansion and conflict reporting.

- [ ] **Step 1: Record the behavior baseline**

Run: `pytest -q tests/test_junos_groups.py tests/test_conversion.py`

Expected: PASS.

- [ ] **Step 2: Extract inherited-statement merge phases**

Split `_merge_inherited_statement` into matching, conflict recording, and
selected-statement application helpers.

- [ ] **Step 3: Extract ambiguous-identity handling**

Split `_handle_unknown_identity_ambiguity` into candidate collection,
ambiguity diagnosis, and rewrite helpers.

- [ ] **Step 4: Extract group expansion phases**

Split `expand_groups`, `_resolve_group_inheritance_for_path`, and
`_expand_current_node` into definition loading, inherited-path resolution, and
node publication helpers.

- [ ] **Step 5: Run focused tests**

Run: `pytest -q tests/test_junos_groups.py tests/test_conversion.py`

Expected: PASS.

- [ ] **Step 6: Commit the group changes**

```bash
git add src/impl/juniper/groups.py
git commit -m "Split oversized Juniper group methods"
```

### Task 5: Apply Repository-Wide Formatting and Lint Fixes

**Files:**
- Modify: All handwritten Python files under `src/`
- Modify: `src/impl/cisco/antlr/parsing.py` only for lint exceptions
- Modify: `src/impl/common/errors.py` when an API-compatible rename is required

**Interfaces:**
- Consumes: The completed method refactors.
- Produces: Pyink-formatted source with zero selected Ruff violations.

- [ ] **Step 1: Format handwritten source**

Run: `pyink --line-length 80 src`

Expected: 66 files are formatted; generated ANTLR files remain unchanged.

- [ ] **Step 2: Apply safe import and syntax fixes**

Run: `ruff check src --fix`

Expected: Safe import order, obsolete imports, and syntax annotations are fixed.

- [ ] **Step 3: Resolve remaining lint findings**

Address unused values, undefined names, loop-variable closures, mutable class
defaults, and naming findings according to the documented exceptions.

- [ ] **Step 4: Verify formatting and lint**

Run: `pyink --check --line-length 80 src`

Run: `ruff check src`

Expected: Both commands exit 0.

- [ ] **Step 5: Commit formatting and lint changes**

```bash
git add src
git commit -m "Apply Google-aligned Python style"
```

### Task 6: Final Verification

**Files:**
- Verify: `docs/python-style.md`
- Verify: `scripts/check_method_lengths.py`
- Verify: `.github/workflows/python-quality.yml`
- Verify: all handwritten Python under `src/`

**Interfaces:**
- Consumes: All previous tasks.
- Produces: A clean, reproducible code-quality baseline.

- [ ] **Step 1: Check all method lengths**

Run: `python scripts/check_method_lengths.py $(rg --files src -g '*.py')`

Expected: `violations 0`.

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`

Expected: 396 tests pass and 3801 subtests pass.

- [ ] **Step 3: Check the complete style baseline**

Run: `pyink --check --line-length 80 src`

Run: `ruff check src`

Run: `python -m compileall -q src tests`

Expected: All commands exit 0.

- [ ] **Step 4: Review the final diff**

Run: `git diff --check`

Run: `git diff --stat`

Expected: No whitespace errors; changes are limited to style compliance,
focused helper extraction, tests, documentation, and quality configuration.
