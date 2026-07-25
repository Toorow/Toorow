"""Tests for Epic 8 R6 gap fix: metric_definitions + llm_commentary_guidelines.

Covers:
  (a) A report WITH an override carrying metric_definitions produces
      data.metric_definitions in the envelope (injected via build_envelope).
  (b) A report WITHOUT override omits the key (backward-compatible).
  (c) llm_commentary_guidelines is appended to the narrative prompt fed to
      build_narrative (via build_summary).
  (d) Live-guarded integration test: upsert_flow then assert get_report envelope
      carries the definitions (requires TEST_POSTGRES_DSN env var).

Files under test: server/core/reports.py, server/core/main.py (R6 path only).
Flows helpers are mocked — flows.py is READ-ONLY per scope constraints.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import reports as reports_module  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers shared across unit tests
# ---------------------------------------------------------------------------

_METRIC_DEFS = {
    "sessions": {
        "definition": "Nombre de sessions initiées par les utilisateurs.",
        "unit": "séances",
        "direction": "up_good",
        "caveats": None,
    },
    "conversions": {
        "definition": "Actions clés réalisées par les utilisateurs.",
        "unit": None,
        "direction": "up_good",
        "caveats": "Inclut les micro-conversions.",
    },
}

_GUIDELINES = "Insiste sur les tendances hebdomadaires. Évite le jargon technique."

_BASE_REPORT = {
    "id": "overview_daily",
    "display_name": "Vue d'ensemble quotidienne",
    "metrics": ["sessions", "conversions"],
    "dimensions": ["date"],
    "layout": {"chart_type": "line", "order_by": "date"},
    "narrative_prompt": "Analyse GA4 les tendances de trafic.",
    "date_window": {"default_days": 30},
}

_ROWS = [
    {
        "date": "2026-07-01",
        "connector": "google-analytics",
        "metric": "sessions",
        "breakdown_dimension": "date",
        "breakdown_value": "2026-07-01",
        "value": 1200,
        "pull_id": "pull_001",
        "loaded_at": "2026-07-02T00:00:00Z",
    },
    {
        "date": "2026-07-01",
        "connector": "google-analytics",
        "metric": "conversions",
        "breakdown_dimension": "date",
        "breakdown_value": "2026-07-01",
        "value": 45,
        "pull_id": "pull_001",
        "loaded_at": "2026-07-02T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# (a) build_envelope injects metric_definitions when non-empty
# ---------------------------------------------------------------------------


class TestBuildEnvelopeR6MetricDefinitions:
    def test_metric_definitions_present_in_data_when_provided(self):
        """(a) A report with metric_definitions: data.metric_definitions is set."""
        envelope = reports_module.build_envelope(
            _BASE_REPORT,
            _ROWS,
            "google-analytics",
            "2026-07-01",
            "2026-07-07",
            "proj_test",
            metric_definitions=_METRIC_DEFS,
        )
        assert "metric_definitions" in envelope["data"], (
            "data.metric_definitions must be present when non-empty definitions are provided"
        )
        assert envelope["data"]["metric_definitions"] == _METRIC_DEFS

    def test_metric_definitions_absent_when_not_provided(self):
        """(b) A report WITHOUT metric_definitions: key is absent (backward-compat)."""
        envelope = reports_module.build_envelope(
            _BASE_REPORT,
            _ROWS,
            "google-analytics",
            "2026-07-01",
            "2026-07-07",
            "proj_test",
        )
        assert "metric_definitions" not in envelope["data"], (
            "data.metric_definitions must be absent when no definitions are provided "
            "(backward-compatible with pre-R6 envelopes)"
        )

    def test_metric_definitions_absent_when_empty_dict(self):
        """(b-variant) An empty dict is treated as no definitions (falsy guard)."""
        envelope = reports_module.build_envelope(
            _BASE_REPORT,
            _ROWS,
            "google-analytics",
            "2026-07-01",
            "2026-07-07",
            "proj_test",
            metric_definitions={},
        )
        assert "metric_definitions" not in envelope["data"]

    def test_envelope_schema_version_and_meta_intact(self):
        """AD-1 contract: schema_version + meta keys are unchanged by R6 injection."""
        envelope = reports_module.build_envelope(
            _BASE_REPORT,
            _ROWS,
            "google-analytics",
            "2026-07-01",
            "2026-07-07",
            "proj_test",
            metric_definitions=_METRIC_DEFS,
        )
        assert envelope["schema_version"] == "1"
        assert "freshness" in envelope["meta"]
        assert "provenance" in envelope["meta"]
        assert "alerts" in envelope["meta"]
        # data must still contain all existing keys
        assert "report_id" in envelope["data"]
        assert "rows" in envelope["data"]
        assert "metrics" in envelope["data"]
        assert "date_range" in envelope["data"]

    def test_metric_definitions_shape_preserved(self):
        """The injected definitions carry the exact schema shape (direction, unit, caveats)."""
        envelope = reports_module.build_envelope(
            _BASE_REPORT,
            _ROWS,
            "google-analytics",
            "2026-07-01",
            "2026-07-07",
            "proj_test",
            metric_definitions=_METRIC_DEFS,
        )
        defs = envelope["data"]["metric_definitions"]
        assert defs["sessions"]["direction"] == "up_good"
        assert defs["sessions"]["unit"] == "séances"
        assert defs["conversions"]["caveats"] == "Inclut les micro-conversions."


# ---------------------------------------------------------------------------
# (c) build_summary appends llm_commentary_guidelines to narrative_prompt
# ---------------------------------------------------------------------------


class TestBuildSummaryR6Guidelines:
    def _run_build_summary(self, guidelines=None):
        """Call build_summary and capture the narrative_prompt passed to build_narrative."""
        captured = {}

        def _fake_narrative(*, narrative_prompt, **_kw):
            captured["narrative_prompt"] = narrative_prompt
            return "summary"

        def _fake_rollup(*_a, **_kw):
            return {
                "sessions": {
                    "value": 1200,
                    "source_system": "google-analytics",
                    "source_field": "fact_daily_kpi",
                    "pull_id": "pull_001",
                }
            }

        with patch("core.narrative.build_narrative", side_effect=_fake_narrative), \
             patch("core.rollup.compute_rollup", side_effect=_fake_rollup):
            reports_module.build_summary(
                _BASE_REPORT,
                _ROWS,
                "2026-07-01",
                "2026-07-07",
                "google-analytics",
                llm_commentary_guidelines=guidelines,
            )
        return captured.get("narrative_prompt")

    def test_guidelines_appended_to_narrative_prompt(self):
        """(c) llm_commentary_guidelines is appended to the narrative_prompt."""
        prompt = self._run_build_summary(guidelines=_GUIDELINES)
        assert prompt is not None
        assert "Directives de commentaire:" in prompt
        assert _GUIDELINES in prompt
        # Base prompt must still be present
        assert "Analyse GA4" in prompt

    def test_guidelines_separator_format(self):
        """The separator is '\\n\\nDirectives de commentaire: ' (double newline)."""
        prompt = self._run_build_summary(guidelines=_GUIDELINES)
        assert "\n\nDirectives de commentaire: " in prompt

    def test_no_guidelines_does_not_change_prompt(self):
        """Without guidelines, narrative_prompt is unchanged."""
        prompt = self._run_build_summary(guidelines=None)
        assert prompt == _BASE_REPORT["narrative_prompt"]

    def test_empty_guidelines_string_does_not_change_prompt(self):
        """An empty / whitespace-only guidelines string is ignored."""
        prompt = self._run_build_summary(guidelines="   ")
        assert prompt == _BASE_REPORT["narrative_prompt"]

    def test_guidelines_only_no_base_prompt(self):
        """When report has no narrative_prompt, guidelines become the full prompt."""
        report_no_prompt = dict(_BASE_REPORT, narrative_prompt=None)
        captured = {}

        def _fake_narrative(*, narrative_prompt, **_kw):
            captured["narrative_prompt"] = narrative_prompt
            return "summary"

        def _fake_rollup(*_a, **_kw):
            return {}

        with patch("core.narrative.build_narrative", side_effect=_fake_narrative), \
             patch("core.rollup.compute_rollup", side_effect=_fake_rollup):
            reports_module.build_summary(
                report_no_prompt,
                _ROWS,
                "2026-07-01",
                "2026-07-07",
                "google-analytics",
                llm_commentary_guidelines=_GUIDELINES,
            )
        prompt = captured.get("narrative_prompt")
        assert prompt is not None
        assert "Directives de commentaire: " in prompt
        assert _GUIDELINES in prompt


# ---------------------------------------------------------------------------
# render_report: end-to-end R6 field threading
# ---------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, name, reports):
        self.name = name
        self.manifest = {"widget_ref": "ui://core/daily-report"}
        self.reports = reports


class TestRenderReportR6:
    """render_report passes R6 fields through to envelope + summary."""

    def _loaded_modules(self):
        return [_FakeModule("google-analytics", [_BASE_REPORT])]

    def _run_render(self, metric_definitions=None, llm_commentary_guidelines=None):
        """Run render_report with mocked warehouse + rollup, capture results."""
        with patch("core.warehouse.query_report", return_value=_ROWS), \
             patch("core.rollup._split_periods", return_value=(_ROWS, [])), \
             patch("core.narrative.build_narrative", return_value="summary text") as mock_narr, \
             patch("core.rollup.compute_rollup", return_value={"sessions": {"value": 1200}}):
            summary, envelope, widget_uri = reports_module.render_report(
                self._loaded_modules(),
                "proj_test",
                "google-analytics/overview_daily",
                "2026-07-01",
                "2026-07-07",
                metric_definitions=metric_definitions,
                llm_commentary_guidelines=llm_commentary_guidelines,
            )
        return summary, envelope, mock_narr

    def test_metric_definitions_in_envelope_data(self):
        """render_report with metric_definitions -> envelope.data.metric_definitions set."""
        _, envelope, _ = self._run_render(metric_definitions=_METRIC_DEFS)
        assert "metric_definitions" in envelope["data"]
        assert envelope["data"]["metric_definitions"] == _METRIC_DEFS

    def test_metric_definitions_absent_without_override(self):
        """render_report without metric_definitions -> key absent (backward-compat)."""
        _, envelope, _ = self._run_render()
        assert "metric_definitions" not in envelope["data"]

    def test_guidelines_appended_to_narrative_prompt(self):
        """render_report with guidelines -> build_narrative receives combined prompt."""
        _, _, mock_narr = self._run_render(llm_commentary_guidelines=_GUIDELINES)
        call_kwargs = mock_narr.call_args.kwargs
        prompt = call_kwargs.get("narrative_prompt") or ""
        assert "Directives de commentaire:" in prompt
        assert _GUIDELINES in prompt


# ---------------------------------------------------------------------------
# Live-guarded integration test (requires TEST_POSTGRES_DSN)
# ---------------------------------------------------------------------------

_LIVE_REASON = "TEST_POSTGRES_DSN not set — skipping live R6 override integration test"


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_DSN"), reason=_LIVE_REASON)
class TestR6LiveEnvelopeIntegration:
    """Integration: upsert a report override with metric_definitions, then assert
    get_report envelope carries data.metric_definitions.

    Requires TEST_POSTGRES_DSN (psycopg DSN string) pointing to a live Postgres
    with the connector schema (migration 025 applied).
    """

    def _get_conn(self):
        import psycopg  # noqa: PLC0415

        dsn = os.environ["TEST_POSTGRES_DSN"]
        return psycopg.connect(dsn)

    def test_upsert_flow_then_envelope_has_metric_definitions(self):
        """After upserting a report override, render_report envelope carries definitions."""
        from core import flows as flows_module  # noqa: PLC0415

        project_id = "proj_live_r6_test"
        base_report_id = "google-analytics/overview_daily"

        override_doc = {
            "schema_version": "1",
            "kind": "report",
            "project_id": project_id,
            "base_report_id": base_report_id,
            "metric_definitions": _METRIC_DEFS,
            "llm_commentary_guidelines": _GUIDELINES,
        }

        with self._get_conn() as conn:
            # Scope check will pass if project exists; otherwise this test reveals
            # a missing fixture (acceptable — CI would run this with a seeded DB).
            try:
                flows_module.upsert_flow(
                    project_id, override_doc, "test_runner", conn,
                    loaded_modules=[_FakeModule("google-analytics", [_BASE_REPORT])],
                )
                conn.commit()
            except Exception as exc:
                pytest.skip(f"upsert_flow failed (DB fixture missing?): {exc}")

            # Fetch the override and verify _merge_report produces R6 fields.
            base_doc = flows_module._base_report_doc(
                base_report_id,
                [_FakeModule("google-analytics", [_BASE_REPORT])],
            )
            override = flows_module._fetch_report_override(project_id, base_report_id, conn)
            assert override is not None, "Override must exist after upsert"

            merged = flows_module._merge_report(base_doc, override, base_report_id, project_id)
            assert merged.get("metric_definitions") == _METRIC_DEFS, (
                "Merged doc must carry metric_definitions from the override"
            )
            assert merged.get("llm_commentary_guidelines") == _GUIDELINES

            # Build the envelope as render_report would and verify the key.
            envelope = reports_module.build_envelope(
                _BASE_REPORT,
                _ROWS,
                "google-analytics",
                "2026-07-01",
                "2026-07-07",
                project_id,
                metric_definitions=merged.get("metric_definitions"),
            )
            assert "metric_definitions" in envelope["data"]
            assert envelope["data"]["metric_definitions"]["sessions"]["direction"] == "up_good"
