"""Story 50.6 (AC2, AC9, AC10) -- the Analyze data tool answers, and never over-answers.

What these tests hold the adapter to:
  * every terminal outcome produces an inspectable compact answer -- including the
    three that produce no rows, which are delivered answers rather than failures;
  * counts are `None` with a stated reason when the outcome cannot know them,
    never 0 -- a zero that means "we do not know" is a lie a chart will draw;
  * `No AI path` is the EXACT literal, never null, never absent;
  * a host with no UI receives bounded evidence, pagination affordances and an
    exact deep link -- and is never deep-link-only;
  * the render tool's model-facing summary is BYTE-IDENTICAL to the data tool's
    for the same Result, because it calls the same composition rather than its own;
  * denied and absent are indistinguishable.
"""

from __future__ import annotations

import json

import pytest
from core import analyze_render_mcp as adapter
from core.model_channel import MODEL_CHANNEL_MAX_BYTES, MODEL_CHANNEL_MAX_TEXT_LINES
from core.result_slices import RESULT_META_KEY

_HASH = "a" * 64
_SCHEMA = {"fields": [{"name": "day"}, {"name": "clicks"}]}


def test_execute_query_spec_uses_one_connection_and_commits_after_tool_result(monkeypatch):
    """The observable path and Result become durable together, before delivery."""
    import contextlib

    from core import ai_path_recorder, query_execution, query_specs_api
    from core.project_access import AccessDecision

    events: list[object] = []

    class Conn:
        def cursor(self):
            return _Cursor([({"measures": []}, "svv_EXAMPLE")])

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

    conn = Conn()

    @contextlib.contextmanager
    def connection(identity):
        assert identity == "person@example.com"
        yield conn

    monkeypatch.setattr(adapter, "_identity", lambda: "person@example.com")
    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *args, **kwargs: AccessDecision(True, "member", "edit", "org_EXAMPLE"),
    )
    monkeypatch.setattr(
        query_execution,
        "accept_execution",
        lambda used, **kwargs: events.append(("accept", used))
        or {
            "attempt_id": "qea_EXAMPLE",
            "result_id": "qr_EXAMPLE",
            "query_spec_version_id": "qsv_EXAMPLE",
        },
    )

    def run(used, *, ai_path_id, **kwargs):
        assert kwargs["defer_terminalization"] is True
        events.append(("run", used, ai_path_id))
        return {
            "result_id": "qr_EXAMPLE",
            "outcome": "success",
            "row_count": 1,
            "truncated": False,
            "content_hash": _HASH,
            "ai_path": ai_path_id,
        }

    monkeypatch.setattr(query_execution, "run_execution", run)
    monkeypatch.setattr(
        query_execution,
        "complete_deferred_result",
        lambda used, pending: events.append(("terminalize", used)) or pending,
    )

    def record(used, *, execute, **kwargs):
        events.append(("path-open", used))
        result = execute("aip_EXAMPLE")
        events.append("path-finalized")
        return result

    monkeypatch.setattr(ai_path_recorder, "record_result_execution", record)
    monkeypatch.setattr(
        adapter,
        "_compose_answer_on_connection",
        lambda used, **kwargs: events.append(
            ("compose", used, kwargs["include_ai_path_walk"])
        )
        or ("answer", {"result": {"result_id": "qr_EXAMPLE"}}, {}),
    )
    delivered = object()
    monkeypatch.setattr(
        adapter,
        "_result_tool_result",
        lambda *args: events.append("tool-result-built") or delivered,
    )

    result = adapter._execute_analyze_query_spec("proj_EXAMPLE", "qsv_EXAMPLE")

    assert result is delivered
    assert events == [
        ("path-open", conn),
        ("accept", conn),
        ("run", conn, "aip_EXAMPLE"),
        "path-finalized",
        ("terminalize", conn),
        ("compose", conn, True),
        "tool-result-built",
        "commit",
    ]


def test_execute_query_spec_rolls_back_an_infrastructure_failure(monkeypatch):
    import contextlib

    from core import ai_path_recorder, query_specs_api
    from core.project_access import AccessDecision

    class Conn:
        committed = False
        rolled_back = False

        def cursor(self):
            return _Cursor([({}, "svv_EXAMPLE")])

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    conn = Conn()

    @contextlib.contextmanager
    def connection(_identity):
        yield conn

    monkeypatch.setattr(adapter, "_identity", lambda: "person@example.com")
    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *args, **kwargs: AccessDecision(True, "member", "edit", "org_EXAMPLE"),
    )
    monkeypatch.setattr(
        ai_path_recorder,
        "record_result_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database failed")),
    )

    with pytest.raises(RuntimeError, match="database failed"):
        adapter._execute_analyze_query_spec("proj_EXAMPLE", "qsv_EXAMPLE")

    assert conn.rolled_back is True
    assert conn.committed is False


def test_execute_composition_exposes_only_the_safe_observed_projection(monkeypatch):
    conn = _Conn(
        [
            _path_result_row(),
            (_HASH, _SCHEMA, {}, [{"day": "2026-07-01", "clicks": 1}]),
            ("svv_EXAMPLE",),
        ]
    )

    monkeypatch.setattr(
        adapter,
        "_load_ai_path_walk",
        lambda *args, **kwargs: {
            "schema_version": "observed-ai-path.v1",
            "state": "completed",
            "path_id": "aip_EXAMPLE",
            "lifecycle": "finalized",
            "outcome": "succeeded",
            "steps": [],
        },
    )
    _text, summary, meta = adapter._compose_answer_on_connection(
        conn,
        identity="person@example.com",
        org_id="org_EXAMPLE",
        project_id="proj_EXAMPLE",
        result_id="qr_EXAMPLE",
        tool_name="execute_analyze_query_spec",
        include_ai_path_walk=True,
    )

    assert summary["ai_path"] == "aip_EXAMPLE"
    result_meta = meta[RESULT_META_KEY]
    assert result_meta["ai_path_walk"]["schema_version"] == "observed-ai-path.v1"
    encoded = json.dumps(result_meta, sort_keys=True)
    for forbidden in ('"actor"', '"assessment"', '"detail"', '"walk"'):
        assert forbidden not in encoded


def _result(outcome="success", row_count=3, truncated=False, ai_path="No AI path"):
    return {
        "id": "qr_EXAMPLE",
        "attempt_id": "qea_EXAMPLE",
        "query_spec_version_id": "qsv_EXAMPLE",
        "outcome": outcome,
        "ai_path": ai_path,
        "content_hash": _HASH,
        "row_count": row_count,
        "cell_count": row_count * 2,
        "byte_count": row_count * 40,
        "truncated": truncated,
        "predecessor_result_id": None,
        "started_at": "2026-07-31T09:00:00+00:00",
        "ended_at": "2026-07-31T09:00:01+00:00",
    }


def _payload(rows=None, manifest=None, schema=None):
    return {
        "content_hash": _HASH,
        "result_schema": _SCHEMA if schema is None else schema,
        "manifest": manifest if manifest is not None else {},
        "rows_chunk": rows if rows is not None else [{"day": "2026-07-01", "clicks": 1}],
    }


