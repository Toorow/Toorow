"""Story 12.11 bounded synchronize / reload / reprocess tests (offline).

Fully offline (no live Postgres): the pure scope/interval/retention helpers are
exercised directly, the DB is a MagicMock cursor, and the AD-27 substrate seams
(``operations_mcp._insert_preparation`` / ``_load_preparation`` /
``_mark_preparation_confirmed`` / ``_load_target``, ``operations.execute_operation``,
``datastream_publication.create_execution``) are monkeypatched. Mirrors
test_epic36_operations_mcp.py / test_epic36_datastream_recovery.py.

Coverage (Story 12.11 AC1-AC4):
  (AC1) each verb states interval/partition + versions + expected impact +
        confirmation; actor/reason/idempotency/trace/source-version/target-candidate
        are recorded on the durable operation.
  (AC2) a reload half-open [from, to) range supersedes ONLY its scope: adjacent
        intervals + unrelated dimensions + the prior published version outside the
        scope are untouched (proven via window tiling + candidate projection plan).
  (AC3) reprocess reapplies the chosen mapping WITHOUT a provider call (asserted: no
        provider/queue call); missing retention / incompatible schema block with an
        actionable reason.
  (AC4) an invalid candidate stays UNPUBLISHED: the dispatch NEVER calls
        commit_publication / mutates current_published_execution_id.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
from core import bounded_recovery as br
from core import pull_window

# ---------------------------------------------------------------------------
# Fake connection helper (MagicMock cursor context manager).
# ---------------------------------------------------------------------------


def _fake_conn(fetch_map=None):
    """Return a MagicMock connection whose cursor returns queued fetchone rows.

    ``fetch_map`` is unused here; individual tests monkeypatch the module I/O
    helpers directly, so the connection is a passthrough MagicMock.
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    return conn, cur


def test_server_quota_estimate_uses_persisted_plan_and_reload_windows():
    conn, cur = _fake_conn()
    cur.fetchone.return_value = (
        {"geographic": {"impact": {"quota_cost": {"read_points": 7}}}},
    )
    points = br._server_estimated_points(
        conn,
        datastream_id="ds-1",
        project_id="proj-1",
        kind=br.KIND_RELOAD,
        interval={"windows": [{"date_from": "a"}, {"date_from": "b"}]},
    )
    assert points == 14
    assert cur.execute.call_args.args[1] == ("ds-1", "proj-1", "ds-1", "proj-1")


def test_server_quota_estimate_blocks_missing_evidence_and_reprocess_is_exact_zero():
    conn, cur = _fake_conn()
    cur.fetchone.return_value = ({"geographic": {"impact": {}}},)
    with pytest.raises(br.BoundedRecoveryError) as exc:
        br._server_estimated_points(
            conn,
            datastream_id="ds-1",
            project_id="proj-1",
            kind=br.KIND_SYNCHRONIZE,
            interval={"from": "2026-07-20", "to": "2026-07-22"},
        )
    assert exc.value.code == "quota_estimate_unavailable"
    cur.reset_mock()
    assert br._server_estimated_points(
        conn,
        datastream_id="ds-1",
        project_id="proj-1",
        kind=br.KIND_REPROCESS,
        interval=None,
    ) == 0
    cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# (Pure) reload half-open scope normalization + windowing.
# ---------------------------------------------------------------------------


def test_reload_scope_is_half_open_and_tiles_into_inclusive_windows():
    # [2026-07-01, 2026-07-06) -> inclusive last day 07-05, one window 01..05.
    scope = br.normalize_reload_scope("2026-07-01", "2026-07-06")
    assert scope.date_from == "2026-07-01"
    assert scope.date_to_exclusive == "2026-07-06"
    assert scope.windows == ({"date_from": "2026-07-01", "date_to": "2026-07-05"},)


def test_reload_scope_splits_wide_range_into_31_day_windows():
    # A 40-day inclusive span (01-01 .. 02-09) splits into 31 + 9.
    scope = br.normalize_reload_scope("2026-01-01", "2026-02-10")  # exclusive 02-10
    assert scope.windows[0] == {"date_from": "2026-01-01", "date_to": "2026-01-31"}
    assert scope.windows[1] == {"date_from": "2026-02-01", "date_to": "2026-02-09"}
    # The windows cover EXACTLY [from, to_exclusive) and nothing beyond.
    assert scope.windows[-1]["date_to"] == "2026-02-09"  # 02-10 is NOT reloaded.


