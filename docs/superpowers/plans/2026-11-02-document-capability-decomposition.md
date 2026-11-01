# Document Capability Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `CiscoDocument` and `JunosDocument` to configuration-tree aggregate roots with narrow, on-demand capability objects.

**Architecture:** Keep tree state, revisions, analysis caches, and edit scopes in Document. Move the 30 business methods behind nine narrow capability Protocols. During migration, old Document methods delegate to the new capabilities; production pipeline callers migrate one Stage at a time.

**Tech Stack:** Python 3.11+, Protocol typing, pytest/unittest, Ruff, Pyink

**Spec:** `docs/superpowers/specs/2026-11-02-document-capability-decomposition-design.md`

## Global Constraints

- Preserve conversion stage order, output files, report fields, errors, warnings, cache invalidation, and rollback semantics.
- Document retains ownership of the tree, revision, analysis cache, parser, writer, and EditScope coordination.
- Capability objects are lightweight and created on demand; Document must not store a long-lived capability collection.
- Capability objects must not cache tree analysis results.
- Validation capabilities remain read-only and must not create editors.
- Mutating capabilities own their local edit transaction; nested scopes may reuse an outer EditScope.
- Do not introduce a DI container, Service Locator, or one service per method.
- Keep `from impl.common import VendorConfiguration` working throughout and after migration.
- Do not create Git commits or push to GitHub during implementation.
- Use TDD: add a failing architecture or behavior test before each structural change.

---

### Task 1: Add Capability Protocols and Architecture Tests

**Files:**
- Create: `src/impl/common/domain/capabilities/__init__.py`
- Create: `src/impl/common/domain/capabilities/document.py`
- Create: `src/impl/common/domain/capabilities/interfaces.py`
- Create: `src/impl/common/domain/capabilities/bundles.py`
- Create: `src/impl/common/domain/capabilities/groups.py`
- Create: `src/impl/common/domain/capabilities/cleaning.py`
- Create: `src/impl/common/domain/capabilities/pruning.py`
- Create: `src/impl/common/domain/capabilities/compatibility.py`
- Create: `src/impl/common/domain/capabilities/validation.py`
- Create: `src/impl/common/domain/capabilities/references.py`
- Create: `src/impl/common/domain/capabilities/simulation.py`
- Modify: `tests/test_architecture.py`

**Interfaces:**
- Consumes: Existing domain types from `impl.common.domain.contracts`, `interface`, `outcomes`, `policies`, and `rules.cleaning`.
- Produces: `DocumentCore`, `VendorDocument`, `InterfaceCapabilities`, `InterfaceAnalysis`, `InterfaceTransform`, `BundleOperations`, `GroupOperations`, `CleaningOperations`, `PruningOperations`, `CompatibilityOperations`, `ValidationOperations`, `ReferenceOperations`, and `SimulationOperations`.

- [ ] **Step 1: Write failing Protocol shape tests**

Append to `tests/test_architecture.py`:

```python
class CapabilityProtocolLayoutTest(unittest.TestCase):
    def test_capability_protocols_are_importable(self):
        from impl.common.domain import capabilities

        expected = (
            "DocumentCore",
            "VendorDocument",
            "InterfaceCapabilities",
            "InterfaceAnalysis",
            "InterfaceTransform",
            "BundleOperations",
            "GroupOperations",
            "CleaningOperations",
            "PruningOperations",
            "CompatibilityOperations",
            "ValidationOperations",
            "ReferenceOperations",
            "SimulationOperations",
        )
        self.assertEqual(set(expected), set(capabilities.__all__))

    def test_interface_protocols_do_not_contain_mutation_methods(self):
        from impl.common.domain.capabilities import InterfaceAnalysis

        self.assertFalse(hasattr(InterfaceAnalysis, "rename"))
        self.assertFalse(hasattr(InterfaceAnalysis, "clone"))
        self.assertFalse(hasattr(InterfaceAnalysis, "remove"))

    def test_validation_protocol_has_no_editing_methods(self):
        from impl.common.domain.capabilities import ValidationOperations

        self.assertFalse(hasattr(ValidationOperations, "edit"))
        self.assertFalse(hasattr(ValidationOperations, "apply_cleaning_rules"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::CapabilityProtocolLayoutTest -q
```

Expected: FAIL because `impl.common.domain.capabilities` does not exist.

