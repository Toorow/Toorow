"""Story 36.19 first-value funnel analytics tests (AD-32 / E36-FR09).

Fully offline (no live Postgres): the pure validators/pseudonymisation are exercised
directly and the DB is a MagicMock cursor whose executed SQL + params are captured so we
can assert EXACTLY what would be persisted. Mirrors test_epic36_setup_responsibilities.py.

Coverage:
  (a) a non-allowlisted stage/outcome/recovery/reason is REJECTED (raise) and NOTHING is
      written.
  (b) no raw email/org/project/provider/error/prompt text is ever persisted -- hostile
      inputs go through the pseudonymiser and only enums + the keyed hash + bucket appear
      in the executed INSERT params.
  (c) durations are coarse-bucketed -- an exact duration is never stored.
  (d) tenant_journeys returns only authorized resource-graph journeys (existence-hidden
      when access is denied).
  (e) small-cohort suppression fires below the versioned minimum and the deterministic
      query-budget marker makes repeated queries non-differencing.
  (f) purge_expired removes per policy with a verifiable count.
  (g) the Support-access ledger is separate and not joined into the product-analytics
      reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from core import first_value_funnel as fvf

PEPPER = "z" * 40


@pytest.fixture(autouse=True)
def _pepper(monkeypatch):
    monkeypatch.setenv("TOOROW_FUNNEL_PEPPER", PEPPER)


def _fake_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    return conn, cur


def _ref():
    return fvf.journey_reference(org_id="org_1", project_id="prj_1", subject="op@example.com")


def _cohort():
    return fvf.cohort_reference(org_id="org_1")


# ---------------------------------------------------------------------------
# (a) off-allowlist stage/outcome/recovery/reason -> raise, nothing written.
# ---------------------------------------------------------------------------


def test_non_allowlisted_stage_rejected_and_not_written():
    conn, cur = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_funnel_stage(
            conn,
            journey_ref=_ref(),
            cohort_ref=_cohort(),
            stage="totally_made_up_stage",
            outcome="succeeded",
            policy_version="v1",
        )
    cur.execute.assert_not_called()


def test_non_allowlisted_outcome_rejected():
    conn, cur = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_funnel_stage(
            conn,
            journey_ref=_ref(),
            cohort_ref=_cohort(),
            stage="preview",
            outcome="exploded",
            policy_version="v1",
        )
    cur.execute.assert_not_called()


def test_non_allowlisted_recovery_and_reason_rejected():
    conn, cur = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_funnel_stage(
            conn,
            journey_ref=_ref(),
            cohort_ref=_cohort(),
            stage="recovery_action",
            outcome="succeeded",
            policy_version="v1",
            recovery_kind="delete_everything",
        )
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_funnel_stage(
            conn,
            journey_ref=_ref(),
            cohort_ref=_cohort(),
            stage="preview",
            outcome="abandoned",
            policy_version="v1",
            abandon_reason="the user hated the color; free text here",
        )
    cur.execute.assert_not_called()


def test_recovery_kind_only_on_recovery_stage():
    conn, _ = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_funnel_stage(
            conn,
            journey_ref=_ref(),
            cohort_ref=_cohort(),
            stage="preview",
            outcome="succeeded",
            policy_version="v1",
            recovery_kind="retry",
        )


# ---------------------------------------------------------------------------
# (b) hostile content never persisted: only enums + keyed hash + bucket.
# ---------------------------------------------------------------------------


def test_hostile_content_never_reaches_persisted_params():
    conn, cur = _fake_conn()
    # A pseudonymous reference built from raw email/org -- the raw values must NOT survive.
    raw_email = "victim@customer.example"
    raw_org = "AcmeCorp-Secret-Org"
    ref = fvf.journey_reference(org_id=raw_org, project_id="prj_9", subject=raw_email)
    cohort = fvf.cohort_reference(org_id=raw_org)

    event = fvf.record_funnel_stage(
        conn,
        journey_ref=ref,
        cohort_ref=cohort,
        stage="recovery_action",
        outcome="succeeded",
        policy_version="v3",
        duration_bucket="1m_5m",
        recovery_kind="retry",
    )

    # Exactly one INSERT; capture the params row.
    assert cur.execute.call_count == 1
    sql, params = cur.execute.call_args[0]
    flat = " ".join(str(p) for p in params)
    # No raw identifier / email / provider / error / prompt text anywhere.
    for forbidden in (raw_email, raw_org, "victim", "Acme", "@", "prj_9"):
        assert forbidden not in flat
    # The journey/cohort references are 64-hex keyed hashes.
    assert event.journey_ref_hash in params
    assert len(event.journey_ref_hash) == 64
    assert all(c in "0123456789abcdef" for c in event.journey_ref_hash)
    # Only allowlisted enums present.
    assert "recovery_action" in params
    assert "succeeded" in params
    assert "retry" in params
    assert "1m_5m" in params


def test_journey_reference_is_deterministic_and_one_way():
    a = fvf.journey_reference(org_id="o", project_id="p", subject="s@x")
    b = fvf.journey_reference(org_id="o", project_id="p", subject="s@x")
    c = fvf.journey_reference(org_id="o", project_id="p", subject="other@x")
    assert a == b and a != c
    assert "s@x" not in a  # hash contains no plaintext
    assert len(a) == 64
    assert all(ch in "0123456789abcdef" for ch in a)


# ---------------------------------------------------------------------------
# (c) coarse duration bucketing -- exact durations never stored.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "lt_1m"),
        (59, "lt_1m"),
        (60, "1m_5m"),
        (299, "1m_5m"),
        (300, "5m_30m"),
        (1799, "5m_30m"),
        (1800, "30m_2h"),
        (7199, "30m_2h"),
        (7200, "2h_1d"),
        (86399, "2h_1d"),
        (86400, "gt_1d"),
        (999999, "gt_1d"),
        (None, None),
    ],
)
def test_bucket_duration(seconds, expected):
    assert fvf.bucket_duration(seconds) == expected


def test_bucket_duration_rejects_negative():
    with pytest.raises(fvf.FunnelValidationError):
        fvf.bucket_duration(-1)


def test_exact_duration_never_persisted():
    conn, cur = _fake_conn()
    bucket = fvf.bucket_duration(137)  # 2m17s -> 1m_5m
    fvf.record_funnel_stage(
        conn,
        journey_ref=_ref(),
        cohort_ref=_cohort(),
        stage="recent_pull",
        outcome="succeeded",
        policy_version="v1",
        duration_bucket=bucket,
    )
    _, params = cur.execute.call_args[0]
    assert "1m_5m" in params
    assert 137 not in params  # the exact second count is gone


# ---------------------------------------------------------------------------
# (d) tenant_journeys: authorized only, existence-hidden otherwise.
# ---------------------------------------------------------------------------


def test_tenant_journeys_denied_returns_empty(monkeypatch):
    conn, cur = _fake_conn()
    from core import project_access

    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *a, **k: project_access.AccessDecision(False, "grant_required"),
    )
    out = fvf.tenant_journeys(conn, identity="u", project_id="prj_1", auth_mode="production")
    assert out == []
    # No journey query ran -- existence hidden.
    cur.execute.assert_not_called()


def test_tenant_journeys_authorized_returns_shaped_view(monkeypatch):
    conn, cur = _fake_conn()
    from core import project_access

    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *a, **k: project_access.AccessDecision(True, "owner_floor", "manage", "org_1"),
    )
    now = datetime.now(timezone.utc)
    cur.fetchall.return_value = [
        ("a" * 64, "invitation_delivery", "blocked", None, None, None, now),
        ("a" * 64, "preview", "succeeded", "1m_5m", None, None, now),
    ]
    out = fvf.tenant_journeys(conn, identity="u", project_id="prj_1", auth_mode="production")
    assert len(out) == 1
    view = out[0]
    assert view.journey_ref_hash == "a" * 64
    assert [s["stage"] for s in view.stages] == ["invitation_delivery", "preview"]
    # The blocked delivery stage surfaces its wait-state owner.
    assert "org_admin" in view.wait_state_owners


# ---------------------------------------------------------------------------
# (e) small-cohort suppression + anti-differencing.
# ---------------------------------------------------------------------------


def test_overall_below_minimum_suppressed():
    conn, cur = _fake_conn()
    cur.fetchone.return_value = (2,)  # only 2 distinct tenants, min floor is 5
    with pytest.raises(fvf.CohortSuppressed):
        fvf.cross_tenant_cohort(conn, filters={"stage": "preview"}, min_cohort_version=5)


def test_per_bucket_small_cohort_suppressed():
    conn, cur = _fake_conn()
    # Overall cohort is large enough...
    cur.fetchone.return_value = (12,)
    # ...but one bucket only has 3 tenants (< 5) -> dropped; the other has 8 -> kept.
    cur.fetchall.return_value = [
        ("preview", "succeeded", 40, 8),
        ("preview", "failed", 3, 3),
    ]
    out = fvf.cross_tenant_cohort(conn, filters={"stage": "preview"}, min_cohort_version=5)
    assert len(out) == 1
    assert out[0].outcome == "succeeded"
    assert out[0].cohort_size == 8


def test_min_cohort_version_clamped_to_floor():
    conn, cur = _fake_conn()
    cur.fetchone.return_value = (4,)  # 4 tenants; version says 1 but floor is 5
    with pytest.raises(fvf.CohortSuppressed):
        fvf.cross_tenant_cohort(conn, filters={"stage": "preview"}, min_cohort_version=1)


def test_query_budget_marker_deterministic_anti_differencing():
    conn, cur = _fake_conn()
    cur.fetchone.return_value = (10,)
    cur.fetchall.return_value = [("preview", "succeeded", 30, 10)]
    r1 = fvf.cross_tenant_cohort(conn, filters={"stage": "preview"}, min_cohort_version=5)
    conn2, cur2 = _fake_conn()
    cur2.fetchone.return_value = (10,)
    cur2.fetchall.return_value = [("preview", "succeeded", 30, 10)]
    r2 = fvf.cross_tenant_cohort(conn2, filters={"stage": "preview"}, min_cohort_version=5)
    # Same query shape -> same deterministic marker AND same suppression outcome, so a
    # repeated query yields nothing new to difference against.
    assert r1[0].query_budget_marker == r2[0].query_budget_marker
    # A different query shape -> different marker.
    conn3, cur3 = _fake_conn()
    cur3.fetchone.return_value = (10,)
    cur3.fetchall.return_value = [("recent_pull", "succeeded", 30, 10)]
    r3 = fvf.cross_tenant_cohort(conn3, filters={"stage": "recent_pull"}, min_cohort_version=5)
    assert r3[0].query_budget_marker != r1[0].query_budget_marker


def test_cross_tenant_rejects_raw_identifier_filter():
    conn, _ = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.cross_tenant_cohort(conn, filters={"org_id": "org_1"}, min_cohort_version=5)


# ---------------------------------------------------------------------------
# (f) verifiable retention purge.
# ---------------------------------------------------------------------------


def test_purge_expired_returns_verifiable_count():
    conn, cur = _fake_conn()
    cur.rowcount = 7
    deleted = fvf.purge_expired_funnel_events(conn, policy_version="v1")
    assert deleted == 7
    # The purge flags itself so the append-only trigger permits the DELETE.
    executed = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    assert "app.funnel_purge" in executed
    assert "DELETE FROM app.first_value_events" in executed


# ---------------------------------------------------------------------------
# (g) Support ledger separate, not joined into analytics.
# ---------------------------------------------------------------------------


def test_support_access_ledger_is_separate_write():
    conn, cur = _fake_conn()
    rec = fvf.record_support_access(
        conn,
        actor="support-agent@toorow.io",
        scope_subject="org_1/prj_1",
        purpose="incident_investigation",
        audit_event_id="audit_123",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    sql, params = cur.execute.call_args[0]
    assert "app.support_access_ledger" in sql
    # Never the product-analytics table.
    assert "first_value_events" not in sql
    # Actor/scope are keyed hashes -- raw values absent.
    flat = " ".join(str(p) for p in params)
    assert "support-agent@toorow.io" not in flat
    assert "org_1/prj_1" not in flat
    assert rec.audit_event_id == "audit_123"
    assert len(rec.actor_hash) == 64


def test_support_purpose_must_be_allowlisted():
    conn, _ = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_support_access(
            conn,
            actor="a",
            scope_subject="s",
            purpose="just because",
            audit_event_id="audit_1",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_support_access_requires_prior_audit_and_future_expiry():
    conn, _ = _fake_conn()
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_support_access(
            conn,
            actor="a",
            scope_subject="s",
            purpose="incident_investigation",
            audit_event_id="",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    with pytest.raises(fvf.FunnelValidationError):
        fvf.record_support_access(
            conn,
            actor="a",
            scope_subject="s",
            purpose="incident_investigation",
            audit_event_id="audit_1",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )


def test_missing_pepper_fails_closed(monkeypatch):
    monkeypatch.delenv("TOOROW_FUNNEL_PEPPER", raising=False)
    with pytest.raises(fvf.FunnelValidationError):
        fvf.journey_reference(org_id="o", project_id="p", subject="s")


# ---------------------------------------------------------------------------
# Migration enforces the SAME enums at the DB level (second gate).
# Path is repo-root-relative (matches the neighboring migration tests); run pytest
# from the repository root.
# ---------------------------------------------------------------------------


def test_migration_074_enforces_enums_and_separate_ledger():
    sql = Path("infra/nango/migrations/074_first_value_funnel.sql").read_text()
    assert "app.first_value_events" in sql
    assert "app.support_access_ledger" in sql
    assert "app.first_value_project_journeys" in sql
    # Every closed enum value is a DB-level CHECK.
    for stage in fvf.STAGES:
        assert stage in sql
    for outcome in fvf.OUTCOMES:
        assert outcome in sql
    for bucket in fvf.DURATION_BUCKETS:
        assert bucket in sql
    for kind in fvf.RECOVERY_KINDS:
        assert kind in sql
    for reason in fvf.ABANDON_REASONS:
        assert reason in sql
    # Append-only guard + verifiable purge flag.
    assert "append-only" in sql
    assert "app.funnel_purge" in sql
    # No raw tenant-identifying COLUMNS in the analytics sink. We check the executable
    # DDL (comment lines start with '--' and legitimately mention "invitation email").
    ddl = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    ).lower()
    for forbidden_column in ("email", "provider_payload", "raw_error", "prompt", "org_id text"):
        assert forbidden_column not in ddl