def test_reload_scope_rejects_empty_and_inverted_and_overwide_ranges():
    for bad in (
        ("2026-07-05", "2026-07-05"),  # empty (from == to_exclusive)
        ("2026-07-06", "2026-07-01"),  # inverted
        ("2026-01-01", "2027-01-01"),  # over-wide (> 31 days bounded)
        ("nope", "2026-07-06"),        # malformed
    ):
        with pytest.raises(br.BoundedRecoveryError) as exc:
            br.normalize_reload_scope(*bad)
        assert exc.value.code == "forbidden_interval"


def test_adjacent_reloads_tile_without_overlap():
    # AC2 isolation: [01, 06) and [06, 11) share NO day -- 06 belongs only to the
    # second reload (half-open), so an earlier reload never supersedes a later day.
    a = br.normalize_reload_scope("2026-07-01", "2026-07-06")
    b = br.normalize_reload_scope("2026-07-06", "2026-07-11")
    a_days = {w["date_to"] for w in a.windows} | {w["date_from"] for w in a.windows}
    b_days = {w["date_from"] for w in b.windows}
    assert "2026-07-06" in b_days
    assert "2026-07-06" not in a_days  # the boundary day is NOT in the first scope.


# ---------------------------------------------------------------------------
# (Pure) synchronize interval.
# ---------------------------------------------------------------------------


def test_synchronize_interval_ends_on_the_reference_day_and_uses_the_declaration():
    iv = br.synchronize_interval(date(2026, 7, 22), {"date_window_days": 3})
    assert iv == {"from": "2026-07-20", "to": "2026-07-22"}  # 3 days ending J-1.


def test_synchronize_obeys_the_retrieval_window_and_not_its_legacy_alias():
    """CORRIGÉ le 2026-08-12: this read `refetch_days` alone.

    A person who set thirty days on the Schedule panel and pressed
    `Synchronize now` collected three -- the legacy alias, which AI-46
    arbitrated away in the dispatcher and in three other doors. One resolver,
    one answer, whoever asks.
    """
    iv = br.synchronize_interval(
        date(2026, 7, 22), {"date_window_days": 30, "refetch_days": 3}
    )
    span = (date.fromisoformat(iv["to"]) - date.fromisoformat(iv["from"])).days + 1
    assert span == 30, "the window a person set is the window that is collected"


def test_synchronize_honours_the_extraction_offset_of_a_lagging_source():
    """AI-145: a source with a two-to-three day lag ends its window at J-3."""
    iv = br.synchronize_interval(
        date(2026, 7, 22), {"date_window_days": 7, "window_offset_days": 3}
    )
    assert iv == {"from": "2026-07-14", "to": "2026-07-20"}


def test_synchronize_falls_back_to_the_one_defensive_default():
    iv = br.synchronize_interval(date(2026, 7, 22), None)
    assert iv["to"] == "2026-07-22"
    span = (date.fromisoformat(iv["to"]) - date.fromisoformat(iv["from"])).days + 1
    assert span == pull_window.DEFENSIVE_WINDOW_DAYS


# ---------------------------------------------------------------------------
# (Pure) reprocess retention + schema compatibility.
# ---------------------------------------------------------------------------


def test_reprocess_retention_missing_blocks_with_actionable_reason():
    check = br.evaluate_reprocess_retention(
        retained_execution_id=None,
        retained_source_schema_hash=None,
        chosen_mapping_source_schema_hash="a" * 64,
    )
    assert check.available is False
    assert check.code == "retention_unavailable"
    assert "synchron" in check.reason.lower() or "recharg" in check.reason.lower()


def test_reprocess_incompatible_schema_blocks_with_actionable_reason():
    check = br.evaluate_reprocess_retention(
        retained_execution_id="dse_OLD",
        retained_source_schema_hash="a" * 64,
        chosen_mapping_source_schema_hash="b" * 64,  # differs -> incompatible.
    )
    assert check.available is False
    assert check.code == "incompatible_schema"
    assert check.retained_execution_id == "dse_OLD"


