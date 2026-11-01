# Group Module Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace each vendor's monolithic `operations/groups.py` with a compatible `operations/groups/` package split by state models, selectors, definitions, merging, expansion, publishing, and capability composition.

**Architecture:** Keep `impl.<vendor>.operations.groups` as the stable public facade. Move pure data to `models`, text selectors to `selectors`, definition/dependency work to `definitions`, semantic merging to `merger`, orchestration to `expander`, Cisco publish work to `publishing`, and capability forwarding to `capabilities`.

**Tech Stack:** Python 3.11+, Protocol typing, pytest/unittest, Ruff, Pyink

**Spec:** `docs/superpowers/specs/2026-11-02-group-module-decomposition-design.md`

## Global Constraints

- Preserve Group priority, selector syntax, exclusion semantics, identity decisions, report fields, and pipeline order.
- Preserve public imports `impl.<vendor>.operations.groups`, `CiscoGroupExpander`, `JunosGroupExpander`, `CiscoGroupOperations`, and `JunosGroupOperations`.
- Keep Cisco and Juniper Group algorithms vendor-specific; do not introduce a shared Group base class.
- Document retains the formal AST, revision, analysis cache, and EditScope ownership.
- Cisco publishes only a successful work-statement tree; Juniper atomically replaces the root through the Editor.
- Any failed active Group expansion keeps the original tree.
- Models, selectors, definitions, and mergers must follow the inward-only dependency rules in the spec.
- Do not create Git commits or push during implementation; leave changes in the working tree.
- Use TDD: add failing architecture tests before structural changes.

---

### Task 1: Lock Group Package Layout and Dependency Direction

**Files:**
- Modify: `tests/test_architecture.py`

**Interfaces:**
- Consumes: `importlib.util.find_spec()` and `ast.parse()`.
- Produces: `GroupModuleLayoutTest`, which fails until both Group packages exist and satisfy the declared dependency rules.

- [ ] **Step 1: Add failing architecture tests**

Add this class before the `if __name__ == "__main__":` block:

```python
class GroupModuleLayoutTest(unittest.TestCase):
    def test_group_packages_have_required_modules(self):
        modules = (
            "impl.cisco.operations.groups.models",
            "impl.cisco.operations.groups.selectors",
            "impl.cisco.operations.groups.definitions",
            "impl.cisco.operations.groups.merger",
            "impl.cisco.operations.groups.expander",
            "impl.cisco.operations.groups.publishing",
            "impl.cisco.operations.groups.capabilities",
            "impl.juniper.operations.groups.models",
            "impl.juniper.operations.groups.selectors",
            "impl.juniper.operations.groups.definitions",
            "impl.juniper.operations.groups.merger",
            "impl.juniper.operations.groups.expander",
            "impl.juniper.operations.groups.capabilities",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_group_single_files_are_removed(self):
        project_root = Path(__file__).parents[1]
        for vendor in ("cisco", "juniper"):
            with self.subTest(vendor=vendor):
                path = (
                    project_root
                    / "src"
                    / "impl"
                    / vendor
                    / "operations"
                    / "groups.py"
                )
                self.assertFalse(path.exists())

    def test_group_submodule_dependencies_point_inward(self):
        project_root = Path(__file__).parents[1]
        forbidden = {
            "models": {
                "selectors",
                "definitions",
                "merger",
                "expander",
                "capabilities",
            },
            "selectors": {
                "definitions",
                "merger",
                "expander",
                "capabilities",
            },
            "definitions": {"merger", "expander", "capabilities"},
            "merger": {"definitions", "expander", "capabilities"},
            "publishing": {"definitions", "merger", "expander", "capabilities"},
        }
        for vendor in ("cisco", "juniper"):
            package = (
                project_root
                / "src"
                / "impl"
                / vendor
                / "operations"
                / "groups"
            )
            for module_name, blocked in forbidden.items():
                path = package / f"{module_name}.py"
                if not path.exists():
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imported = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.level == 1
                    for alias in node.names
                }
                with self.subTest(vendor=vendor, module=module_name):
                    self.assertFalse(imported & blocked)

    def test_group_capability_classes_remain_compatible(self):
        from impl.cisco.operations.groups import (
            CiscoGroupExpander,
            CiscoGroupOperations,
        )
        from impl.juniper.operations.groups import (
            JunosGroupExpander,
            JunosGroupOperations,
        )

        self.assertEqual(
            CiscoGroupExpander.__module__,
            "impl.cisco.operations.groups.expander",
        )
        self.assertEqual(
            CiscoGroupOperations.__module__,
            "impl.cisco.operations.groups.capabilities",
        )
        self.assertEqual(
            JunosGroupExpander.__module__,
            "impl.juniper.operations.groups.expander",
        )
        self.assertEqual(
            JunosGroupOperations.__module__,
            "impl.juniper.operations.groups.capabilities",
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::GroupModuleLayoutTest -q
```