- [ ] **Step 3: Add the Protocol modules**

Create the capability package. `DocumentCore` contains exactly:

```python
@runtime_checkable
class DocumentCore(Protocol):
    vendor: str

    def hostname(self) -> str | None: ...
    def render(self) -> str: ...
```

Create Protocol files using the method signatures from the spec. Keep each Protocol
limited to one capability. `InterfaceAnalysis`, `InterfaceTransform`,
`CleaningOperations`, and `ValidationOperations` expose all signatures listed in
the spec. `VendorDocument` composes `DocumentCore` plus the nine capability
properties; `InterfaceCapabilities` exposes `analysis` and `transform`.

Export every public Protocol from `capabilities/__init__.py` and define `__all__`
with the names asserted by the test.

- [ ] **Step 4: Run the tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::CapabilityProtocolLayoutTest -q
.venv/bin/ruff check src/impl/common/domain/capabilities tests/test_architecture.py
```

Expected: PASS.

---

### Task 2: Add Cisco Capability Implementations Without Removing Compatibility APIs

**Files:**
- Modify: `src/impl/cisco/document.py`
- Modify: `src/impl/cisco/operations/interfaces.py`
- Modify: `src/impl/cisco/operations/bundles.py`
- Modify: `src/impl/cisco/operations/groups.py`
- Modify: `src/impl/cisco/operations/cleaning.py`
- Modify: `src/impl/cisco/operations/management_cleaning.py`
- Modify: `src/impl/cisco/operations/declarative_cleaning.py`
- Modify: `src/impl/cisco/operations/references.py`
- Modify: `src/impl/cisco/rules/pruning.py`
- Modify: `src/impl/cisco/rules/compatibility.py`
- Modify: `src/impl/cisco/rules/simulation.py`
- Test: `tests/test_architecture.py`
- Test: `tests/test_document_cache.py`
- Test: `tests/test_interfaces.py`

**Interfaces:**
- Consumes: Protocols from Task 1 and the existing Cisco implementation functions.
- Produces: `CiscoInterfaceCapabilities`, `CiscoInterfaceAnalysis`, `CiscoInterfaceTransform`, `CiscoBundleOperations`, `CiscoGroupOperations`, `CiscoCleaningOperations`, `CiscoPruningOperations`, `CiscoCompatibilityOperations`, `CiscoValidationOperations`, `CiscoReferenceOperations`, and `CiscoSimulationOperations`.

- [ ] **Step 1: Add failing capability-object tests**

Add to `tests/test_architecture.py`:

```python
class CiscoCapabilityObjectTest(unittest.TestCase):
    def test_document_exposes_narrow_capabilities(self):
        from impl.cisco import CiscoDocument

        document = CiscoDocument("interface Gi0\n!\n")
        self.assertEqual(document.interfaces.analysis.resolve_interface("Gi0"), "GigabitEthernet0")
        self.assertEqual(document.interfaces.analysis.interface_specs()[0].name, "GigabitEthernet0")
        self.assertFalse(hasattr(document, "_bundles_cache"))

    def test_capability_access_does_not_create_long_lived_services(self):
        from impl.cisco import CiscoDocument

        document = CiscoDocument("")
        first = document.interfaces
        second = document.interfaces
        self.assertIsNot(first, second)
        self.assertEqual(document.__dict__.get("_interfaces"), None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::CiscoCapabilityObjectTest -q
```

Expected: FAIL because Document has no capability properties.

- [ ] **Step 3: Implement Cisco capability classes and Document properties**

In each existing Cisco module, add the named capability class with
`__slots__ = ("_document",)`. Constructor stores `self._document = document`.
Methods delegate to the existing module functions.

Add lazy properties to `CiscoDocument`:

```python
@property
def interfaces(self):
    from .operations.interfaces import CiscoInterfaceCapabilities

    return CiscoInterfaceCapabilities(self)
```

Add equivalent on-demand properties for `bundles`, `groups`, `cleaning`,
`pruning`, `compatibility`, `validation`, `references`, and `simulation`.

Keep all existing public Document methods unchanged; temporarily rewrite their
bodies to delegate to the matching capability. For interface query caching,
capability objects call the existing Document cache through a private
`_read_interface_analysis(query, collect, copy_result)` helper; the cache itself
remains owned by Document.

- [ ] **Step 4: Run focused compatibility tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::CiscoCapabilityObjectTest tests/test_document_cache.py tests/test_interfaces.py -q
```

Expected: PASS.

---

### Task 3: Add Juniper Capability Implementations and Move Private Interface Logic

**Files:**
- Modify: `src/impl/juniper/document.py`
- Modify: `src/impl/juniper/syntax/ast.py`
- Modify: `src/impl/juniper/operations/interfaces.py`
- Modify: `src/impl/juniper/operations/bundles.py`
- Modify: `src/impl/juniper/operations/groups.py`
- Modify: `src/impl/juniper/operations/cleaning.py`
- Modify: `src/impl/juniper/operations/management_cleaning.py`
- Modify: `src/impl/juniper/operations/declarative_cleaning.py`
- Modify: `src/impl/juniper/operations/references.py`
- Modify: `src/impl/juniper/rules/pruning.py`
- Modify: `src/impl/juniper/rules/compatibility.py`
- Modify: `src/impl/juniper/rules/simulation.py`
- Test: `tests/test_architecture.py`
- Test: `tests/test_ast_boundaries.py`
- Test: `tests/test_document_cache.py`

**Interfaces:**
- Consumes: Protocols from Task 1 and Juniper's existing interface/rule functions.
- Produces: The same nine capability classes with `Junos` prefix, plus Juniper helper functions for top blocks, units, VLANs, and interface nodes outside `JunosDocument`.

- [ ] **Step 1: Add failing Juniper capability tests**

Add to `tests/test_architecture.py`:

```python
class JuniperCapabilityObjectTest(unittest.TestCase):
    def test_document_exposes_narrow_capabilities(self):
        from impl.juniper import JunosDocument

        document = JunosDocument("interfaces { ge-0/0/0 {} }")
        self.assertEqual(
            document.interfaces.analysis.resolve_interface("ge-0/0/0"),
            "ge-0/0/0",
        )
        self.assertEqual(
            document.interfaces.analysis.interface_specs()[0].name,
            "ge-0/0/0",
        )

    def test_private_interface_helpers_are_no_longer_document_methods(self):
        from impl.juniper import JunosDocument

        for name in (
            "_top_block",
            "_interfaces_block",
            "_interface_nodes",
            "_unit_nodes",
            "_find_vlan",
            "_find_inner_vlan",
        ):
            self.assertFalse(hasattr(JunosDocument, name))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::JuniperCapabilityObjectTest -q
```

Expected: FAIL because capability properties do not exist and private helpers
remain on `JunosDocument`.

- [ ] **Step 3: Move Juniper syntax/interface helpers**

Move the generic active top-block lookup to `syntax/ast.py`. Move interface,
unit, VLAN, inner-VLAN, and interface-node helpers to
`operations/interfaces.py`. The capability implementation accepts the Document
and calls these functions; no helper stores the Document or caches results.

Move Juniper's explicit `edit().bulk()` wrapping for remove, rename, clone,
ensure-parent, bundle adaptation, pruning, and simulation into the corresponding
capability methods. Document compatibility methods must not wrap the same
transaction a second time.

- [ ] **Step 4: Update Juniper internal callers**

Replace direct private Document helper calls in Juniper modules with imports from
the new syntax/interface helper functions. Search before finishing:

```bash
rg -n "document\\._(top_block|interfaces_block|interface_nodes|unit_nodes|find_vlan|find_inner_vlan)" src tests
```

Expected: no matches.

- [ ] **Step 5: Run focused Juniper tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::JuniperCapabilityObjectTest tests/test_ast_boundaries.py tests/test_document_cache.py tests/test_interfaces.py -q
```

Expected: PASS.

---

### Task 4: Route Application Stages Through Narrow Capabilities

**Files:**
- Modify: `src/adaptation/groups.py`
- Modify: `src/adaptation/interfaces.py`
- Modify: `src/adaptation/nni.py`
- Modify: `src/adaptation/cleaning_rules.py`
- Modify: `src/adaptation/simulation.py`
- Modify: `src/adaptation/topology.py`
- Test: `tests/test_architecture.py`
- Test: `tests/test_core_interface_pipeline.py`
- Test: `tests/test_conversion.py`

**Interfaces:**
- Consumes: Cisco/Juniper capability properties and the shared Protocols.
- Produces: Production adaptation code that does not call the legacy business methods on `DeviceContext.document`.

- [ ] **Step 1: Add a failing source-level caller test**

Add to `tests/test_architecture.py`:

```python
class CapabilityCallerTest(unittest.TestCase):
    def test_adaptation_uses_capabilities_not_legacy_document_methods(self):
        source_root = Path(__file__).parents[1] / "src" / "adaptation"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(source_root.glob("*.py"))
        )
        for legacy_call in (
            ".interface_specs(",
            ".prune_non_nni(",
            ".adapt_interfaces(",
            ".validate_interfaces(",
            ".apply_cleaning_rules(",
            ".adapt_to_simulation(",
        ):
            self.assertNotIn(legacy_call, source)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::CapabilityCallerTest -q