def test_reprocess_available_when_retained_and_schema_match():
    check = br.evaluate_reprocess_retention(
        retained_execution_id="dse_OLD",
        retained_source_schema_hash="c" * 64,
        chosen_mapping_source_schema_hash="c" * 64,
    )
    assert check.available is True
    assert check.code is None


def test_reprocess_unknown_retained_schema_blocks():
    check = br.evaluate_reprocess_retention(
        retained_execution_id="dse_OLD",
        retained_source_schema_hash=None,  # unknown shape -> cannot prove safe.
        chosen_mapping_source_schema_hash="c" * 64,
    )
    assert check.available is False
    assert check.code == "incompatible_schema"


# ---------------------------------------------------------------------------
# (Pure) impact statement per verb (AC1).
# ---------------------------------------------------------------------------


def test_impact_never_touches_published_pointer_and_is_candidate_only():
    for kind in br.BOUNDED_KINDS:
        impact = br.build_impact(kind, interval={"from": "a", "to": "b"},
                                 reload_scope=None, reprocess=None)
        assert impact["touches_published_pointer"] is False
        assert impact["candidate_only"] is True


def test_reprocess_impact_declares_no_provider_call():
    check = br.RetentionCheck(available=True, retained_execution_id="dse_OLD")
    impact = br.build_impact(br.KIND_REPROCESS, interval=None, reload_scope=None,
                             reprocess=check)
    assert impact["calls_provider"] is False
    assert impact["retained_execution_id"] == "dse_OLD"


# ---------------------------------------------------------------------------
# assemble_proposal -- composes the AD-27 substrate per verb.
# ---------------------------------------------------------------------------


def _patch_target(monkeypatch, **over):
    from core import operations_mcp

    base = {
        "datastream_id": "ds-1", "project_id": "proj-1", "org_id": "org-1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    }
    base.update(over)
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: base)
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate",
                        lambda p, n: {"can_proceed": True, "verdict": "ok",
                                      "estimated_points": n, "platform_known": True})
    monkeypatch.setattr(br, "_server_estimated_points", lambda *args, **kwargs: 5)
    return base


def test_assemble_synchronize_states_interval_and_versions(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_declared_window", lambda conn, ds: {"date_window_days": 3})
    monkeypatch.setattr(br, "_server_estimated_points", lambda *args, **kwargs: 5)
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_SYNCHRONIZE,
        actor="user-1", reason="late data", date_from=None, date_to_exclusive=None,
        partition=None, chosen_mapping_version_id=None,
        today=date(2026, 7, 23),
    )
    assert proposal.kind == "synchronize"
    assert proposal.interval == {"from": "2026-07-20", "to": "2026-07-22"}
    assert proposal.target_versions == {"plan_version_id": "p1",
                                        "mapping_version_id": "m1",
                                        "policy_version": "pol-1"}
    assert proposal.impact["touches_published_pointer"] is False
    assert proposal.rollback_ref == "dse_GOOD"
    assert proposal.reason == "late data"


def test_assemble_reload_carries_half_open_windows(monkeypatch):
    _patch_target(monkeypatch)
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_RELOAD,
        actor="u", reason=None, date_from="2026-07-01", date_to_exclusive="2026-07-06",
        partition=None, chosen_mapping_version_id=None,
    )
    assert proposal.kind == "reload"
    assert proposal.interval["from"] == "2026-07-01"
    assert proposal.interval["to"] == "2026-07-05"  # inclusive last day.
    assert proposal.interval["to_exclusive"] == "2026-07-06"
    assert proposal.interval["windows"] == [{"date_from": "2026-07-01",
                                             "date_to": "2026-07-05"}]
    assert proposal.impact["supersedes_only_scope"] is True


def test_assemble_reload_rejects_overwide_range(monkeypatch):
    _patch_target(monkeypatch)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.assemble_proposal(
            conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_RELOAD,
            actor="u", reason=None, date_from="2026-01-01",
            date_to_exclusive="2027-01-01", partition=None,
            chosen_mapping_version_id=None,
        )
    assert exc.value.code == "forbidden_interval"