#: The shape `core/query_execution.py` ACTUALLY writes when a Result has no rows:
#: `terminalize` defaults `result_schema` to `{"fields": []}`, and the
#: `unavailable` / `refused` terminalizations pass no schema at all. A test whose
#: helper always injects a three-field schema is testing a shape a zero-row Result
#: can never have -- which is how three raising outcomes went unnoticed.
_NO_FIELD_SCHEMA: dict = {"fields": []}


def _summary(**kwargs):
    params = {
        "org_id": "org_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "result": _result(),
        "payload": _payload(),
        "semantic_view_version_id": "svv_EXAMPLE",
    }
    params.update(kwargs)
    return adapter.build_summary(**params)


# ---------------------------------------------------------------------------
# AC2 -- the concise summary carries exactly the declared keys, and no widget.
# ---------------------------------------------------------------------------


def test_the_summary_carries_exactly_the_ten_declared_top_level_keys():
    """NINE became TEN on 2026-08-07, deliberately: `ai_path` joined the summary.

    The render tool's widget used to wait on an `ai_path` key the summary
    documented but never carried. The key holds the IDENTITY only -- an id or
    the exact literal `No AI path`, a handful of bytes, so the model-channel
    budget is untouched. The WALK itself stays out of the model channel: it
    travels in `_meta["toorow.result"]["ai_path_walk"]`, because a projection
    with judged branches does not fit 4 KiB and the widget is its only reader.
    """
    assert set(_summary()) == {
        "schema_version",
        "result",
        "outcome",
        "ai_path",
        "summary",
        "provenance",
        "freshness",
        "evidence",
        "pagination",
        "deep_link",
    }


def test_the_ai_path_key_carries_the_identity_and_nothing_else():
    """An id or the exact literal, model-visible; the walk is app-channel data."""
    assert _summary()["ai_path"] == "No AI path"
    assert _summary(result=_result(ai_path="aip_EXAMPLE"))["ai_path"] == "aip_EXAMPLE"


def test_the_identity_block_pins_the_four_story_501_identifiers():
    identity = _summary()["result"]
    assert set(identity) == {
        "result_id",
        "query_spec_version_id",
        "semantic_view_version_id",
        "content_hash",
    }
    assert identity["content_hash"] == _HASH


def test_no_app_resource_field_appears_anywhere_in_the_model_channel():
    """A model-visible field advertising an app resource is the wrong channel twice."""
    serialized = json.dumps(_summary())
    assert "app_resource" not in serialized
    assert "resourceUri" not in serialized
    assert "ui://" not in serialized


def test_the_ai_path_literal_is_exact_and_never_absent():
    text = adapter.compact_answer(_result(), {"semantic_view_version_id": "x"}, {"state": "y"})
    assert "Parcours IA : No AI path." in text
    # And an actual AI Path id passes through unchanged.
    text2 = adapter.compact_answer(
        _result(ai_path="aip_EXAMPLE"), {"semantic_view_version_id": "x"}, {"state": "y"}
    )
    assert "Parcours IA : aip_EXAMPLE." in text2


# ---------------------------------------------------------------------------
# Every outcome produces an inspectable answer -- including the empty ones.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["success", "empty", "degraded", "refused", "unavailable"])
def test_every_terminal_outcome_produces_a_compact_inspectable_answer(outcome, wire):
    """Through `_answer`, so the payload GUARD is part of what is being tested.

    This test used to call `build_summary` and `compact_answer` directly, past
    `enforce_model_channel`, and its `_payload()` helper always injected a
    three-field schema. A zero-row Result carries `{"fields": []}` and no rows,
    and on that shape the guard REFUSED the answer for `empty`, `refused` and
    `unavailable` -- every Result this environment can produce (gate G5). Driving
    `_answer` is what makes the guard part of the assertion instead of something
    the test walks around.
    """
    from core.query_execution import OUTCOMES

    assert outcome in OUTCOMES
    empty = outcome in {"empty", "refused", "unavailable"}
    row_count = 0 if empty else 3
    wire(
        [
            _result_row(outcome=outcome, row_count=row_count),
            (
                _HASH,
                _NO_FIELD_SCHEMA if empty else _SCHEMA,
                {},
                [] if empty else [{"day": "2026-07-01", "clicks": 1}],
            ),
            ("svv_EXAMPLE",),
        ]
    )

    text, summary, _meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")

    assert summary["outcome"] == outcome
    assert f"issue : {outcome}" in text
    assert len(text.splitlines()) <= MODEL_CHANNEL_MAX_TEXT_LINES
    # Never deep-link-only, on EVERY outcome -- including the three that produce
    # no row and therefore no column to quote.
    assert summary["deep_link"]
    assert summary["evidence"], f"{outcome} answered with a deep link and no evidence"
    assert all(set(f) == {"column", "first_value"} for f in summary["evidence"])


@pytest.mark.parametrize(
    "outcome,expected_second",
    [
        ("empty", "row_count"),
        ("refused", "counts_unknown_reason"),
        ("unavailable", "counts_unknown_reason"),
    ],
)
def test_a_result_with_no_column_to_quote_carries_the_outcome_as_its_evidence(
    outcome, expected_second, wire
):
    """The zero-row case carries honest evidence rather than bypassing the rule.

    What a host can still check when no column exists is the outcome itself and
    either the stated reason the counts are unknown (`refused` / `unavailable`)
    or the exact count (`empty` knows it is zero). Both travel in the same
    `{column, first_value}` shape as a real figure, so a reader does not meet a
    second evidence vocabulary in the case that matters most.
    """
    wire(
        [
            _result_row(outcome=outcome, row_count=0),
            (_HASH, _NO_FIELD_SCHEMA, {}, []),
            ("svv_EXAMPLE",),
        ]
    )
    _text, summary, _meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")

    evidence = summary["evidence"]
    assert [f["column"] for f in evidence] == ["outcome", expected_second]
    assert evidence[0]["first_value"] == outcome
    assert evidence[1]["first_value"] == summary["summary"][expected_second]


def test_evidence_falls_back_to_the_row_itself_when_the_schema_declares_no_field():
    """Rows without a declared schema: name the row's own keys, do not say `unavailable`."""
    figures = adapter.bounded_evidence(
        [{"day": "2026-07-01", "clicks": 7}], {"fields": []}, outcome="success", row_count=1
    )
    assert figures == [
        {"column": "day", "first_value": "2026-07-01"},
        {"column": "clicks", "first_value": 7},
    ]


@pytest.mark.parametrize("outcome", ["refused", "unavailable"])
def test_counts_are_none_with_a_reason_never_zero_when_the_outcome_cannot_know(outcome):
    summary = _summary(result=_result(outcome=outcome, row_count=0), payload=_payload(rows=[]))
    assert summary["summary"]["row_count"] is None
    assert summary["summary"]["cell_count"] is None
    assert outcome in summary["summary"]["counts_unknown_reason"]
    assert summary["pagination"]["total_rows"] is None


@pytest.mark.parametrize("outcome", ["success", "degraded"])
def test_counts_are_exact_when_the_outcome_knows_them(outcome):
    summary = _summary(result=_result(outcome=outcome, row_count=3))
    assert summary["summary"]["row_count"] == 3
    assert summary["summary"]["counts_unknown_reason"] is None


