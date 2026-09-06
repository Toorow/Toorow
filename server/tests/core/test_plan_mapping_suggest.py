"""Tests for Story 27.5 -- semi-auto plan<->actual mapping suggestions (Epic 27).

Offline (no DB, no warehouse): the PURE pairing pipeline (reuses 27.4's normalize_value /
similarity), the set_line_mappings payload builder, the AD-2 "no provider/dimension name"
grep, the write-frontier grep (no mutation, no set_line_mappings( call), and the
injection-driven orchestration with fake get_plan / campaign_spend_fn.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): a real plan/version/lines
fixtured by direct INSERT (calque sur test_mediaplan_mapping.py), proving the suggestions
are coherent AND that 27.5 writes NOTHING, and that the proposed payload is consumable by
the real set_line_mappings (integration, without 27.5 having written itself).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import plan_mapping_suggest as pms  # noqa: E402


def _line(line_key, label, *, start="2026-03-01", end="2026-03-31", budget="1000.00"):
    return {
        "line_key": line_key,
        "label": label,
        "channel": "Social",
        "start_date": start,
        "end_date": end,
        "budget": budget,
    }


# ===========================================================================
# Offline -- suggestion (PURE, no DB nor warehouse)
# ===========================================================================


def test_exact_match_scores_one():
    """Sec.1: label == campaign_ref -> method='exact', score=1.0, right line_key."""
    lines = [_line("l1", "Summer Sale")]
    actuals = {"meta-ads": ["Summer Sale"]}
    out = pms.suggest_line_mappings(lines, actuals)
    assert len(out) == 1
    s = out[0]
    assert s.method == "exact"
    assert s.score == 1.0
    assert s.line_key == "l1"
    assert s.connector == "meta-ads"
    assert s.campaign_ref == "Summer Sale"


def test_normalized_match_scores_one():
    """Sec.2: case/separators/tag/date differ, normalized form identical -> 'normalized'."""
    lines = [_line("l1", "[FR] Summer-Sale 2026")]
    actuals = {"google-ads": ["summer sale"]}
    out = pms.suggest_line_mappings(lines, actuals)
    assert len(out) == 1
    assert out[0].method == "normalized"
    assert out[0].score == 1.0


def test_similarity_match_uses_difflib_ratio():
    """Sec.3: above the threshold -> method='similarity', score == difflib ratio."""
    from core.dimension_conformance import normalize_value, similarity

    lines = [_line("l1", "Summer Sale Campaign")]
    actuals = {"google-ads": ["Summer Sale Campaigns"]}
    out = pms.suggest_line_mappings(lines, actuals, similarity_threshold=0.5)
    assert len(out) == 1
    assert out[0].method == "similarity"
    expected = similarity(
        normalize_value("Summer Sale Campaign"),
        normalize_value("Summer Sale Campaigns"),
    )
    assert out[0].score == expected


def test_below_threshold_no_suggestion():
    """Sec.4: below the threshold -> NO suggestion (honest)."""
    lines = [_line("l1", "Winter Coats")]
    actuals = {"meta-ads": ["Summer Sale"]}
    out = pms.suggest_line_mappings(lines, actuals)
    assert out == []


def test_n_to_m_pairings():
    """Sec.5: one line -> 2 campaigns of 2 connectors; one campaign -> 2 lines."""
    lines = [_line("l1", "Summer Sale"), _line("l2", "Summer Sale")]
    actuals = {"meta-ads": ["Summer Sale"], "google-ads": ["Summer Sale"]}
    out = pms.suggest_line_mappings(lines, actuals)
    # 2 lines x 2 campaigns all exact -> 4 suggestions.
    assert len(out) == 4
    l1 = {(s.connector, s.campaign_ref) for s in out if s.line_key == "l1"}
    assert l1 == {("meta-ads", "Summer Sale"), ("google-ads", "Summer Sale")}
    meta_lines = {s.line_key for s in out if s.connector == "meta-ads"}
    assert meta_lines == {"l1", "l2"}


def test_plan_labels_never_pair_with_each_other():
    """Sec.6: two near plan labels do NOT produce an inter-plan suggestion.

    The plan side and the actual side are disjoint sets (cross-connector rule of 27.4 is
    structural here) -- with no actuals, nothing is emitted even for near-identical labels.
    """
    lines = [_line("l1", "Summer Sale"), _line("l2", "Summer Sales")]
    out = pms.suggest_line_mappings(lines, {})
    assert out == []


def test_determinism():
    """Sec.7: two successive calls -> identical output (stable sort)."""
    lines = [_line("l2", "Summer Sale"), _line("l1", "Winter Sale")]
    actuals = {"meta-ads": ["Winter Sale", "Summer Sale"], "google-ads": ["Summer Sale"]}
    a = pms.suggest_line_mappings(lines, actuals)
    b = pms.suggest_line_mappings(lines, actuals)
    assert a == b


def test_empty_label_falls_back_to_line_key():
    """Sec.8: empty label -> the line_key is matched instead (documented)."""
    lines = [
        {
            "line_key": "camp-42",
            "label": "",
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
            "budget": "10.00",
        }
    ]
    actuals = {"meta-ads": ["camp-42"]}
    out = pms.suggest_line_mappings(lines, actuals)
    assert len(out) == 1
    assert out[0].label == "camp-42"
    assert out[0].method == "exact"


def test_threshold_is_parametrable():
    """Sec.9: similarity_threshold filters in both directions around a real score.

    "Brand Awarness" vs "Brand Awareness" scores ~0.9655: ABOVE the 0.88 default
    (suggested), below a strict 0.99 (filtered) -- the threshold is parametrable.
    """
    lines = [_line("l1", "Brand Awareness")]
    actuals = {"meta-ads": ["Brand Awarness"]}  # one letter missing
    strict = pms.suggest_line_mappings(lines, actuals, similarity_threshold=0.99)
    default = pms.suggest_line_mappings(lines, actuals)
    lowered = pms.suggest_line_mappings(lines, actuals, similarity_threshold=0.5)
    assert strict == []
    assert len(default) == 1
    assert default[0].method == "similarity"
    assert len(lowered) == 1
    assert lowered[0].method == "similarity"


def test_duplicate_campaign_refs_deduped():
    """A connector listing a campaign twice yields one suggestion, not two."""
    lines = [_line("l1", "Summer Sale")]
    actuals = {"meta-ads": ["Summer Sale", "Summer Sale"]}
    out = pms.suggest_line_mappings(lines, actuals)
    assert len(out) == 1


# ===========================================================================
# Offline -- payload builder (PURE)
# ===========================================================================


def test_payload_groups_by_line_key_without_weight():
    """Sec.10: groups by line_key, entries WITHOUT split_weight.

    STORY 61.3 ADDED TWO KEYS, and it is the whole point of that story: until it,
    the entry was `{connector, campaign_ref}` and nothing else, so the level died
    at the exact moment a person validated the suggestion. `set_line_mappings`
    also replaces a line's WHOLE set, so a level that did not travel here would be
    erased again by the next write on the same line.
    """
    lines = [_line("l1", "Summer Sale")]
    actuals = {"meta-ads": ["Summer Sale"], "google-ads": ["Summer Sale"]}
    sugs = pms.suggest_line_mappings(lines, actuals)
    payload = pms.propose_set_line_mappings_payload(sugs)
    assert set(payload) == {"l1"}
    for entry in payload["l1"]:
        assert set(entry) == {"connector", "campaign_ref", "match_method", "match_score"}
        assert "split_weight" not in entry
        assert entry["match_method"] == "exact"
        assert entry["match_score"] == 1.0
    # Deterministic entry order (sorted by connector, campaign_ref).
    assert [e["connector"] for e in payload["l1"]] == ["google-ads", "meta-ads"]


def test_the_payload_carries_the_level_of_each_pair_and_not_of_the_line():
    """Story 61.3: two campaigns on one line can have been paired differently.

    A payload that carried one level per line would have to pick one of them, and
    the one it picked would be a fabrication for the other.
    """
    lines = [_line("l1", "Summer Sale")]
    actuals = {"meta-ads": ["Summer Sale", "Summer Sales"]}
    sugs = pms.suggest_line_mappings(lines, actuals, similarity_threshold=0.5)
    payload = pms.propose_set_line_mappings_payload(sugs)

    by_ref = {entry["campaign_ref"]: entry for entry in payload["l1"]}
    assert by_ref["Summer Sale"]["match_method"] == "exact"
    assert by_ref["Summer Sales"]["match_method"] == "similarity"
    # And the similarity carries the ratio it was judged on, not a rounded 1.0.
    assert 0.5 <= by_ref["Summer Sales"]["match_score"] < 1.0


def test_payload_with_default_weights_annotation():
    """Sec.11: include_default_weights=True -> proposed_split_weight, Sum=1.0 Decimal."""
    # Two lines share the SAME campaign -> each gets 0.5 (compute_default_splits).
    lines = [_line("l1", "Summer Sale"), _line("l2", "Summer Sale")]
    actuals = {"meta-ads": ["Summer Sale"]}
    sugs = pms.suggest_line_mappings(lines, actuals)
    payload = pms.propose_set_line_mappings_payload(sugs, include_default_weights=True)
    weights = []
    for line_key in ("l1", "l2"):
        entry = payload[line_key][0]
        assert "proposed_split_weight" in entry
        assert "split_weight" not in entry  # indicative field is distinct
        weights.append(Decimal(entry["proposed_split_weight"]))
    assert sum(weights) == Decimal("1.000000")


def test_payload_consumable_shape_matches_set_line_mappings_entries():
    """The payload entries are exactly the {connector, campaign_ref[, split_weight]} dicts
    set_line_mappings iterates (no extra required key)."""
    lines = [_line("l1", "Summer Sale")]
    sugs = pms.suggest_line_mappings(lines, {"meta-ads": ["Summer Sale"]})
    payload = pms.propose_set_line_mappings_payload(sugs)
    entry = payload["l1"][0]
    assert entry["connector"] == "meta-ads"
    assert entry["campaign_ref"] == "Summer Sale"


# ===========================================================================
# Story 61.3 -- the connector scope, and what an empty answer has to say
# ===========================================================================


def test_the_connector_scope_keeps_only_the_candidates_that_surface_can_show(monkeypatch):
    """Arbitrage A7: the `Placements` tab reaches ONE connector, so this does too.

    Without it the sweep proposes matches on connectors that surface cannot draw,
    and a person is offered a decision about something they have no way to look
    at. The PURE `suggest_line_mappings` is untouched -- it stays a function of
    what it is given.
    """
    plan = {"id": "plan-1", "project_id": "proj-1", "lines": [_line("l1", "Summer Sale")]}
    _patch_get_plan(monkeypatch, plan)

    everything = {"meta-ads": ["Summer Sale"], "google-ads": ["Summer Sale"]}
    unscoped = pms.suggest_line_mappings_for_plan("plan-1", actuals_by_connector=everything)
    scoped = pms.suggest_line_mappings_for_plan(
        "plan-1", connector="google-ads", actuals_by_connector=everything
    )

    assert {s["connector"] for s in unscoped["suggestions"]} == {"meta-ads", "google-ads"}
    assert {s["connector"] for s in scoped["suggestions"]} == {"google-ads"}
    assert scoped["connector"] == "google-ads"
    # And nothing of the other connector survives into the entries either.
    assert all(
        entry["connector"] == "google-ads"
        for entries in scoped["payload_by_line_key"].values()
        for entry in entries
    )


def test_a_scope_with_no_campaign_says_so_instead_of_answering_silence(monkeypatch):
    """"This connector reported nothing" and "we did not look at it" are not the same."""
    plan = {"id": "plan-1", "project_id": "proj-1", "lines": [_line("l1", "Summer Sale")]}
    _patch_get_plan(monkeypatch, plan)

    out = pms.suggest_line_mappings_for_plan(
        "plan-1", connector="google-ads", actuals_by_connector={"meta-ads": ["Summer Sale"]}
    )

    assert out["suggestions"] == []
    assert any("requested connector" in note for note in out["notes"])


def test_an_empty_answer_says_over_what_it_looked(monkeypatch):
    """A measurement that cannot state its threshold and its window is an opinion."""
    plan = {
        "id": "plan-1",
        "project_id": "proj-1",
        "lines": [_line("l1", "Winter Coats", start="2026-03-01", end="2026-03-31")],
    }
    _patch_get_plan(monkeypatch, plan)

    out = pms.suggest_line_mappings_for_plan(
        "plan-1", connector="meta-ads", actuals_by_connector={"meta-ads": ["Summer Sale"]}
    )

    assert out["suggestions"] == []
    assert out["similarity_threshold"] == pms.DEFAULT_SIMILARITY_THRESHOLD
    assert out["window"] == {"start": "2026-03-01", "end": "2026-03-31"}
    assert out["project_id"] == "proj-1"


def test_the_scoped_read_still_writes_nothing(monkeypatch):
    """The module gained a parameter and a payload key, not a write.

    `test_write_frontier_no_mutation` greps the source; this one drives the
    orchestration with a connector scope and proves no statement was executed on
    the connection it opened.
    """
    plan = {"id": "plan-1", "project_id": "proj-1", "lines": [_line("l1", "Summer Sale")]}
    _patch_get_plan(monkeypatch, plan)

    out = pms.suggest_line_mappings_for_plan(
        "plan-1", connector="meta-ads", actuals_by_connector={"meta-ads": ["Summer Sale"]}
    )

    assert len(out["suggestions"]) == 1
    # `_FakeConn.cursor()` hands back a cursor with no `execute` at all, so any
    # statement issued here would have raised instead of passing quietly.
    assert not hasattr(_FakeCursor, "execute")


# ===========================================================================
# Offline -- write-frontier & AD-2 greps
# ===========================================================================

_MODULE_SRC = Path(pms.__file__).read_text(encoding="utf-8")
_ALIGN_SRC = (Path(pms.__file__).parent / "plan_actual_alignment.py").read_text(encoding="utf-8")


def test_write_frontier_no_mutation():
    """Sec.12: the module never calls set_line_mappings( and emits no INSERT/UPDATE/DELETE."""
    assert "set_line_mappings(" not in _MODULE_SRC
    for token in ("INSERT ", "UPDATE ", "DELETE ", ".execute("):
        assert token not in _MODULE_SRC, f"unexpected mutation token: {token!r}"


def test_align_write_frontier_no_mutation():
    """F-2: plan_actual_alignment is READ-ONLY too -- no mutation, no set_line_mappings( call.

    Symmetric to test_write_frontier_no_mutation. Bans MUTATION tokens only: the alignment
    module legitimately does a READ ``.execute("SELECT org_id FROM app.projects ...")`` in
    _project_org_id, so ``.execute(`` / ``SELECT`` are NOT banned here (they are reads);
    only INSERT INTO / UPDATE / DELETE FROM / set_line_mappings( are.
    """
    assert "set_line_mappings(" not in _ALIGN_SRC
    for token in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert token not in _ALIGN_SRC, f"unexpected mutation token in alignment: {token!r}"


def test_ad2_no_provider_or_dimension_name():
    """Sec.25: zero connector/dimension name in EITHER module (grep).

    The plan sentinel is a documented reserved marker, not a provider.
    """
    banned = ("meta-ads", "google-ads", "tiktok", "linkedin", "klaviyo", "pinterest")
    for src in (_MODULE_SRC, _ALIGN_SRC):
        low = src.lower()
        for name in banned:
            assert name not in low, f"provider name leaked into module: {name!r}"


# ===========================================================================
# Offline -- orchestration with injection (fakes, no DB/warehouse)
# ===========================================================================


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor()


def _patch_get_plan(monkeypatch, plan):
    """Patch mediaplan_store.get_plan AND get_connection so the orchestrator reads *plan*."""
    import core.mediaplan_store as store

    monkeypatch.setattr(store, "get_plan", lambda conn, *, plan_id: plan)

    import core.db as db

    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())


def test_orchestration_with_explicit_actuals(monkeypatch):
    """Sec.20: explicit actuals_by_connector -> suggestions + payload, no warehouse read."""
    plan = {
        "id": "plan-1",
        "project_id": "proj-1",
        "lines": [_line("l1", "Summer Sale")],
    }
    _patch_get_plan(monkeypatch, plan)

    called = {"warehouse": False}

    def _spend_fn(*a):
        called["warehouse"] = True
        return []

    out = pms.suggest_line_mappings_for_plan(
        "plan-1",
        actuals_by_connector={"meta-ads": ["Summer Sale"]},
        campaign_spend_fn=_spend_fn,
    )
    assert called["warehouse"] is False
    assert len(out["suggestions"]) == 1
    assert out["window"] == {"start": "2026-03-01", "end": "2026-03-31"}
    assert "l1" in out["payload_by_line_key"]


def test_orchestration_reads_warehouse_when_no_actuals(monkeypatch):
    """Sec.21: no actuals -> reads via campaign_spend_fn on the plan window envelope."""
    plan = {
        "id": "plan-1",
        "project_id": "proj-1",
        "lines": [
            _line("l1", "Summer Sale", start="2026-03-01", end="2026-03-15"),
            _line("l2", "Winter Sale", start="2026-03-10", end="2026-03-31"),
        ],
    }
    _patch_get_plan(monkeypatch, plan)

    captured = {}

    def _spend_fn(project_id, start, end):
        captured["args"] = (project_id, start, end)
        return [{"connector": "meta-ads", "campaign_ref": "Summer Sale", "spend": 10.0}]

    out = pms.suggest_line_mappings_for_plan("plan-1", campaign_spend_fn=_spend_fn)
    # Window envelope = [min(start), max(end)].
    assert captured["args"] == ("proj-1", "2026-03-01", "2026-03-31")
    assert len(out["suggestions"]) == 1
    assert out["suggestions"][0]["line_key"] == "l1"


def test_orchestration_warehouse_unavailable_propagates(monkeypatch):
    """Sec.22: campaign_spend_fn raising WarehouseUnavailable -> propagates (no silent [])."""
    from core.warehouse import WarehouseUnavailable

    plan = {
        "id": "plan-1",
        "project_id": "proj-1",
        "lines": [_line("l1", "Summer Sale")],
    }
    _patch_get_plan(monkeypatch, plan)

    def _spend_fn(*a):
        raise WarehouseUnavailable("down")

    with pytest.raises(WarehouseUnavailable):
        pms.suggest_line_mappings_for_plan("plan-1", campaign_spend_fn=_spend_fn)


def test_orchestration_unknown_plan_returns_empty_with_note(monkeypatch):
    """Sec.23: unknown plan / inactive version -> empty suggestions + honest note."""
    _patch_get_plan(monkeypatch, None)
    out = pms.suggest_line_mappings_for_plan("nope")
    assert out["suggestions"] == []
    assert out["window"] is None
    assert out["notes"]


def test_orchestration_inactive_version_no_lines(monkeypatch):
    """A plan with an inactive version (empty lines) -> empty suggestions + note, no crash."""
    plan = {"id": "plan-1", "project_id": "proj-1", "lines": []}
    _patch_get_plan(monkeypatch, plan)
    out = pms.suggest_line_mappings_for_plan("plan-1")
    assert out["suggestions"] == []
    assert out["notes"]


# ===========================================================================
# Live-Postgres (opt-in) -- calque sur test_mediaplan_mapping.py
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


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


def _seed_project(conn) -> str:
    project_id = f"proj-27-5-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, %s, %s, 'active', 'system', 'org_test_fixture')
            """,
            (project_id, "Bridge Test", project_id),
        )
    conn.commit()
    return project_id


def _drop_project(conn, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.media_plan_versions DISABLE TRIGGER USER")
        # Same fixture-only bypass as test_plan_actual_alignment._drop (the file
        # this teardown was copied from BEFORE f1112fda repaired it there): the
        # guard trigger (rightly) refuses deleting a published version's
        # allocations, and this teardown erases the whole fixture project.
        cur.execute("ALTER TABLE app.plan_allocation_daily DISABLE TRIGGER USER")
        # The audit log is append-only (FR12) and its guard blocks the DELETE
        # below -- a failure the old `finally:` masked behind
        # InFailedSqlTransaction. Same fixture-only bypass as the sibling file.
        cur.execute("ALTER TABLE app.audit_log DISABLE TRIGGER USER")
        try:  # noqa: SIM105 -- the except ROLLS BACK, see below
            cur.execute(
                "DELETE FROM app.plan_line_mappings WHERE plan_id IN "
                "(SELECT id FROM app.media_plans WHERE project_id = %s)",
                (project_id,),
            )
            # mediaplan_store materialises a daily allocation per line
            # (mediaplan_store.py:547, migration 040 FK ON DELETE RESTRICT), so
            # the allocations go before the lines they reference. Same
            # pre-existing red as the sibling file's, proven on HEAD bb5b0038
            # (2026-08-18): this teardown, not the read under test.
            cur.execute(
                """
                DELETE FROM app.plan_allocation_daily WHERE line_id IN (
                    SELECT l.id FROM app.media_plan_lines l
                    JOIN app.media_plan_versions v ON v.id = l.version_id
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
            # The governed mutations of this fixture (publish_version,
            # set_line_mappings) each wrote an app.operations row holding its
            # audit event in ON DELETE RESTRICT (060:121), and operations and
            # operation_outbox reference EACH OTHER -- the cycle is broken by
            # nulling the outbox pointer first. Without this sweep the audit
            # delete below is refused, which the old `finally:` masked.
            cur.execute(
                """
                UPDATE app.operations SET outbox_event_id = NULL
                WHERE audit_event_id IN
                    (SELECT id FROM app.audit_log WHERE identity IN ('tester', 'system'))
                """
            )
            cur.execute(
                """
                DELETE FROM app.operation_outbox ob USING app.operations o
                WHERE ob.operation_id = o.id AND o.audit_event_id IN
                    (SELECT id FROM app.audit_log WHERE identity IN ('tester', 'system'))
                """
            )
            cur.execute(
                """
                DELETE FROM app.operations WHERE audit_event_id IN
                    (SELECT id FROM app.audit_log WHERE identity IN ('tester', 'system'))
                """
            )
            cur.execute("DELETE FROM app.audit_log WHERE identity IN ('tester', 'system')")
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
        except Exception:
            # A failed statement leaves this transaction ABORTED: the ENABLE
            # statements a `finally` would issue are refused (masking the real
            # failure behind InFailedSqlTransaction), the DISABLEs roll back
            # with the transaction anyway, but the ACCESS EXCLUSIVE locks they
            # took are held until rollback -- and an open aborted connection
            # blocks every other teardown on the cluster. Roll back, THEN let
            # the failure speak (test_plan_actual_alignment._drop, same shape).
            conn.rollback()
            raise
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cur.execute("ALTER TABLE app.audit_log ENABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.plan_allocation_daily ENABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.media_plan_versions ENABLE TRIGGER USER")
    conn.commit()


_LINES = [
    {
        "line_key": "social/summer",
        "label": "Summer Sale",
        "channel": "Social",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "10000.00",
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
def test_live_suggestions_write_nothing(monkeypatch):
    """Sec.26: LIVE suggestions coherent AND no plan_line_mappings row created."""
    import core.db as db

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)

        # Route the module's get_connection to our test connection.
        import contextlib

        @contextlib.contextmanager
        def _fake_conn():
            yield conn

        monkeypatch.setattr(db, "get_connection", _fake_conn)

        out = pms.suggest_line_mappings_for_plan(
            plan["id"], actuals_by_connector={"meta-ads": ["Summer Sale"]}
        )
        assert len(out["suggestions"]) == 1
        assert out["suggestions"][0]["method"] == "exact"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.plan_line_mappings WHERE plan_id = %s",
                (plan["id"],),
            )
            assert int(cur.fetchone()[0]) == 0  # 27.5 wrote nothing
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_live_payload_consumable_by_set_line_mappings():
    """Sec.27: the proposed payload, passed to the REAL set_line_mappings, creates the
    mapping with Sum(split_weight) == 1.0 (proof the format is consumable)."""
    from core.mediaplan_mapping import list_mappings, set_line_mappings

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = _make_published_plan(conn, project_id)
        sugs = pms.suggest_line_mappings([dict(_LINES[0])], {"meta-ads": ["Summer Sale"]})
        payload = pms.propose_set_line_mappings_payload(sugs)
        for line_key, entries in payload.items():
            set_line_mappings(
                conn, plan_id=plan["id"], line_key=line_key, entries=entries, actor="tester"
            )
        conn.commit()

        mappings = list_mappings(conn, plan_id=plan["id"])
        total = Decimal("0")
        for line in mappings["lines"]:
            for entry in line["mappings"]:
                total += Decimal(entry["split_weight"])
        # One campaign, one line -> the single active weight is exactly 1.0.
        assert total == Decimal("1.000000")
    finally:
        _drop_project(conn, project_id)
        conn.close()