def _patch_eligibility(monkeypatch, plan):
    """Substitute the ONE eligibility decision `assemble_proposal` now asks for.

    Amended 2026-08-17 (67-15b). This branch used to answer from
    `_load_retained_source` + `evaluate_reprocess_retention` -- a published
    execution's `source_schema_hash` against the chosen mapping's. That is the
    connector-landing form of the question, and it refused every real reprocess
    as `incompatible_schema` because a managed feed's mapping versions carry no
    such hash. Prepare now asks `datastream_reprocess`, which is what dispatch
    asks too: one question with one answer, instead of two halves that
    disagreed. The two pure functions it replaced are still exercised by their
    own tests below.
    """
    from core import datastream_reprocess as dr

    monkeypatch.setattr(dr, "evaluate_reprocess_plan", lambda conn, **kwargs: plan)


def _eligible_plan(**overrides):
    from core import datastream_reprocess as dr

    base = {
        "state": dr.STATE_AVAILABLE,
        "mode": "managed_feed",
        "datastream_id": "ds-1",
        "project_id": "proj-1",
        "org_id": "org-1",
        "plan_version_id": "p1",
        "from_mapping_version_id": "m1",
        "to_mapping_version_id": "m2",
        "artifact": dr.RetainedArtifact(
            raw_import_id="inbraw_1", content_hash="c" * 64,
            quarantine_uri="gs://q/1", filename="delivery.csv",
            published_relation="main.managed_feed_ds_1",
            published_ledger_id="mfl_1", published_execution_id="dse_OLD",
            superseded_relation="execution/dse_OLD/candidate/relation",
        ),
        "mapping_gate": dr.decide_mapping_gate(
            published_mapping_version_id="m1", chosen_mapping_version_id="m2", actor="u"
        ),
    }
    base.update(overrides)
    return dr.ReprocessPlan(**base)


def test_assemble_reprocess_blocks_when_nothing_is_retained(monkeypatch):
    """The refusal reaching the caller is the ENGINE's -- code and sentence alike."""
    from core import datastream_reprocess as dr

    _patch_target(monkeypatch)
    _patch_eligibility(monkeypatch, dr.ReprocessPlan(
        state=dr.REFUSAL_ARTIFACT_NOT_RETAINED, message=dr._ARTIFACT_NOT_RETAINED,
    ))
    conn, _ = _fake_conn()

    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.assemble_proposal(
            conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
            actor="u", reason=None, date_from=None, date_to_exclusive=None,
            partition=None, chosen_mapping_version_id="m2",
        )
    assert exc.value.code == "artifact_not_retained"
    # And it names the gesture, never the table that happened to be empty.
    assert "Re-import" in exc.value.message


def test_assemble_reprocess_blocks_when_the_source_would_be_called(monkeypatch):
    from core import datastream_reprocess as dr

    _patch_target(monkeypatch)
    _patch_eligibility(monkeypatch, dr.ReprocessPlan(
        state=dr.REFUSAL_SOURCE_WOULD_BE_CALLED, message=dr._SOURCE_WOULD_BE_CALLED,
    ))
    conn, _ = _fake_conn()

    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.assemble_proposal(
            conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
            actor="u", reason=None, date_from=None, date_to_exclusive=None,
            partition=None, chosen_mapping_version_id="m2",
        )
    assert exc.value.code == "source_would_be_called"
    assert "Day-by-day coverage" in exc.value.message


def test_assemble_reprocess_pins_the_chosen_mapping_and_states_its_gate(monkeypatch):
    _patch_target(monkeypatch)
    _patch_eligibility(monkeypatch, _eligible_plan())
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
        actor="u", reason=None, date_from=None, date_to_exclusive=None,
        partition=None, chosen_mapping_version_id="m2",
    )
    # The proposal pins the CHOSEN mapping (m2), not the live one (m1).
    assert proposal.target_versions["mapping_version_id"] == "m2"
    assert proposal.interval["calls_provider"] is False
    assert proposal.interval["retained_execution_id"] == "dse_OLD"
    assert proposal.interval["retained_raw_import_id"] == "inbraw_1"
    assert proposal.impact["calls_provider"] is False
    # A DIFFERENT mapping means a person confirms, and the proposal SAYS so --
    # the gate is the one thing about a reprocess that its scope cannot show.
    assert proposal.impact["mapping_gate"]["state"] == "passed"
    assert proposal.impact["mapping_gate"]["from_mapping_version_id"] == "m1"
    assert proposal.impact["supersedes_output_relation"] == (
        "execution/dse_OLD/candidate/relation"
    )