def test_an_empty_result_reports_zero_because_zero_is_the_true_answer():
    """`empty` KNOWS the count is zero. Only `refused`/`unavailable` do not know."""
    summary = _summary(result=_result(outcome="empty", row_count=0), payload=_payload(rows=[]))
    assert summary["summary"]["row_count"] == 0
    assert summary["summary"]["counts_unknown_reason"] is None


# ---------------------------------------------------------------------------
# Provenance and freshness project what the manifest HOLDS, and say `unavailable`.
# ---------------------------------------------------------------------------


def test_a_silent_manifest_yields_unavailable_not_an_empty_object():
    """Writing `{}` or a plausible default is the deferred-evidence placeholder."""
    provenance = adapter.project_provenance({})
    assert provenance["lineage"] == "unavailable"
    assert provenance["data_quality"] == "unavailable"
    freshness = adapter.project_freshness({})
    assert freshness["state"] == "unavailable"
    assert freshness["stale_since_evaluated"] is False


def test_what_the_manifest_does_hold_is_projected_faithfully():
    manifest = {
        "semantic_view_version_id": "svv_EXAMPLE",
        "missing_link": "datastream_output_versions",
        "freshness": {"state": "stale", "as_of": "2026-07-30", "stale_since_evaluated": True},
    }
    provenance = adapter.project_provenance(manifest)
    assert provenance["semantic_view_version_id"] == "svv_EXAMPLE"
    assert provenance["missing_link"] == "datastream_output_versions"
    freshness = adapter.project_freshness(manifest)
    assert freshness["state"] == "stale"
    assert freshness["stale_since_evaluated"] is True


def test_the_projection_reads_the_keys_the_writer_actually_spells():
    """data.md [9]: `lineage` and `data_quality` were read under names nobody wrote.

    The manifest below is the shape `query_execution.capture_evidence` really
    returns. Read for `manifest["lineage"]` and `manifest["data_quality"]` -- keys
    written nowhere in this repository -- it projected `unavailable` over a
    complete chain.
    """
    manifest = {
        "semantic_view_version_id": "svv_EXAMPLE",
        "datastream_output_version_id": "dsov_EXAMPLE",
        "provenance": {
            "source_system": "google_ads",
            "datastream_id": "ds_EXAMPLE",
            "mapping_version_id": "dmv_EXAMPLE",
            "relation": "main_marts.spend_daily",
            "pull_id": "dse_EXAMPLE",
            "publication_log_id": None,
            "values": [{"member_id": "sc_1"}, {"member_id": "sc_2"}],
        },
        "dq_evaluation_ids": ["cc_1", "cc_2"],
        "dq_unavailable_reason": None,
    }
    provenance = adapter.project_provenance(manifest)

    assert provenance["datastream_output_version_id"] == "dsov_EXAMPLE"
    assert provenance["lineage"] == {
        "source_system": "google_ads",
        "datastream_id": "ds_EXAMPLE",
        "mapping_version_id": "dmv_EXAMPLE",
        "relation": "main_marts.spend_daily",
        "pull_id": "dse_EXAMPLE",
        "member_count": 2,
    }
    assert provenance["data_quality"]["evaluation_ids"] == ["cc_1", "cc_2"]
    assert provenance["lineage_unavailable_reason"] is None


def test_a_result_frozen_before_the_writer_says_so_instead_of_shrugging():
    """An old manifest keeps `unavailable`, and gains the reason it is one."""
    manifest = {
        "semantic_view_version_id": "svv_EXAMPLE",
        "provenance": {"datastream_id": "ds_EXAMPLE", "values": []},
    }
    provenance = adapter.project_provenance(manifest)

    assert provenance["datastream_output_version_id"] == "unavailable"
    assert provenance["lineage_unavailable_reason"] == (
        "published before the lineage was recorded"
    )
    # What it DOES hold is still projected: an old Result is not a blank one.
    assert provenance["lineage"]["datastream_id"] == "ds_EXAMPLE"


def test_an_execution_that_read_nothing_is_not_called_old():
    """No evidence at all is a different fact, and `missing_link` already says it."""
    manifest = {"missing_link": "datastream_output_versions"}
    provenance = adapter.project_provenance(manifest)

    assert provenance["lineage"] == "unavailable"
    assert provenance["data_quality"] == "unavailable"
    assert provenance["lineage_unavailable_reason"] is None
    assert provenance["missing_link"] == "datastream_output_versions"


def test_an_unreadable_dq_owner_never_reads_as_nothing_open():
    """`capture_evidence` keeps the two apart; the projection must not merge them."""
    unreadable = adapter.project_provenance(
        {"dq_evaluation_ids": [], "dq_unavailable_reason": "relation does not exist"}
    )
    healthy = adapter.project_provenance(
        {"dq_evaluation_ids": [], "dq_unavailable_reason": None}
    )

    assert unreadable["data_quality"]["unavailable_reason"] == "relation does not exist"
    assert healthy["data_quality"]["unavailable_reason"] is None
    assert healthy["data_quality"]["evaluation_ids"] == []


def test_the_projected_evidence_stays_inside_the_model_channel_budget():
    """50 open cases and a long DQ error must not cost the answer its budget."""
    manifest = {
        "provenance": {
            "source_system": "google_ads",
            "datastream_id": "ds_EXAMPLE",
            "mapping_version_id": "dmv_EXAMPLE",
            "relation": "main_marts.spend_daily",
            "pull_id": "dse_EXAMPLE",
            "values": [{"member_id": f"sc_{index}"} for index in range(200)],
        },
        "dq_evaluation_ids": [f"cc_{index}" for index in range(50)],
        "dq_unavailable_reason": "x" * 5000,
    }
    provenance = adapter.project_provenance(manifest)

    assert len(provenance["data_quality"]["evaluation_ids"]) == 12
    assert provenance["data_quality"]["evaluation_ids_withheld"] == 38
    assert len(provenance["data_quality"]["unavailable_reason"]) == 200
    # The tuples themselves stay in the manifest, where a reader can page them.
    assert provenance["lineage"]["member_count"] == 200
    assert len(json.dumps(provenance).encode("utf-8")) < 1024


def test_a_missing_link_becomes_a_stated_limitation_in_the_text():
    manifest = {"missing_link": "datastream_output_versions"}
    text = adapter.compact_answer(
        _result(outcome="unavailable", row_count=0),
        adapter.project_provenance(manifest),
        adapter.project_freshness(manifest),
    )
    assert "Limitation" in text
    assert "datastream_output_versions" in text


# ---------------------------------------------------------------------------
# AC9 -- a host with no UI gets a useful answer, and never deep-link-only.
# ---------------------------------------------------------------------------


def test_a_no_ui_host_receives_bounded_evidence_pagination_and_an_exact_deep_link():
    summary = _summary()
    assert summary["evidence"], "bounded evidence, never zero evidence"
    assert set(summary["pagination"]) == {
        "total_rows",
        "returned_rows",
        "has_more",
        "how_to_page",
    }
    link = summary["deep_link"]
    assert link["requires_authenticated_session"] is True
    assert link["object_type"] == "result"
    assert link["object_id"] == "qr_EXAMPLE"
    assert link["organization_id"] == "org_EXAMPLE"
    assert link["project_id"] == "proj_EXAMPLE"
    assert link["tab"]


