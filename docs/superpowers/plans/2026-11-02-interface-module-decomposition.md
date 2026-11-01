# Interface Module Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace each vendor's monolithic `operations/interfaces.py` with a compatible `operations/interfaces/` package split by access, analysis, business, transform, and capability composition.

**Architecture:** The package path `impl.<vendor>.operations.interfaces` remains the stable public facade. `access` owns read-only tree lookup, `business` owns business/VLAN inference, `analysis` owns cross-vendor specifications and analysis capability, `transform` owns mutations and transactions, and `capabilities` composes Analysis/Transform without long-lived state.

**Tech Stack:** Python 3.11+, Protocol typing, pytest/unittest, Ruff, Pyink

**Spec:** `docs/superpowers/specs/2026-11-02-interface-module-decomposition-design.md`

## Global Constraints

- Preserve `impl.<vendor>.operations.interfaces` public imports throughout and after migration.
- Preserve interface algorithms, output, report fields, cache invalidation, and transaction behavior.
- `access.py` must not import analysis, business, or transform.
- `business.py` must not import analysis or transform.
- `analysis.py` and `transform.py` must not import each other.
- Document continues to own interface analysis caches, revision, and EditScope coordination.
- Analysis and business functions remain read-only and create no Editor.
- Transform public methods continue to own local `document.edit().bulk()` transactions.
- Rules and support modules import concrete submodules rather than private helpers re-exported by the package.
- Do not create Git commits or push during implementation; leave changes in the working tree.
- Use TDD: add failing architecture tests before structural changes.

---

### Task 1: Lock the Target Package and Dependency Shape

**Files:**
- Modify: `tests/test_architecture.py`

**Interfaces:**
- Consumes: `importlib.util.find_spec()`, `ast.parse()`, and the current interface capability classes.
- Produces: `InterfaceModuleLayoutTest`, which fails until both vendor packages and dependency directions match the spec.

- [ ] **Step 1: Add failing architecture tests**

Append this class before the final `if __name__ == "__main__":` block in
`tests/test_architecture.py`:

```python
class InterfaceModuleLayoutTest(unittest.TestCase):
    def test_interface_packages_have_required_modules(self):
        modules = (
            "impl.cisco.operations.interfaces.access",
            "impl.cisco.operations.interfaces.analysis",
            "impl.cisco.operations.interfaces.business",
            "impl.cisco.operations.interfaces.capabilities",
            "impl.cisco.operations.interfaces.transform",
            "impl.juniper.operations.interfaces.access",
            "impl.juniper.operations.interfaces.analysis",
            "impl.juniper.operations.interfaces.business",
            "impl.juniper.operations.interfaces.capabilities",
            "impl.juniper.operations.interfaces.transform",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_interface_single_files_are_removed(self):
        project_root = Path(__file__).parents[1]
        for vendor in ("cisco", "juniper"):
            with self.subTest(vendor=vendor):
                path = (
                    project_root
                    / "src"
                    / "impl"
                    / vendor
                    / "operations"
                    / "interfaces.py"
                )
                self.assertFalse(path.exists())

    def test_interface_submodule_dependencies_point_inward(self):
        project_root = Path(__file__).parents[1]
        forbidden = {
            "access": {"analysis", "business", "transform"},
            "business": {"analysis", "transform"},
            "analysis": {"transform"},
            "transform": {"analysis"},
        }
        for vendor in ("cisco", "juniper"):
            package = (
                project_root
                / "src"
                / "impl"
                / vendor
                / "operations"
                / "interfaces"
            )
            for module_name, blocked in forbidden.items():
                path = package / f"{module_name}.py"
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

    def test_capability_classes_remain_compatible(self):
        from impl.cisco.operations.interfaces import (
            CiscoInterfaceAnalysis,
            CiscoInterfaceTransform,
        )
        from impl.juniper.operations.interfaces import (
            JunosInterfaceAnalysis,
            JunosInterfaceTransform,
        )

        self.assertEqual(
            CiscoInterfaceAnalysis.__module__,
            "impl.cisco.operations.interfaces.analysis",
        )
        self.assertEqual(
            CiscoInterfaceTransform.__module__,
            "impl.cisco.operations.interfaces.transform",
        )
        self.assertEqual(
            JunosInterfaceAnalysis.__module__,
            "impl.juniper.operations.interfaces.analysis",
        )
        self.assertEqual(
            JunosInterfaceTransform.__module__,
            "impl.juniper.operations.interfaces.transform",
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::InterfaceModuleLayoutTest -q
```

Expected: FAIL because the interface packages do not yet exist.

---

### Task 2: Split the Cisco Interface Module

