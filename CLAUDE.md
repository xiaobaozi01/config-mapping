# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Converts real Cisco IOS XR and Juniper Junos configs (plus an Excel topology) into configs runnable on GNS3 XRv9000 / vMX images. Docs, comments, reports and error messages are written in Chinese; keep that convention. Start with `docs/onboarding-guide.md`; `docs/requirements.md` is the requirements baseline, `docs/processing-flow.md` the full conversion flow, `docs/semantic-rule-authoring.md` how to add group identity rules, `docs/config-importer.md` the GNS3 import contract.

## Commands

Python >= 3.11. A `.venv` exists (Python 3.12) but does not have pytest installed; tests are `unittest`-based.

```bash
# Install
python3 -m pip install -e .

# Run all tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests

# Single file / single test
PYTHONPATH=src .venv/bin/python -m unittest tests.test_conversion
PYTHONPATH=src .venv/bin/python -m unittest tests.test_architecture.NniPlanningTest.test_same_bundle_and_peer_are_grouped_without_mutating_links

# Run a conversion without installing
PYTHONPATH=src python3 -m config_adaptor convert --topology topology.xlsx \
  --config-dir configs --output-dir output --profiles config/image_profiles.yaml
```

No linter or formatter is configured.

## Architecture

Two independent packages under `src/`:

- `config_adaptor/` — the conversion tool (CLI `config-adaptor`, entry `adaptation/cli.py:main`).
- `config_importer/` — library (no CLI) that pushes one already-converted cfg into a running GNS3 node via a temporary FTP node, telnet console and the gns3-server v3 API. Does not depend on `config_adaptor`.

### config_adaptor layering

- `cisco/` and `juniper/` each own their vendor AST (`document.py`), parsing/rendering, group expansion, interfaces, cleaning, simulation adaptation, and packaged YAML rules (`cisco/rules/xrv9000/`, `juniper/rules/vmx/`).
- `common/contracts.py` defines `VendorConfiguration`, the Facade protocol both `CiscoDocument` and `JunosDocument` implement. Vendor AST nodes must not leak into `adaptation/`; the flow layer only calls business-level methods on this protocol. `tests/test_architecture.py` checks that both documents implement the protocol and that `cisco/`, `juniper/` and `common/` never import from `adaptation/`.
- `common/` also holds shared interface types, outcome/policy dataclasses, and the semantic identity engine (`common/semantic_identity/`) used to decide whether group-inherited and explicit leaf statements occupy the same config slot.
- `adaptation/` is vendor-neutral orchestration. `factory.parse_document` picks the vendor document; `service.py` loads inputs, runs the pipeline and writes outputs. `service.adapt_device` is the in-memory single-device entry point: it builds a synthetic topology from `DeviceLink`s and lists supported peers in `ConversionContext.external_devices`, so the preflight and NNI stages treat them as valid endpoints without parsing their configs. `tests/test_device_adaptation.py` checks that its output matches `convert` for every device in the end-to-end fixtures.

### Pipeline (`adaptation/pipeline.py:build_default_pipeline`)

Stages run in fixed dependency order on a shared `ConversionContext` and stop at the first error:

1. `CleaningRulesHandler(PRE_ANALYSIS)` — code rules that must run before analysis
2. `TopologyPreflightHandler` — Excel read/validation (sheets `设备列表`, `链接表`; Huawei devices/links are skipped)
3. `GroupExpansionHandler` — transactional static expansion of IOS XR `group/apply-group` and Junos `groups/apply-groups`; any unresolvable active group fails the whole conversion (no partial output). Conflicts go to `report.json` `group_conflicts`.
4. `PreconfiguredInterfaceCleanupHandler` — ignores IOS XR `interface preconfigure`
5. `InterfaceClassificationHandler` — interfaces in the link sheet are NNI
6. `NNIHandler` — must run before UNI; flattens Bundle-Ether/ae, handles M-LAG splits, allocates ports per side independently (cross-vendor ends need not match names)
7. `UNIHandler` — migrates only interfaces with real business references, rewrites to QinQ on a single parent port
8. `ReferenceRewriteHandler` — rewrites remaining interface references
9. `CleaningRulesHandler(POST_REWRITE)` — YAML cleaning rules then auth replacement
10. `SimulationAdaptationHandler` — per-profile `off`/`compatible`/`stable` modes

Raw-input echo stripping (`adaptation/input_normalization.py`, vendor `input_normalization.yaml`) happens before parsing.

### Rules and profiles

- `config/image_profiles.yaml`: image interface lists. Position matters: `interfaces[:-3]` NNI pool, `interfaces[-3]` UNI parent, `interfaces[-2:]` reserved.
- Cleaning rules ship with the package (`cleaning.yaml`); external rule files are not accepted. Simple rules are YAML; complex ones implement `ExecutableCleaningRule` and must be registered in the built-in registry in `adaptation/cleaning_rules.py`.
- Identity rules live in `interfaces.yaml`/`routing.yaml`/`policy.yaml`/`system.yaml`. A new identity rule file must be added to that directory's `manifest.yaml` `rule_files`; `cleaning.yaml` is not part of the manifest. New YAML rule dirs must also be listed in `pyproject.toml` package-data.

## Tests and fixtures

Tests are split by concern in `tests/test_*.py`. Device configs are always `.cfg` files under `tests/fixtures/` and topologies are two-sheet `topology.xlsx` files; do not embed configs in Python. End-to-end fixture dirs contain `topology.xlsx` plus `configs/*.cfg`.

## Lab credentials

Converted configs use lab password `admin123` (IOS XR local user `root`, Junos root auth only), intended solely for isolated GNS3 labs.