def test_assemble_reprocess_records_a_skipped_gate_rather_than_omitting_it(monkeypatch):
    """Same mapping in and out: nothing to decide, and the skip is still written."""
    from core import datastream_reprocess as dr

    _patch_target(monkeypatch)
    _patch_eligibility(monkeypatch, _eligible_plan(
        to_mapping_version_id="m1",
        mapping_gate=dr.decide_mapping_gate(
            published_mapping_version_id="m1", chosen_mapping_version_id="m1", actor="u"
        ),
    ))
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
        actor="u", reason=None, date_from=None, date_to_exclusive=None,
        partition=None, chosen_mapping_version_id="m1",
    )
    gate = proposal.impact["mapping_gate"]
    assert gate["state"] == "skipped"
    assert gate["reason"] == "mapping_unchanged"
    # Attributed and dated: "nobody looked" and "somebody established there was
    # nothing to look at" are different facts a week later.
    assert gate["decided_by"] == "u" and gate["decided_at"]


# ---------------------------------------------------------------------------
# prepare_bounded_recovery -- persists into the AD-27 store, no durable op.
# ---------------------------------------------------------------------------


def test_prepare_refuses_the_three_verbs_and_freezes_nothing(monkeypatch):
    """Story 63.7: a proposal is a promise that confirming it does something.

    None of the three verbs has an engine in this build -- no `enqueue`, no
    `advance_state`, no worker reading `recovery_kind` -- so the candidate they
    minted stayed in `created`, an ACTIVE state, and
    `uq_datastream_executions_active` then answered 409 to every later
    publication while `open_collection_run` returned `None` every following
    night. The refusal happens at prepare, before an immutable proposal is
    frozen and before a person reads a scope they cannot act on.
    """
    from core import operations_mcp, run_origins

    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_declared_window", lambda conn, ds: {"date_window_days": 3})
    monkeypatch.setattr(br, "_server_estimated_points", lambda *args, **kwargs: 5)
    inserted = MagicMock(side_effect=AssertionError("nothing may be frozen"))
    monkeypatch.setattr(operations_mcp, "_insert_preparation", inserted)
    conn, _ = _fake_conn()

    # AMENDED 2026-08-17 (67-15b): `Reprocess` is BUILT and no longer refuses
    # here -- `core.datastream_reprocess` replays the retained artifact through
    # the import path that lands, promotes and publishes. The two that remain are
    # RETIRED, not unbuilt, and that is a different sentence for a different
    # reason: the schedule and `Day-by-day coverage` already deliver them.
    #
    # The loop reads the REGISTRY rather than a hand-written pair, so the day a
    # third verb gains an engine this test follows instead of asserting a stale
    # list. `REFUSED_ORIGINS` is the same source `_refuse_without_engine` reads.
    refused_kinds = [
        kind for kind, origin in br._ORIGIN_BY_KIND.items()
        if not run_origins.has_engine(origin)
    ]
    assert br.KIND_REPROCESS not in refused_kinds, (
        "Reprocess has an engine since 67-15b; if it lost one, say why here"
    )
    for kind in refused_kinds:
        with pytest.raises(br.BoundedRecoveryError) as refused:
            br.prepare_bounded_recovery(
                conn, org_id="org-1", datastream_id="ds-1", kind=kind,
                actor="user-1", reason="late data", today=date(2026, 7, 23),
                date_from="2026-07-01", date_to_exclusive="2026-07-06",
            )
        assert refused.value.code == run_origins.NO_ENGINE
        # The sentence names the verb that was pressed and what did NOT happen.
        assert refused.value.message.startswith(
            run_origins.label_for(br._ORIGIN_BY_KIND[kind])
        )
        assert "Nothing was created." in refused.value.message
    inserted.assert_not_called()


# ---------------------------------------------------------------------------
# confirm_bounded_recovery -- routes EXACTLY ONE durable operation (AC1) and
# records actor/reason/idempotency/trace/source-version/target-candidate.
# ---------------------------------------------------------------------------