**Files:**
- Delete: `src/impl/cisco/operations/interfaces.py`
- Create: `src/impl/cisco/operations/interfaces/__init__.py`
- Create: `src/impl/cisco/operations/interfaces/access.py`
- Create: `src/impl/cisco/operations/interfaces/analysis.py`
- Create: `src/impl/cisco/operations/interfaces/business.py`
- Create: `src/impl/cisco/operations/interfaces/capabilities.py`
- Create: `src/impl/cisco/operations/interfaces/transform.py`
- Modify: `src/impl/cisco/operations/bundles.py`
- Modify: `src/impl/cisco/rules/pruning.py`
- Modify: `src/impl/cisco/rules/compatibility.py`
- Modify: `src/impl/cisco/rules/scopes.py`
- Modify: `src/impl/cisco/support/clone_constraints.py`
- Test: `tests/test_cisco_compatibility.py`
- Test: `tests/test_interface_analysis_cache.py`
- Test: `tests/test_rule_extensions.py`

**Interfaces:**
- Consumes: Existing Cisco interface functions and the current capability Protocols.
- Produces: Cisco `access`, `analysis`, `business`, `transform`, `capabilities`, and package-level compatibility exports.

- [ ] **Step 1: Create the Cisco package skeleton and move code**

Move existing implementations without changing algorithms:

```text
access.py
    interface_nodes
    find_interface_node
    resolve_interface

analysis.py
    interface_specs
    interface_kind
    bundle_members
    logical_names_under
    CiscoInterfaceAnalysis

business.py
    interface_has_business
    bundle_binding_is_ambiguous
    business_interface_names
    bridge_domain_bindings
    gateway_inner_vlan_hints
    external_configuration_text

transform.py
    remove_interface
    rename_interface_tree
    clone_interface_tree
    ensure_parent_interface
    node_identity
    clone_fragment
    merge_duplicate_interface
    insert_top_level_before_end
    CiscoInterfaceTransform

capabilities.py
    CiscoInterfaceCapabilities
```

Import rules:

```text
analysis.py      -> access.py, business.py
business.py      -> access.py
transform.py     -> access.py, syntax modules
capabilities.py  -> analysis.py, transform.py
__init__.py      -> capability classes and compatibility helper exports only
```

The package `__init__.py` must re-export these existing public imports:

```python
from .access import find_interface_node, interface_nodes, resolve_interface
from .analysis import (
    CiscoInterfaceAnalysis,
    bundle_members,
    interface_kind,
    interface_specs,
    logical_names_under,
)
from .business import business_interface_names
from .capabilities import CiscoInterfaceCapabilities
from .transform import (
    CiscoInterfaceTransform,
    clone_interface_tree,
    ensure_parent_interface,
    remove_interface,
    rename_interface_tree,
)

__all__ = [
    "CiscoInterfaceAnalysis",
    "CiscoInterfaceCapabilities",
    "CiscoInterfaceTransform",
    "bundle_members",
    "business_interface_names",
    "clone_interface_tree",
    "ensure_parent_interface",
    "find_interface_node",
    "interface_kind",
    "interface_nodes",
    "interface_specs",
    "logical_names_under",
    "remove_interface",
    "rename_interface_tree",
    "resolve_interface",
]
```

- [ ] **Step 2: Update Cisco production imports**

Use concrete modules:

```python
# operations/bundles.py
from .interfaces.access import find_interface_node
from .interfaces.transform import ensure_parent_interface

# rules/pruning.py, rules/compatibility.py, rules/scopes.py,
# support/clone_constraints.py
from ..operations.interfaces.access import interface_nodes
```

Use `business` for business-only functions and `analysis` for analysis capability
calls. Do not import private helper names from the package facade.

- [ ] **Step 3: Update Cisco test patch/import paths**

Update tests that monkeypatch implementation functions to target the concrete
submodule. Keep package-level public imports in behavior tests where they test
compatibility.

- [ ] **Step 4: Run Cisco-focused tests**

Run:

```bash
.venv/bin/pytest tests/test_interfaces.py tests/test_interface_analysis_cache.py tests/test_cisco_compatibility.py tests/test_cisco_pruning.py tests/test_cisco_groups.py tests/test_rule_extensions.py -q
```

Expected: PASS.

---

### Task 3: Split the Juniper Interface Module

**Files:**
- Delete: `src/impl/juniper/operations/interfaces.py`
- Create: `src/impl/juniper/operations/interfaces/__init__.py`
- Create: `src/impl/juniper/operations/interfaces/access.py`
- Create: `src/impl/juniper/operations/interfaces/analysis.py`
- Create: `src/impl/juniper/operations/interfaces/business.py`
- Create: `src/impl/juniper/operations/interfaces/capabilities.py`
- Create: `src/impl/juniper/operations/interfaces/transform.py`
- Modify: `src/impl/juniper/operations/bundles.py`
- Modify: `src/impl/juniper/rules/pruning.py`
- Modify: `src/impl/juniper/rules/compatibility.py`
- Modify: `src/impl/juniper/rules/simulation.py`
- Modify: `src/impl/juniper/support/clone_constraints.py`
- Test: `tests/test_junos_pruning.py`
- Test: `tests/test_junos_pruning_scope.py`
- Test: `tests/test_interface_analysis_cache.py`
- Test: `tests/test_interfaces.py`