Expected: FAIL because the package modules do not exist.

---

### Task 2: Split the Cisco Group Module

**Files:**
- Delete: `src/impl/cisco/operations/groups.py`
- Create: `src/impl/cisco/operations/groups/__init__.py`
- Create: `src/impl/cisco/operations/groups/models.py`
- Create: `src/impl/cisco/operations/groups/selectors.py`
- Create: `src/impl/cisco/operations/groups/definitions.py`
- Create: `src/impl/cisco/operations/groups/merger.py`
- Create: `src/impl/cisco/operations/groups/expander.py`
- Create: `src/impl/cisco/operations/groups/publishing.py`
- Create: `src/impl/cisco/operations/groups/capabilities.py`
- Test: `tests/test_cisco_groups.py`
- Test: `tests/test_rule_extensions.py`
- Test: `tests/test_semantic_identity.py`

**Interfaces:**
- Consumes: Existing Cisco data classes and `CiscoGroupExpander` algorithms.
- Produces: Cisco `models`, `selectors`, `definitions`, `merger`, `expander`, `publishing`, `capabilities`, and package-level compatibility exports.

- [ ] **Step 1: Move the Cisco data models**

Move these current classes to `models.py` without changing fields or defaults:

```text
GroupStatement
GroupDefinition
GroupApplication
GroupControls
ExpansionState
```

`models.py` may import common outcomes/policies and syntax nodes only. Do not
add algorithm imports.

- [ ] **Step 2: Move pure selector and control parsing**

Move these methods into the state-free `CiscoGroupSelectors` helper in
`selectors.py`:

```text
parse_group_names
rewrite_group_control
selector_matches
selector_literal_length
payload_nodes_at_path
group_definition_from_nodes
```

The helper is composed explicitly by the expander. Keep all regex,
abbreviation and longest-match behavior identical.

- [ ] **Step 3: Move definition discovery**

Move these methods into the `CiscoGroupDefinitions` helper in
`definitions.py`:

```text
has_applied_group(statements)
load_group_definitions(statements, state)
contains_nested_apply(nodes)
build_working_tree(statements, root_names, state)
collect_root_group_names(root)
```

Functions receive `document`, `state`, `selectors` and `publishing` explicitly
where required. Do not retain an expander instance in module globals.

- [ ] **Step 4: Move semantic merging**

Create `GroupMerger` in `merger.py` with constructor:

```python
class GroupMerger:
    def __init__(self, document, policy, outcome):
        self._document = document
        self._policy = policy
        self._outcome = outcome
```

Move the existing merge/conflict/ambiguity methods into this class without
changing signatures or precedence decisions. Keep the identity resolver call at
the same semantic point.

- [ ] **Step 5: Move publishing**

Move these functions to `publishing.py`:

```text
from_context
clone_statements
render_statements
publish_expansion
rollback_expansion
signature
```

`publish_expansion()` remains the only bridge from the Cisco work-statement
tree to the formal AST. It must call the Editor transaction exactly once.

- [ ] **Step 6: Keep orchestration in the expander**