def test_the_deep_link_is_a_structured_owner_reference_not_a_url():
    """Handing a host a hard-coded path would freeze a route this module does not own."""
    link = adapter.result_deep_link("org_EXAMPLE", "proj_EXAMPLE", "qr_EXAMPLE")
    serialized = json.dumps(link)
    assert "http://" not in serialized and "https://" not in serialized
    assert link["owner_reference"]["object_id"] == "qr_EXAMPLE"


def test_the_evidence_is_bounded_not_the_dataset():
    from core.model_channel import MAX_BOUNDED_EVIDENCE_FIGURES

    wide = {"fields": [{"name": f"c{i}"} for i in range(200)]}
    figures = adapter.bounded_evidence([{f"c{i}": i for i in range(200)}], wide)
    assert len(figures) == MAX_BOUNDED_EVIDENCE_FIGURES


def test_the_pagination_block_tells_a_no_ui_host_how_to_ask_for_more():
    how = _summary()["pagination"]["how_to_page"]
    assert "app_read_result_slice" in how
    assert "500" in how


# ---------------------------------------------------------------------------
# AC4 -- the whole summary fits the model channel, on a WIDE Result.
# ---------------------------------------------------------------------------


def test_the_summary_of_a_wide_result_stays_inside_the_model_channel_budget():
    from core.model_channel import enforce_model_channel, serialized_bytes

    wide_schema = {"fields": [{"name": f"column_number_{i}"} for i in range(300)]}
    rows = [{f"column_number_{i}": i for i in range(300)} for _ in range(1000)]
    summary = _summary(
        result=_result(row_count=1000, truncated=True),
        payload={
            "content_hash": _HASH,
            "result_schema": wide_schema,
            "manifest": {},
            "rows_chunk": rows,
        },
    )
    measured = serialized_bytes(summary)
    assert measured <= MODEL_CHANNEL_MAX_BYTES, measured
    enforce_model_channel("analyze_result", "one line", summary)


# ---------------------------------------------------------------------------
# AC10 -- the render tool mirrors the data tool byte for byte.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


def _result_row(outcome="success", row_count=3, truncated=False):
    """The `app.query_results` tuple `_load_result` reads, in column order."""
    return (
        "qr_EXAMPLE", "qea_EXAMPLE", "qsv_EXAMPLE", outcome, None, "No AI path",
        _HASH, row_count, row_count * 2, row_count * 40, truncated, None, None, None,
    )


class _Conn:
    """Answers, in order, the SELECTs `_answer` performs, then swallows the INSERT."""

    def __init__(self, responses=None):
        self.responses = (
            list(responses)
            if responses is not None
            else [
                _result_row(),
                (_HASH, _SCHEMA, {}, [{"day": "2026-07-01", "clicks": 1}]),
                ("svv_EXAMPLE",),
            ]
        )

    def cursor(self):
        return _Cursor([self.responses.pop(0)] if self.responses else [])

    def commit(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    """Return a function wiring the adapter's seams over a chosen set of DB rows.

    The grant is NOT stubbed. `result_app_grants.issue_handle` runs for real
    against the fake connection, so the `GrantRefused` a fieldless Result
    genuinely raises is the one `_answer` meets -- a stub that always returns a
    handle would hide exactly the branch this suite has to exercise.
    """
    import contextlib

    from core import query_specs_api

    class _Decision:
        allowed = True
        org_id = "org_EXAMPLE"

    def _wire(responses=None):
        monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
        monkeypatch.setattr(adapter, "_guard_project_view", lambda p, i: _Decision())

        @contextlib.contextmanager
        def fake_connection(identity):
            yield _Conn(responses)

        monkeypatch.setattr(query_specs_api, "analyze_connection", fake_connection)

    return _wire


@pytest.fixture
def wired(wire):
    """The default wiring: one `success` Result with two columns and one row."""
    wire()


def test_the_render_tool_returns_a_byte_identical_summary_to_the_data_tool(wired):
    _t1, data_summary, _m1 = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    _t2, render_summary, _m2 = adapter._answer(
        "proj_EXAMPLE", "qr_EXAMPLE", adapter.RENDER_TOOL_NAME
    )
    assert json.dumps(render_summary, sort_keys=True) == json.dumps(data_summary, sort_keys=True)


def test_the_analytics_door_registers_bounded_discovery_and_confirmed_composition(
    monkeypatch,
):
    from core import analyze_feedback, mcp_profiles

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self, handler, **declaration):
            self.tools[declaration.get("name") or handler.__name__] = declaration

    monkeypatch.setattr(analyze_feedback, "feedback_context_secret", lambda: b"x" * 32)
    monkeypatch.setattr(adapter, "_visualization_runtime_available", lambda: False)
    mcp_profiles.reset_registry_for_tests()
    mcp = FakeMCP()

    adapter.register(mcp)

    assert mcp.tools["discover_analyze_matches"]["meta"] == {
        "profile": "insights",
        "effect": "read",
        "data_class": "operational",
        "confirmation_mode": "none",
    }
    assert mcp.tools["compose_analyze_pivot"]["meta"] == {
        "profile": "operations",
        "effect": "prepare",
        "data_class": "operational",
        "confirmation_mode": "server",
    }


def test_the_answer_puts_the_rows_in_meta_and_not_in_the_model_channel(wired):
    _text, summary, meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    assert list(meta) == [RESULT_META_KEY]
    assert meta[RESULT_META_KEY]["rows"] == [{"day": "2026-07-01", "clicks": 1}]
    # No row ARRAY reaches the model channel. What does reach it is bounded
    # evidence -- at most 12 first-values, which AC9 REQUIRES so a host with no UI
    # is not left with a deep link and nothing to check. Counts named `*_rows` are
    # not rows either; they are the pagination affordance.
    serialized = json.dumps(summary)
    assert '"rows":' not in serialized
    assert len(summary["evidence"]) <= 12
    assert isinstance(summary["evidence"][0], dict)
    assert set(summary["evidence"][0]) == {"column", "first_value"}


