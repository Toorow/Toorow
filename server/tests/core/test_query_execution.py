"""Story 50.1 -- governed execution proofs.

The `unavailable` cases below are not hypothetical: they are the live state of
this repository on 2026-07-31. The one published Semantic View binds five members
to a Datastream that has a mapping version but has never executed, so
`app.datastream_output_versions` is empty. An execution today MUST reach
`unavailable` with `missing_link = datastream_output_versions` -- and must never
reach `empty`, which would claim we asked and nothing matched.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import core.query_execution as query_execution
import pytest
from core.query_execution import (
    NO_AI_PATH,
    ExecutionUnavailable,
    build_sql,
    capture_evidence,
    complete_deferred_result,
    resolve_physical_plan,
    run_execution,
    shape_result_payload,
    terminalize,
)

CLICKS, DATE, COUNTRY = "sc_clicks", "sc_date", "sc_country"

_SPEC = {
    "measures": [{"id": CLICKS, "version_id": "scv_clicks"}],
    "dimensions": [{"id": DATE, "version_id": "scv_date"}],
    "filters": [],
    "sort": [],
    "grain": "day",
    "row_limit": 500,
    "time": {},
}

_PLAN = {
    "relation": "mart_gsc_daily",
    "columns": {CLICKS: "clicks", DATE: "date", COUNTRY: "country"},
    "grain": ["date", "country"],
}


def test_semantic_query_crossing_exposes_only_governed_identity(monkeypatch):
    observed = []
    monkeypatch.setattr(
        "core.ai_path_recorder.emit_step_sync",
        lambda **fields: observed.append(fields) or True,
    )

    query_execution._record_semantic_query_crossing(
        semantic_view_version_id="svv_EXAMPLE",
        outcome="succeeded",
    )

    assert observed == [
        {
            "step_kind": "semantic_query",
            "outcome": "succeeded",
            "tool_name": "execute_analyze_query_spec",
            "owner_workspace": "analyze",
            "owner_object_type": "semantic-view-version",
            "owner_object_id": "svv_EXAMPLE",
        }
    ]


class _Cursor:
    def __init__(self, script):
        self._script = script
        self._last = ""
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.executed.append((sql, params))

    def fetchone(self):
        for pattern, value in self._script:
            if pattern in self._last:
                return value
        return None

    def fetchall(self):
        for pattern, value in self._script:
            if pattern in self._last:
                return value
        return []


class _Conn:
    def __init__(self, script=()):
        self._script = list(script)
        self.cursors = []

    def cursor(self):
        cur = _Cursor(self._script)
        self.cursors.append(cur)
        return cur

    @property
    def statements(self):
        return [sql for cur in self.cursors for sql, _ in cur.executed]

    @property
    def calls(self):
        return [(sql, params) for cur in self.cursors for sql, params in cur.executed]


# ---------------------------------------------------------------------------
# Resolving the physical target -- every broken link is named, not lumped.
# ---------------------------------------------------------------------------


def test_no_binding_is_unavailable_and_names_the_missing_link():
    plan = resolve_physical_plan(
        _Conn([("semantic_view_version_bindings", [])]),
        project_id="proj_EXAMPLE",
        semantic_view_version_id="sv_ver",
        spec=_SPEC,
    )
    assert plan["missing_link"] == "semantic_view_version_bindings"


def test_a_mapped_datastream_with_no_published_output_is_unavailable_not_empty():
    """The live state on 2026-07-31, and the distinction that matters most."""
    conn = _Conn(
        [
            (
                "semantic_view_version_bindings",
                [
                    (CLICKS, "ds_1", "dmap_1", "active", "clicks"),
                    (DATE, "ds_1", "dmap_1", "active", "date"),
                ],
            ),
            ("datastream_output_versions", None),
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan["missing_link"] == "datastream_output_versions"
    # CHANTIER A: the CODE stays machine-facing, the SENTENCE names the gesture.
    # It used to read "this Datastream has no published output to query yet" --
    # true, and it left a person with nothing to do.
    assert "Run it" in plan["unavailable_reason"]
    assert "relation" not in plan


def test_an_inactive_binding_does_not_silently_execute():
    conn = _Conn(
        [
            (
                "semantic_view_version_bindings",
                [
                    (CLICKS, "ds_1", "dmap_1", "active", "clicks"),
                    (DATE, "ds_1", "dmap_1", "superseded", "date"),
                ],
            )
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan["missing_link"] == "binding_state"


def test_a_request_spanning_two_datastreams_refuses_instead_of_inventing_a_join():
    """Story 66.11 amended the SENTENCE, not the refusal.

    This resolver is the single-source one and still refuses a cross -- widening
    it would give the product two answers to "how is a cross executed". What
    changed is where the refusal sends a reader: to `core.multi_source_plan`,
    which exists since story 66.4, instead of to work described as not yet done.
    """
    conn = _Conn(
        [
            (
                "semantic_view_version_bindings",
                [
                    (CLICKS, "ds_1", "dmap_1", "active", "clicks"),
                    (DATE, "ds_2", "dmap_2", "active", "date"),
                ],
            )
        ]
    )
    plan = resolve_physical_plan(
        conn, project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=_SPEC
    )
    assert plan["missing_link"] == "multi_source_plan"
    assert "does not exist" not in plan["unavailable_reason"]


# ---------------------------------------------------------------------------
# SQL construction -- identifiers allowlisted, values always bound.
# ---------------------------------------------------------------------------


def test_values_are_bound_and_never_interpolated():
    spec = {
        **_SPEC,
        "filters": [{"member_id": COUNTRY, "operator": "eq", "value": "FR'; DROP TABLE x;--"}],
    }
    sql, params = build_sql(_PLAN, spec)
    assert "DROP TABLE" not in sql
    assert params == ["FR'; DROP TABLE x;--"]
    assert sql.count("?") == len(params)


def test_an_identifier_the_mapping_did_not_declare_cannot_reach_sql():
    hostile = {**_PLAN, "columns": {**_PLAN["columns"], CLICKS: "clicks); DROP TABLE x;--"}}
    with pytest.raises(ExecutionUnavailable):
        build_sql(hostile, _SPEC)


def test_the_query_asks_for_one_row_over_the_limit_so_truncation_is_detected():
    sql, _ = build_sql(_PLAN, {**_SPEC, "row_limit": 500})
    assert re.search(r"LIMIT 501\b", sql), "asking for exactly the limit cannot detect truncation"


def test_the_query_never_fetches_beyond_the_real_result_storage_bound():
    sql, _ = build_sql(_PLAN, {**_SPEC, "row_limit": 50_000})
    assert re.search(r"LIMIT 1001\b", sql)


def test_dimensions_group_and_measures_aggregate():
    sql, _ = build_sql(_PLAN, _SPEC)
    assert "SUM(clicks)" in sql
    assert "GROUP BY date" in sql


def test_a_time_window_binds_both_bounds():
    spec = {**_SPEC, "time": {"member_id": DATE, "start": "2026-01-01", "end": "2026-01-31"}}
    sql, params = build_sql(_PLAN, spec)
    assert sql.count("date >= ?") == 1 and sql.count("date <= ?") == 1
    assert params == ["2026-01-01", "2026-01-31"]


# ---------------------------------------------------------------------------
# Terminalization -- one Result, always, with its mandatory payload.
# ---------------------------------------------------------------------------


def _attempt():
    return {"attempt_id": "qea_1", "result_id": "qr_1", "query_spec_version_id": "qsv_1"}


def _terminalize(outcome, **kwargs):
    conn = _Conn()
    out = terminalize(
        conn,
        attempt=_attempt(),
        org_id="org_1",
        project_id="proj_EXAMPLE",
        outcome=outcome,
        started_at=datetime.now(timezone.utc),
        **kwargs,
    )
    return conn, out


def test_a_human_result_stores_the_exact_literal_not_a_null():
    conn, out = _terminalize("empty", manifest={"why": "no rows matched"})
    assert out["ai_path"] == NO_AI_PATH == "No AI path"
    insert = next(p for sql, p in conn.calls if "INSERT INTO app.query_results" in sql)
    assert NO_AI_PATH in insert and None in insert  # literal set, ai_path_id null


def test_every_outcome_writes_a_payload_including_refused_and_unavailable():
    for outcome in ("success", "empty", "degraded", "refused", "unavailable"):
        conn, _ = _terminalize(outcome, manifest={"reason": outcome})
        assert any("INSERT INTO app.query_result_payloads" in sql for sql in conn.statements), (
            f"{outcome} left no inspectable manifest"
        )


def test_an_unknown_outcome_cannot_be_written():
    with pytest.raises(ValueError):
        _terminalize("partial")


def test_the_attempt_is_closed_in_the_same_transaction_as_its_result():
    conn, _ = _terminalize("success", rows=[{"date": "2026-01-01", "clicks": 3}])
    joined = " ".join(conn.statements)
    assert "INSERT INTO app.query_results" in joined
    assert "INSERT INTO app.query_result_payloads" in joined
    assert "SET state = 'terminal'" in joined


def test_the_content_hash_covers_rows_schema_and_manifest_together():
    _, a = _terminalize("success", rows=[{"clicks": 1}], manifest={"m": 1})
    _, b = _terminalize("success", rows=[{"clicks": 1}], manifest={"m": 1})
    _, c = _terminalize("success", rows=[{"clicks": 2}], manifest={"m": 1})
    _, d = _terminalize("success", rows=[{"clicks": 1}], manifest={"m": 2})
    assert a["content_hash"] == b["content_hash"]
    assert a["content_hash"] != c["content_hash"]
    assert a["content_hash"] != d["content_hash"], "a manifest change must change the hash"


def test_terminalization_freezes_server_classification_before_hash(monkeypatch):
    from core import feedback_review

    observed = []
    classification = {
        "schema_version": "evaluation-classification.v1",
        "result_type": "refusal",
        "classification_hash": "c" * 64,
    }

    def freeze(_conn, **kwargs):
        observed.append(kwargs)
        return classification

    monkeypatch.setattr(feedback_review, "freeze_result_classification", freeze, raising=False)
    conn, result = _terminalize("refused", manifest={"result_shape": "tabular_v1"})

    payload_params = next(
        params for sql, params in conn.calls if "INSERT INTO app.query_result_payloads" in sql
    )
    manifest = json.loads(payload_params[5])
    assert manifest["evaluation_classification"] == classification
    assert observed == [
        {
            "org_id": "org_1",
            "project_id": "proj_EXAMPLE",
            "query_spec_version_id": "qsv_1",
            "outcome": "refused",
            "ai_path_id": None,
        }
    ]
    assert result["content_hash"] == query_execution.canonical_hash(
        {"schema": {"fields": []}, "rows": [], "manifest": manifest}
    )


def test_mcp_execution_can_defer_immutable_result_until_ai_path_is_finalized(monkeypatch):
    from core import feedback_review

    conn = _Conn([("app.project_capabilities", ("disabled", None))])
    pending = run_execution(
        conn,
        attempt=_attempt(),
        org_id="org_1",
        project_id="proj_EXAMPLE",
        spec={**_SPEC, "required_capability": {"key": "tax_fees"}},
        semantic_view_version_id="sv_ver",
        ai_path_id="aip_1",
        defer_terminalization=True,
    )
    assert pending["outcome"] == "refused"
    assert not any("INSERT INTO app.query_results" in sql for sql in conn.statements)

    monkeypatch.setattr(
        feedback_review,
        "freeze_result_classification",
        lambda *_args, **_kwargs: {
            "schema_version": "evaluation-classification.v1",
            "skills": [{"skill_version_id": "skv_final"}],
            "classification_hash": "d" * 64,
        },
        raising=False,
    )
    result = complete_deferred_result(conn, pending)
    assert result["result_id"] == "qr_1"
    assert any("INSERT INTO app.query_results" in sql for sql in conn.statements)


def test_terminalization_freezes_only_the_real_storage_bound_and_tells_the_truth():
    rows = [{"row": index} for index in range(1001)]
    conn, out = _terminalize("success", rows=rows)
    assert out["row_count"] == 1000
    assert out["truncated"] is True
    result_insert = next(params for sql, params in conn.calls if "app.query_results" in sql)
    payload_insert = next(
        params for sql, params in conn.calls if "INSERT INTO app.query_result_payloads" in sql
    )
    assert result_insert[9] == 1000
    assert result_insert[12] is True
    assert len(json.loads(payload_insert[6])) == 1000


# ---------------------------------------------------------------------------
# Evidence capture (AC6), closing the gap Story 50.2 named.
#
# `analyze_workbench.py` reports provenance and DQ as `not_recorded` and names
# this module as their owner, deliberately: filling them when a Result is READ
# would describe a past execution from today's binding and quality state. These
# tests hold the snapshot at the only moment it is honest -- execution time.
# ---------------------------------------------------------------------------

_PLAN_FULL = {
    **_PLAN,
    "datastream_id": "ds_1",
    "mapping_version_id": "dmap_1",
    "pull_id": "dse_1",
    "publication_log_id": "dpl_1",
    "output_created_at": None,
}


def _evidence(script):
    return capture_evidence(_Conn(script), project_id="proj_EXAMPLE", plan=_PLAN_FULL)


def test_provenance_carries_one_tuple_per_member_not_one_per_datastream():
    ev = _evidence([("app.datastreams", ("gsc",)), ("control_cases", [])])
    values = ev["provenance"]["values"]
    assert {v["member_id"] for v in values} == set(_PLAN_FULL["columns"])
    for v in values:
        # AC6 asks for per-VALUE provenance. A single Datastream-level pointer
        # would not survive the day a Result spans more than one binding.
        assert v["source_system"] == "gsc"
        assert v["source_field"]
        assert v["pull_id"] == "dse_1"


def test_the_pull_and_publication_are_pinned_at_execution_not_looked_up_later():
    ev = _evidence([("app.datastreams", ("gsc",)), ("control_cases", [])])
    assert ev["provenance"]["pull_id"] == "dse_1"
    assert ev["provenance"]["publication_log_id"] == "dpl_1"
    assert ev["provenance"]["mapping_version_id"] == "dmap_1"


def test_open_dq_cases_are_snapshotted_by_id():
    ev = _evidence([("app.datastreams", ("gsc",)), ("control_cases", [("cc_1",), ("cc_2",)])])
    assert ev["dq_evaluation_ids"] == ["cc_1", "cc_2"]
    assert ev["dq_unavailable_reason"] is None


def test_an_unreadable_dq_owner_is_unavailable_never_zero_open_cases():
    """ "No evidence" and "evidence says healthy" are different answers (AC6)."""

    class _Failing(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "control_cases" in sql:
                raise RuntimeError("control plane unreachable")

    class _Conn2:
        def cursor(self):
            return _Failing([("app.datastreams", ("gsc",))])

    ev = capture_evidence(_Conn2(), project_id="proj_EXAMPLE", plan=_PLAN_FULL)
    assert ev["dq_evaluation_ids"] == []
    assert "unreachable" in ev["dq_unavailable_reason"]


def test_waterfall_shape_maps_physical_columns_to_stable_members_and_pins_evidence():
    components = [
        {
            "component": "net_media",
            "label": "Net media",
            "waterfall_role": "base",
            "member_id": "m1",
            "tax_basis": "HT",
        },
        {
            "component": "platform_fee",
            "label": "Platform fees",
            "waterfall_role": "delta",
            "member_id": "m2",
            "tax_basis": "HT",
        },
        {
            "component": "regulatory_tax",
            "label": "Regulatory tax",
            "waterfall_role": "delta",
            "member_id": "m3",
            "tax_basis": "HT",
        },
        {
            "component": "withholding_gross_up",
            "label": "Withholding gross-up",
            "waterfall_role": "delta",
            "member_id": "m4",
            "tax_basis": "HT",
        },
        {
            "component": "agency_fee",
            "label": "Agency fee",
            "waterfall_role": "delta",
            "member_id": "m5",
            "tax_basis": "HT",
        },
        {
            "component": "total_cost_ht",
            "label": "Total cost HT",
            "waterfall_role": "subtotal",
            "member_id": "m6",
            "tax_basis": "HT",
        },
        {
            "component": "vat_sales_tax",
            "label": "VAT / sales tax",
            "waterfall_role": "delta",
            "member_id": "m7",
            "tax_basis": "TTC",
        },
        {
            "component": "invoice_ttc",
            "label": "Invoice TTC",
            "waterfall_role": "total",
            "member_id": "m8",
            "tax_basis": "TTC",
        },
    ]
    descriptor = {
        "components": components,
        "currency": {"member_id": "currency"},
        "covered_row_count": {"member_id": "covered"},
        "total_row_count": {"member_id": "total"},
        "gap_codes": {"member_id": "gaps"},
        "allowed_basis_transition": ["HT", "TTC"],
        "non_additive_grains": ["plan_line"],
        "manifest_pins": {
            "tax_fee_rule_set_version_id": {"member_id": "rule_version", "required": True},
            "tax_fee_rule_set_content_hash": {"member_id": "rule_hash", "required": True},
            "project_configuration_version_id": {"member_id": "config_version", "required": True},
            "money_policy_version_id": {"member_id": "money_version", "required": True},
            "geography_version_id": {"member_id": "geo_version", "required": True},
        },
        "verification": {"keep_separate": True, "rows": []},
    }
    member_values = {
        "m1": 1_000_000,
        "m2": 100_000,
        "m3": 50_000,
        "m4": 25_000,
        "m5": 75_000,
        "m6": 1_250_000,
        "m7": 250_000,
        "m8": 1_500_000,
        "currency": "EUR",
        "covered": 9,
        "total": 10,
        "gaps": [],
        "rule_version": "grsv_1",
        "rule_hash": "a" * 64,
        "config_version": "pcv_1",
        "money_version": "grsv_money",
        "geo_version": "ghv_1",
    }
    plan = {"columns": {member: f"physical_{index}" for index, member in enumerate(member_values)}}
    physical_row = {plan["columns"][member]: value for member, value in member_values.items()}
    evidence = {
        "provenance": {
            "values": [
                {
                    "member_id": member,
                    "source_system": "dbt",
                    "source_field": physical,
                    "pull_id": "pull_1",
                }
                for member, physical in plan["columns"].items()
            ]
        }
    }

    shaped = shape_result_payload(
        result_id="qr_1",
        physical_rows=[physical_row],
        plan=plan,
        spec={
            "result_shape": "waterfall_v1",
            "result_shape_descriptor": descriptor,
            "grain": "project",
        },
        evidence=evidence,
    )

    assert shaped["manifest"]["pins"] == {
        "tax_fee_rule_set_version_id": "grsv_1",
        "tax_fee_rule_set_content_hash": "a" * 64,
        "project_configuration_version_id": "pcv_1",
        "money_policy_version_id": "grsv_money",
        "geography_version_id": "ghv_1",
    }
    first = shaped["rows"][0]
    assert first["value_micros"] == 1_000_000
    assert first["evidence_key"] == "qr_1:evidence:m1"
    assert shaped["manifest"]["datum_evidence"][first["evidence_key"]]["pull_id"] == "pull_1"


def test_disabled_required_capability_terminalizes_refused_before_physical_resolution(monkeypatch):
    conn = _Conn([("app.project_capabilities", ("disabled", None))])

    def _must_not_resolve(*_args, **_kwargs):
        raise AssertionError("disabled capability must stop before warehouse resolution")

    monkeypatch.setattr("core.query_execution.resolve_physical_plan", _must_not_resolve)
    outcome = run_execution(
        conn,
        attempt=_attempt(),
        org_id="org_1",
        project_id="proj_EXAMPLE",
        spec={**_SPEC, "required_capability": {"key": "tax_fees"}},
        semantic_view_version_id="sv_ver",
    )

    assert outcome["outcome"] == "refused"
    payload = next(
        params for sql, params in conn.calls if "INSERT INTO app.query_result_payloads" in sql
    )
    manifest = payload[5]
    assert '"reason_code": "CAPABILITY_DISABLED"' in manifest
    # Story 67.11 -- CE TEST PINNAIT UNE PORTE QUI N EXISTE PAS. Il exigeait
    # `workspace: governance` / `section: capabilities`, un couple qu'aucun
    # registre de navigation de la console ne declare : le refus nommait donc un
    # geste que personne ne pouvait faire, et un refus qui envoie au mauvais
    # endroit est pire qu'un refus muet. Les capacites d'un Projet vivent sur la
    # surface GLOBALE des reglages (`ownerResolution.ts:104`).
    assert '"global_surface": "project-settings"' in manifest
    assert '"global_section": "capabilities"' in manifest
    # ...et QUELLE capacite est refusee reste dans la charge : arriver sur la
    # bonne page sans savoir quoi y activer n'est pas arriver.
    assert '"capability_key"' in manifest


# ---------------------------------------------------------------------------
# AI-308 -- a refusal TERMINALIZES. An accepted attempt never stays without its
# Result.
#
# `_safe_identifier` raises `ExecutionUnavailable`, and until 2026-08-21 that
# class was caught NOWHERE in `server/`. Both raise sites sat outside a
# terminalizing `try`, so the live state of a Datastream whose candidate landed
# nowhere readable was: attempt accepted, no Result row, 500 at the caller.
# ---------------------------------------------------------------------------


def _published_output_conn(relation_ref):
    return _Conn(
        [
            ("app.project_capabilities", None),
            (
                "semantic_view_version_bindings",
                [
                    (CLICKS, "ds_1", "dmap_1", "active", "clicks"),
                    (DATE, "ds_1", "dmap_1", "active", "date"),
                ],
            ),
            (
                "datastream_output_versions",
                (relation_ref, "dse_1", "dplog_1", None, "dsov_1", "ds_1"),
            ),
            (
                "datastream_mapping_versions",
                (
                    {
                        "fields": [
                            {"field_id": "clicks", "binding": {"canonical_target": "clicks"}},
                            {"field_id": "date", "binding": {"canonical_target": "date"}},
                        ]
                    },
                ),
            ),
        ]
    )


def test_a_run_that_landed_nowhere_readable_is_ANSWERED_and_the_result_exists():
    """The reference BOTH activation drivers publish, walked through the reader.

    `raw_landing.unlanded_candidate_ref` is the exact string
    `datastream_output_versions.relation_ref` receives when a candidate landed no
    relation (`datastream_activation.py:1069` copies `artifact_ref` verbatim), so
    this is the production case and not an invented one.
    """
    from core.raw_landing import unlanded_candidate_ref

    conn = _published_output_conn(unlanded_candidate_ref("dse_01LANDEDNOTHING000000000000"))
    outcome = run_execution(
        conn,
        attempt=_attempt(),
        org_id="org_1",
        project_id="proj_EXAMPLE",
        spec=_SPEC,
        semantic_view_version_id="sv_ver",
    )

    assert outcome["outcome"] == "unavailable"
    # THE RESULT EXISTS. This is the half that was missing: an outcome nobody can
    # read, replay or diagnose is worse than a refused one.
    assert any("INSERT INTO app.query_results" in sql for sql in conn.statements)
    payload = next(
        params for sql, params in conn.calls if "INSERT INTO app.query_result_payloads" in sql
    )
    manifest = json.loads(payload[5])
    assert manifest["missing_link"] == "relation_ref"

    sentence = manifest["unavailable_reason"]
    assert "Re-run it" in sentence, "the message must name the gesture that repairs"
    assert "/" not in sentence
    assert "dse_" not in sentence and "candidate" not in sentence
    for jargon in ("relation", "identifier", "SQL", "exception", "null"):
        assert jargon not in sentence, f"the sentence says `{jargon}` to a person"


def test_a_mapping_naming_something_that_is_not_a_column_is_ANSWERED_not_raised():
    """The other raise site: `resolve_physical_plan` allowlists mapped columns.

    It sat outside every `try` in `run_execution`, so this exact mapping produced
    an uncaught `ExecutionUnavailable` with the attempt already accepted.
    """
    conn = _Conn(
        [
            ("app.project_capabilities", None),
            (
                "semantic_view_version_bindings",
                [
                    (CLICKS, "ds_1", "dmap_1", "active", "clicks"),
                    (DATE, "ds_1", "dmap_1", "active", "date"),
                ],
            ),
            (
                "datastream_output_versions",
                ("mart_gsc_daily", "dse_1", "dplog_1", None, "dsov_1", "ds_1"),
            ),
            (
                "datastream_mapping_versions",
                (
                    {
                        "fields": [
                            {
                                "field_id": "clicks); DROP TABLE x;--",
                                "binding": {"canonical_target": "clicks"},
                            },
                            {"field_id": "date", "binding": {"canonical_target": "date"}},
                        ]
                    },
                ),
            ),
        ]
    )
    outcome = run_execution(
        conn,
        attempt=_attempt(),
        org_id="org_1",
        project_id="proj_EXAMPLE",
        spec=_SPEC,
        semantic_view_version_id="sv_ver",
    )

    assert outcome["outcome"] == "unavailable"
    assert any("INSERT INTO app.query_results" in sql for sql in conn.statements)
    payload = next(
        params for sql, params in conn.calls if "INSERT INTO app.query_result_payloads" in sql
    )
    manifest = json.loads(payload[5])
    assert manifest["missing_link"] == "physical_identifier"
    # THE ANSWER STAYS OPAQUE. The refused identifier is evidence for a repairer,
    # it goes to the log, and it must never travel to a screen.
    assert "DROP TABLE" not in manifest["unavailable_reason"]


def test_the_planner_and_the_compiler_answer_the_same_question_about_a_relation():
    """One predicate, imported and never retyped -- the disagreement WAS the defect.

    Anything the planner accepts must compile; anything it refuses must be an
    outcome, not a raise.
    """
    from core.query_execution import names_a_readable_relation

    for relation in (
        "mart_gsc_daily",
        "dataset.mart_gsc_daily",
        "raw_gsc_daily__cand_dse_01ABC",
        "execution/dse_01ABC/candidate/relation",
        "app.media_plan_versions:mpv_01ABC",
        "",
        "1_starts_with_a_digit",
        None,
    ):
        readable = names_a_readable_relation(relation)
        try:
            build_sql({**_PLAN, "relation": relation}, _SPEC)
            compiles = True
        except ExecutionUnavailable:
            compiles = False
        assert readable == compiles, f"planner and compiler disagree on {relation!r}"


def test_unreadable_required_capability_terminalizes_unavailable(monkeypatch):
    class _FailCapabilityCursor(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "app.project_capabilities" in sql:
                raise RuntimeError("control plane offline")

    class _FailCapabilityConn(_Conn):
        def cursor(self):
            cur = _FailCapabilityCursor([])
            self.cursors.append(cur)
            return cur

    conn = _FailCapabilityConn()
    monkeypatch.setattr(
        "core.query_execution.resolve_physical_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    outcome = run_execution(
        conn,
        attempt=_attempt(),
        org_id="org_1",
        project_id="proj_EXAMPLE",
        spec={**_SPEC, "required_capability": {"key": "tax_fees"}},
        semantic_view_version_id="sv_ver",
    )
    assert outcome["outcome"] == "unavailable"


# ---------------------------------------------------------------------------
# AI-303 -- a shared landing must be read at ONE grain.
#
# `raw_youtube_daily` holds two report grains: `channel_daily` lands one row per
# day for the channel (`video` empty), `video_daily` one row per day PER VIDEO.
# Summed together they are the same views twice -- measured on production
# 2026-07-18: 1556 answered where the source says 778, and all 33 days exactly
# double. It was hidden until migration 275 gave each profile its own pull: before
# that only one of the two ever landed, and the sum was right BY ACCIDENT.
#
# The grain is not invented here. Each report profile already DECLARES its
# `dimensions`: `channel_daily` = [date, channel_id], `video_daily` =
# [date, video, channel_id]. A column a sibling of the same `raw_relation` reports
# on and this one does not is a finer grain lying beside these rows.
# ---------------------------------------------------------------------------


def test_a_shared_landing_is_restricted_to_the_profile_s_own_grain():
    """The finer grain is excluded by its absent marker, before any SUM."""
    plan = {**_PLAN, "grain_restrictions": [("video", "=")]}

    sql, params = query_execution.build_sql(plan, _SPEC)

    assert "video = ?" in sql, (
        "without this restriction the per-video rows are summed beside the "
        "channel row and every measure doubles"
    )
    assert "" in params, (
        "the marker is the value the connector lands on the axis it does not report"
    )


def test_the_finer_profile_excludes_the_coarse_roll_up_beside_it():
    """AI-310 -- the exclusion runs BOTH ways, and only one way existed.

    Asked for `video_daily`, the one-sided rule produced no restriction at all
    (every column its sibling reports, it reports too), so the channel roll-up was
    summed beside the videos: the same double, in the direction nobody measured.
    """
    plan = {**_PLAN, "grain_restrictions": [("video", "<>")]}

    sql, params = query_execution.build_sql(plan, _SPEC)

    assert "video <> ?" in sql
    assert "" in params


def test_the_restriction_comes_before_the_aggregate_not_after():
    """A filter that ran after the SUM would report the double and hide it."""
    plan = {**_PLAN, "grain_restrictions": [("video", "=")]}

    sql, _ = query_execution.build_sql(plan, _SPEC)

    where_at = sql.upper().find("WHERE")
    group_at = sql.upper().find("GROUP BY")
    assert where_at != -1 and (group_at == -1 or where_at < group_at)


class _ProfileConn:
    """A connection that answers the one question `_grain_restrictions` asks."""

    def __init__(self, module_name, profile_id):
        self._row = (module_name, profile_id)

    def cursor(self):
        outer = self

        class _Cur:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_exc):
                return False

            def execute(self_inner, *_a, **_kw):
                return None

            def fetchone(self_inner):
                return outer._row

        return _Cur()


def _youtube_restrictions(profile_id):
    """Derived from the REAL manifest -- the declaration, not a copy of it."""
    return query_execution._grain_restrictions(
        _ProfileConn("youtube-analytics", profile_id),
        "proj_EXAMPLE",
        "ds_youtube",
        {"date", "channel_id", "video", "metric", "value", "pull_id", "loaded_at"},
    )


def test_the_two_youtube_profiles_partition_their_shared_landing():
    """AI-310 -- disjoint AND exhaustive, read off the manifest itself.

    Before the symmetric rule, `channel_daily` restricted to `video = ''` and
    `video_daily` restricted to nothing: between them they returned the channel
    row TWICE and left nothing out. Measured on production 2026-08-22 the ratio
    was exactly 2.000 on every day of the window.
    """
    assert _youtube_restrictions("channel_daily") == [("video", "=")]
    assert _youtube_restrictions("video_daily") == [("video", "<>")]


def test_the_snapshot_profile_of_the_same_landing_is_restricted_too():
    """`channel_snapshot` lands in `raw_youtube_daily` -- its manifest said otherwise.

    `pull_channel_snapshot` calls `_insert_raw_rows`; it is not one of
    `_BREAKDOWN_PROFILES`, so it never reaches the breakdown table its declaration
    named. With the address corrected it is a coarse profile of the shared landing
    and inherits the same exclusion as `channel_daily`.
    """
    assert _youtube_restrictions("channel_snapshot") == [("video", "=")]


def test_a_landing_no_other_profile_shares_is_read_exactly_as_before():
    """The 37 connectors whose profiles do not share a landing are untouched."""
    sql_plain, params_plain = query_execution.build_sql(_PLAN, _SPEC)
    sql_empty, params_empty = query_execution.build_sql(
        {**_PLAN, "grain_restrictions": []}, _SPEC
    )

    assert sql_plain == sql_empty
    assert params_plain == params_empty
    assert "video = ?" not in sql_plain


# ---------------------------------------------------------------------------
# The governed time controls reach the SQL, or they never reach the Spec.
#
# `comparison` and `time.as_of` were accepted, frozen into an immutable Spec and
# described by the Definitions lens while `build_sql` read only `start` and
# `end`. These prove the two of them are now EXECUTED -- the other two,
# `timezone` and `reporting_boundary_id`, are refused when the Spec is written
# (`test_query_specs.py`) and can no longer arrive here at all.
# ---------------------------------------------------------------------------


_COMPARISON_SPEC = {
    **_SPEC,
    "comparison": "previous_period",
    "time": {"member_id": DATE, "start": "2026-07-01", "end": "2026-07-31"},
    "comparison_windows": {
        "kind": "previous_period",
        "period_field": "comparison_period",
        "member_id": DATE,
        "current": {"start": "2026-07-01", "end": "2026-07-31"},
        "baseline": {"start": "2026-05-31", "end": "2026-06-30"},
    },
}


def test_a_period_comparison_labels_each_row_with_the_window_it_belongs_to():
    sql, params = build_sql(_PLAN, _COMPARISON_SPEC)

    assert "THEN 'current'" in sql and "THEN 'baseline'" in sql
    assert "AS comparison_period" in sql
    # Grouped by the ALIAS: a second copy of the CASE would repeat its four bound
    # values in a clause that comes after the WHERE, and positional binding would
    # then read them out of order.
    group_by = sql.split("GROUP BY")[1].split("LIMIT")[0]
    assert "comparison_period" in group_by and "CASE" not in group_by
    assert sql.count("?") == len(params)


def test_a_period_comparison_reads_both_windows_and_nothing_between_them():
    sql, params = build_sql(_PLAN, _COMPARISON_SPEC)

    where = sql.split("WHERE")[1].split("GROUP BY")[0]
    assert where.count("date >= ?") == 2 and where.count("date <= ?") == 2
    assert "OR" in where
    # SELECT list first, then WHERE -- the order of the appends IS the order of
    # the placeholders in the string.
    assert params == [
        "2026-07-01", "2026-07-31", "2026-05-31", "2026-06-30",
        "2026-07-01", "2026-07-31", "2026-05-31", "2026-06-30",
    ]


def test_a_comparison_the_spec_never_froze_reads_one_window_exactly_as_before():
    """The enum alone changes nothing: only frozen windows are executed."""
    unfrozen = {k: v for k, v in _COMPARISON_SPEC.items() if k != "comparison_windows"}
    sql, params = build_sql(_PLAN, unfrozen)

    assert "comparison_period" not in sql
    assert params == ["2026-07-01", "2026-07-31"]


_ASOF_PLAN = {
    **_PLAN,
    "present_columns": ["clicks_value", "date", "loaded_at", "metric", "pull_id"],
    "long_form": ("metric", "clicks_value"),
}


def test_an_as_of_keeps_only_the_rows_that_had_landed_by_then():
    spec = {**_SPEC, "time": {"member_id": DATE, "as_of": "2026-07-15"}}

    sql, params = build_sql(_ASOF_PLAN, spec)

    # END of the day: binding the bare date would compare a timestamp against
    # midnight and drop the whole day the person asked about.
    assert "loaded_at <= ?" in sql
    assert params == ["clicks", "2026-07-15T23:59:59.999999"]


def test_the_as_of_filter_runs_before_the_later_pull_supersedes_the_earlier():
    """Filtering after the QUALIFY would answer today's revision as of July."""
    spec = {**_SPEC, "time": {"member_id": DATE, "as_of": "2026-07-15"}}

    sql, _ = build_sql(_ASOF_PLAN, spec)

    assert sql.index("loaded_at <= ?") < sql.index("QUALIFY")


