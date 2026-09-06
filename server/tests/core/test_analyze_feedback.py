from __future__ import annotations

import base64
import hmac
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from core.analyze_feedback import (
    FeedbackContextError,
    feedback_context_secret,
    feedback_fields_from_visualization_spec,
    mint_delivery_feedback_context,
    mint_eligible_delivery_feedback_context,
    mint_feedback_context,
    normalize_feedback_request,
    request_fingerprint,
    score_feedback_after_commit,
    verify_feedback_context,
)

SECRET = b"s" * 32
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _context(**overrides):
    claims = {
        "org_id": "org_1",
        "project_id": "prj_1",
        "surface": "mcp_app",
        "interaction_ref": "afi_01K00000000000000000000000",
        "result_id": "res_01K00000000000000000000000",
        "result_content_hash": "a" * 64,
        "delivered_rows": {"start": 0, "count": 2, "fields": ["day", "running_total_micros"]},
    }
    claims.update(overrides)
    return mint_feedback_context(claims, secret=SECRET, now=NOW)


def test_signed_context_round_trip_is_bounded_and_tamper_evident():
    sidecar = _context()

    assert set(sidecar) == {"schema_version", "token", "interaction_ref", "expires_at"}
    assert sidecar["schema_version"] == "exact-feedback.v1"
    assert len(sidecar["token"].encode()) < 4096
    claims = verify_feedback_context(sidecar, secret=SECRET, now=NOW)
    assert claims["purpose"] == "analyze_feedback"
    assert claims["delivered_rows"]["start"] == 0
    assert claims["delivered_rows"]["count"] == 2
    assert claims["delivered_rows"]["field_count"] == 2
    assert len(claims["delivered_rows"]["fields_hash"]) == 64

    damaged = dict(sidecar)
    damaged["token"] = sidecar["token"][:-1] + ("A" if sidecar["token"][-1] != "A" else "B")
    with pytest.raises(FeedbackContextError, match="not valid"):
        verify_feedback_context(damaged, secret=SECRET, now=NOW)


def test_mint_round_trip_accepts_a_clock_with_microseconds():
    sidecar = mint_feedback_context(
        {
            "org_id": "org_1",
            "project_id": "prj_1",
            "surface": "console",
            "interaction_ref": "afi_01K00000000000000000000000",
            "result_id": "res_01K00000000000000000000000",
            "result_content_hash": "a" * 64,
            "delivered_rows": {"start": 0, "count": 0, "fields": []},
        },
        secret=SECRET,
        now=NOW.replace(microsecond=123456),
    )
    assert verify_feedback_context(sidecar, secret=SECRET, now=NOW)["result_id"].startswith(
        "res_"
    )


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    [
        (True, 10),
        (1.0, 10),
        ("1", 10),
        (10, 10),
        (10, 10 + 3601),
    ],
)
def test_verifier_refuses_non_integer_or_invalid_signed_times(issued_at, expires_at):
    sidecar = _context()
    body, _signature = sidecar["token"].split(".")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    claims = json.loads(raw)
    claims["issued_at"] = issued_at
    claims["expires_at"] = expires_at
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.digest(SECRET, encoded.encode(), "sha256")
    ).rstrip(b"=").decode()
    forged = {
        **sidecar,
        "token": f"{encoded}.{signature}",
        "expires_at": datetime.fromtimestamp(
            expires_at if type(expires_at) is int else 10, timezone.utc
        ).isoformat().replace("+00:00", "Z"),
    }
    with pytest.raises(FeedbackContextError):
        verify_feedback_context(forged, secret=SECRET, now=NOW)


def test_verifier_refuses_a_signed_context_issued_in_the_future():
    sidecar = _context()
    with pytest.raises(FeedbackContextError, match="not valid"):
        verify_feedback_context(sidecar, secret=SECRET, now=NOW - timedelta(seconds=1))


def test_secret_fallback_exists_only_when_authentication_is_disabled(monkeypatch):
    monkeypatch.delenv("TOOROW_FEEDBACK_CONTEXT_SECRET", raising=False)
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    with pytest.raises(RuntimeError, match="required"):
        feedback_context_secret()
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    assert len(feedback_context_secret()) == 32


def test_pin_mapping_cannot_override_signed_result_authority():
    with pytest.raises(FeedbackContextError, match="pins"):
        mint_delivery_feedback_context(
            org_id="org_1",
            project_id="prj_1",
            result_id="res_01K00000000000000000000000",
            result_content_hash="a" * 64,
            surface="console",
            stored_rows=[],
            delivered_rows=[],
            delivered_fields=[],
            pins={"result_id": "res_FOREIGN"},
        )