def _prep_row(**over):
    base = {
        "id": "prep_1", "org_id": "org-1", "project_id": "proj-1",
        "datastream_id": "ds-1", "kind": "synchronize", "actor": "user-1",
        "target_versions": {"plan_version_id": "p1", "mapping_version_id": "m1",
                            "policy_version": "pol-1"},
        "interval": {"from": "2026-07-20", "to": "2026-07-22"},
        "impact": {"reason": "late data"}, "quota": {"estimated_points": 5},
        "lock_ref": None, "rollback_ref": "dse_GOOD",
        "idempotency_key_hash": None, "state": "prepared", "operation_id": None,
        "expired": False,
    }
    base.update(over)
    return base


def _wire_confirm(monkeypatch, prep, target, exec_return):
    """Wire the confirm seams; pin the idempotency hash so the tamper guard passes."""
    from core import operations, operations_mcp
    from core.operations import _sha256

    monkeypatch.setattr(operations_mcp, "_load_preparation", lambda conn, pid: prep)
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: target)
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate",
                        lambda p, n: {"can_proceed": True, "verdict": "ok",
                                      "estimated_points": n, "platform_known": True})
    monkeypatch.setattr(br, "_server_estimated_points", lambda *args, **kwargs: 5)
    marked = {}
    monkeypatch.setattr(operations_mcp, "_mark_preparation_confirmed",
                        lambda conn, pid, op_id: marked.setdefault("op_id", op_id))

    monkeypatch.setattr(
        operations_mcp, "_load_operation_trace", lambda conn, op_id: "e" * 32
    )
    fp = operations_mcp._proposal_fingerprint(
        {
            "org_id": prep["org_id"],
            "project_id": prep["project_id"],
            "datastream_id": prep["datastream_id"],
            "actor": prep["actor"],
            "kind": prep["kind"],
            "interval": prep["interval"],
            "target_versions": prep["target_versions"],
        }
    )
    key = operations_mcp._recovery_idempotency_key(
        prep["org_id"], prep["datastream_id"], prep["kind"], fp
    )
    prep["idempotency_key_hash"] = _sha256(key)

    captured = {"calls": 0}

    def fake_execute(conn, spec, *, mutation):
        captured["calls"] += 1
        captured["spec"] = spec
        captured["mutation"] = mutation
        return exec_return

    monkeypatch.setattr(operations, "execute_operation", fake_execute)
    return captured, marked


