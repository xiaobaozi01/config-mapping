# Vendor Directory Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Reorganize `common`, `cisco`, and `juniper` implementation files into responsibility-based packages without changing conversion behavior.

**Architecture:** Keep each vendor's `document.py` as the stable facade, move syntax concerns into `syntax/`, business transformations into `operations/`, and ancillary policies into `support/`. Group shared code into `domain/`, `rules/`, and `support/` while preserving the existing `config_ast/` and `semantic_identity/` packages.

**Tech Stack:** Python 3.11+, unittest/pytest, Ruff, Pyink

**Spec:** `docs/superpowers/specs/2026-11-02-directory-reorganization-design.md`

## Global Constraints

- Preserve conversion behavior, pipeline order, output formats, and public package exports.
- Do not split large business modules during this move.
- Remove old flat implementation modules; do not add compatibility forwarding files.
- Update all internal imports, tests, and documentation to the new paths.
- Do not create a Git commit and do not push to GitHub.
- Finish with 452 tests and 3784 subtests passing, plus all repository quality checks.

---

### Task 1: Lock the Target Package Layout with Tests

**Files:**
- Modify: `tests/test_architecture.py`

**Interfaces:**
- Consumes: Python import discovery through `importlib.util.find_spec()`.
- Produces: `ImplementationPackageLayoutTest`, which fails until the target modules are moved and old flat modules are removed.

- [x] **Step 1: Add the failing layout tests**

Append these classes to `tests/test_architecture.py`, before the final
`if __name__ == "__main__":` block:

```python
class ImplementationPackageLayoutTest(unittest.TestCase):
    def test_target_packages_are_importable(self):
        modules = (
            "impl.common.domain.contracts",
            "impl.common.domain.interface",
            "impl.common.domain.outcomes",
            "impl.common.domain.policies",
            "impl.common.rules.engine",
            "impl.common.rules.cleaning",
            "impl.common.rules.empty_blocks",
            "impl.common.support.config_paths",
            "impl.common.support.errors",
            "impl.common.support.interface_cache",
            "impl.common.support.lab_account",
            "impl.cisco.syntax.ast",
            "impl.cisco.syntax.parser",
            "impl.cisco.syntax.writer",
            "impl.cisco.syntax.semantics",
            "impl.cisco.syntax.rewrite_helpers",
            "impl.cisco.operations.groups",
            "impl.cisco.operations.interfaces",
            "impl.cisco.operations.bundles",
            "impl.cisco.operations.cleaning",
            "impl.cisco.operations.configuration_cleaning",
            "impl.cisco.operations.references",
            "impl.cisco.support.clone_constraints",
            "impl.cisco.support.empty_blocks",
            "impl.cisco.support.identity",
            "impl.juniper.syntax.ast",
            "impl.juniper.syntax.parser",
            "impl.juniper.syntax.writer",
            "impl.juniper.operations.groups",
            "impl.juniper.operations.interfaces",
            "impl.juniper.operations.bundles",
            "impl.juniper.operations.cleaning",
            "impl.juniper.operations.configuration_cleaning",
            "impl.juniper.operations.references",
            "impl.juniper.support.clone_constraints",
            "impl.juniper.support.empty_blocks",
            "impl.juniper.support.identity",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_old_flat_modules_are_removed(self):
        modules = (
            "impl.common.contracts",
            "impl.common.interface",
            "impl.common.outcomes",
            "impl.common.policies",
            "impl.common.cleaning",
            "impl.common.empty_blocks",
            "impl.common.config_paths",
            "impl.common.errors",
            "impl.common.interface_cache",
            "impl.common.lab_account",
            "impl.cisco.ast",
            "impl.cisco.parser",
            "impl.cisco.writer",
            "impl.cisco.semantics",
            "impl.cisco.rewrite_helpers",
            "impl.cisco.groups",
            "impl.cisco.interfaces",
            "impl.cisco.bundles",
            "impl.cisco.cleaning",
            "impl.cisco.configuration_cleaning",
            "impl.cisco.references",
            "impl.cisco.clone_constraints",
            "impl.cisco.empty_blocks",
            "impl.cisco.identity",
            "impl.juniper.ast",
            "impl.juniper.parser",
            "impl.juniper.writer",
            "impl.juniper.groups",
            "impl.juniper.interfaces",
            "impl.juniper.bundles",
            "impl.juniper.cleaning",
            "impl.juniper.configuration_cleaning",
            "impl.juniper.references",
            "impl.juniper.clone_constraints",
            "impl.juniper.empty_blocks",
            "impl.juniper.identity",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec(module))
```