def test_compact_attestation_covers_every_visible_row_and_eligible_field():
    fields = [f"metric_{index}" for index in range(150)]
    sidecar = mint_delivery_feedback_context(
        org_id="org_1",
        project_id="prj_1",
        result_id="res_01K00000000000000000000000",
        result_content_hash="a" * 64,
        surface="console",
        stored_rows=[{"metric_149": index} for index in range(1000)],
        delivered_rows=[{"metric_149": index} for index in range(1000)],
        delivered_fields=fields,
    )

    assert len(sidecar["token"].encode()) < 4096
    claims = verify_feedback_context(sidecar)
    assert claims["delivered_rows"]["count"] == 1000
    assert claims["delivered_rows"]["field_count"] == 150
    assert normalize_feedback_request(
        claims=claims,
        target={"kind": "datum", "row_index": 999, "field": "metric_149"},
        polarity="negative",
        comment=None,
        retry_key="last-visible-cell",
    )["target"] == {"kind": "datum", "row_index": 999, "field": "metric_149"}


def test_feedback_capability_refuses_more_than_150_delivered_fields():
    with pytest.raises(FeedbackContextError, match="window is not valid"):
        mint_delivery_feedback_context(
            org_id="org_1",
            project_id="prj_1",
            result_id="res_01K00000000000000000000000",
            result_content_hash="a" * 64,
            surface="console",
            stored_rows=[],
            delivered_rows=[],
            delivered_fields=[f"metric_{index}" for index in range(151)],
        )


def test_feedback_capability_refuses_more_than_1000_delivered_rows():
    rows = [{"metric": index} for index in range(1001)]
    with pytest.raises(FeedbackContextError, match="window is not valid"):
        mint_delivery_feedback_context(
            org_id="org_1",
            project_id="prj_1",
            result_id="res_01K00000000000000000000000",
            result_content_hash="a" * 64,
            surface="console",
            stored_rows=rows,
            delivered_rows=rows,
            delivered_fields=["metric"],
        )


def test_feedback_capability_refuses_more_than_200_path_steps():
    with pytest.raises(FeedbackContextError, match="AI Path is not valid"):
        mint_delivery_feedback_context(
            org_id="org_1",
            project_id="prj_1",
            result_id="res_01K00000000000000000000000",
            result_content_hash="a" * 64,
            surface="console",
            stored_rows=[],
            delivered_rows=[],
            delivered_fields=[],
            ai_path_id="aip_01K00000000000000000000000",
            path_step_ordinals=list(range(201)),
        )


def test_delivery_sidecar_exists_only_after_eligibility_is_recorded(monkeypatch):
    from core import feedback_review

    recorded = []
    monkeypatch.setattr(
        feedback_review,
        "record_feedback_eligibility",
        lambda conn, **kwargs: recorded.append((conn, kwargs))
        or {"schema_version": "feedback-eligibility.v1"},
        raising=False,
    )
    conn = object()
    sidecar = mint_eligible_delivery_feedback_context(
        conn,
        org_id="org_1",
        project_id="prj_1",
        result_id="res_01K00000000000000000000000",
        result_content_hash="a" * 64,
        surface="console",
        stored_rows=[],
        delivered_rows=[],
        delivered_fields=[],
    )

    assert sidecar is not None
    claims = verify_feedback_context(sidecar)
    assert recorded == [(conn, {"claims": claims, "source": "authenticated"})]