```

Expected: FAIL while adaptation still uses legacy methods.

- [ ] **Step 3: Migrate stages in pipeline order**

Migrate one Stage at a time and run its focused test after each change:

```text
GroupExpansionHandler -> document.groups.expand
SourceSemanticValidationHandler -> document.interfaces.analysis + document.validation.validate_source_semantics
InterfaceClassificationHandler -> document.interfaces.analysis.interface_specs
NonNNICleanupHandler -> document.pruning.prune_non_nni
NNIHandler -> document.interfaces.analysis/transform + document.bundles + document.validation.validate_nni_clone
ReferenceRewriteHandler -> document.references.replace
InterfaceCompatibilityHandler -> document.compatibility.adapt_interfaces
FinalInterfaceValidationHandler -> document.validation.validate_final_integrity
SimulationAdaptationHandler -> document.simulation.adapt
```

`PreconfiguredInterfaceCleanupHandler` remains unchanged and Cisco-specific.

- [ ] **Step 4: Run integration tests**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::CapabilityCallerTest tests/test_core_interface_pipeline.py tests/test_conversion.py -q
```

Expected: PASS.

---

### Task 5: Remove Legacy Business Methods and Narrow Public Types

**Files:**
- Modify: `src/impl/cisco/document.py`
- Modify: `src/impl/juniper/document.py`
- Modify: `src/impl/common/domain/contracts.py`
- Modify: `src/impl/common/__init__.py`
- Modify: `src/adaptation/models.py`
- Modify: `tests/test_architecture.py`
- Modify: all tests that directly call legacy Document business methods

