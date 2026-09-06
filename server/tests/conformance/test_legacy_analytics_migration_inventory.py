"""Story 65.11 whole-class legacy analytics retirement guards."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sign_evidence = importlib.import_module("e2e.host_qualification").sign_evidence
build_host_bundle = importlib.import_module("e2e.tests.test_host_qualification")._bundle
ATTESTATION_KEY = b"g10-inventory-test-attestation-key-32-bytes"


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_script("check_legacy_analytics_migration")
audit = _load_script("finished_work_audit")


def _write(root: Path, relative: str, text: str = "proof") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(kind: str, locators: list[str]) -> dict:
    return {
        "id": kind,
        "class": kind,
        "user_door": f"{kind} user door",
        "replacement_id": "shared-runtime.v1",
        "expected_locators": locators,
        "disposition": "blocked",
        "retirement_mode": "retained",
        "owner": f"{kind} owner",
        "reason": "Retirement waits for exact Story 65.10 G10 evidence.",
        "replacement_proof": {"paths": ["proof/replacement.txt"], "claim": "candidate"},
        "qualification_evidence": None,
        "required_shared_authority_locators": [],
        "forbidden_legacy_locators": [],
    }


def _decision(
    *,
    evidence_hash: str,
    decision: str,
    ordinal: int,
    predecessor: str | None,
) -> dict:
    return {
        "schema_version": "g10-qualification-decision.v1",
        "replacement_id": "shared-runtime.v1",
        "decision": decision,
        "host": {"name": "ChatGPT", "profile": "production", "version": "1"},
        "canonical_target": "https://app.toorow.com",
        "evidence_sha256": evidence_hash,
        "reason": None if decision == "qualified" else "parity_failed",
        "recorded_at": f"2026-08-11T10:0{ordinal}:00Z",
        "attempt_ordinal": ordinal,
        "predecessor_evidence_sha256": predecessor,
    }


def _pin_qualified_head(root: Path, data: dict) -> str:
    first_hash = "1" * 64
    evidence, retained_path = build_host_bundle(root)
    evidence["attestation_signature"] = sign_evidence(evidence, key=ATTESTATION_KEY)
    retained_path.write_text(json.dumps(evidence), encoding="utf-8")
    evidence_path = retained_path.relative_to(root).as_posix()
    head_hash = hashlib.sha256((root / evidence_path).read_bytes()).hexdigest()
    host = {
        "name": evidence["host"]["name"],
        "profile": evidence["host"]["profile"],
        "version": evidence["host"]["version"],
    }
    rows = [
        _decision(evidence_hash=first_hash, decision="blocked", ordinal=1, predecessor=None),
        _decision(
            evidence_hash=head_hash,
            decision="qualified",
            ordinal=2,
            predecessor=first_hash,
        ),
    ]
    for row in rows:
        row["host"] = host
    rows[-1]["recorded_at"] = evidence["recorded_at"]
    ledger = "evidence/g10-decisions.jsonl"
    _write(root, ledger, "".join(f"{json.dumps(row)}\n" for row in rows))
    data["qualification_baseline"] = {
        "story": "65.10",
        "commit": "a" * 40,
        "gates": {
            "G10": {
                "decision_ledger": ledger,
                "evidence_path": evidence_path,
                "replacement_id": "shared-runtime.v1",
                "canonical_target": "https://app.toorow.com",
                "evidence_sha256": head_hash,
                "host": host,
            }
        },
    }
    return head_hash


@pytest.fixture
def synthetic_inventory(tmp_path: Path) -> tuple[Path, dict]:
    census = {
        "producer-tool": (
            "server/modules/example/connector.py",
            "def produce():\n    return {'structuredContent': {}}\n",
        ),
        "mcp-binding": ("server/modules/example/manifest.json", '{"widget_ref":"ui://example"}'),
        "client-semantic-aggregator": (
            "ui/cards/example/src/App.tsx",
            "export function aggregate() { return rows.reduce(sum); }",
        ),
        "ai-path-projection": (
            "server/core/path.py",
            "def project():\n    return ai_path_id\n",
        ),
        "feedback-door": (
            "ui/cards/example/src/Feedback.tsx",
            "export function submit() { submit_feedback(); }",
        ),
        "historical-reader": (
            "server/core/history.py",
            "def read_history():\n    return 'SELECT * FROM render_snapshots'\n",
        ),
        "renderer": (
            "ui/cards/example/src/renderers.ts",
            'export function install() { declare("bar", () => null); }\n',
        ),
    }
    for path, text in census.values():
        _write(tmp_path, path, text)
    _write(tmp_path, "proof/replacement.txt")
    _write(tmp_path, "history/evidence.sql")
    data = {
        "schema_version": "legacy-analytics-migration-inventory.v1",
        "story": "65.11",
        "qualification_baseline": {"story": "65.10", "commit": None, "gates": {"G10": None}},
        "historical_evidence": {
            "policy": "supersede-never-delete",
            "protected_paths": ["history/evidence.sql"],
        },
        "entries": [],
    }
    discovered = checker.discover_inventory_locators(tmp_path)
    data["entries"] = [
        _entry(kind, sorted(discovered[kind])) for kind in sorted(census)
    ]
    assert checker.validate_inventory(root=tmp_path, data=data) == []
    return tmp_path, data


def test_repository_inventory_exactly_covers_the_independent_census() -> None:
    assert checker.validate_inventory() == []
    data = checker.load_inventory()
    summary = checker.inventory_summary(data)
    assert summary.paths > 0
    assert summary.by_disposition["migrated"] == 0
    assert summary.by_mode["removed"] == 0
    assert summary.retirement_ready is False


def test_python_prose_is_not_inventoried_but_executable_sql_is(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "server/core/history.py",
        '"""A rejected bridge over app.render_snapshots."""\n'
        "# app.notebooks is historical context, not a read.\n"
        "QUERY = 'SELECT * FROM app.reports'\n"
        "def read():\n"
        "    \"\"\"Référence refusée à app.render_snapshots.\"\"\"; "
        "return 'SELECT * FROM app.notebooks'\n",
    )
    discovered = checker.discover_inventory_locators(tmp_path)["historical-reader"]
    assert discovered == {
        "server/core/history.py#symbol:QUERY",
        "server/core/history.py#symbol:read",
    }


@pytest.mark.parametrize(
    ("kind", "path", "text"),
    [
        (
            "producer-tool",
            "server/modules/example/connector.py",
            "\ndef second_producer():\n    return {'structuredContent': {}}\n",
        ),
        (
            "mcp-binding",
            "server/modules/example/manifest.json",
            '\n{"widget_ref":"ui://second"}',
        ),
        (
            "client-semantic-aggregator",
            "ui/cards/example/src/App.tsx",
            "\nexport function secondAggregate() { return rows.reduce(sum); }",
        ),
        (
            "ai-path-projection",
            "server/core/path.py",
            "\ndef second_projection():\n    return ai_path_id\n",
        ),
        (
            "feedback-door",
            "ui/cards/example/src/Feedback.tsx",
            "\nexport function secondSubmit() { submit_analyze_feedback(); }",
        ),
        (
            "historical-reader",
            "server/core/history.py",
            "\ndef second_reader():\n    return 'SELECT * FROM app.notebooks'\n",
        ),
        (
            "renderer",
            "ui/cards/example/src/renderers.ts",
            '\nexport function secondRenderer() { declare("line", () => null); }\n',
        ),
    ],
)
def test_each_census_class_fails_closed_on_an_unclassified_path(
    synthetic_inventory: tuple[Path, dict], kind: str, path: str, text: str
) -> None:
    root, data = synthetic_inventory
    target = root / path
    target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")
    errors = checker.validate_inventory(root=root, data=data)
    assert any(error.startswith(f"{kind} census has unclassified locators:") for error in errors)


def test_removed_or_migrated_is_refused_without_g10(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, original = synthetic_inventory
    data = copy.deepcopy(original)
    data["entries"][0]["disposition"] = "migrated"
    data["entries"][0]["retirement_mode"] = "removed"
    data["entries"][0]["qualification_evidence"] = {
        "baseline_commit": "a" * 40,
        "replacement_id": "shared-runtime.v1",
        "canonical_target": "https://app.toorow.com",
        "evidence_sha256": "2" * 64,
    }
    data["entries"][0]["required_shared_authority_locators"] = [
        data["entries"][0]["expected_locators"][0]
    ]
    data["entries"][0]["forbidden_legacy_locators"] = [
        "server/core/path.py#symbol:removed_legacy_authority"
    ]
    errors = checker.validate_inventory(root=root, data=data)
    assert any("cannot be migrated without pinned 65.10 G10" in error for error in errors)
    assert any("cannot be removed without pinned 65.10 G10" in error for error in errors)


def test_last_ordered_g10_head_authorizes_exact_matching_evidence(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, original = synthetic_inventory
    data = copy.deepcopy(original)
    head_hash = _pin_qualified_head(root, data)
    entry = data["entries"][0]
    entry["disposition"] = "migrated"
    entry["retirement_mode"] = "removed"
    entry["required_shared_authority_locators"] = [entry["expected_locators"][0]]
    entry["forbidden_legacy_locators"] = [
        "server/core/path.py#symbol:removed_legacy_authority"
    ]
    entry["qualification_evidence"] = {
        "baseline_commit": "a" * 40,
        "replacement_id": "shared-runtime.v1",
        "canonical_target": "https://app.toorow.com",
        "evidence_sha256": head_hash,
    }
    assert checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=ATTESTATION_KEY,
        legacy_locator_verifier=lambda _root, _locator: True,
    ) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_commit", "b" * 40),
        ("replacement_id", "another-runtime.v1"),
        ("canonical_target", "https://wrong.example.com"),
        ("evidence_sha256", "3" * 64),
    ],
)
def test_per_entry_evidence_must_match_baseline_and_current_decision_head(
    synthetic_inventory: tuple[Path, dict], field: str, value: str
) -> None:
    root, data = synthetic_inventory
    head_hash = _pin_qualified_head(root, data)
    entry = data["entries"][0]
    entry["disposition"] = "migrated"
    entry["qualification_evidence"] = {
        "baseline_commit": "a" * 40,
        "replacement_id": "shared-runtime.v1",
        "canonical_target": "https://app.toorow.com",
        "evidence_sha256": head_hash,
    }
    entry["qualification_evidence"][field] = value
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=ATTESTATION_KEY,
    )
    assert any("cannot be migrated without pinned 65.10 G10" in error for error in errors)


def test_g10_decision_attempt_chain_must_be_contiguous(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    ledger = root / data["qualification_baseline"]["gates"]["G10"]["decision_ledger"]
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    rows[1]["attempt_ordinal"] = 3
    _write(root, str(ledger.relative_to(root)), "".join(f"{json.dumps(row)}\n" for row in rows))
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=ATTESTATION_KEY,
    )
    assert "G10 decision attempts must be contiguous and ordered per replacement_id" in errors


def test_profiled_tool_registration_is_a_distinct_census_identity(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    _write(
        root,
        "server/core/new_tool.py",
        "def exact_tool():\n    return None\n\ndef register(mcp):\n"
        "    register_profiled(mcp, exact_tool, profile='insights')\n",
    )
    errors = checker.validate_inventory(root=root, data=data)
    assert any(
        "server/core/new_tool.py#tool:exact_tool" in error
        for error in errors
        if error.startswith("producer-tool census has unclassified locators:")
    )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "canonical_target",
            "https://wrong.example.com",
            "qualification_baseline.gates.G10.canonical_target must be https://app.toorow.com",
        ),
        (
            "replacement_id",
            "unknown-runtime.v1",
            "G10 decision ledger has no head for the pinned replacement_id",
        ),
        (
            "evidence_sha256",
            "f" * 64,
            "qualification_baseline.gates.G10 evidence hash does not match bytes",
        ),
        (
            "host",
            {"name": "Another host", "profile": "production", "version": "1"},
            "qualification_baseline.gates.G10.host does not match the decision row",
        ),
    ],
)
def test_baseline_pin_must_match_retained_evidence_and_current_head(
    synthetic_inventory: tuple[Path, dict], field: str, value: object, expected: str
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    data["qualification_baseline"]["gates"]["G10"][field] = value
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=ATTESTATION_KEY,
    )
    assert expected in errors


def test_baseline_commit_must_resolve_in_git(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: False,
        attestation_key=ATTESTATION_KEY,
    )
    assert "qualification_baseline.commit does not resolve to a Git commit" in errors


def test_qualified_baseline_without_attestation_key_is_refused(
    synthetic_inventory: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    monkeypatch.delenv("TOOROW_QA_G10_ATTESTATION_KEY", raising=False)
    errors = checker.validate_inventory(
        root=root, data=data, commit_verifier=lambda _root, _commit: True
    )
    assert "qualified G10 evidence requires TOOROW_QA_G10_ATTESTATION_KEY" in errors


def test_qualified_baseline_rejects_short_attestation_key(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=b"too-short",
    )
    assert any("at least 32 bytes" in error for error in errors)


def test_qualified_baseline_with_bad_detached_signature_is_refused(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    g10 = data["qualification_baseline"]["gates"]["G10"]
    evidence_path = root / g10["evidence_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["attestation_signature"] = "0" * 64
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    changed_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    ledger_path = root / g10["decision_ledger"]
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["evidence_sha256"] = changed_hash
    ledger_path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")
    g10["evidence_sha256"] = changed_hash
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=ATTESTATION_KEY,
    )
    assert any("detached attestation signature is invalid" in error for error in errors)


def test_signed_but_structurally_invalid_g10_evidence_cannot_qualify(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    _pin_qualified_head(root, data)
    g10 = data["qualification_baseline"]["gates"]["G10"]
    evidence_path = root / g10["evidence_path"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["samples"][0].pop("authority")
    evidence["attestation_signature"] = sign_evidence(evidence, key=ATTESTATION_KEY)
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    changed_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    ledger_path = root / g10["decision_ledger"]
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    rows[-1]["evidence_sha256"] = changed_hash
    ledger_path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")
    g10["evidence_sha256"] = changed_hash
    errors = checker.validate_inventory(
        root=root,
        data=data,
        commit_verifier=lambda _root, _commit: True,
        attestation_key=ATTESTATION_KEY,
    )
    assert any("qualified G10 evidence contract is invalid" in error for error in errors)


def test_entry_text_and_paths_are_bounded_and_confined(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, original = synthetic_inventory
    data = copy.deepcopy(original)
    data["entries"][0]["user_door"] = "x" * 301
    data["entries"][0]["replacement_proof"]["paths"] = ["../outside.txt"]
    errors = checker.validate_inventory(root=root, data=data)
    assert any("user_door must be non-empty and at most 300" in error for error in errors)
    assert any("replacement_proof.paths escapes the repository" in error for error in errors)


def test_replacement_entry_cycles_are_refused(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, original = synthetic_inventory
    data = copy.deepcopy(original)
    first, second = data["entries"][:2]
    first["replacement_id"] = second["id"]
    second["replacement_id"] = first["id"]
    errors = checker.validate_inventory(root=root, data=data)
    assert any(error.startswith("replacement cycle detected:") for error in errors)


def test_fictitious_forbidden_locator_cannot_authorize_delegation(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    entry = next(item for item in data["entries"] if item["class"] == "ai-path-projection")
    entry["disposition"] = "migrated"
    entry["retirement_mode"] = "delegated"
    entry["required_shared_authority_locators"] = [entry["expected_locators"][0]]
    forbidden = "server/core/path.py#symbol:legacy_ai_path_authority"
    entry["forbidden_legacy_locators"] = [forbidden]
    errors = checker.validate_inventory(
        root=root,
        data=data,
        legacy_locator_verifier=lambda _root, _locator: False,
    )
    assert f"forbidden legacy locator has no historical identity: {forbidden}" in errors
    with (root / "server/core/path.py").open("a", encoding="utf-8") as handle:
        handle.write("\ndef legacy_ai_path_authority():\n    return None\n")
    errors = checker.validate_inventory(root=root, data=data)
    assert f"forbidden legacy authority locator is present: {forbidden}" in errors


def test_historical_evidence_cannot_disappear(
    synthetic_inventory: tuple[Path, dict],
) -> None:
    root, data = synthetic_inventory
    (root / "history/evidence.sql").unlink()
    errors = checker.validate_inventory(root=root, data=data)
    assert "protected historical evidence is missing: history/evidence.sql" in errors


def test_analyze_audit_invokes_checker_and_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checker, "validate_inventory", lambda: ["forced invalid census"])
    surface = next(surface for surface in audit.SURFACES if surface.key == "analyze-and-test")
    result = audit.evaluate(surface, {}, [])
    assert "legacy analytics inventory: forced invalid census" in result.structural
    monkeypatch.setattr(audit, "sweep_plumbing", audit.Plumbing)
    assert audit.report([result], [], detailed=False) == 1


def test_analyze_audit_structurally_blocks_unqualified_retirement() -> None:
    surface = next(surface for surface in audit.SURFACES if surface.key == "analyze-and-test")
    result = audit.evaluate(surface, {}, [])
    assert any(
        "retirement REFUSED pending Story 65.10 G10" in problem
        for problem in result.structural
    )