def test_delivery_omits_feedback_when_eligibility_savepoint_fails(monkeypatch):
    from core import feedback_review

    monkeypatch.setattr(
        feedback_review,
        "record_feedback_eligibility",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    assert (
        mint_eligible_delivery_feedback_context(
            object(),
            org_id="org_1",
            project_id="prj_1",
            result_id="res_01K00000000000000000000000",
            result_content_hash="a" * 64,
            surface="console",
            stored_rows=[],
            delivered_rows=[],
            delivered_fields=[],
        )
        is None
    )


def test_delivery_omits_feedback_before_recording_when_it_exceeds_a_cap(monkeypatch):
    from core import feedback_review

    recorded = []
    monkeypatch.setattr(
        feedback_review,
        "record_feedback_eligibility",
        lambda *_args, **_kwargs: recorded.append(True),
        raising=False,
    )
    assert (
        mint_eligible_delivery_feedback_context(
            object(),
            org_id="org_1",
            project_id="prj_1",
            result_id="res_01K00000000000000000000000",
            result_content_hash="a" * 64,
            surface="console",
            stored_rows=[],
            delivered_rows=[],
            delivered_fields=[f"metric_{index}" for index in range(151)],
        )
        is None
    )
    assert recorded == []


def test_visual_target_fields_map_bound_ids_to_safe_result_names():
    schema = {
        "fields": [
            {"id": "wf_label", "name": "label"},
            {"id": "wf_running_total_micros", "name": "running_total_micros"},
            {"id": "internal", "name": "secret", "hidden": True},
            {"id": "not_bound", "name": "source_value_micros"},
        ]
    }
    spec = {
        "bindings": {
            "dimension": ["wf_label"],
            "measure": ["wf_running_total_micros"],
        }
    }

    manifest = {
        "provenance": {
            "values": [
                {"member_id": "metric.sessions", "source_field": "running_total_micros"}
            ]
        }
    }
    spec["bindings"]["measure"] = ["metric.sessions"]

    assert feedback_fields_from_visualization_spec(schema, spec, manifest) == [
        "label",
        "running_total_micros",
    ]


def test_langfuse_score_is_recorded_only_and_failure_is_non_fatal(monkeypatch):
    from core import tracing

    calls = []

    class FakeLangfuse:
        def score(self, **kwargs):
            calls.append(kwargs)

        def flush(self):
            calls.append("flush")

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=FakeLangfuse))
    score_feedback_after_commit(
        status="recorded", trace_id="a" * 32, polarity="negative", comment="Wrong"
    )
    score_feedback_after_commit(
        status="replayed", trace_id="a" * 32, polarity="negative", comment="Wrong"
    )
    score_feedback_after_commit(
        status="refresh_required", trace_id="a" * 32, polarity="negative", comment=None
    )
    assert calls == [
        {
            "trace_id": "a" * 32,
            "name": "user_feedback",
            "value": -1.0,
            "comment": "Wrong",
        },
        "flush",
    ]

    monkeypatch.setitem(
        sys.modules,
        "langfuse",
        SimpleNamespace(Langfuse=lambda: (_ for _ in ()).throw(RuntimeError("offline"))),
    )
    score_feedback_after_commit(
        status="recorded", trace_id="b" * 32, polarity="positive", comment=None
    )


def test_expired_context_is_verified_for_authenticated_reissue_only():
    sidecar = _context()

    with pytest.raises(FeedbackContextError) as caught:
        verify_feedback_context(sidecar, secret=SECRET, now=NOW + timedelta(minutes=16))

    assert caught.value.code == "feedback_context_expired"
    assert caught.value.verified_claims["result_id"].startswith("res_")


def test_closed_target_algebra_and_delivered_window_accept_waterfall_visible_field():
    claims = verify_feedback_context(_context(), secret=SECRET, now=NOW)

    normalized = normalize_feedback_request(
        claims=claims,
        target={"kind": "datum", "row_index": 1, "field": "running_total_micros"},
        polarity="negative",
        comment="The cumulative jump looks wrong.",
        retry_key="retry-1",
    )
    assert normalized["target"] == {
        "kind": "datum",
        "row_index": 1,
        "field": "running_total_micros",
    }

    with pytest.raises(FeedbackContextError, match="not available"):
        normalize_feedback_request(
            claims=claims,
            target={"kind": "datum", "row_index": 2, "field": "running_total_micros"},
            polarity="negative",
            comment=None,
            retry_key="retry-2",
        )


def test_refresh_does_not_change_semantic_request_fingerprint():
    claims = verify_feedback_context(_context(), secret=SECRET, now=NOW)
    request = normalize_feedback_request(
        claims=claims,
        target={"kind": "answer"},
        polarity="positive",
        comment="Useful",
        retry_key="one-client-key",
    )
    refreshed = verify_feedback_context(
        _context(), secret=SECRET, now=NOW + timedelta(minutes=1)
    )

    assert request_fingerprint(request, claims=claims, secret=SECRET) == request_fingerprint(
        request, claims=refreshed, secret=SECRET
    )


def test_path_step_ordinal_zero_is_valid_when_the_result_delivered_it():
    claims = verify_feedback_context(
        _context(ai_path_id="aip_01K00000000000000000000000", path_step_ordinals=[0]),
        secret=SECRET,
        now=NOW,
    )

    normalized = normalize_feedback_request(
        claims=claims,
        target={"kind": "path_step", "ordinal": 0},
        polarity="negative",
        comment=None,
        retry_key="path-retry",
    )

    assert normalized["target"] == {"kind": "path_step", "ordinal": 0}


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "answer", "field": "day"},
        {"kind": "datum", "row_index": -1, "field": "day"},
        {"kind": "path_step", "ordinal": 0},
        {"kind": "invented"},
    ],
)
def test_invalid_or_unattested_targets_refuse(target):
    claims = verify_feedback_context(_context(), secret=SECRET, now=NOW)
    with pytest.raises(FeedbackContextError):
        normalize_feedback_request(
            claims=claims,
            target=target,
            polarity="positive",
            comment=None,
            retry_key="retry",
        )