**Interfaces:**
- Consumes: Production code already migrated in Task 4.
- Produces: `VendorConfiguration` as a compatibility alias of `VendorDocument`; Documents expose only core methods and capability properties.

- [ ] **Step 1: Write failing final-surface tests**

Add to `tests/test_architecture.py`:

```python
class DocumentSurfaceTest(unittest.TestCase):
    def test_documents_do_not_define_business_methods(self):
        from impl.cisco import CiscoDocument
        from impl.juniper import JunosDocument

        removed = {
            "expand_groups",
            "interface_specs",
            "interface_kind",
            "bundle_members",
            "business_interface_names",
            "remove_interface",
            "rename_interface_tree",
            "clone_interface_tree",
            "bundle_mode",
            "adapt_bundle",
            "prune_non_nni",
            "adapt_interfaces",
            "validate_source_semantics",
            "validate_final_integrity",
            "validate_interfaces",
            "replace_references",
            "clean_management_access",
            "add_lab_account",
            "adapt_to_simulation",
            "apply_cleaning_rules",
        }
        for document_type in (CiscoDocument, JunosDocument):
            for name in removed:
                with self.subTest(document=document_type.__name__, name=name):
                    self.assertFalse(hasattr(document_type, name))

    def test_vendor_configuration_alias_is_vendor_document(self):
        from impl.common import VendorConfiguration
        from impl.common.domain.capabilities import VendorDocument

        self.assertIs(VendorConfiguration, VendorDocument)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_architecture.py::DocumentSurfaceTest -q
```

Expected: FAIL while legacy methods and the wide Protocol remain.

- [ ] **Step 3: Delete Document forwarding methods**

Delete interface, Bundle, Group, cleaning, pruning, compatibility, validation,
reference, and simulation forwarding methods. Keep Document core methods and
capability properties.

In `contracts.py`, replace the 30-method Protocol with:

```python
from .capabilities.document import DocumentCore, VendorDocument

VendorConfiguration = VendorDocument

__all__ = ["DocumentCore", "VendorConfiguration", "VendorDocument"]
```