**Interfaces:**
- Consumes: Existing Juniper interface functions and current capability Protocols.
- Produces: Juniper `access`, `analysis`, `business`, `transform`, `capabilities`, and package-level compatibility exports.

- [ ] **Step 1: Create the Juniper package skeleton and move code**

Move existing implementations without changing algorithms:

```text
access.py
    interfaces_block
    interface_nodes
    interface_name
    unit_nodes
    unit_number
    find_vlan
    find_inner_vlan
    find_interface_node
    resolve_interface
    walk_effective_nodes

analysis.py
    interface_specs
    interface_kind
    bundle_members
    logical_names_under
    JunosInterfaceAnalysis

business.py
    interface_has_business
    bundle_binding_is_ambiguous
    business_interface_names
    collect_interface_business
    collect_reference_business
    expand_vlan_tokens
    activate_domain_gateways
    domain_gateways
    collect_active_vlans
    interface_index
    update_active_vlans

transform.py
    remove_interface
    rename_interface_tree
    clone_interface_tree
    ensure_parent_interface
    strip_bundle_options
    merge_duplicate_interface
    JunosInterfaceTransform

capabilities.py
    JunosInterfaceCapabilities
```

Do not keep private aliases `_walk`, `_collect_active_vlans`, or
`_expand_vlan_tokens` in the public package.

- [ ] **Step 2: Add the Juniper compatibility exports**

The package facade re-exports the same capability-facing names as Cisco, plus
Juniper access helpers:

```python
from .access import (
    find_inner_vlan,
    find_interface_node,
    find_vlan,
    interface_name,
    interface_nodes,
    interfaces_block,
    resolve_interface,
    unit_nodes,
    unit_number,
    walk_effective_nodes,
)
from .analysis import (
    JunosInterfaceAnalysis,
    bundle_members,
    interface_kind,
    interface_specs,
    logical_names_under,
)
from .business import business_interface_names
from .capabilities import JunosInterfaceCapabilities
from .transform import (
    JunosInterfaceTransform,
    clone_interface_tree,
    ensure_parent_interface,
    remove_interface,
    rename_interface_tree,
)
```

- [ ] **Step 3: Update Juniper production and test imports**

Replace private helper imports in rules and support:

```text
_walk                  -> access.walk_effective_nodes
_collect_active_vlans  -> business.collect_active_vlans
_expand_vlan_tokens    -> business.expand_vlan_tokens
```

Update tests that patch concrete functions to target the defining submodule.

- [ ] **Step 4: Run Juniper-focused tests**

Run:

```bash
.venv/bin/pytest tests/test_interfaces.py tests/test_interface_analysis_cache.py tests/test_junos_pruning.py tests/test_junos_pruning_scope.py tests/test_junos_groups.py tests/test_junos_ast.py -q
```

Expected: PASS.

---

### Task 4: Verify Public Compatibility and Dependency Direction

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_ast_boundaries.py`

**Interfaces:**
- Consumes: Both new interface packages from Tasks 2 and 3.
- Produces: Architecture guarantees for import compatibility and inward-only dependencies.

- [ ] **Step 1: Run the package layout tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::InterfaceModuleLayoutTest tests/test_architecture.py::CapabilityImplementationContractTest -q
```

Expected: PASS.

- [ ] **Step 2: Audit import paths**

Run:

```bash
rg -n "operations\.interfaces import _|operations/interfaces\.py|operations\.interfaces\.(analysis|business|transform|access)" src tests
```

Expected: no private helper import, no single-file reference, and concrete
submodule imports only where implementation ownership matters.

- [ ] **Step 3: Run interface and cache boundary suites**

Run:

```bash
.venv/bin/pytest tests/test_interfaces.py tests/test_interface_analysis_cache.py tests/test_document_cache.py tests/test_ast_boundaries.py -q
```

Expected: PASS.

---

### Task 5: Update Documentation and Run Final Gates

**Files:**
- Modify: `README.md`
- Modify: `docs/config-ast.md`
- Modify: `docs/onboarding-guide.md`
- Modify: `docs/processing-flow.md`

**Interfaces:**
- Consumes: Final `interfaces/` package structure.
- Produces: Documentation that explains access, analysis, business, transform, and facade responsibilities.

- [ ] **Step 1: Update documentation**

Document the package tree and state that rules/support import concrete submodules.
Do not describe `operations/interfaces.py` as a single file.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass with no skipped or removed behavior coverage.

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

- [ ] **Step 4: Inspect final interface module sizes**

Run:

```bash
find src/impl/cisco/operations/interfaces src/impl/juniper/operations/interfaces -name '*.py' -print0 | xargs -0 wc -l | sort -n
```

Expected: the original 736-line and 890-line files are gone; each module has a
single responsibility and no business logic remains in `__init__.py`.

- [ ] **Step 5: Leave changes uncommitted**

Run:

```bash
git status --short
```

Expected: implementation and test changes are visible in the worktree with no
commit or push.
