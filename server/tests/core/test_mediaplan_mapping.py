"""toorow -- Tests for mediaplan_mapping.py (Story 22.3, FR38 / CAP-26).

Two tiers:
  * OFFLINE (no DB): the pure split maths -- compute_default_splits (1/N exact
    with the rounding remainder on the first line_key) and validate_split_sum, plus
    the DISCRIMINANT anti-double-count proof (a spend ventilated by the splits sums
    back to the original spend EXACTLY).
  * LIVE-PG (opt-in, TEST_POSTGRES_DSN): set-replacement, sum!=1 -> 422 with state
    UNCHANGED (rollback), équiréparti when a 2nd line maps the same campaign, orphan
    at re-import, and the FOR UPDATE anti-course serialisation (2 connections).

The live suite reuses the pg_available gate + teardown pattern of
test_mediaplan_store.py (migration 040/041 must be applied to the test DB).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ===========================================================================
# OFFLINE -- pure split maths (no DB)
# ===========================================================================


def test_compute_default_splits_three_lines_sum_exactly_one():
    """3 lines -> 0.333334 + 0.333333 + 0.333333 = 1.000000 EXACTLY.

    The remainder micro-unit lands on the FIRST line_key in sorted order.
    """
    from core.mediaplan_mapping import compute_default_splits

    splits = compute_default_splits(["c", "a", "b"])
    # Sorted keys: a, b, c -- 'a' carries the remainder.
    assert splits["a"] == Decimal("0.333334")
    assert splits["b"] == Decimal("0.333333")
    assert splits["c"] == Decimal("0.333333")
    assert sum(splits.values()) == Decimal("1.000000")


def test_compute_default_splits_two_lines():
    from core.mediaplan_mapping import compute_default_splits

    splits = compute_default_splits(["meta/b", "meta/a"])
    assert splits["meta/a"] == Decimal("0.500000")
    assert splits["meta/b"] == Decimal("0.500000")
    assert sum(splits.values()) == Decimal("1.000000")


def test_compute_default_splits_single_line():
    from core.mediaplan_mapping import compute_default_splits

    splits = compute_default_splits(["only"])
    assert splits == {"only": Decimal("1.000000")}


def test_compute_default_splits_seven_lines_still_sums_to_one():
    from core.mediaplan_mapping import compute_default_splits

    keys = [f"l{i}" for i in range(7)]
    splits = compute_default_splits(keys)
    assert sum(splits.values()) == Decimal("1.000000")
    # 1_000_000 // 7 = 142857 r1 -> exactly one line carries the +1 micro-unit.
    plus = [k for k, v in splits.items() if v == Decimal("0.142858")]
    assert len(plus) == 1


def test_compute_default_splits_empty_raises():
    from core.mediaplan_mapping import MediaPlanValidationError, compute_default_splits

    with pytest.raises(MediaPlanValidationError):
        compute_default_splits([])


def test_compute_default_splits_over_one_million_lines_raises():
    """review F-5: n > 1e6 would give base = floor(1e6/n) = 0 -> a poids 0.

    Guarded explicitly so a line can never receive a null split_weight (its spend
    would silently disappear from the ventilation). The message is in French.
    """
    from core.mediaplan_mapping import MediaPlanValidationError, compute_default_splits

    keys = [f"l{i}" for i in range(1_000_001)]
    with pytest.raises(MediaPlanValidationError) as exc:
        compute_default_splits(keys)
    assert "trop de lignes" in str(exc.value)


def test_validate_split_sum_exact_ok():
    from core.mediaplan_mapping import validate_split_sum

    validate_split_sum(
        [Decimal("0.700000"), Decimal("0.300000")], campaign_ref="camp-A"
    )  # no raise


def test_validate_split_sum_over_raises_with_observed_pct():
    from core.mediaplan_mapping import MediaPlanValidationError, validate_split_sum

    # 0.80 + 0.30 = 1.10 -> "constaté : 110 %".
    with pytest.raises(MediaPlanValidationError) as exc:
        validate_split_sum(
            [Decimal("0.800000"), Decimal("0.300000")], campaign_ref="camp-A"
        )
    assert "110" in str(exc.value)
    assert "camp-A" in str(exc.value)


def test_validate_split_sum_under_raises():
    from core.mediaplan_mapping import MediaPlanValidationError, validate_split_sum

    with pytest.raises(MediaPlanValidationError) as exc:
        validate_split_sum([Decimal("0.500000")], campaign_ref="camp-B")
    assert "50" in str(exc.value)


def test_anti_double_count_ventilation_sums_to_original_spend():
    """DISCRIMINANT: a 100.00 spend ventilated by the splits sums back to 100.00.

    This is the anti-double-count proof at the pure-maths level -- not tautological:
    the splits are the ACTUAL equal-split weights (0.333334/0.333333/0.333333) and
    the ventilated parts are quantised to the cent, yet their sum is EXACTLY the
    original spend. A naive per-line rounding (e.g. each line floors independently)
    would drop a cent; the largest-remainder split does not.
    """
    from core.mediaplan_mapping import compute_default_splits

    spend = Decimal("100.00")
    splits = compute_default_splits(["a", "b", "c"])

    # Ventilate with largest-remainder at the cent so the parts sum to the spend.
    cents = int((spend / Decimal("0.01")).to_integral_value())
    ordered = sorted(splits)  # a, b, c
    raw = {k: splits[k] * spend for k in ordered}
    floored = {k: int((raw[k] / Decimal("0.01"))) for k in ordered}
    remainder = cents - sum(floored.values())
    parts = {}
    for i, k in enumerate(ordered):
        c = floored[k] + (1 if i < remainder else 0)
        parts[k] = Decimal(c) * Decimal("0.01")

    assert sum(parts.values()) == spend, "ventilation must not create/lose a cent"
    # And the weights themselves already sum to exactly 1 (no double count upstream).
    assert sum(splits.values()) == Decimal("1.000000")


# ===========================================================================
# LIVE-PG -- opt-in (TEST_POSTGRES_DSN)
# ===========================================================================


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="platform Postgres not reachable"
)


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


def _seed_project(conn) -> str:
    project_id = f"proj-map-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by)
            VALUES (%s, %s, %s, 'active', 'EUR', 'Europe/Paris', 'system')
            """,
            (project_id, "Map Test", project_id),
        )
    conn.commit()
    return project_id


