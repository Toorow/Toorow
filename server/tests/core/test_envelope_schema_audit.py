"""Story 5.1 (AC9 / AI-31) -- envelope schema audit.

Confirms the canonical envelope schema declares the additive meta keys introduced
across Epic 4 (as_of, context_events, confidence) and documents the additive-under-1.x
policy, and that a get_daily_report-shaped envelope validates against it.

Epic 9 (review fixes 2026-07-14): also asserts the get_card additive meta keys
(card_selection, project_id) are declared AND that a REAL get_card envelope validates
against the canonical schema (Schema Change Checklist -- there was no get_card golden
fixture, so this is the conformance/regression guard for the new keys).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import jsonschema  # noqa: E402

_SCHEMA_PATH = (
    Path(__file__).parent.parent  # server/tests/
    / "conformance"
    / "schemas"
    / "envelope.schema.json"
)


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_meta_keys_declared():
    meta_props = _schema()["properties"]["meta"]["properties"]
    for key in ("as_of", "context_events", "confidence"):
        assert key in meta_props, f"envelope schema is missing declared meta key: {key}"


def test_additive_policy_documented():
    desc = _schema().get("description", "")
    assert "1.x" in desc or "additive" in desc.lower(), (
        "envelope schema must document the additive-under-1.x policy (AI-31)"
    )


def test_enriched_envelope_validates():
    """An envelope carrying all Epic-4 additive keys validates (Layer-2 shape)."""
    envelope = {
        "schema_version": "1",
        "meta": {
            "freshness": {"stale_since": "2026-07-10T00:00:00+00:00"},
            "provenance": {"source_system": "connector-core"},
            "alerts": [],
            "as_of": "2026-07-08T23:59:59+00:00",
            "context_events": [
                {"id": "evt_1", "event_date": "2026-07-05", "type": "business", "label": "sale"}
            ],
            "confidence": {"per_connector": {"google-analytics": 0.98}},
        },
        "data": {"metrics": []},
    }
    jsonschema.Draft202012Validator(_schema()).validate(envelope)


def test_current_view_envelope_validates():
    """The null/scalar current-view shape still validates (backward compat)."""
    envelope = {
        "schema_version": "1",
        "meta": {
            "freshness": "live",
            "provenance": None,
            "alerts": [],
            "as_of": None,
            "context_events": [],
            "confidence": None,
        },
        "data": {},
    }
    jsonschema.Draft202012Validator(_schema()).validate(envelope)


# ---------------------------------------------------------------------------
# Epic 9 (get_card) -- new additive meta keys + a real envelope validation.
# ---------------------------------------------------------------------------


def test_card_meta_keys_declared():
    """card_selection + project_id are declared in the schema (Schema Change Checklist)."""
    meta_props = _schema()["properties"]["meta"]["properties"]
    for key in ("card_selection", "project_id"):
        assert key in meta_props, f"envelope schema is missing declared get_card meta key: {key}"


def test_gate_meta_key_declared():
    """Story 11.6 (AD-18): the measured pre-query gate verdict key is declared."""
    meta_props = _schema()["properties"]["meta"]["properties"]
    assert "gate" in meta_props, "envelope schema is missing declared meta key: gate"


def _card_rows():
    return [
        {
            "date": "2026-07-05",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "mobile",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-06",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "mobile",
            "value": 120.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-06T00:00:00",
        },
    ]


def test_get_card_envelope_validates_against_schema():
    """A REAL get_card envelope (with meta.card_selection + meta.project_id) validates.

    No get_card golden fixture existed; this is the conformance/regression guard the
    Schema Change Checklist requires for the new keys (review-9-1 F-2 / review-9-2b F-10).
    """
    from unittest.mock import patch

    from core import cards as cards_module

    with patch("core.warehouse.query_daily_report", return_value=_card_rows()):
        _summary, envelope, _uri = cards_module.get_card(
            [], "default", metrics=["sessions"],
            date_from="2026-07-01", date_to="2026-07-06",
            trace_id="0123456789abcdef0123456789abcdef",
        )

    # The new keys are actually present on the envelope (not just declarable).
    assert "card_selection" in envelope["meta"]
    assert envelope["meta"]["project_id"] == "default"
    jsonschema.Draft202012Validator(_schema()).validate(envelope)