def _wire_render_answer(monkeypatch, *, result=None, payload=None, spec=None):
    import contextlib

    from core import feedback_review, query_specs_api, render_app_payload, visualization_specs

    result = result or _result(row_count=1)
    payload = payload or _payload()
    spec = spec or {
        "id": "vsv_EXAMPLE",
        "query_spec_version_id": "qsv_EXAMPLE",
        "spec_contract_version": "visualization-spec.v1",
        "schema_version": 1,
        "family": "bar",
        "spec": {
            "spec_contract_version": "visualization-spec.v1",
            "schema_version": 1,
            "family": "bar",
        },
    }
    base_meta = {
        RESULT_META_KEY: {
            "projection_size": "small",
            "content_hash": payload["content_hash"],
            "rows": payload["rows_chunk"],
        }
    }
    def answer(*args, **kwargs):
        finalized = kwargs["meta_finalizer"](
            conn=object(),
            identity="owner@example.com",
            org_id="org_EXAMPLE",
            result=result,
            payload=payload,
            meta=base_meta,
        )
        return "same text", {"schema_version": 1, "marker": "same"}, finalized

    monkeypatch.setattr(adapter, "_answer", answer)
    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        feedback_review,
        "record_feedback_eligibility",
        lambda *_args, **_kwargs: {"schema_version": "feedback-eligibility.v1"},
        raising=False,
    )

    class Decision:
        org_id = "org_EXAMPLE"

    monkeypatch.setattr(adapter, "_guard_project_view", lambda *args: Decision())

    @contextlib.contextmanager
    def connection(identity):
        yield object()

    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(adapter, "_load_result", lambda *args, **kwargs: result)
    monkeypatch.setattr(adapter, "_load_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        visualization_specs,
        "load_visualization_spec_version",
        lambda *args, **kwargs: spec,
    )
    monkeypatch.setattr(
        render_app_payload,
        "load_runtime_manifest",
        lambda: {"manifest": "exact-served-build"},
    )
    monkeypatch.setattr(
        render_app_payload,
        "resolve_runtime_pins",
        lambda *args, **kwargs: {
            "runtime_build": "runtime@1",
            "renderer_build": "bar@1",
            "theme_version": "theme@1",
            "formatter_version": "formatters@1",
        },
    )
    return base_meta


def test_render_answer_requires_a_pinned_spec_before_reading_the_result(monkeypatch):
    called = False

    def answer(*args):
        nonlocal called
        called = True

    monkeypatch.setattr(adapter, "_answer", answer)
    with pytest.raises(Exception) as excinfo:
        adapter._render_answer("proj_EXAMPLE", "qr_EXAMPLE", " ")
    assert json.loads(str(excinfo.value))["code"] == "missing_param"
    assert called is False


def test_render_answer_adds_one_complete_typed_payload_without_changing_summary(monkeypatch):
    base_meta = _wire_render_answer(monkeypatch)
    text, summary, meta = adapter._render_answer(
        "proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE"
    )
    assert text == "same text"
    assert summary == {"schema_version": 1, "marker": "same"}
    assert meta[RESULT_META_KEY] is base_meta[RESULT_META_KEY]
    payload = meta["toorow.app_payload"]
    assert set(payload) == {"schema_version", "kind", "render_input"}
    assert set(payload["render_input"]) == {"result", "spec", "pins", "profile", "display"}
    assert payload["render_input"]["result"]["rows"] == [
        {"day": "2026-07-01", "clicks": 1}
    ]
    assert payload["render_input"]["profile"] == "mcp-inline"


def test_render_answer_uses_the_bounded_initial_projection_for_a_large_result(monkeypatch):
    rows = [{"day": f"2026-07-{(i % 28) + 1:02d}", "clicks": i} for i in range(205)]
    result = _result(row_count=len(rows), truncated=False)
    payload = _payload(rows=rows)
    base_meta = _wire_render_answer(monkeypatch, result=result, payload=payload)
    result_meta = base_meta[RESULT_META_KEY]
    result_meta.clear()
    result_meta.update(
        {
            "result_id": result["id"],
            "content_hash": _HASH,
            "projection_size": "large",
            "schema": _SCHEMA,
            "allowed_columns": ["day", "clicks"],
            "row_count": len(rows),
            "truncated": False,
            "result_handle": "rh_" + "0" * 26,
            "initial_projection": rows[:200],
            "next_cursor": "cursor-next",
        }
    )

    text, summary, meta = adapter._render_answer(
        "proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE"
    )

    assert text == "same text"
    assert summary == {"schema_version": 1, "marker": "same"}
    render_result = meta["toorow.app_payload"]["render_input"]["result"]
    assert render_result["rows"] == rows[:200]
    assert render_result["row_count"] == len(rows)
    assert render_result["truncated"] is False
    assert meta[RESULT_META_KEY] is result_meta


def test_render_answer_refuses_a_spec_for_another_query_spec(monkeypatch):
    spec = {
        "id": "vsv_EXAMPLE",
        "query_spec_version_id": "qsv_OTHER",
        "spec_contract_version": "visualization-spec.v1",
        "schema_version": 1,
        "family": "bar",
        "spec": {},
    }
    _wire_render_answer(monkeypatch, spec=spec)
    with pytest.raises(Exception) as excinfo:
        adapter._render_answer("proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE")
    assert json.loads(str(excinfo.value))["code"] == "incompatible_visualization_spec"


def test_render_answer_refuses_an_incomplete_result_projection(monkeypatch):
    result = _result(row_count=2)
    _wire_render_answer(monkeypatch, result=result)
    with pytest.raises(Exception) as excinfo:
        adapter._render_answer("proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE")
    assert json.loads(str(excinfo.value))["code"] == "render_result_not_complete"


def test_render_answer_refuses_any_hash_divergence(monkeypatch):
    payload = _payload()
    payload["content_hash"] = "b" * 64
    _wire_render_answer(monkeypatch, payload=payload)
    with pytest.raises(Exception) as excinfo:
        adapter._render_answer("proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE")
    assert json.loads(str(excinfo.value))["code"] == "render_result_identity_mismatch"


def test_answer_refuses_result_payload_hash_mismatch_before_issuing_a_grant(
    monkeypatch, wire
):
    from core import result_app_grants

    payload_hash = "b" * 64
    wire(
        [
            _result_row(),
            (payload_hash, _SCHEMA, {}, [{"day": "2026-07-01", "clicks": 1}]),
            ("svv_EXAMPLE",),
        ]
    )
    issued = False

    def issue(*args, **kwargs):
        nonlocal issued
        issued = True
        return {
            "handle_id": "rh_" + "0" * 26,
            "allowed_columns": ["day", "clicks"],
        }

    monkeypatch.setattr(result_app_grants, "issue_handle", issue)

    with pytest.raises(Exception) as excinfo:
        adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")

    assert json.loads(str(excinfo.value))["code"] == "render_result_identity_mismatch"
    assert issued is False


def test_answer_does_not_commit_a_grant_when_render_finalization_fails(monkeypatch):
    import contextlib

    from core import query_specs_api, result_app_grants

    class Decision:
        org_id = "org_EXAMPLE"

    conn = _Conn()
    committed = False

    def commit():
        nonlocal committed
        committed = True

    conn.commit = commit

    @contextlib.contextmanager
    def connection(_identity):
        yield conn

    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(adapter, "_guard_project_view", lambda *args: Decision())
    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(
        result_app_grants,
        "issue_handle",
        lambda *args, **kwargs: {
            "handle_id": "rh_" + "0" * 26,
            "allowed_columns": ["day", "clicks"],
        },
    )

    def refuse(**_context):
        raise adapter._tool_error(
            "incompatible_visualization_spec", "Choose a compatible visual."
        )

    with pytest.raises(Exception):
        adapter._answer(
            "proj_EXAMPLE",
            "qr_EXAMPLE",
            adapter.RENDER_TOOL_NAME,
            meta_finalizer=refuse,
        )

    assert committed is False