- [x] **Step 2: Run the layout tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::ImplementationPackageLayoutTest -q
```

Expected: FAIL because the target modules do not yet exist and the flat modules
still exist.

---

### Task 2: Reorganize the Common Package

**Files:**
- Move: `src/impl/common/contracts.py` -> `src/impl/common/domain/contracts.py`
- Move: `src/impl/common/interface.py` -> `src/impl/common/domain/interface.py`
- Move: `src/impl/common/outcomes.py` -> `src/impl/common/domain/outcomes.py`
- Move: `src/impl/common/policies.py` -> `src/impl/common/domain/policies.py`
- Move: `src/impl/common/rules.py` -> `src/impl/common/rules/engine.py`
- Move: `src/impl/common/cleaning.py` -> `src/impl/common/rules/cleaning.py`
- Move: `src/impl/common/empty_blocks.py` -> `src/impl/common/rules/empty_blocks.py`
- Move: `src/impl/common/config_paths.py` -> `src/impl/common/support/config_paths.py`
- Move: `src/impl/common/errors.py` -> `src/impl/common/support/errors.py`
- Move: `src/impl/common/interface_cache.py` -> `src/impl/common/support/interface_cache.py`
- Move: `src/impl/common/lab_account.py` -> `src/impl/common/support/lab_account.py`
- Create: `src/impl/common/domain/__init__.py`
- Create: `src/impl/common/rules/__init__.py`
- Create: `src/impl/common/support/__init__.py`
- Modify: `src/impl/common/__init__.py`

**Interfaces:**
- Consumes: Existing domain classes and rule-engine functions.
- Produces: `impl.common.domain.*`, `impl.common.rules.*`, and `impl.common.support.*`; `impl.common.rules` continues to export the existing rule-engine API.

- [x] **Step 1: Create the package directories and move files**

Move every file in the mapping above. Keep source contents unchanged during the
move.

- [x] **Step 2: Add the package initializers**

Create `src/impl/common/domain/__init__.py`:

```python
"""Shared cross-vendor domain contracts, data, and policies."""

from .contracts import VendorConfiguration
from .interface import (
    BundleMode,
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    interface_unit,
    normalized_command,
)
from .outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from .policies import SimulationAdaptationPolicy, WashingPolicy

__all__ = [
    "BundleMode",
    "CleanupOutcome",
    "GroupExpansionOutcome",
    "InterfaceAdaptationOutcome",
    "InterfaceKind",
    "InterfacePruningOutcome",
    "InterfaceSpec",
    "SimulationAdaptationOutcome",
    "SimulationAdaptationPolicy",
    "VendorConfiguration",
    "WashingPolicy",
    "interface_parent",
    "interface_unit",
    "normalized_command",
]
```

Create `src/impl/common/rules/__init__.py`:

```python
"""Shared configuration rule engine and cleaning primitives."""

from .engine import (
    AdaptContext,
    ConfigurationRule,
    FunctionRule,
    PruneContext,
    SimulationContext,
    ValidationContext,
    execute_prune_rules,
    execute_rules,
    merge_interface_results,
    merge_simulation_results,
    validate_rule_registry,
)

__all__ = [
    "AdaptContext",
    "ConfigurationRule",
    "FunctionRule",
    "PruneContext",
    "SimulationContext",
    "ValidationContext",
    "execute_prune_rules",
    "execute_rules",
    "merge_interface_results",
    "merge_simulation_results",
    "validate_rule_registry",
]
```

Create `src/impl/common/support/__init__.py`:

```python
"""Shared paths, errors, caches, and lab-environment constants."""

from .config_paths import config_path
from .errors import InvariantViolation, require_invariant
from .lab_account import LAB_PASSWORD, LAB_USERNAME