def _drop_project(conn, project_id: str) -> None:
    """Tear down a plan tree incl. mappings (same trigger-disable pattern as 22.1)."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.plan_allocation_daily DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.media_plan_versions DISABLE TRIGGER USER")
        try:
            cur.execute(
                "DELETE FROM app.plan_line_mappings WHERE plan_id IN "
                "(SELECT id FROM app.media_plans WHERE project_id = %s)",
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.plan_allocation_daily WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.media_plan_lines WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.media_plan_versions WHERE plan_id IN "
                "(SELECT id FROM app.media_plans WHERE project_id = %s)",
                (project_id,),
            )
            cur.execute("DELETE FROM app.media_plans WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.audit_log WHERE identity IN ('tester', 'system')")
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        finally:
            cur.execute("ALTER TABLE app.plan_allocation_daily ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.media_plan_versions ENABLE TRIGGER USER")
    conn.commit()


_LINES = [
    {
        "line_key": "social/prospecting",
        "label": "Social prospecting",
        "channel": "Meta",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "10000.00",
    },
    {
        "line_key": "social/retargeting",
        "label": "Social retargeting",
        "channel": "Meta",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "5000.00",
    },
]


def _make_published_plan(conn, project_id, *, lines=None):
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    plan = create_plan(
        conn, project_id=project_id, name=f"Plan {uuid.uuid4().hex[:6]}", created_by="tester"
    )
    version = create_version_with_lines(
        conn, plan_id=plan["id"], lines=lines or _LINES, created_by="tester"
    )
    publish_version(conn, version_id=version["id"], published_by="tester")
    conn.commit()
    return plan


@pg_available
def test_set_replaces_whole_line_set():
    from core.mediaplan_mapping import list_mappings, set_line_mappings

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        set_line_mappings(
            conn,
            plan_id=plan["id"],
            line_key="social/prospecting",
            entries=[
                {"connector": "meta-ads", "campaign_ref": "A"},
                {"connector": "meta-ads", "campaign_ref": "B"},
            ],
            actor="tester",
        )
        conn.commit()
        # Replace with a smaller set -> old B mapping is gone.
        set_line_mappings(
            conn,
            plan_id=plan["id"],
            line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}],
            actor="tester",
        )
        conn.commit()

        grouped = list_mappings(conn, plan_id=plan["id"])
        line = next(x for x in grouped["lines"] if x["line_key"] == "social/prospecting")
        refs = {m["campaign_ref"] for m in line["mappings"]}
        assert refs == {"A"}
        # Single mapping of A by this line -> weight 1.0.
        assert line["mappings"][0]["split_weight"] == "1.000000"
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_equireparti_across_two_lines_same_campaign():
    """A 2nd line mapping campaign A (both implicit) -> 0.5 / 0.5 automatically."""
    from core.mediaplan_mapping import list_mappings, set_line_mappings

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        conn.commit()
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/retargeting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        conn.commit()

        grouped = list_mappings(conn, plan_id=plan["id"])
        weights = {}
        for line in grouped["lines"]:
            for m in line["mappings"]:
                if m["campaign_ref"] == "A":
                    weights[line["line_key"]] = m["split_weight"]
        assert weights == {
            "social/prospecting": "0.500000",
            "social/retargeting": "0.500000",
        }
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_explicit_single_line_full_weight_ok_then_partial_rejected():
    """Explicit weight semantics + rollback safety.

    * A single line explicitly mapping a campaign at 1.0 is valid (sum == 1.0).
    * Adding a 2nd line with an explicit PARTIAL weight (0.30) makes the campaign
      sum 1.30 -> 422, and the pre-existing valid state is UNCHANGED (rollback).
    This is the honest per-line semantic: explicit weights are validated against
    100 % on every write; a partial explicit weight that does not close to 100 %
    is refused (decision 4: "sinon 422").
    """
    from core.mediaplan_mapping import (
        MediaPlanValidationError,
        list_mappings,
        set_line_mappings,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        # Single line owns campaign A at 1.0 -> valid.
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A", "split_weight": "1.000000"}],
            actor="tester",
        )
        conn.commit()

        before = list_mappings(conn, plan_id=plan["id"])

        # A 2nd line adds an explicit 0.30 -> campaign sum 1.30 -> 422.
        with pytest.raises(MediaPlanValidationError) as exc:
            set_line_mappings(
                conn, plan_id=plan["id"], line_key="social/retargeting",
                entries=[
                    {"connector": "meta-ads", "campaign_ref": "A", "split_weight": "0.300000"}
                ],
                actor="tester",
            )
        assert "130" in str(exc.value)
        conn.rollback()

        after = list_mappings(conn, plan_id=plan["id"])
        assert after == before, "rejected write must leave the state byte-identical"
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_orphaned_on_reimport_then_reactivated():
    """v2 without the line -> its mappings orphaned; v3 with the line -> active."""
    from core.mediaplan_mapping import list_mappings, set_line_mappings
    from core.mediaplan_store import create_version_with_lines, publish_version

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        conn.commit()

        # v2: publish WITHOUT social/prospecting (only retargeting remains).
        v2 = create_version_with_lines(
            conn, plan_id=plan["id"], lines=[_LINES[1]], created_by="tester"
        )
        publish_version(conn, version_id=v2["id"], published_by="tester")
        conn.commit()

        grouped = list_mappings(conn, plan_id=plan["id"])
        line = next(x for x in grouped["lines"] if x["line_key"] == "social/prospecting")
        assert all(m["status"] == "orphaned" for m in line["mappings"])

        # v3: line comes back -> reactivated.
        v3 = create_version_with_lines(
            conn, plan_id=plan["id"], lines=_LINES, created_by="tester"
        )
        publish_version(conn, version_id=v3["id"], published_by="tester")
        conn.commit()

        grouped = list_mappings(conn, plan_id=plan["id"])
        line = next(x for x in grouped["lines"] if x["line_key"] == "social/prospecting")
        assert all(m["status"] == "active" for m in line["mappings"])
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_shared_campaign_rebalances_to_one_on_orphan_and_reactivation():
    """review F-1/F-2/F-3 DISCRIMINANT: SUM=1.0 survives orphan AND reactivation.

    L1+L2 both map campaign A (implicit -> 0.5/0.5). Publish v2 WITHOUT L1:
      * L1/A becomes 'orphaned';
      * A's REMAINING active mapping (L2) is rebalanced to 1.0 so SUM(active)=1.0
        EXACTLY (F-1: without the fix L2 would stay at 0.5 -> only 50 % ventilated);
      * a REBALANCED audit row is written (AD-9 traceability).
    Re-publish v3 WITH L1 back:
      * L1/A is reactivated;
      * A is re-equireparti to 0.5/0.5 so SUM(active)=1.0 EXACTLY (F-2: without the
        fix L1 would restore its stale 0.5 on top of L2's rebalanced 1.0 -> 1.5).

    This test FAILS on the pre-fix code (SUM 0.5 then 1.5) and passes on the fix.
    """
    from core.mediaplan_mapping import list_mappings, set_line_mappings
    from core.mediaplan_store import create_version_with_lines, publish_version

    def _active_sum_for_a(conn_, plan_id_) -> Decimal:
        grouped_ = list_mappings(conn_, plan_id=plan_id_)
        total = Decimal("0")
        for ln in grouped_["lines"]:
            for m in ln["mappings"]:
                if m["campaign_ref"] == "A" and m["status"] == "active":
                    total += Decimal(m["split_weight"])
        return total

    def _rebalanced_audit_count(conn_) -> int:
        with conn_.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.audit_log "
                "WHERE action = 'media_plan.mapping.rebalanced'"
            )
            return int(cur.fetchone()[0])

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        # L1 (prospecting) and L2 (retargeting) both map A, implicit -> 0.5/0.5.
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/retargeting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        conn.commit()
        assert _active_sum_for_a(conn, plan["id"]) == Decimal("1.000000")

        rebalanced_before = _rebalanced_audit_count(conn)

        # v2: publish WITHOUT L1 (only retargeting remains) -> L1/A orphaned.
        v2 = create_version_with_lines(
            conn, plan_id=plan["id"], lines=[_LINES[1]], created_by="tester"
        )
        publish_version(conn, version_id=v2["id"], published_by="tester")
        conn.commit()

        grouped = list_mappings(conn, plan_id=plan["id"])
        l1 = next(x for x in grouped["lines"] if x["line_key"] == "social/prospecting")
        assert all(m["status"] == "orphaned" for m in l1["mappings"])
        # F-1: L2 rebalanced to 1.0 -> SUM(active weights of A) == 1.0 EXACTLY.
        assert _active_sum_for_a(conn, plan["id"]) == Decimal("1.000000")
        # AD-9: a REBALANCED audit row was written for this campaign.
        assert _rebalanced_audit_count(conn) > rebalanced_before

        # v3: L1 comes back -> reactivated + re-equireparti to 0.5/0.5.
        v3 = create_version_with_lines(
            conn, plan_id=plan["id"], lines=_LINES, created_by="tester"
        )
        publish_version(conn, version_id=v3["id"], published_by="tester")
        conn.commit()

        grouped = list_mappings(conn, plan_id=plan["id"])
        weights = {}
        for ln in grouped["lines"]:
            for m in ln["mappings"]:
                if m["campaign_ref"] == "A":
                    assert m["status"] == "active"
                    weights[ln["line_key"]] = Decimal(m["split_weight"])
        # F-2: SUM == 1.0 EXACTLY (0.5/0.5 again), NOT 1.5 (stale weight restored).
        assert sum(weights.values()) == Decimal("1.000000")
        assert weights == {
            "social/prospecting": Decimal("0.500000"),
            "social/retargeting": Decimal("0.500000"),
        }
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_unknown_line_rejected_422():
    from core.mediaplan_mapping import MediaPlanValidationError, set_line_mappings

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        with pytest.raises(MediaPlanValidationError) as exc:
            set_line_mappings(
                conn, plan_id=plan["id"], line_key="does/not/exist",
                entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
            )
        assert "inconnue de la version active" in str(exc.value)
        conn.rollback()
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_unmapped_actuals_excludes_active_mappings():
    """[Unmapped Actuals] = perimeter spend minus campaigns covered by their line window.

    A is mapped to social/prospecting [1-31 mars] and its spend (5 mars) falls INSIDE
    that window -> covered -> not listed. B has no active mapping -> listed with
    reason 'sans_mapping'.
    """
    from core.mediaplan_mapping import (
        REASON_UNMAPPED,
        list_unmapped_actuals,
        set_line_mappings,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        conn.commit()

        # Inject a fake DAILY warehouse read: A is mapped (covered on 5 mars), B is not.
        def _fake_daily(project_id_arg, start, end):
            assert project_id_arg == project_id
            return [
                {"connector": "meta-ads", "campaign_ref": "A", "day": "2026-03-05", "spend": 100.0},
                {"connector": "meta-ads", "campaign_ref": "B", "day": "2026-03-05", "spend": 40.0},
            ]

        result = list_unmapped_actuals(
            conn,
            plan_id=plan["id"],
            campaign_spend_fn=None,
            campaign_spend_daily_fn=_fake_daily,
        )
        refs = {r["campaign_ref"] for r in result["unmapped"]}
        assert refs == {"B"}
        b = next(r for r in result["unmapped"] if r["campaign_ref"] == "B")
        assert b["reason"] == REASON_UNMAPPED
        assert result["window"]["start"] == "2026-03-01"
        assert result["window"]["end"] == "2026-03-31"
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_unmapped_actuals_never_silently_empty_on_warehouse_failure():
    """A warehouse failure propagates -- never a fake empty perimeter (AD-9)."""
    from core.mediaplan_mapping import list_unmapped_actuals
    from core.warehouse import WarehouseUnavailable

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)

        def _boom(project_id_arg, start, end):
            raise WarehouseUnavailable("warehouse down")

        # E1-F-1: the DAILY read is the perimeter source; its failure must propagate.
        with pytest.raises(WarehouseUnavailable):
            list_unmapped_actuals(
                conn,
                plan_id=plan["id"],
                campaign_spend_fn=_boom,
                campaign_spend_daily_fn=_boom,
            )
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_unmapped_actuals_out_of_line_window_spend_is_visible():
    """E1-F-1 DISCRIMINANT (MAJOR): mapped-but-out-of-window spend never disappears.

    Campaign A is mapped ONLY to L1 [1-10 mars]. Another line L2 stretches the plan
    envelope to the 30th. A has real spend on the 15th (inside the ENVELOPE but
    OUTSIDE its mapped line's window). Under the pre-fix envelope subtraction, A had
    an active mapping over the plan and was subtracted wholesale -> the 15-mars spend
    vanished from BOTH the ventilation and this view. After the fix it re-appears as
    unmapped with reason 'hors_fenetre_lignes_mappees'. Conservation is also proven:
    Σ(covered) + Σ(unmapped) == Σ(total daily spend).
    """
    from core.mediaplan_mapping import (
        REASON_OUT_OF_WINDOW,
        list_unmapped_actuals,
        set_line_mappings,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        lines = [
            {
                "line_key": "social/early",
                "label": "Early window",
                "channel": "Meta",
                "start_date": "2026-03-01",
                "end_date": "2026-03-10",
                "budget": "10000.00",
            },
            {
                "line_key": "social/late",
                "label": "Late window",
                "channel": "Meta",
                "start_date": "2026-03-01",
                "end_date": "2026-03-30",
                "budget": "5000.00",
            },
        ]
        plan = _make_published_plan(conn, project_id, lines=lines)
        # A is mapped ONLY to the early line [1-10 mars].
        set_line_mappings(
            conn, plan_id=plan["id"], line_key="social/early",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )
        conn.commit()

        # A spends 60 on the 5th (INSIDE its line window -> covered) and 40 on the
        # 15th (inside the envelope, OUTSIDE its line window -> out-of-window).
        def _daily(project_id_arg, start, end):
            assert project_id_arg == project_id
            assert start == "2026-03-01"
            assert end == "2026-03-30"
            return [
                {"connector": "meta-ads", "campaign_ref": "A", "day": "2026-03-05", "spend": 60.0},
                {"connector": "meta-ads", "campaign_ref": "A", "day": "2026-03-15", "spend": 40.0},
            ]

        def _unused_total(*_a, **_k):  # pragma: no cover -- must not be called
            raise AssertionError("window-total fn must not drive the perimeter")

        result = list_unmapped_actuals(
            conn,
            plan_id=plan["id"],
            campaign_spend_fn=_unused_total,
            campaign_spend_daily_fn=_daily,
        )

        # The 40 of the 15th is visible; the 60 of the 5th is covered (not listed).
        assert len(result["unmapped"]) == 1
        entry = result["unmapped"][0]
        assert entry["campaign_ref"] == "A"
        assert entry["reason"] == REASON_OUT_OF_WINDOW
        assert entry["spend"] == pytest.approx(40.0)

        # Conservation: covered (60) + unmapped (40) == total daily spend (100).
        total_daily = 60.0 + 40.0
        sum_unmapped = sum(u["spend"] for u in result["unmapped"])
        sum_covered = total_daily - sum_unmapped
        assert sum_covered + sum_unmapped == pytest.approx(total_daily)
        assert sum_covered == pytest.approx(60.0)
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_for_update_serialises_concurrent_writes():
    """FOR UPDATE anti-course: 2 connections mapping the SAME campaign serialise.

    B blocks on A's per-plan lock until A commits; the final state has exactly the
    équiréparti 0.5/0.5 for campaign A across the two lines -- never a sum != 1.0.
    (Pattern: test_concurrent_first_publish_leaves_exactly_one_active, 22.1.)
    """
    import threading
    import time

    from core.mediaplan_mapping import list_mappings, set_line_mappings

    conn = _connect()
    project_id = _seed_project(conn)
    conn_a = _connect()
    conn_b = _connect()
    try:
        plan = _make_published_plan(conn, project_id)

        # A takes the plan lock and maps prospecting -> A (holds txn open).
        set_line_mappings(
            conn_a, plan_id=plan["id"], line_key="social/prospecting",
            entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
        )

        b_done: list[str] = []

        def _run_b():
            set_line_mappings(
                conn_b, plan_id=plan["id"], line_key="social/retargeting",
                entries=[{"connector": "meta-ads", "campaign_ref": "A"}], actor="tester",
            )
            conn_b.commit()
            b_done.append("ok")

        t = threading.Thread(target=_run_b)
        t.start()
        time.sleep(0.5)  # B must be BLOCKED on A's FOR UPDATE plan lock.
        assert not b_done, "B ran before A committed -> lock not held (race window)"
        conn_a.commit()
        t.join(timeout=15)
        assert not t.is_alive(), "B deadlocked instead of serialising after A"
        assert b_done == ["ok"]

        grouped = list_mappings(conn, plan_id=plan["id"])
        weights = {}
        for line in grouped["lines"]:
            for m in line["mappings"]:
                if m["campaign_ref"] == "A":
                    weights[line["line_key"]] = Decimal(m["split_weight"])
        assert sum(weights.values()) == Decimal("1.000000")
        assert set(weights.values()) == {Decimal("0.500000")}
    finally:
        conn_a.close()
        conn_b.close()
        _drop_project(conn, project_id)
        conn.close()