def test_answer_does_not_commit_when_model_channel_enforcement_fails(monkeypatch):
    import contextlib

    from core import model_channel, query_specs_api, result_app_grants

    class Decision:
        org_id = "org_EXAMPLE"

    conn = _Conn()
    committed = False

    def commit():
        nonlocal committed
        committed = True

    conn.commit = commit

    @contextlib.contextmanager
    def connection(_identity):
        yield conn

    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(adapter, "_guard_project_view", lambda *args: Decision())
    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(
        result_app_grants,
        "issue_handle",
        lambda *args, **kwargs: {
            "handle_id": "rh_" + "0" * 26,
            "allowed_columns": ["day", "clicks"],
        },
    )
    monkeypatch.setattr(
        model_channel,
        "enforce_model_channel",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("model channel refused")),
    )

    with pytest.raises(ValueError, match="model channel refused"):
        adapter._answer(
            "proj_EXAMPLE",
            "qr_EXAMPLE",
            adapter.RENDER_TOOL_NAME,
            meta_finalizer=lambda **context: context["meta"],
        )

    assert committed is False


def test_render_answer_compares_complete_rows_to_the_frozen_payload(monkeypatch):
    payload = _payload(rows=[{"day": "2026-07-01", "clicks": 1}, "not-a-row"])
    result = _result(row_count=1)
    base_meta = _wire_render_answer(monkeypatch, result=result, payload=payload)
    base_meta[RESULT_META_KEY]["rows"] = [{"day": "2026-07-01", "clicks": 1}]
    with pytest.raises(Exception) as excinfo:
        adapter._render_answer("proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE")
    assert json.loads(str(excinfo.value))["code"] == "render_result_not_complete"


def test_render_answer_projects_only_safe_result_manifest_fields(monkeypatch):
    payload = _payload(
        manifest={
            "grain": "day",
            "freshness": {"state": "fresh"},
            "query_sql": "SELECT * FROM private",
            "relation": "warehouse.private.table",
        }
    )
    _wire_render_answer(monkeypatch, payload=payload)
    _text, _summary, meta = adapter._render_answer(
        "proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE"
    )
    projected = meta["toorow.app_payload"]["render_input"]["result"]["manifest"]
    assert projected == {"grain": "day", "freshness": {"state": "fresh"}}


def test_render_answer_delivers_the_server_owned_pivot_beside_the_closed_render_input(
    monkeypatch,
):
    _wire_render_answer(monkeypatch)
    pivot = {
        "schema_version": "analyze-pivot-render.v1",
        "result_id": "qr_EXAMPLE",
        "content_hash": _HASH,
        "matrix": {
            "result_id": "qr_EXAMPLE",
            "content_hash": _HASH,
            "row_fields": ["day"],
            "column_fields": [],
            "value_fields": [{"name": "clicks"}],
            "row_keys": [["2026-07-01"]],
            "column_keys": [[]],
            "cells": [],
            "bounds": {"response_bytes": 300},
        },
    }
    monkeypatch.setattr(adapter, "_pivot_render_sidecar", lambda *_args, **_kwargs: pivot)

    _text, _summary, meta = adapter._render_answer(
        "proj_EXAMPLE", "qr_EXAMPLE", "vsv_EXAMPLE"
    )

    assert meta["toorow.pivot"] == pivot
    assert "pivot" not in meta["toorow.app_payload"]["render_input"]


def test_the_answer_carries_the_opaque_handle_and_no_claim_inside_it(wired):
    _text, _summary, meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    handle = meta[RESULT_META_KEY]["result_handle"]
    assert handle.startswith("rh_")
    # Decoding it yields nothing: no project, no result, no column, no expiry.
    for claim in ("proj_EXAMPLE", "qr_EXAMPLE", "day", "clicks", _HASH):
        assert claim not in handle


def test_a_small_wide_result_keeps_its_inline_columns_when_no_slice_grant_is_possible(
    monkeypatch, wire
):
    from core import result_app_grants

    columns = [f"column_{index}" for index in range(101)]
    schema = {"fields": [{"name": column} for column in columns]}
    row = {column: index for index, column in enumerate(columns)}
    wire([_result_row(row_count=1), (_HASH, schema, {}, [row]), ("svv_EXAMPLE",)])

    def refuse_wide_schema(*args, **kwargs):
        raise result_app_grants.GrantRefused(
            "result_schema_too_wide", "The Result schema is too wide for app slicing."
        )

    monkeypatch.setattr(result_app_grants, "issue_handle", refuse_wide_schema)
    _text, _summary, meta = adapter._answer(
        "proj_EXAMPLE", "qr_EXAMPLE", "analyze_result"
    )

    result_meta = meta[RESULT_META_KEY]
    assert result_meta["projection_size"] == "small"
    assert result_meta["rows"] == [row]
    assert result_meta["result_handle"] is None


def test_a_missing_project_or_result_id_is_refused_before_any_work(wired):
    for args in (("", "qr_EXAMPLE"), ("proj_EXAMPLE", "")):
        with pytest.raises(Exception) as excinfo:
            adapter._answer(*args, "analyze_result")
        assert json.loads(str(excinfo.value))["code"] == "missing_param"


def test_denied_and_absent_are_indistinguishable(monkeypatch):
    """Both raise the ONE envelope, from the ONE call site."""
    import contextlib

    from core import query_specs_api

    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")

    class _Denied:
        allowed = False
        org_id = None

    envelopes = []

    # 1) Denied by the access guard.
    import core.project_access as project_access

    monkeypatch.setattr(project_access, "resolve_strict_resource_access", lambda *a, **k: _Denied())

    @contextlib.contextmanager
    def fake_connection(identity):
        yield _Conn()

    monkeypatch.setattr(query_specs_api, "analyze_connection", fake_connection)
    with pytest.raises(Exception) as excinfo:
        adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    envelopes.append(str(excinfo.value))

    # 2) Authorized, but the Result does not exist.
    class _Allowed:
        allowed = True
        org_id = "org_EXAMPLE"

    monkeypatch.setattr(
        project_access, "resolve_strict_resource_access", lambda *a, **k: _Allowed()
    )

    class _EmptyConn(_Conn):
        def __init__(self):
            self.responses = []

    @contextlib.contextmanager
    def empty_connection(identity):
        yield _EmptyConn()

    monkeypatch.setattr(query_specs_api, "analyze_connection", empty_connection)
    with pytest.raises(Exception) as excinfo:
        adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    envelopes.append(str(excinfo.value))

    assert envelopes[0] == envelopes[1] == json.dumps(
        {"code": "not_found", "message": "Not found"}
    )


# ---------------------------------------------------------------------------
# The walk travels in the app channel, and never takes the answer down.
#
# The summary carries the path's IDENTITY (`ai_path`); the walk itself --
# header, ordered steps, judged branches (`detail`) -- travels in
# `_meta["toorow.result"]["ai_path_walk"]`, projected by the owner's ONE wire
# shape (`core.ai_paths.wire_step_projection`). A path that cannot be read
# degrades to identity plus reason: the render tool's first job is the Result.
# ---------------------------------------------------------------------------


def _path_result_row():
    """The `app.query_results` tuple for a Result WITH a path (id, not literal)."""
    return (
        "qr_EXAMPLE", "qea_EXAMPLE", "qsv_EXAMPLE", "success", "aip_EXAMPLE", None,
        _HASH, 3, 6, 120, False, None, None, None,
    )