__all__ = [
    "InvariantViolation",
    "LAB_PASSWORD",
    "LAB_USERNAME",
    "config_path",
    "require_invariant",
]
```

- [x] **Step 3: Update internal common imports**

Apply these exact module replacements across `src/impl/common`:

```text
impl.common.contracts          -> relative `.contracts` in domain
impl.common.interface          -> relative `.interface` in domain
impl.common.outcomes           -> relative `.outcomes` in domain
impl.common.policies           -> relative `.policies` in domain
.cleaning                      -> `..rules.cleaning` in domain/contracts.py
.outcomes                      -> `..domain.outcomes` in rules/engine.py
.policies                      -> `..domain.policies` in rules/engine.py
.interface                     -> `..domain.interface` in support/interface_cache.py
```

Update `src/impl/common/__init__.py` to import from `.domain.contracts`,
`.domain.interface`, `.domain.outcomes`, and `.domain.policies`, while preserving
its current `__all__`.

- [x] **Step 4: Update imports outside common**

Replace the old common module paths throughout `src`, `tests`, `benchmarks`, and
`scripts`:

```text
impl.common.contracts        -> impl.common.domain.contracts
impl.common.interface        -> impl.common.domain.interface
impl.common.outcomes         -> impl.common.domain.outcomes
impl.common.policies         -> impl.common.domain.policies
impl.common.cleaning         -> impl.common.rules.cleaning
impl.common.empty_blocks     -> impl.common.rules.empty_blocks
impl.common.config_paths     -> impl.common.support.config_paths
impl.common.errors           -> impl.common.support.errors
impl.common.interface_cache  -> impl.common.support.interface_cache
impl.common.lab_account      -> impl.common.support.lab_account
```

Keep imports of `impl.common.rules` unchanged because the new package initializer
preserves that API.

- [x] **Step 5: Run the common-focused tests**

Run:

```bash
.venv/bin/pytest tests/test_cleaning_rules.py tests/test_empty_blocks.py tests/test_semantic_identity.py tests/test_config_ast.py -q
```

Expected: PASS.

---

### Task 3: Reorganize Cisco and Juniper Implementation Packages

**Files:**
- Move the Cisco files listed in the spec into `syntax/`, `operations/`, and `support/`.
- Move the Juniper files listed in the spec into `syntax/`, `operations/`, and `support/`.
- Create: `src/impl/cisco/syntax/__init__.py`
- Create: `src/impl/cisco/operations/__init__.py`
- Create: `src/impl/cisco/support/__init__.py`
- Create: `src/impl/juniper/syntax/__init__.py`
- Create: `src/impl/juniper/operations/__init__.py`
- Create: `src/impl/juniper/support/__init__.py`
- Modify: `src/impl/cisco/document.py`
- Modify: `src/impl/juniper/document.py`
- Modify: `src/impl/cisco/rules/*.py`
- Modify: `src/impl/juniper/rules/*.py`
- Modify: `src/impl/cisco/__init__.py`
- Modify: `src/impl/juniper/__init__.py`

**Interfaces:**
- Consumes: New `common.domain`, `common.rules`, and `common.support` packages from Task 2.
- Produces: Vendored `syntax`, `operations`, and `support` packages while keeping `CiscoDocument`, `CiscoNode`, `CiscoGroupExpander`, `JunosDocument`, `JuniperNode`, and `JunosGroupExpander` available at package level.

- [x] **Step 1: Move the vendor files**

Apply the Cisco and Juniper move mappings from the spec. Preserve file contents
during the move.

- [x] **Step 2: Add lightweight package initializers**

Create each new package initializer with a one-line package docstring. Re-export
only stable facade symbols where needed:

```python
# src/impl/cisco/syntax/__init__.py
"""Cisco configuration syntax model, parsing, and rendering."""

from .ast import CiscoNode

__all__ = ["CiscoNode"]
```

```python
# src/impl/cisco/operations/__init__.py
"""Cisco configuration transformation operations."""
```

```python
# src/impl/cisco/support/__init__.py
"""Cisco image policies, semantic identity, and operation constraints."""
```

```python
# src/impl/juniper/syntax/__init__.py
"""Junos configuration syntax model, parsing, and rendering."""

from .ast import JuniperNode

__all__ = ["JuniperNode"]
```

```python
# src/impl/juniper/operations/__init__.py
"""Junos configuration transformation operations."""
```

```python
# src/impl/juniper/support/__init__.py
"""Junos image policies, semantic identity, and operation constraints."""
```

- [x] **Step 3: Update vendor-local imports**

Apply these relative-import changes:

```text
syntax/parser.py:          .ast stays `.ast`
syntax/writer.py:          .ast stays `.ast`
syntax/ast.py:             .semantics stays `.semantics`
operations/groups.py:      .document -> ..document
operations/interfaces.py:  .document -> ..document, .writer -> ..syntax.writer
operations/bundles.py:     .interfaces stays `.interfaces`
operations/cleaning.py:    .document -> ..document
operations/references.py:  .semantics -> ..syntax.semantics,
                           .ast -> ..syntax.ast
support/clone_constraints.py:
    Cisco `.semantics` -> `..syntax.semantics`
    Juniper `.document` -> `..document`
document.py:
    `.ast` -> `.syntax.ast`
    `.parser` -> `.syntax.parser`
    `.writer` -> `.syntax.writer`
    `.semantics` -> `.syntax.semantics`
    `.rewrite_helpers` -> `.syntax.rewrite_helpers`
    `.groups` -> `.operations.groups`
    `.interfaces` -> `.operations.interfaces`
    `.bundles` -> `.operations.bundles`
    `.cleaning` -> `.operations.cleaning`
    `.configuration_cleaning` -> `.operations.configuration_cleaning`
    `.references` -> `.operations.references`
    `.clone_constraints` -> `.support.clone_constraints`
    `.empty_blocks` -> `.support.empty_blocks`
    `.identity` -> `.support.identity`
rules/*.py:
    `..ast` -> `..syntax.ast`
    `..semantics` -> `..syntax.semantics`
    `..rewrite_helpers` -> `..syntax.rewrite_helpers`
    `..interfaces` -> `..operations.interfaces`
    `..groups` -> `..operations.groups`
    `..identity` -> `..support.identity`
```

Update package initializers to import `CiscoNode` from `.syntax.ast` and
`JuniperNode` from `.syntax.ast`; import group expanders from
`.operations.groups`. Keep `CiscoDocument` and `JunosDocument` imports at their
existing package-level names.

- [x] **Step 4: Update all test imports**

Replace deep old vendor module paths with the new package paths. Package-level
imports from `impl.cisco` and `impl.juniper` must remain unchanged.

Run:

```bash
rg -n "impl\.(cisco|juniper)\.(ast|parser|writer|semantics|rewrite_helpers|groups|interfaces|bundles|cleaning|configuration_cleaning|references|clone_constraints|empty_blocks|identity)" tests src docs
```

Expected: no stale imports or documentation paths.

- [x] **Step 5: Run the layout tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::ImplementationPackageLayoutTest -q
```

Expected: PASS.

---

### Task 4: Update Documentation and Verify the Complete Repository

**Files:**
- Modify: `README.md`
- Modify: `docs/config-ast.md`
- Modify: `docs/configuration-rule-authoring.md`
- Modify: `docs/onboarding-guide.md`
- Modify: `docs/processing-flow.md`
- Modify: `docs/rule-templates/internal-model-prompt.md`
- Modify: `tests/test_architecture.py`

**Interfaces:**
- Consumes: Reorganized packages from Tasks 2 and 3.
- Produces: Documentation and architecture checks that reference only the final layout.

- [x] **Step 1: Update documentation paths**

Replace old module paths with the new responsibility-based paths. Do not rewrite
business descriptions beyond what is needed to identify the new file locations.

- [x] **Step 2: Run the architecture suite**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py tests/test_ast_boundaries.py -q
```

Expected: PASS.

- [x] **Step 3: Run the complete test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: `452 passed, 3784 subtests passed`.

- [x] **Step 4: Run all repository quality gates**

Run:

```bash
.venv/bin/pyink --check --line-length 80 src
.venv/bin/ruff check src
.venv/bin/python scripts/check_method_lengths.py $(find src -name '*.py' -type f)
.venv/bin/python scripts/check_docstrings.py
git diff --check
```

Expected: every command exits with status 0.

- [x] **Step 5: Inspect the final worktree without committing**

Run:

```bash
git status --short
find src/impl/common src/impl/cisco src/impl/juniper -maxdepth 2 -type d | sort
```

Expected: the new package hierarchy is present, old flat modules are absent, and
all changes remain uncommitted.
