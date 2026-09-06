"""Story 48.5, AC3: the tracked-entity declaration, checked across every installed Connector.

The class, not the instance. Four connectors declare something today; the other
thirty-three declare nothing, and this file asserts that both answers are honest
for all of them at once -- a declared contract resolves inside its own descriptor,
and an absent one produces ``not_applicable`` with a reason rather than a guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.source_capabilities import (
    normalize_capabilities,
    validate_manifest_capabilities,
)

MODULES_DIR = Path(__file__).resolve().parents[2] / "modules"


def _manifests() -> list[tuple[str, dict]]:
    found = []
    for path in sorted(MODULES_DIR.glob("*/manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        found.append((manifest["name"], manifest))
    return found


_MANIFESTS = _manifests()
_NAMES = [name for name, _ in _MANIFESTS]


def test_the_installed_catalog_is_not_empty():
    assert len(_MANIFESTS) >= 30


@pytest.mark.parametrize("name,manifest", _MANIFESTS, ids=_NAMES)
def test_every_manifest_still_passes_conformance_with_the_new_block(name, manifest):
    assert validate_manifest_capabilities(manifest) == [], name


@pytest.mark.parametrize("name,manifest", _MANIFESTS, ids=_NAMES)
def test_every_connector_answers_the_tracked_entity_question(name, manifest):
    """Declared or not, the answer carries a reason a person can read."""
    response = normalize_capabilities(
        manifest, project_id="proj_EXAMPLE", connection_ref_id="conn_EXAMPLE"
    )
    tracked = response["tracked_entity"]
    assert tracked["support"] in {"declared", "not_applicable"}, name
    assert tracked["reason"], name
    if tracked["support"] == "not_applicable":
        assert tracked["reports"] == [], name
        assert tracked["directions"] == [], name


def test_exactly_the_connectors_that_declare_support_report_it():
    """Support is a declaration. Nothing infers it from a name or a field spelling."""
    declared = {
        name
        for name, manifest in _MANIFESTS
        if isinstance(manifest["source_capabilities"].get("tracked_entity"), dict)
    }
    projected = {
        name
        for name, manifest in _MANIFESTS
        if normalize_capabilities(
            manifest, project_id="proj_EXAMPLE", connection_ref_id="conn_EXAMPLE"
        )["tracked_entity"]["support"]
        == "declared"
    }
    assert declared == projected

    # Connectors carrying a field whose NAME suggests an entity, without a
    # declaration, must stay `not_applicable`. This is the guess the story forbids.
    suggestive = ("brand", "advertiser", "domain", "keyword", "search_term", "publisher")
    tempting = {
        name
        for name, manifest in _MANIFESTS
        if any(
            any(word in field["field_id"] or word in field["source_field"] for word in suggestive)
            for field in manifest["source_capabilities"]["fields"]
        )
    }
    assert tempting - declared, (
        "expected at least one connector with a suggestively named field and no "
        "declaration, so the negative case is actually exercised"
    )
    for name in tempting - declared:
        manifest = dict(_MANIFESTS)[name]
        response = normalize_capabilities(
            manifest, project_id="proj_EXAMPLE", connection_ref_id="conn_EXAMPLE"
        )
        assert response["tracked_entity"]["support"] == "not_applicable", name


def test_at_least_one_collect_and_one_observe_contract_exist():
    """Both directions are real, not theoretical shapes in a schema."""
    directions: dict[str, set[str]] = {}
    for name, manifest in _MANIFESTS:
        declaration = manifest["source_capabilities"].get("tracked_entity")
        if isinstance(declaration, dict):
            directions[name] = {entry["direction"] for entry in declaration["reports"]}
    collect = {name for name, kinds in directions.items() if "collect" in kinds}
    observe = {name for name, kinds in directions.items() if "observe" in kinds}
    assert collect, "no Connector declares a collect contract"
    assert observe, "no Connector declares an observe-only contract"


def test_a_collect_contract_names_a_parameter_its_own_callable_accepts():
    """The declared keyword argument must exist on the dispatch function.

    A declaration is only worth something if the request it describes can be made.
    Reading the callable's signature is what turns "the manifest says club_ids"
    into "the pull actually takes club_ids".
    """
    import importlib
    import inspect

    checked = 0
    for name, manifest in _MANIFESTS:
        declaration = manifest["source_capabilities"].get("tracked_entity")
        if not isinstance(declaration, dict):
            continue
        reports = {item["id"]: item for item in manifest["source_capabilities"]["reports"]}
        for entry in declaration["reports"]:
            driver = entry.get("query_driver")
            if not driver:
                continue
            callable_name = reports[entry["report_id"]].get("dispatch", {}).get("callable")
            assert callable_name, f"{name}: a collect report must declare a dispatch callable"
            # A module directory may carry a dash (`youtube-analytics`), which
            # `import_module` cannot spell -- until 2026-09-01 this line only
            # ever exercised `strava`, and a dash-named declarer would have
            # failed on the IMPORT rather than on its contract. Load by file
            # location, the way every module test already does.
            module_dir = Path(__file__).parents[2] / "modules" / name
            spec = importlib.util.spec_from_file_location(
                f"conformance_tracked_{name.replace('-', '_')}",
                module_dir / "connector.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fn = getattr(module, callable_name)
            parameters = inspect.signature(fn).parameters
            assert driver["parameter"] in parameters, (
                f"{name}.{callable_name} does not accept {driver['parameter']!r}"
            )
            own_parameter = driver.get("own_marker_parameter")
            if own_parameter:
                assert own_parameter in parameters, (
                    f"{name}.{callable_name} does not accept {own_parameter!r}"
                )
            checked += 1
    assert checked, "no collect contract was exercised"