def _path_header_row():
    """The `app.ai_paths` tuple `load_path` reads, in its SELECT's column order."""
    from datetime import datetime, timezone

    return (
        "aip_EXAMPLE", "org_EXAMPLE", "proj_EXAMPLE", "finalized", "succeeded",
        "trace_EXAMPLE", "person_EXAMPLE", None,
        datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 8, 0, 9, tzinfo=timezone.utc),
        None, None, None, None, "b" * 64,
    )


def _path_step_rows():
    """The `app.ai_path_steps` tuples, in `_STEP_COLUMNS` order, ordinal 0 first."""
    from datetime import datetime, timezone

    observed_at = datetime(2026, 7, 31, 8, 0, 1, tzinfo=timezone.utc)
    return [
        (
            "aps_1", 0, "tool_call", "analyze", "semantic-view", "sv_1", None, None,
            None, "get_daily_report", "succeeded", None, observed_at,
            {"missing_context": ["required_but_missing", "unsafe"]},
        ),
        (
            "aps_2", 1, "tool_call", None, None, None, None, None, None,
            "health", "succeeded", None, observed_at, None,
        ),
    ]


class _WalkCursor:
    """One cursor scripted for `load_path`: the header row, then the step rows."""

    def __init__(self, header_row, step_rows, *, boom=False):
        self._header_row = header_row
        self._step_rows = step_rows
        self._boom = boom

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError("ai path store down")

    def fetchone(self):
        return self._header_row

    def fetchall(self):
        return self._step_rows


_DEFAULT = object()


def _wire_walk(monkeypatch, *, header_row=_DEFAULT, step_rows=None, boom=False):
    """The standard three answers, then one cursor for `load_path`, then none.

    The walk read happens BEFORE the grant (`_answer` order), so the empty
    cursors behind the scripted one keep `issue_handle` on its real code path,
    exactly as the `wire` fixture does. `header_row=None` is a real input (the
    path is not found), so the default travels by sentinel, not by None.
    """
    import contextlib

    from core import query_specs_api

    if header_row is _DEFAULT and not boom:
        header_row = _path_header_row()
    if step_rows is None:
        step_rows = _path_step_rows()

    class _Decision:
        allowed = True
        org_id = "org_EXAMPLE"

    class _WalkConn:
        def __init__(self):
            self._responses = [
                _path_result_row(),
                (_HASH, _SCHEMA, {}, [{"day": "2026-07-01", "clicks": 1}]),
                ("svv_EXAMPLE",),
            ]
            self._walk_served = False

        def cursor(self):
            if self._responses:
                return _Cursor([self._responses.pop(0)])
            if not self._walk_served:
                self._walk_served = True
                return _WalkCursor(header_row, step_rows, boom=boom)
            return _Cursor([])

        def commit(self):
            pass

    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(adapter, "_guard_project_view", lambda p, i: _Decision())

    @contextlib.contextmanager
    def fake_connection(identity):
        yield _WalkConn()

    monkeypatch.setattr(query_specs_api, "analyze_connection", fake_connection)


def test_a_result_with_a_path_carries_the_walk_in_the_app_channel(monkeypatch):
    _wire_walk(monkeypatch)
    _text, summary, meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", adapter.RENDER_TOOL_NAME)

    # The identity is model-visible; the walk is not (the 4 KiB budget is for
    # answers, and a walk with judged branches is not one).
    assert summary["ai_path"] == "aip_EXAMPLE"
    assert "aps_1" not in json.dumps(summary)

    walk = meta[RESULT_META_KEY]["ai_path_walk"]
    assert walk["schema_version"] == "observed-ai-path.v1"
    assert walk["state"] == "completed"
    assert walk["path_id"] == "aip_EXAMPLE"
    assert walk["lifecycle"] == "finalized"
    assert walk["outcome"] == "succeeded"
    assert set(walk) == {"schema_version", "state", "path_id", "lifecycle", "outcome", "steps"}
    # Policy assessment stays owner-side; it is not part of the safe surface contract.
    assert "assessment" not in walk
    # The Workbench consumes these same keys through the canonical projector.
    assert set(walk["steps"][0]) == {
        "ordinal", "step_kind", "outcome", "observed_at", "tool_name", "owner",
        "missing_context",
    }
    # Aucune epingle sur ce pas : `skill` est ABSENT de valeur, pas un
    # dictionnaire vide qui se lirait « resolu, et il n'y avait rien ».
    assert "skill" not in walk["steps"][0]
    assert "evidence_record_id" not in walk["steps"][0]
    assert [s["ordinal"] for s in walk["steps"]] == [0, 1]
    assert walk["steps"][0]["tool_name"] == "get_daily_report"
    assert walk["steps"][0]["missing_context"] == ["required_but_missing"]
    encoded = json.dumps(walk, sort_keys=True)
    for forbidden in ("actor", "trace", "policy", "assessment", "detail", "unsafe"):
        assert forbidden not in encoded


def test_the_walk_is_identical_in_the_data_tool_and_the_render_tool(monkeypatch):
    """AC10's rule, extended to the app channel: ONE composition, two tools."""
    _wire_walk(monkeypatch)
    _t1, summary1, meta1 = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    _t2, summary2, meta2 = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", adapter.RENDER_TOOL_NAME)
    assert json.dumps(summary1, sort_keys=True) == json.dumps(summary2, sort_keys=True)
    # The handles differ (each call issues its own grant); the walk does not.
    assert meta1[RESULT_META_KEY]["ai_path_walk"] == meta2[RESULT_META_KEY]["ai_path_walk"]


def test_a_path_that_cannot_be_read_degrades_to_identity_and_reason(monkeypatch):
    _wire_walk(monkeypatch, header_row=None, step_rows=[])
    _text, summary, meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", adapter.RENDER_TOOL_NAME)

    # The Result answer is intact -- the walk never takes it down.
    assert summary["outcome"] == "success"
    assert summary["ai_path"] == "aip_EXAMPLE"
    # And the walk says what it knows: the identity, and that it is not readable.
    # Foreign, denied and absent converge on the same reason, as on the REST route.
    walk = meta[RESULT_META_KEY]["ai_path_walk"]
    assert walk == {
        "schema_version": "observed-ai-path.v1",
        "state": "unavailable",
        "reason": "unavailable",
    }


def test_a_failing_path_store_never_takes_the_render_answer_down(monkeypatch):
    _wire_walk(monkeypatch, boom=True)
    _text, summary, meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", adapter.RENDER_TOOL_NAME)

    assert summary["outcome"] == "success"
    walk = meta[RESULT_META_KEY]["ai_path_walk"]
    assert walk == {
        "schema_version": "observed-ai-path.v1",
        "state": "unavailable",
        "reason": "unavailable",
    }


def test_a_result_without_a_path_carries_the_exact_human_projection(wired):
    _text, _summary, meta = adapter._answer("proj_EXAMPLE", "qr_EXAMPLE", "analyze_result")
    assert meta[RESULT_META_KEY]["ai_path_walk"] == {
        "schema_version": "observed-ai-path.v1",
        "state": "human_absent",
        "literal": "No AI path",
    }