_WIDE_PLAN = {
    **_PLAN,
    "present_columns": ["campaign", "clicks", "date", "execution_id", "spend"],
    "long_form": None,
    "grain_columns": ["campaign", "date"],
}


def test_a_wide_landing_supersedes_on_the_published_grain_by_execution():
    """AI-369: a managed feed published again must not double its figures.

    Measured 2026-09-04 on the harness project: three publications of the same
    two rows read as 240 clicks for 120, because a wide landing got no
    supersede at all. The key is the mapping's grain (`app.managed_feed_grain_v`,
    what the marts model reads) and the order is the load identity the rows
    carry -- `execution_id` here, `pull_id` for a connector.
    """
    sql, _ = build_sql(_WIDE_PLAN, {**_SPEC, "time": {"member_id": DATE}})

    assert (
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY campaign, date ORDER BY execution_id DESC) = 1"
        in sql
    )
    # The measures never enter the partition.
    assert "PARTITION BY campaign, date ORDER BY" in sql and "clicks, " not in sql.split("PARTITION BY")[1].split("ORDER BY")[0]


def test_a_wide_landing_without_a_declared_grain_is_read_as_it_is():
    """No guessed key: a landing whose mapping declares no grain is not deduplicated."""
    plan = {**_WIDE_PLAN, "grain_columns": []}

    sql, _ = build_sql(plan, {**_SPEC, "time": {"member_id": DATE}})

    assert "QUALIFY" not in sql