def _live_target(**over):
    base = {
        "datastream_id": "ds-1", "project_id": "proj-1", "org_id": "org-1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    }
    base.update(over)
    return base


def _confirm(conn, **over):
    kwargs = {
        "preparation_id": "prep_1",
        "expected_org_id": "org-1",
        "expected_project_id": "proj-1",
        "expected_datastream_id": "ds-1",
        "actor": "user-1",
        "trace_id": "f" * 32,
    }
    kwargs.update(over)
    return br.confirm_bounded_recovery(conn, **kwargs)


@pytest.mark.parametrize(
    "override",
    [
        {"expected_org_id": "org-other"},
        {"expected_project_id": "proj-other"},
        {"expected_datastream_id": "ds-other"},
        {"actor": "user-other"},
    ],
)
def test_confirm_refuses_any_scope_or_actor_mismatch(monkeypatch, override):
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    captured, _ = _wire_confirm(monkeypatch, _prep_row(), _live_target(), op)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        _confirm(conn, **override)
    assert exc.value.code == "not_found"
    assert captured["calls"] == 0


def test_confirm_routes_exactly_one_operation_and_records_audit_fields(monkeypatch):
    from core import operations

    op = operations.OperationResult("op-1", "succeeded", {"kind": "synchronize"},
                                    "audit-1", "opout-1", False)
    captured, marked = _wire_confirm(monkeypatch, _prep_row(), _live_target(), op)
    conn, _ = _fake_conn()

    out = _confirm(conn, reason="late data")

    assert captured["calls"] == 1
    spec = captured["spec"]
    # AC1: actor / reason / idempotency / trace / source-version / target-candidate.
    assert spec.command_type == "datastream.bounded.synchronize"
    assert spec.actor == "user-1"
    assert spec.trace_id == "f" * 32
    assert spec.request_payload["reason"] == "late data"
    assert spec.request_payload["plan_version_id"] == "p1"
    assert spec.request_payload["mapping_version_id"] == "m1"
    assert spec.confirmation_mode == "human"
    assert spec.resource_path == (
        "organization:org-1", "project:proj-1", "flux:ds-1"
    )
    assert out["trace_id"] == "e" * 32
    assert out["operation_id"] == "op-1"
    assert marked["op_id"] == "op-1"


def test_confirm_reports_unknown_when_durable_trace_is_missing(monkeypatch):
    from core import operations, operations_mcp

    op = operations.OperationResult("op-1", "succeeded", {}, "a", "b", False)
    captured, _ = _wire_confirm(monkeypatch, _prep_row(), _live_target(), op)
    monkeypatch.setattr(operations_mcp, "_load_operation_trace", lambda conn, op_id: None)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        _confirm(conn)
    assert exc.value.code == "outcome_unknown"
    assert captured["calls"] == 1


def test_duplicate_confirm_replays_original_operation(monkeypatch):
    from core import operations

    replay = operations.OperationResult("op-1", "succeeded", {"kind": "reload"},
                                        "audit-1", "opout-1", True)
    prep = _prep_row(kind="reload",
                     state="confirmed", operation_id="op-1",
                     interval={"from": "2026-07-01", "to": "2026-07-05",
                               "to_exclusive": "2026-07-06",
                               "windows": [{"date_from": "2026-07-01",
                                            "date_to": "2026-07-05"}],
                               "partition": None})
    captured, _ = _wire_confirm(monkeypatch, prep, _live_target(), replay)
    conn, _ = _fake_conn()

    out = _confirm(conn)
    assert captured["calls"] == 1  # execute_operation itself dedups.
    assert out["replayed"] is True
    assert out["operation_id"] == "op-1"


def test_confirm_refuses_stale_versions_for_repull_verbs(monkeypatch):
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    captured, _ = _wire_confirm(monkeypatch, _prep_row(),
                                _live_target(plan_version_id="p2"), op)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        _confirm(conn)
    assert exc.value.code == "stale_versions"
    assert captured["calls"] == 0  # NO dispatch on refusal.


def test_confirm_refuses_lock_conflict(monkeypatch):
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    captured, _ = _wire_confirm(monkeypatch, _prep_row(),
                                _live_target(active_execution_id="dse_active"), op)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        _confirm(conn)
    assert exc.value.code == "lock_conflict"
    assert captured["calls"] == 0


def test_confirm_reprocess_skips_quota_and_exposure_gates(monkeypatch):
    from core import operations

    # Reprocess is provider-free: even with NO platform exposure it must proceed
    # (retention was proven at prepare); it never calls the provider or quota.
    op = operations.OperationResult("op-1", "succeeded", {"kind": "reprocess"},
                                    "a", "b", False)
    prep = _prep_row(kind="reprocess",
                     target_versions={"plan_version_id": "p1",
                                      "mapping_version_id": "m2",
                                      "policy_version": "pol-1"},
                     interval={"retained_execution_id": "dse_OLD",
                               "chosen_mapping_version_id": "m2",
                               "calls_provider": False})
    captured, _ = _wire_confirm(monkeypatch, prep, _live_target(platform=None), op)
    # Quota must NOT be consulted for reprocess.
    from core import operations_mcp
    monkeypatch.setattr(operations_mcp, "_quota_estimate",
                        MagicMock(side_effect=AssertionError("reprocess must not check quota")))
    conn, _ = _fake_conn()

    out = _confirm(conn)
    assert captured["calls"] == 1
    assert out["operation_id"] == "op-1"


# ---------------------------------------------------------------------------
# (AC3) reprocess mutation NEVER calls the provider / queue.
# (AC4) the dispatch NEVER calls commit_publication / mutates the pointer.
# ---------------------------------------------------------------------------


def test_dispatch_refuses_and_mints_nothing_whatever_the_verb(monkeypatch):
    """The confirm path refuses too, because frozen proposals outlive the change.

    `app.operation_preparations` holds proposals frozen before story 63.7, and a
    confirm reads one of those. Refusing only at prepare would leave every one of
    them able to inflict the same damage: one non-terminal execution nothing
    advances, and a Datastream that answers 409 to its own next publication.
    """
    import core.datastream_publication as pub
    from core import run_origins

    minted = MagicMock(side_effect=AssertionError("nothing may be minted"))
    monkeypatch.setattr(pub, "create_execution", minted)
    commit_spy = MagicMock(side_effect=AssertionError("commit_publication must not be called"))
    monkeypatch.setattr(pub, "commit_publication", commit_spy, raising=False)
    # And no provider pull either -- the refusal costs a provider call as little
    # as the reprocess it replaces.
    import sys
    import types

    fake_queue = types.ModuleType("core.queue")
    fake_queue.enqueue_pull = MagicMock(side_effect=AssertionError("no pull may be enqueued"))
    monkeypatch.setitem(sys.modules, "core.queue", fake_queue)

    # AMENDED 2026-08-17 (67-15b): `reprocess` is no longer in this table. It has
    # an engine, so it is DISPATCHED -- the test below proves it is routed to that
    # engine and not quietly handled here. What stays is the pair that is retired.
    cases = {
        "reload": _prep_row(
            kind="reload", state="confirmed", operation_id="op-1",
            interval={"from": "2026-07-01", "to": "2026-07-05",
                      "to_exclusive": "2026-07-06",
                      "windows": [{"date_from": "2026-07-01", "date_to": "2026-07-05"}],
                      "partition": None},
        ),
        "synchronize": _prep_row(),
    }
    for kind, prep in cases.items():
        conn, cur = _fake_conn()
        result = br._dispatch_bounded_recovery(conn, "op-1", prep, _live_target())

        assert result.outcome == "failed", kind
        assert result.result["reason"] == run_origins.NO_ENGINE, kind
        # The REFUSING path carries an origin too -- this is the moment a person
        # asks why, so it is the last moment to be silent about which treatment
        # they asked for.
        assert result.result["origin"] == br._ORIGIN_BY_KIND[kind], kind
        assert result.result["execution_id"] is None, kind
        assert "Nothing was created." in result.result["message"], kind
        # AC4 still holds, and now trivially: no SQL touched the published
        # pointer, because no candidate was written at all.
        sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list if c.args)
        assert "current_published_execution_id" not in sql, kind

    minted.assert_not_called()
    commit_spy.assert_not_called()
    fake_queue.enqueue_pull.assert_not_called()


