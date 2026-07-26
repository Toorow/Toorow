"""Unit tests for the live ratification probe harness (Story 25.6).

All tests are pure-Python, NO network, NO shell, NO live credentials: the harness
HTTP layer (``probe_request``) and account discovery (``discover``) are injected
with deterministic fakes. Two concerns are covered:

  1. ratify_connector.py -- field-probe status classification (ok / empty /
     rejected(<class>) / unsupported), batching + surgical bisection, rate-limit
     pause+resume, error probe, topology probe, structural ("none") ratification,
     report determinism + redaction, verdict computation.
  2. export_connector_registry.py -- the verified-state chain: a manifest that
     declares verification.status "ratified" is accepted ONLY with a valid
     reports/ratification-*.json (verdict ratified + probed_at); otherwise the
     registry refuses (RegistryValidationError). Current blocked manifests keep
     projecting the conservative validation_required readiness.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror test_catalog_gen.py) so scripts + core import.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_SERVER_DIR = _REPO_ROOT / "server"
for _p in (_SCRIPTS_DIR, _SERVER_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ratify_connector as rc  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"
_SCHEMAS_DIR = _SERVER_DIR / "core" / "schemas"


def _load_exporter():
    """Load export_connector_registry.py as a module (mirror conformance test)."""
    path = _SCRIPTS_DIR / "export_connector_registry.py"
    spec = importlib.util.spec_from_file_location("export_connector_registry", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# Fakes for the injected probe_request / discover.
# ===========================================================================


def _target(field_id, kind="metric", source_field=None):
    return {
        "field_id": field_id,
        "source_field": source_field or field_id,
        "kind": kind,
    }


class RecordingProbe:
    """A deterministic fake probe_request driven by a per-field verdict map.

    verdicts: {field_id: "ok"|"empty"|"unsupported"|("rejected", <error_class>)}.
    Records every batch (list of field_ids) it was asked, so tests can assert
    batching and bisection behaviour. A special field_id in *rate_limit_once*
    triggers a single 429 then succeeds (pause+resume path).
    """

    def __init__(self, verdicts, rate_limit_first_n=0, retry_after=None):
        self.verdicts = verdicts
        self.calls = []
        self._rate_limit_left = rate_limit_first_n
        self.retry_after = retry_after

    def __call__(self, *, module, request_style, account, batch, token):
        ids = [t["field_id"] for t in batch]
        self.calls.append(ids)
        if self._rate_limit_left > 0:
            self._rate_limit_left -= 1
            return rc.ProbeOutcome(rate_limited=True, retry_after=self.retry_after)
        present, empty, rejected, unsupported = set(), set(), {}, set()
        for fid in ids:
            v = self.verdicts.get(fid, "empty")
            if isinstance(v, tuple) and v[0] == "rejected":
                rejected[fid] = v[1]
            elif v == "unsupported":
                unsupported.add(fid)
            elif v == "ok":
                present.add(fid)
            else:
                empty.add(fid)
        return rc.ProbeOutcome(
            present=frozenset(present),
            empty=frozenset(empty),
            rejected=rejected,
            unsupported=frozenset(unsupported),
        )


# ===========================================================================
# Field-probe status classification.
# ===========================================================================


class TestFieldProbeClassification:
    def test_ok_empty_rejected_unsupported(self):
        targets = [
            _target("a"),
            _target("b"),
            _target("c"),
            _target("d"),
        ]
        probe = RecordingProbe(
            {
                "a": "ok",
                "b": "empty",
                "c": ("rejected", "invalid_request"),
                "d": "unsupported",
            }
        )
        statuses = rc.probe_fields(
            "m",
            "report_statistics",
            10,
            "acct",
            targets,
            probe,
            sleep=lambda _s: None,
        )
        assert statuses["a"] == "ok"
        assert statuses["b"] == "empty"
        assert statuses["c"] == "rejected(invalid_request)"
        assert statuses["d"] == "unsupported"

    def test_rejected_captures_exact_error_class(self):
        targets = [_target("x")]
        probe = RecordingProbe({"x": ("rejected", "permission_denied")})
        statuses = rc.probe_fields(
            "m", "fields_param", 5, "acct", targets, probe, sleep=lambda _s: None
        )
        assert statuses["x"] == "rejected(permission_denied)"


# ===========================================================================
# Batching + surgical bisection.
# ===========================================================================


class TestBatching:
    def test_respects_batch_size(self):
        targets = [_target(f"f{i}") for i in range(45)]
        probe = RecordingProbe({t["field_id"]: "ok" for t in targets})
        rc.probe_fields("m", "fields_param", 20, "acct", targets, probe, sleep=lambda _s: None)
        # 45 fields / 20 -> batches of 20, 20, 5 (all clean, no bisection).
        assert [len(c) for c in probe.calls] == [20, 20, 5]

    def test_clean_batch_is_not_bisected(self):
        targets = [_target(f"f{i}") for i in range(3)]
        probe = RecordingProbe({t["field_id"]: "ok" for t in targets})
        rc.probe_fields("m", "fields_param", 20, "acct", targets, probe, sleep=lambda _s: None)
        assert probe.calls == [["f0", "f1", "f2"]]

    def test_rejected_batch_is_bisected_to_isolate_the_field(self):
        targets = [_target("good1"), _target("bad"), _target("good2")]
        probe = RecordingProbe(
            {"good1": "ok", "bad": ("rejected", "invalid_request"), "good2": "ok"}
        )
        statuses = rc.probe_fields(
            "m", "fields_param", 20, "acct", targets, probe, sleep=lambda _s: None
        )
        # First the whole batch, then each field alone (bisection).
        assert probe.calls[0] == ["good1", "bad", "good2"]
        assert probe.calls[1:] == [["good1"], ["bad"], ["good2"]]
        assert statuses["good1"] == "ok"
        assert statuses["good2"] == "ok"
        assert statuses["bad"] == "rejected(invalid_request)"

    def test_single_field_batch_not_recursed(self):
        # batch_size 1 -> no bisection recursion even on rejection.
        targets = [_target("bad")]
        probe = RecordingProbe({"bad": ("rejected", "invalid_request")})
        rc.probe_fields("m", "dimensions_only", 1, "acct", targets, probe, sleep=lambda _s: None)
        assert probe.calls == [["bad"]]


# ===========================================================================
# Rate-limit pause + resume (breaker-aware).
# ===========================================================================


class TestRateLimit:
    def test_pauses_and_resumes_same_batch(self):
        targets = [_target("a"), _target("b")]
        probe = RecordingProbe({"a": "ok", "b": "ok"}, rate_limit_first_n=1, retry_after=7)
        slept = []
        statuses = rc.probe_fields(
            "m", "fields_param", 20, "acct", targets, probe, sleep=slept.append
        )
        # 429 once -> paused for retry_after, then the SAME batch resolved.
        assert slept == [7.0]
        assert statuses == {"a": "ok", "b": "ok"}
        assert probe.calls == [["a", "b"], ["a", "b"]]

    def test_gives_up_after_max_retries_without_hanging(self):
        targets = [_target("a")]
        probe = RecordingProbe({"a": "ok"}, rate_limit_first_n=99)
        statuses = rc.probe_fields(
            "m",
            "fields_param",
            20,
            "acct",
            targets,
            probe,
            sleep=lambda _s: None,
            max_retries=3,
        )
        # Never resolves -> the field is left unprobed (absent from statuses).
        assert "a" not in statuses


# ===========================================================================
# Error + topology probes.
# ===========================================================================


class TestErrorProbe:
    def test_invalid_request_contract_matches(self):
        probe = RecordingProbe({"__toorow_probe_invalid_field__": ("rejected", "invalid_request")})
        result = rc.probe_errors(
            "m",
            "fields_param",
            "acct",
            probe,
            token="t",
            probe_auth=False,
            sleep=lambda _s: None,
        )
        assert result["invalid_request"]["match"] is True
        assert result["invalid_token"]["probed"] is False

    def test_invalid_request_contract_violation_detected(self):
        probe = RecordingProbe({"__toorow_probe_invalid_field__": "ok"})
        result = rc.probe_errors(
            "m",
            "fields_param",
            "acct",
            probe,
            token="t",
            probe_auth=False,
            sleep=lambda _s: None,
        )
        assert result["invalid_request"]["match"] is False

    def test_probe_auth_opt_in_issues_invalid_token_request(self):
        # The auth probe uses a sentinel bad token; here it maps to auth_expired.
        def probe(*, module, request_style, account, batch, token):
            if token == "__toorow_probe_invalid_token__":
                return rc.ProbeOutcome(rejected={"__toorow_probe_invalid_field__": "auth_expired"})
            return rc.ProbeOutcome(rejected={"__toorow_probe_invalid_field__": "invalid_request"})

        result = rc.probe_errors(
            "m",
            "fields_param",
            "acct",
            probe,
            token="t",
            probe_auth=True,
            sleep=lambda _s: None,
        )
        assert result["invalid_token"]["probed"] is True
        assert result["invalid_token"]["match"] is True


class TestTopologyProbe:
    def test_account_reachable(self):
        result = rc.probe_topology("m", "act_1", lambda: ["act_1", "act_2"])
        assert result["account_reachable"] is True
        assert result["reachable_count"] == 2

    def test_account_not_reachable(self):
        result = rc.probe_topology("m", "act_9", lambda: ["act_1", "act_2"])
        assert result["account_reachable"] is False

    def test_skipped_when_no_topology(self):
        result = rc.probe_topology("m", "act_1", None)
        assert result == {"declared": False, "probed": False, "account_reachable": None}


# ===========================================================================
# Report determinism + redaction + verdict (via a temp module tree).
# ===========================================================================


def _write_module(
    tmp_path,
    module,
    request_style,
    batch_size,
    fields,
    structural_note="note",
    account_topology=True,
):
    mdir = tmp_path / module
    (mdir / "catalog_sources").mkdir(parents=True)
    (mdir / "reports").mkdir()
    probe = {"request_style": request_style, "batch_size": batch_size}
    if request_style == "none":
        probe["structural_note"] = structural_note
    (mdir / "catalog_sources" / "catalog_sources.json").write_text(
        json.dumps({"connector": module, "probe": probe}), encoding="utf-8"
    )
    (mdir / "api_catalog.json").write_text(
        json.dumps({"api_version": "vX", "fields": fields}), encoding="utf-8"
    )
    manifest = {"name": module}
    if account_topology:
        manifest["account_topology"] = {"selection_level": "account"}
    (mdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return mdir


_CORE_FIELDS = [
    {
        "field_id": "sessions",
        "kind": "metric",
        "tier": "core",
        "exposure": "exposed",
        "source_field": "sessions",
    },
    {
        "field_id": "active_users",
        "kind": "metric",
        "tier": "core",
        "exposure": "exposed",
        "source_field": "activeUsers",
    },
    {
        "field_id": "country",
        "kind": "dimension",
        "tier": "core",
        "exposure": "planned",
        "source_field": "country",
    },
    {
        "field_id": "revenue",
        "kind": "metric",
        "tier": "standard",
        "exposure": "exposed",
        "source_field": "revenue",
    },
]


class TestReportAssembly:
    def test_deterministic_bytes(self, tmp_path):
        _write_module(tmp_path, "ga", "report_statistics", 10, _CORE_FIELDS)
        probe = RecordingProbe(
            {
                "sessions": "ok",
                "active_users": "ok",
                "country": "empty",
                "__toorow_probe_invalid_field__": ("rejected", "invalid_request"),
            }
        )
        kwargs = dict(
            module="ga",
            connection_ref="conn-abcd1234",
            account="properties/9",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            discover=lambda: ["properties/9"],
            modules_dir=tmp_path,
            sleep=lambda _s: None,
        )
        r1 = rc.run_ratification(probe_request=RecordingProbe(probe.verdicts), **kwargs)
        r2 = rc.run_ratification(probe_request=RecordingProbe(probe.verdicts), **kwargs)
        assert rc.serialize_report(r1) == rc.serialize_report(r2)
        # Field ordering is sorted regardless of probe order.
        assert list(r1["fields"]) == sorted(r1["fields"])

    def test_redacts_connection_ref_and_carries_no_token(self, tmp_path):
        _write_module(tmp_path, "ga", "report_statistics", 10, _CORE_FIELDS)
        report = rc.run_ratification(
            module="ga",
            connection_ref="connection-SECRET-9f2a",
            account="properties/9",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            probe_request=RecordingProbe(
                {"sessions": "ok", "active_users": "ok", "country": "empty"}
            ),
            discover=lambda: ["properties/9"],
            modules_dir=tmp_path,
            sleep=lambda _s: None,
            token="SUPER-SECRET-TOKEN",
        )
        blob = json.dumps(report)
        assert report["connection_ref"] == "conn_***9f2a"
        assert "connection-SECRET-9f2a" not in blob
        assert "SUPER-SECRET-TOKEN" not in blob

    def test_verdict_ratified_when_all_ok_and_contract_holds(self, tmp_path):
        _write_module(tmp_path, "ga", "report_statistics", 10, _CORE_FIELDS)
        report = rc.run_ratification(
            module="ga",
            connection_ref="conn-1",
            account="p/9",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            probe_request=RecordingProbe(
                {
                    "sessions": "ok",
                    "active_users": "ok",
                    "country": "ok",
                    "__toorow_probe_invalid_field__": ("rejected", "invalid_request"),
                }
            ),
            discover=lambda: ["p/9"],
            modules_dir=tmp_path,
            sleep=lambda _s: None,
        )
        assert report["verdict"] == "ratified"
        assert report["coverage"]["ok"] == 3
        assert report["topology_probe"]["account_reachable"] is True

    def test_verdict_failed_on_rejected_field(self, tmp_path):
        _write_module(tmp_path, "ga", "report_statistics", 10, _CORE_FIELDS)
        report = rc.run_ratification(
            module="ga",
            connection_ref="conn-1",
            account="p/9",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            probe_request=RecordingProbe(
                {
                    "sessions": "ok",
                    "active_users": "ok",
                    "country": ("rejected", "invalid_request"),
                    "__toorow_probe_invalid_field__": ("rejected", "invalid_request"),
                }
            ),
            discover=lambda: ["p/9"],
            modules_dir=tmp_path,
            sleep=lambda _s: None,
        )
        assert report["verdict"] == "failed"

    def test_verdict_failed_when_account_not_reachable(self, tmp_path):
        _write_module(tmp_path, "ga", "report_statistics", 10, _CORE_FIELDS)
        report = rc.run_ratification(
            module="ga",
            connection_ref="conn-1",
            account="p/UNKNOWN",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            probe_request=RecordingProbe(
                {
                    "sessions": "ok",
                    "active_users": "ok",
                    "country": "ok",
                    "__toorow_probe_invalid_field__": ("rejected", "invalid_request"),
                }
            ),
            discover=lambda: ["p/9"],
            modules_dir=tmp_path,
            sleep=lambda _s: None,
        )
        assert report["verdict"] == "failed"

    def test_only_selected_tier_is_probed(self, tmp_path):
        _write_module(tmp_path, "ga", "report_statistics", 10, _CORE_FIELDS)
        report = rc.run_ratification(
            module="ga",
            connection_ref="conn-1",
            account="p/9",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            probe_request=RecordingProbe({"sessions": "ok", "active_users": "ok", "country": "ok"}),
            discover=lambda: ["p/9"],
            modules_dir=tmp_path,
            sleep=lambda _s: None,
        )
        # revenue is 'standard' -> excluded from a core-only probe.
        assert "revenue" not in report["fields"]
        assert set(report["fields"]) == {"sessions", "active_users", "country"}


class TestStructuralRatification:
    def test_none_style_marks_every_field_ok_without_a_request(self, tmp_path):
        fields = [
            {
                "field_id": "date",
                "kind": "dimension",
                "tier": "core",
                "exposure": "exposed",
                "source_field": "date",
            },
            {
                "field_id": "tag_name",
                "kind": "dimension",
                "tier": "core",
                "exposure": "planned",
                "source_field": "tag_name",
            },
        ]
        _write_module(tmp_path, "github", "none", 0, fields, account_topology=False)

        def boom(**_kw):  # a structural probe must NEVER issue a request
            raise AssertionError("structural ratification must not call the API")

        report = rc.run_ratification(
            module="github",
            connection_ref="conn-1",
            account="",
            tiers=("core",),
            probed_at="2026-07-21T12:00:00Z",
            probe_auth=False,
            probe_request=boom,
            discover=None,
            modules_dir=tmp_path,
        )
        assert report["verdict"] == "ratified"
        assert report["fields"] == {"date": "ok", "tag_name": "ok"}
        assert "structural_note" in report
        assert report["error_probe"]["invalid_request"]["probed"] is False


class TestProbeConfigLoading:
    def test_all_twelve_real_modules_declare_a_probe_block(self):
        modules_dir = _SERVER_DIR / "modules"
        names = sorted(
            p.parent.parent.name for p in modules_dir.glob("*/catalog_sources/catalog_sources.json")
        )
        assert len(names) >= 12  # concurrent sessions add modules; every one must declare a probe
        for name in names:
            probe = rc.load_probe_config(name, modules_dir)
            assert probe["request_style"] in rc.KNOWN_REQUEST_STYLES
            assert isinstance(probe["batch_size"], int)

    def test_missing_probe_block_is_refused(self, tmp_path):
        mdir = tmp_path / "x" / "catalog_sources"
        mdir.mkdir(parents=True)
        (mdir / "catalog_sources.json").write_text(json.dumps({"connector": "x"}), encoding="utf-8")
        with pytest.raises(rc.RatificationError, match="missing the required 'probe'"):
            rc.load_probe_config("x", tmp_path)


# ===========================================================================
# Registry verified-state chain (export_connector_registry.py) -- fail-closed.
# ===========================================================================


def _minimal_public_manifest(name):
    """A schema-valid public manifest skeleton with one report profile."""
    return {
        "name": name,
        "display_name": f"{name.title()} Public",
        "auth_type": "oauth2",
        "module_kind": "kpi",
        "public_catalog": {
            "visibility": "public",
            "category": "analytics_product",
            "onboarding_modes": ["connector_pull"],
            "verification": {
                "status": "blocked",
                "reason_code": "live_evidence_not_ratified",
                "follow_up": "Ratify and run the independent connector evidence contract.",
            },
        },
        "report_profiles": [{"id": "overview", "display_name": "Overview"}],
        "source_capabilities": {
            "reports": [
                {
                    "id": "overview",
                    "availability": {"status": "available"},
                    "metrics": ["sessions"],
                    "dimensions": ["date"],
                    "supported_grains": [["date"]],
                    "cadence": {"minimum_interval_minutes": 1440, "supported_modes": ["daily"]},
                }
            ]
        },
    }


class TestRegistryVerifiedChain:
    def test_resolve_readiness_default_is_validation_required(self, tmp_path):
        exporter = _load_exporter()
        manifest = _minimal_public_manifest("google-analytics")
        mdir = tmp_path / "google-analytics"
        (mdir / "reports").mkdir(parents=True)
        readiness = exporter._resolve_readiness(
            manifest, mdir, manifest["public_catalog"]["verification"]
        )
        assert readiness["status"] == "validation_required"
        assert "verifiedAt" not in readiness

    def test_ratified_without_report_is_refused(self, tmp_path):
        exporter = _load_exporter()
        manifest = _minimal_public_manifest("google-analytics")
        manifest["public_catalog"]["verification"]["status"] = "ratified"
        mdir = tmp_path / "google-analytics"
        (mdir / "reports").mkdir(parents=True)
        with pytest.raises(exporter.RegistryValidationError, match="requires a valid"):
            exporter._resolve_readiness(manifest, mdir, manifest["public_catalog"]["verification"])

    def test_ratified_with_fixture_report_becomes_verified(self, tmp_path):
        exporter = _load_exporter()
        manifest = _minimal_public_manifest("google-analytics")
        manifest["public_catalog"]["verification"]["status"] = "ratified"
        mdir = tmp_path / "google-analytics"
        (mdir / "reports").mkdir(parents=True)
        report = json.loads(
            (_FIXTURES / "ratification_report_ratified.json").read_text(encoding="utf-8")
        )
        (mdir / "reports" / "ratification-2026-07-21T120000Z.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        readiness = exporter._resolve_readiness(
            manifest, mdir, manifest["public_catalog"]["verification"]
        )
        assert readiness["status"] == "verified"
        assert readiness["verifiedAt"] == "2026-07-21T12:00:00Z"
        assert "live_ratification_report" in readiness["basis"]

    def test_partial_and_malformed_reports_do_not_lift_readiness(self, tmp_path):
        exporter = _load_exporter()
        manifest = _minimal_public_manifest("google-analytics")
        manifest["public_catalog"]["verification"]["status"] = "ratified"
        mdir = tmp_path / "google-analytics"
        (mdir / "reports").mkdir(parents=True)
        (mdir / "reports" / "ratification-a.json").write_text(
            json.dumps({"verdict": "partial", "probed_at": "2026-07-21T00:00:00Z"}),
            encoding="utf-8",
        )
        (mdir / "reports" / "ratification-b.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises(exporter.RegistryValidationError):
            exporter._resolve_readiness(manifest, mdir, manifest["public_catalog"]["verification"])

    def test_full_registry_regenerates_with_all_validation_required(self):
        exporter = _load_exporter()
        registry = exporter.build_registry(_SERVER_DIR / "modules")
        statuses = {c["readiness"]["status"] for c in registry["connectors"]}
        assert statuses == {"validation_required"}
        # No verified evidence in the tree -> no verifiedAt leaks into the registry.
        assert "verifiedAt" not in exporter.serialize_registry(registry).decode("utf-8")

    def test_relaxed_schema_accepts_ratified_status_structurally(self):
        exporter = _load_exporter()
        validator = exporter._manifest_validator()
        manifest = _minimal_public_manifest("google-analytics")
        # The in-memory schema copy widens verification.status to allow "ratified"
        # so a ratified manifest reaches the fail-closed readiness resolver; the
        # on-disk schema still pins const "blocked" (never mutated here).
        blocked_errors = [
            e
            for e in validator.iter_errors(manifest)
            if "status" in [str(p) for p in e.absolute_path]
            and "verification" in [str(p) for p in e.absolute_path]
        ]
        assert not blocked_errors  # "blocked" is accepted
        manifest["public_catalog"]["verification"]["status"] = "ratified"
        ratified_errors = [
            e
            for e in validator.iter_errors(manifest)
            if "status" in [str(p) for p in e.absolute_path]
            and "verification" in [str(p) for p in e.absolute_path]
        ]
        assert not ratified_errors  # "ratified" is now accepted structurally too

    def test_ondisk_schema_still_pins_blocked(self):
        # Guard: the FORBIDDEN core schema file is untouched (const blocked).
        schema = json.loads((_SCHEMAS_DIR / "manifest.schema.json").read_text(encoding="utf-8"))
        status = schema["properties"]["public_catalog"]["properties"]["verification"]["properties"][
            "status"
        ]
        assert status.get("const") == "blocked"