def test_a_wide_landing_whose_grain_is_not_fully_landed_is_read_as_it_is():
    """A grain column the relation does not carry cannot partition it."""
    plan = {**_WIDE_PLAN, "grain_columns": ["campaign", "date", "market"]}

    sql, _ = build_sql(plan, {**_SPEC, "time": {"member_id": DATE}})

    assert "QUALIFY" not in sql


def test_a_request_without_an_as_of_builds_the_same_sql_it_always_did():
    with_key, params_with = build_sql(_ASOF_PLAN, {**_SPEC, "time": {"member_id": DATE}})
    without, params_without = build_sql(_ASOF_PLAN, {**_SPEC, "time": {}})

    assert with_key == without and params_with == params_without
    assert "loaded_at <= ?" not in without


def test_an_as_of_on_a_landing_that_does_not_record_when_rows_arrived_is_refused(monkeypatch):
    """Executed or refused. Never answered with today's rows under an old date."""
    monkeypatch.setattr(query_execution, "_relation_columns", lambda *_: {"clicks", "date"})
    monkeypatch.setattr(query_execution, "_dataset_for", lambda *_: "marts")
    monkeypatch.setattr(query_execution, "_grain_restrictions", lambda *_: [])
    conn = _Conn(
        [
            (
                "semantic_view_version_bindings",
                [
                    (CLICKS, "ds_1", "dmv_1", "active", "clicks"),
                    (DATE, "ds_1", "dmv_1", "active", "date"),
                ],
            ),
            ("datastream_output_versions", ("mart_gsc_daily", "pull_1", "pl_1", None)),
            (
                "datastream_mapping_versions",
                (
                    {
                        "fields": [
                            {"field_id": "clicks", "binding": {"canonical_target": "clicks"}},
                            {"field_id": "date", "binding": {"canonical_target": "date"}},
                        ]
                    },
                ),
            ),
        ]
    )

    plan = resolve_physical_plan(
        conn,
        project_id="proj_EXAMPLE",
        semantic_view_version_id="sv_ver",
        spec={**_SPEC, "time": {"member_id": DATE, "as_of": "2026-07-15"}},
    )

    assert plan["missing_link"] == "loaded_at"
    assert "Ask without a Reported-as-of date" in plan["unavailable_reason"]