Keep `DeviceContext.document` typed as `VendorConfiguration`.

- [ ] **Step 4: Migrate direct tests to capabilities**

Replace calls such as `document.interface_specs()` with
`document.interfaces.analysis.interface_specs()`. Replace mutating and pipeline
calls with their corresponding capability methods. Do not weaken assertions or
remove behavior coverage.

- [ ] **Step 5: Run the full suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass with no legacy Document business calls.

---

### Task 6: Add Lifecycle and Rollback Regression Tests

**Files:**
- Test: `tests/test_document_cache.py`
- Test: `tests/test_config_ast.py`
- Test: `tests/test_cleaning_batch.py`
- Test: `tests/test_preserve_bundles.py`

**Interfaces:**
- Consumes: Final capability API from Tasks 2-5.
- Produces: Regression coverage for cache invalidation, capability lifetime, read-only validation, and nested rollback.

- [ ] **Step 1: Add lifecycle assertions**

Add tests that:

```python
def test_interface_capabilities_do_not_outlive_document_reference():
    import gc
    import weakref
    from impl.cisco import CiscoDocument

    document = CiscoDocument("interface Gi0\n!\n")
    reference = weakref.ref(document)
    document.interfaces.analysis.interface_specs()
    del document
    gc.collect()
    assert reference() is None


def test_capability_query_refreshes_after_tree_edit():
    from impl.juniper import JunosDocument

    document = JunosDocument("interfaces { ge-0/0/0 {} }")
    before = document.interfaces.analysis.interface_specs()
    assert [item.name for item in before] == ["ge-0/0/0"]
    transform = document.interfaces.transform
    transform.rename("ge-0/0/0", "ge-0/0/1")
    after = document.interfaces.analysis.interface_specs()
    assert [item.name for item in after] == ["ge-0/0/1"]
```

Also add an outer EditScope test that invokes two mutating capabilities, raises,
and verifies both the tree and revision return to their baseline.

- [ ] **Step 2: Run lifecycle tests**

Run:

```bash
.venv/bin/pytest tests/test_document_cache.py tests/test_config_ast.py tests/test_cleaning_batch.py tests/test_preserve_bundles.py -q
```

Expected: PASS.

---

### Task 7: Update Documentation and Run Final Quality Gates

**Files:**
- Modify: `README.md`
- Modify: `docs/config-ast.md`
- Modify: `docs/onboarding-guide.md`
- Modify: `docs/processing-flow.md`
- Modify: `docs/configuration-rule-authoring.md`

**Interfaces:**
- Consumes: Final capability API from Tasks 1-6.
- Produces: Documentation that teaches the new Document/capability boundary and final verification evidence.

- [ ] **Step 1: Update code-boundary documentation**

Document the Document core, capability package, on-demand lifecycle, cache
ownership, transaction ownership, and pipeline-stage mapping. Remove examples
that call legacy business methods on Document.

- [ ] **Step 2: Run all quality gates**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/pyink --check --line-length 80 src
.venv/bin/ruff check src
.venv/bin/python scripts/check_docstrings.py
.venv/bin/python scripts/check_method_lengths.py $(find src -name '*.py' -type f)
git diff --check
```

Expected: every command exits with status 0.

- [ ] **Step 3: Audit the public surface and imports**

Run:

```bash
rg -n "document\\.(interface_specs|interface_kind|bundle_members|expand_groups|prune_non_nni|adapt_interfaces|validate_interfaces|apply_cleaning_rules|adapt_to_simulation|replace_references)\\(" src tests
```

Expected: no production or test matches.

- [ ] **Step 4: Inspect final document sizes**

Run:

```bash
.venv/bin/python - <<'PY'
import ast
from pathlib import Path

for name in ("cisco/document.py", "juniper/document.py"):
    path = Path("src/impl") / name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    document = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.endswith("Document")
    )
    methods = [
        node
        for node in document.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    print(name, len(path.read_text(encoding="utf-8").splitlines()), len(methods))
PY
```

Expected: no business forwarding methods remain; Document method count is
limited to core methods and capability properties.

- [ ] **Step 5: Leave changes uncommitted**

Run:

```bash
git status --short
```

Expected: implementation changes are visible in the worktree, with no commit or
push performed.