class _ContextOptionsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.selected = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params):
        if "mdm_business_domains" in sql:
            self.selected = self.rows["domains"]
        elif "golden_questions" in sql:
            self.selected = self.rows["questions"]
        elif "procedures p" in sql:
            self.selected = self.rows["skills"]
        else:  # pragma: no cover - every new authority needs an explicit test
            raise AssertionError(sql)

    def fetchall(self):
        return self.selected


class _ContextOptionsConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _ContextOptionsCursor(self.rows)


def test_mcp_context_options_are_exact_compact_and_grouped_by_semantic_view():
    options = adapter._discover_analysis_context_options(
        _ContextOptionsConnection(
            {
                "domains": [("svv_1", "bd_growth", 4, "Growth")],
                "questions": [
                    ("svv_1", "gq_1", "gqv_3", 3, "Qualified growth", "active",
                     "bd_growth", 4)
                ],
                "skills": [
                    ("proc_paid", "Paid media investigation", 7, "project"),
                    ("proc_platform", "Platform analysis", 2, "platform"),
                ],
            }
        ),
        project_id="proj_1",
        semantic_view_version_ids=["svv_1"],
    )

    assert options == {
        "contract_version": "analysis-context-options.v1",
        "state": "available",
        "semantic_views": [
            {
                "semantic_view_version_id": "svv_1",
                "business_domains": [
                    {
                        "id": "bd_growth",
                        "version_number": 4,
                        "version_id": "bd_growth:4",
                        "name": "Growth",
                    }
                ],
                "golden_questions": [
                    {
                        "id": "gq_1",
                        "version_id": "gqv_3",
                        "version_number": 3,
                        "title": "Qualified growth",
                        "lifecycle": "active",
                        "business_domain_id": "bd_growth",
                        "business_domain_version_number": 4,
                    }
                ],
            }
        ],
        "requested_skills": [
            {
                "procedure_id": "proc_paid",
                "version_number": 7,
                "version_id": "proc_paid@7",
                "name": "Paid media investigation",
                "scope": "project",
            },
            {
                "procedure_id": "proc_platform",
                "version_number": 2,
                "version_id": "proc_platform@2",
                "name": "Platform analysis",
                "scope": "platform",
            },
        ],
        "bounds": {
            "max_business_domains": 50,
            "max_golden_questions": 50,
            "max_requested_skills": 50,
            "max_selected_skills": 8,
            "context_offset": 0,
            "next_context_offset": None,
            "business_domains_truncated": False,
            "golden_questions_truncated": False,
            "requested_skills_truncated": False,
        },
    }
    assert "body" not in json.dumps(options)
    assert "instruction" not in json.dumps(options)


def test_mcp_match_catalog_trims_context_guidance_before_governed_matches(monkeypatch):
    monkeypatch.setattr(adapter, "_MCP_MATCH_CATALOG_MAX_BYTES", 1_250)
    response = {
        "schema_version": "analyze-match-catalog.v1",
        "project_id": "proj_1",
        "organization_id": "org_1",
        "matches": [{"authority": "governed", "analysis": "keep me"}],
        "counts": {"returned": 1},
        "bounds": {"truncated": False, "response_bytes": 0, "max_response_bytes": 1_250},
        "empty_reason": None,
        "analysis_context_options": {
            "contract_version": "analysis-context-options.v1",
            "state": "available",
            "semantic_views": [
                {
                    "semantic_view_version_id": "svv_1",
                    "business_domains": [],
                    "golden_questions": [],
                }
            ],
            "requested_skills": [
                {
                    "procedure_id": f"proc_{index}",
                    "version_number": 1,
                    "version_id": f"proc_{index}@1",
                    "name": "x" * 300,
                    "scope": "project",
                }
                for index in range(8)
            ],
            "bounds": {
                "max_business_domains": 50,
                "max_golden_questions": 50,
                "max_requested_skills": 50,
                "max_selected_skills": 8,
                "business_domains_truncated": False,
                "golden_questions_truncated": False,
                "requested_skills_truncated": False,
            },
        },
    }

    bounded = adapter._bound_analyze_match_catalog(response)

    assert bounded["matches"] == [{"authority": "governed", "analysis": "keep me"}]
    assert bounded["analysis_context_options"]["bounds"]["requested_skills_truncated"] is True
    assert bounded["analysis_context_options"]["bounds"]["continuations"] == [
        {
            "context_kind": "requested_skills",
            "context_offset": len(bounded["analysis_context_options"]["requested_skills"]),
        }
    ]
    assert bounded["analysis_context_options"]["bounds"]["continuations"][0][
        "context_offset"
    ] > 0
    assert bounded["bounds"]["response_bytes"] == len(
        json.dumps(bounded, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert bounded["bounds"]["response_bytes"] <= 1_250


def test_unavailable_context_guidance_keeps_each_discovered_view_explicit():
    options = adapter._unavailable_analysis_context_options(["svv_1", "svv_2"])

    assert options["state"] == "unavailable"
    assert options["reason"] == "unavailable"
    assert [entry["semantic_view_version_id"] for entry in options["semantic_views"]] == [
        "svv_1",
        "svv_2",
    ]
    assert options["bounds"]["max_selected_skills"] == 8


def test_context_kind_projects_one_independently_pageable_catalog():
    options = {
        "semantic_views": [
            {
                "business_domains": [{"id": "bd_1"}],
                "golden_questions": [{"id": "gq_1"}],
            }
        ],
        "requested_skills": [{"procedure_id": "proc_1"}],
        "bounds": {
            "business_domains_truncated": True,
            "golden_questions_truncated": True,
            "requested_skills_truncated": True,
        },
    }

    adapter._select_analysis_context_kind(options, "golden_questions")

    assert options["semantic_views"][0]["business_domains"] == []
    assert options["semantic_views"][0]["golden_questions"] == [{"id": "gq_1"}]
    assert options["requested_skills"] == []
    assert options["bounds"] == {
        "business_domains_truncated": False,
        "golden_questions_truncated": True,
        "requested_skills_truncated": False,
    }


def test_context_byte_bound_caps_one_oversized_picker_label(monkeypatch):
    monkeypatch.setattr(adapter, "_MCP_MATCH_CATALOG_MAX_BYTES", 262_144)
    response = {
        "schema_version": "analyze-match-catalog.v1",
        "project_id": "proj_1",
        "organization_id": "org_1",
        "matches": [],
        "counts": {"returned": 0},
        "bounds": {"truncated": False, "response_bytes": 0},
        "empty_reason": None,
        "analysis_context_options": {
            "contract_version": "analysis-context-options.v1",
            "state": "available",
            "semantic_views": [],
            "requested_skills": [
                {
                    "procedure_id": "proc_1",
                    "version_number": 1,
                    "version_id": "proc_1@1",
                    "name": "x" * 300_000,
                    "scope": "project",
                }
            ],
            "bounds": {
                "context_offset": 0,
                "business_domains_truncated": False,
                "golden_questions_truncated": False,
                "requested_skills_truncated": False,
            },
        },
    }

    bounded = adapter._bound_analyze_match_catalog(response)

    assert len(bounded["analysis_context_options"]["requested_skills"][0]["name"]) == 160
    assert bounded["bounds"]["response_bytes"] <= 262_144