Move `CiscoGroupExpander` to `expander.py`. Keep recursive expansion,
application resolution and transaction orchestration there. It should compose:

```text
definitions.prepare/build functions
GroupMerger
selectors functions
publishing functions
```

Do not move merge decisions or selector matching back into `expander.py`.

- [ ] **Step 7: Add the Cisco capability and package facade**

Create `capabilities.py` with the existing `CiscoGroupOperations`, forwarding to
`CiscoGroupExpander`.

Create `__init__.py` with only:

```python
from .capabilities import CiscoGroupOperations
from .expander import CiscoGroupExpander

__all__ = ["CiscoGroupExpander", "CiscoGroupOperations"]
```

- [ ] **Step 8: Run Cisco Group tests**

Run:

```bash
.venv/bin/pytest tests/test_cisco_groups.py tests/test_rule_extensions.py tests/test_semantic_identity.py -q
.venv/bin/ruff check src/impl/cisco/operations/groups tests/test_cisco_groups.py
```

Expected: PASS.

---

### Task 3: Split the Juniper Group Module

**Files:**
- Delete: `src/impl/juniper/operations/groups.py`
- Create: `src/impl/juniper/operations/groups/__init__.py`
- Create: `src/impl/juniper/operations/groups/models.py`
- Create: `src/impl/juniper/operations/groups/selectors.py`
- Create: `src/impl/juniper/operations/groups/definitions.py`
- Create: `src/impl/juniper/operations/groups/merger.py`
- Create: `src/impl/juniper/operations/groups/expander.py`
- Create: `src/impl/juniper/operations/groups/capabilities.py`
- Test: `tests/test_junos_groups.py`
- Test: `tests/test_semantic_identity.py`
- Test: `tests/test_conversion.py`

**Interfaces:**
- Consumes: Existing Juniper Group data classes and `JunosGroupExpander` algorithms.
- Produces: Juniper `models`, `selectors`, `definitions`, `merger`, `expander`, `capabilities`, and package-level compatibility exports.

- [ ] **Step 1: Move Juniper data models**

Move these classes to `models.py`, preserving fields and defaults:

```text
GroupApplication
ResolvedGroupInheritance
ExpansionState
PreparedExpansion
ResolvedStatement
```

- [ ] **Step 2: Move Juniper selector logic**

Move these methods into the state-free `JunosGroupSelectors` helper in
`selectors.py`:

```text
selector_matches
parse_group_names
group_definition_name
format_group_name
rewrite_group_control
selector_literal_length
payload_nodes_at_path
```

Preserve `<...>` wildcard, `fnmatch`, nested group name parsing and quoted-name
behavior exactly.

- [ ] **Step 3: Move Juniper definition and exclusion handling**

Move the definition-related methods to the `JunosGroupDefinitions` helper in
`definitions.py`:

```text
has_group_controls
prepare_expansion
group_containers
normalize_group_containers
merge_group_definitions
remove_extra_group_containers
add_known_interfaces
group_dependencies
group_exclusions
root_group_exclusions
validate_group_exclusions
collect_root_group_names
resolve_dependency_closure
prune_excluded_groups
```

Also move `record_pruned_groups`, `prune_excluded_definitions` and
`prune_group_references` where they support this lifecycle. Keep exclusion
policy ownership in this module.

- [ ] **Step 4: Move Juniper semantic merging**

Create `GroupMerger` in `merger.py` and move:

```text
record_group_conflict
merge_inherited_children
is_mergeable_group_child
merge_inherited_block
merge_inherited_statement
prepare_merge
resolve_target_statements
handle_unknown_identity_ambiguity
ambiguity_record
merge_statement_by_precedence
```

The merger receives the document, policy and outcome for one expansion call. It
must not cache nodes or survive the call.

- [ ] **Step 5: Keep traversal and orchestration in the expander**

Move `JunosGroupExpander` to `expander.py` and keep:

```text
expand_groups
validate_dependency_chain
validate_root_exclusions
validate_selected_exclusions
select_effective_applications
group_controls_for_path
collect_nested_exclusions
discover_nested_applications
resolve_group_inheritance_for_path
advance_inheritance
expand_current_node
merge_effective_groups
walk_expansion_tree
read_local_group_controls
rewrite_group_controls
```

Preserve the bulk EditScope and `replace_root()` rollback behavior. Do not move
definition normalization or merge precedence into the expander.

- [ ] **Step 6: Add Juniper capability and package facade**

Create `capabilities.py` with `JunosGroupOperations` forwarding to
`JunosGroupExpander`.

Create `__init__.py` with only:

```python
from .capabilities import JunosGroupOperations
from .expander import JunosGroupExpander

__all__ = ["JunosGroupExpander", "JunosGroupOperations"]
```

- [ ] **Step 7: Run Juniper Group tests**

Run:

```bash
.venv/bin/pytest tests/test_junos_groups.py tests/test_semantic_identity.py tests/test_conversion.py -q
.venv/bin/ruff check src/impl/juniper/operations/groups tests/test_junos_groups.py
```

Expected: PASS.

---

### Task 4: Verify Public Compatibility and Regression Behavior

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_semantic_identity.py`
- Modify: `tests/test_conversion.py`

**Interfaces:**
- Consumes: Both new Group packages from Tasks 2 and 3.
- Produces: Compatibility and dependency guarantees for package imports and Group behavior.

- [ ] **Step 1: Run Group architecture and capability tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::GroupModuleLayoutTest tests/test_architecture.py::CapabilityImplementationContractTest -q
```

Expected: PASS.

- [ ] **Step 2: Audit stale imports and private helper exposure**

Run:

```bash
rg -n "operations/groups\\.py|from .*operations\\.groups import" src tests
rg -n "groups\\.(models|selectors|definitions|merger|publishing) import" src tests
```

Expected: package-level public imports remain for extenders and operations;
internal algorithm modules import concrete submodules.

- [ ] **Step 3: Run focused Group regression tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_cisco_groups.py \
  tests/test_junos_groups.py \
  tests/test_semantic_identity.py \
  tests/test_conversion.py \
  tests/test_ast_boundaries.py -q
```

Expected: PASS, including priority, exclusion, conflict, identity and rollback
coverage.

---

### Task 5: Update Documentation and Run Final Gates

**Files:**
- Modify: `README.md`
- Modify: `docs/config-ast.md`
- Modify: `docs/onboarding-guide.md`
- Modify: `docs/processing-flow.md`
- Modify: `docs/configuration-rule-authoring.md`

**Interfaces:**
- Consumes: Final `groups/` package structure.
- Produces: Documentation that distinguishes selector, definition, merge,
  expander, publishing and capability responsibilities.

- [ ] **Step 1: Update documentation**

Replace references to `operations/groups.py` with the concrete Group submodule
that owns the described behavior. Explain that package `__init__.py` is only a
compatibility facade.

- [ ] **Step 2: Run the complete suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass with no removed or weakened Group coverage.

- [ ] **Step 3: Run all quality gates**

Run:

```bash
.venv/bin/pyink --check --line-length 80 src
.venv/bin/ruff check src
.venv/bin/python scripts/check_docstrings.py
.venv/bin/python scripts/check_method_lengths.py $(find src -name '*.py' -type f)
git diff --check
```

Expected: every command exits with status 0.

- [ ] **Step 4: Inspect final Group module sizes**

Run:

```bash
find src/impl/cisco/operations/groups src/impl/juniper/operations/groups \
  -name '*.py' -print0 | xargs -0 wc -l | sort -n
```

Expected: original 1189/1775-line files are gone; no algorithm module is a
renamed copy of the monolith, and `__init__.py` contains no business logic.

- [ ] **Step 5: Leave changes uncommitted**

Run:

```bash
git status --short
```

Expected: Group implementation and test changes are visible in the worktree,
with no commit or push.