def test_dispatch_routes_reprocess_to_its_engine_by_the_registry(monkeypatch):
    """The built verb reaches `datastream_reprocess`, chosen by `has_engine`.

    Routed by the registry lookup the refusal already used, not by a second
    `if kind == "reprocess"`. A second branch is how the console and the server
    came to disagree about which verbs could run in the first place -- one table
    decides, and both halves read it.
    """
    from core import datastream_reprocess as dr

    seen = {}

    def _fake_dispatch(conn, **kwargs):
        seen.update(kwargs)
        return "ROUTED"

    monkeypatch.setattr(dr, "dispatch_reprocess", _fake_dispatch)
    prep = _prep_row(
        kind="reprocess",
        target_versions={"plan_version_id": "p1", "mapping_version_id": "m2",
                         "policy_version": "pol-1"},
        interval={"retained_execution_id": "dse_OLD",
                  "chosen_mapping_version_id": "m2", "calls_provider": False},
    )
    conn, _ = _fake_conn()
    assert br._dispatch_bounded_recovery(conn, "op-9", prep, _live_target()) == "ROUTED"
    # The mapping the PROPOSAL pinned travels, not whatever is live at this
    # instant: a governed act does not re-resolve its own inputs after a person
    # confirmed different ones.
    assert seen["chosen_mapping_version_id"] == "m2"
    assert seen["operation_id"] == "op-9"


# ---------------------------------------------------------------------------
# Static guard: the module never calls commit_publication (AC4) and never
# imports a connector module (AD-2).
# ---------------------------------------------------------------------------


def test_module_source_never_calls_commit_publication():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "core" / "bounded_recovery.py"
    ).read_text(encoding="utf-8")
    assert "commit_publication(" not in src
    assert "current_published_execution_id =" not in src  # never writes the pointer.


def test_migration_080_widens_kind_check_additively():
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[3]
        / "infra" / "nango" / "migrations" / "080_bounded_recovery.sql"
    ).read_text(encoding="utf-8")
    assert sql.strip().startswith("-- Story 12.11")
    assert "operation_preparations_kind_check" in sql
    for verb in ("synchronize", "reload", "reprocess"):
        assert f"'{verb}'" in sql
    # The 36.13 verbs remain admissible (superset, additive).
    for verb in ("retry", "refetch", "reconcile"):
        assert f"'{verb}'" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
