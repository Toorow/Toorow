"""Tests for the Connecteurs context card + flow.report card_template (Story 9.8).

Covers:
  * registry entry for the connectors card (context kind, explicit-only selection);
  * get_card(template="connectors") full envelope against the exact UI contract
    (mock DB rows: 2 connectors, one stale -> comment cites it);
  * multi-project scoping: project B connections never leak into project A (AD-5);
  * DB-down => designed empty envelope (never crashes);
  * flow.report card_template honored by get_card(report_ref=...); explicit template
    overrides; unknown card_template warns and falls back;
  * envelope.schema.json compliance for the connectors card.

All DB calls are mocked (no live Postgres required).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import jsonschema  # noqa: E402
import pytest  # noqa: E402
from core import cards as cards_module  # noqa: E402

from tests.support.statement_router import (  # noqa: E402
    StatementInventory,
    UnknownStatement,
    describe,
)

_SCHEMA_PATH = Path(__file__).parent.parent / "conformance" / "schemas" / "envelope.schema.json"


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Mock DB connection that dispatches fetchall() by the SQL keyword executed.
# ---------------------------------------------------------------------------


# EVERY STATEMENT a connectors card issues through `core.db.get_connection`,
# named -- and nothing else. The old fake dispatched on three fragments and
# answered `[]` to anything else, so four statements it had never been taught got
# a plausible answer instead of a failure (AI-317):
#
#   * `SELECT org_id FROM app.projects` (answerable_topics.py:146), run twice per
#     card because Story 52.1 resolves the Project catalog and Story 52.3 its
#     knowledge pins through the same connection this fixture patches. It is read
#     with `fetchone()`, which the old fake did not even define -- the
#     AttributeError was swallowed by the product's best-effort `except` and the
#     card degraded for a reason that had nothing to do with the product;
#   * the two reads that lookup opens: the stored topic heads
#     (answerable_topics.py:158) and the knowledge pins
#     (answerable_topics.py:756);
#   * the freshness evaluator's window query (health_enrichment.py:83), which
#     `_evaluate_freshness` runs at the exit of EVERY card since AI-273. The old
#     fake answered it with the CONNECTION INVENTORY rows -- its first branch
#     tested `connection_ref` + `connection_health`, and that query names both
#     tables. `_worst_health` accepts only rows of arity 3 or 6, so the inventory
#     4-tuples were dropped and the card read "not evaluated" for entirely the
#     wrong reason.
#
# Declaration order is first match wins, and it carries one real ambiguity:
# `connections_for_project` embeds `(SELECT org_id FROM app.projects WHERE id =
# %s)` as its own scope predicate, so `org_for_project` is declared LAST -- ahead
# of it, it would swallow the inventory query whole.
_CONNECTORS = StatementInventory(
    "_DispatchCursor (connectors card)",
    health_for_window=("from app.connection_ref r", "h.populate_failed_pull_id"),
    connections_for_project=("from app.connection_ref r", "r.owner_org_id"),
    last_extract_by_connection=(
        "select distinct on (pj.connection_ref_id)",
        "from app.pull_jobs pj",
    ),
    flows_by_connection=("from app.datastreams ds", "ds.enabled = true"),
    stored_topics="from app.answerable_topics t",
    knowledge_pins="from app.answerable_topic_knowledge_bindings b",
    org_for_project="select org_id from app.projects",
)

#: The org the fixture's connections hang from. `app.connection_ref` is scoped by
#: `owner_org_id`, so a project that returns connections is a project with an org.
_OWNER_ORG_ID = "org_EXAMPLE"


class _DispatchCursor:
    """A cursor whose fetchall() returns rows keyed by which query was executed."""

    def __init__(self, connections_rows, extract_rows, datastream_rows):
        self._connections = connections_rows
        self._extract = extract_rows
        self._datastreams = datastream_rows
        self._last = None
        self._sql = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def description(self):
        """psycopg's ``description``, DERIVED from the last statement.

        A property rather than a stored column list: no path this card takes
        reads it today, and a column tuple nothing reads is the copy that goes
        stale first. `describe` refuses a projection it cannot read -- the
        `DISTINCT ON (...)` ledger query, the `COALESCE(...)` pin query -- and
        refusing is the right answer there too: a fake that guessed a column list
        would be holding a second copy of the product's SELECT.
        """
        return describe(self._sql)

    def execute(self, sql, params=None):
        self._sql = sql
        match _CONNECTORS.match(sql):
            case "connections_for_project":
                self._last = self._connections
            case "last_extract_by_connection":
                self._last = self._extract
            case "flows_by_connection":
                self._last = self._datastreams
            case "health_for_window":
                # The evaluator asks which connections this project PULLED
                # THROUGH over the card's window: `pull_jobs` joined to
                # `datastreams`, bounded by date_from/date_to. This fixture
                # declares no pull job over any window -- its ledger rows carry a
                # data date but no window, and no datastream links them to a job
                # -- so zero rows is the honest answer. `_worst_health` then
                # returns None and the card keeps `stale_since_evaluated: False`,
                # which is what these tests already observed, now for a reason.
                self._last = []
            case "org_for_project":
                # The project exists and belongs to an org: exactly the fact the
                # inventory query's own scope predicate assumes when this fixture
                # answers it with connections.
                self._last = [(_OWNER_ORG_ID,)]
            case "stored_topics" | "knowledge_pins":
                # This project authored no topic and pinned no knowledge, so the
                # catalog resolves to the platform defaults and the card reports
                # `no_knowledge_declared`.
                self._last = []
            case _ as statement:  # pragma: no cover - a named route left unanswered
                raise AssertionError(f"_DispatchCursor: no row shape for {statement}")

    def fetchall(self):
        return self._last or []

    def fetchone(self):
        rows = self._last or []
        return rows[0] if rows else None


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an untaught statement must NAME itself, not answer no rows.

    The old `else` answered `[]`, which is a legitimate answer to every read this
    card makes -- no connection, no pull, no datastream, no topic. A moved query
    would have gone on getting it, and every assertion downstream would have been
    about a path the test no longer exercised.
    """
    cursor = _DispatchCursor([], [], [])
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT pj.id, pj.error_code FROM app.pull_jobs pj "
            "WHERE pj.connection_ref_id = ANY(%s) AND pj.state = 'error'"
        )
    message = str(raised.value)
    assert "pj.error_code" in message
    assert "last_extract_by_connection" in message


class _MockConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cursor


def _mock_connection(connections_rows, extract_rows=None, datastream_rows=None):
    cur = _DispatchCursor(connections_rows, extract_rows or [], datastream_rows or [])
    conn = _MockConn(cur)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


class _DT:
    """Minimal object with .isoformat() to mimic a psycopg timestamp column."""

    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


# ---------------------------------------------------------------------------
# Registry + explicit-only selection
# ---------------------------------------------------------------------------


def test_connectors_registered_as_context_card():
    tpl = cards_module.get_template("connectors")
    assert tpl is not None
    assert tpl.widget_uri == "ui://core/card-connectors"
    assert tpl.is_context
    assert tpl.kind == cards_module.CARD_KIND_CONTEXT
    assert tpl.answers_question == ("Which connectors are available and what do they feed?")


def test_connectors_not_auto_suggested():
    # Even with rich metrics available, a context card is never in the suggestion pool.
    best, alts = cards_module.suggest_template(
        {"sessions", "conversions", "clicks", "impressions"},
        {"device_category", "page"},
    )
    assert best is not None
    assert best.id != "connectors"
    assert all(a.id != "connectors" for a in alts)


def test_connectors_catalog_entry_declares_kind():
    entry = next(e for e in cards_module.list_templates() if e["id"] == "connectors")
    assert entry["kind"] == "context"


# ---------------------------------------------------------------------------
# get_card(template="connectors") -- full envelope against the UI contract
# ---------------------------------------------------------------------------


def _two_connectors_rows():
    """2 connectors: google-ads (fresh, ok) + google-analytics (stale, older extract)."""
    connections = [
        # (id, provider, health_status, last_fetched_at)
        ("conn_A", "google-ads", "ok", _DT("2026-07-14T06:00:00+00:00")),
        ("conn_B", "google-analytics", "stale", _DT("2026-07-10T06:00:00+00:00")),
    ]
    extract = [
        # (connection_ref_id, last_date, completed_at)
        ("conn_A", "2026-07-13", _DT("2026-07-14T02:00:00+00:00")),
        ("conn_B", "2026-07-09", _DT("2026-07-10T02:00:00+00:00")),
    ]
    datastreams = [
        # (connection_ref_id, module_name)
        ("conn_A", "google-ads"),
        ("conn_B", "google-analytics"),
    ]
    return connections, extract, datastreams


def test_get_card_connectors_full_envelope():
    connections, extract, datastreams = _two_connectors_rows()
    with (
        patch(
            "core.db.get_connection",
            return_value=_mock_connection(connections, extract, datastreams),
        ),
        patch(
            "core.cards._reports_by_module",
            return_value={
                "google-ads": ["spend_daily"],
                "google-analytics": ["overview_daily"],
            },
        ),
    ):
        summary, envelope, widget_uri = cards_module.get_card(
            [],
            "projA",
            template="connectors",
            trace_id="a" * 32,
        )

    assert widget_uri == "ui://core/card-connectors"
    data = envelope["data"]
    assert data["card_type"] == "connectors"

    comp = data["composition"]
    assert comp[0]["type"] == "table"
    assert comp[0]["title"] == "Connectors"
    cols = comp[0]["data"]["columns"]
    assert [c["key"] for c in cols] == ["connector", "status", "last_extract", "flows"]

    rows = comp[0]["data"]["rows"]
    assert len(rows) == 2
    by_conn = {r["connector"]: r for r in rows}
    # French-first status labels.
    assert by_conn["google-ads"]["status"] == "Operational"
    assert by_conn["google-analytics"]["status"] == "Stale"
    assert by_conn["google-ads"]["last_extract"] == "2026-07-13"
    assert "google-ads/spend_daily" in by_conn["google-ads"]["flows"]

    # Comment block cites the LEAST-FRESH connector (google-analytics, older extract).
    assert comp[1]["type"] == "comment"
    comment = comp[1]["data"]["text"]
    assert "google-analytics" in comment
    assert "2026-07-09" in comment

    # Provenance: source_system "app" (no pull_ids on a context card).
    assert envelope["meta"]["provenance"]["source_system"] == "app"
    assert envelope["meta"]["provenance"]["pull_ids"] == []
    assert envelope["meta"]["card_selection"]["mode"] == "explicit"


def test_get_card_connectors_status_labels_all():
    # revoked + error + unknown (None) -> the full French label set.
    connections = [
        ("c1", "prov-a", "revoked", None),
        ("c2", "prov-b", "error", None),
        ("c3", "prov-c", None, None),
    ]
    with (
        patch(
            "core.db.get_connection",
            return_value=_mock_connection(connections, [], []),
        ),
        patch("core.cards._reports_by_module", return_value={}),
    ):
        _summary, envelope, _uri = cards_module.get_card(
            [],
            "projA",
            template="connectors",
        )
    rows = envelope["data"]["composition"][0]["data"]["rows"]
    labels = {r["connector"]: r["status"] for r in rows}
    assert labels["prov-a"] == "Revoked"
    assert labels["prov-b"] == "En erreur"
    assert labels["prov-c"] == "Inconnu"


def test_get_card_connectors_project_scoping():
    """AD-5: the inventory query is scoped by project_id -- only projA rows returned.

    We assert the project_id is threaded into the connections query params so
    project B's connections can never be selected into project A's card.
    """
    captured = {}

    class _CapturingCursor(_DispatchCursor):
        def execute(self, sql, params=None):
            # `owner_org_id`, not the two table names. Since AI-273 the card
            # evaluates its own freshness before returning, and that evaluator
            # joins the same two tables -- so the looser predicate captured its
            # params LAST and this test read `('projA', <window>)` where it
            # expects the inventory's own `('projA',)`. The inventory query is
            # the one scoped by org through the project; the other is scoped by
            # the pull jobs of a window.
            if "owner_org_id" in sql.lower() and "connection_health" in sql.lower():
                captured["conn_params"] = params
            super().execute(sql, params)

    cur = _CapturingCursor([("cA", "google-ads", "ok", _DT("2026-07-14T00:00:00+00:00"))], [], [])
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=_MockConn(cur))
    ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=ctx),
        patch("core.cards._reports_by_module", return_value={}),
    ):
        cards_module.get_card([], "projA", template="connectors")

    assert captured["conn_params"] == ("projA",)


def test_last_extract_orders_by_data_window_end_not_completed_at():
    """review-epic-9-backend F-5: last_extract = the pull covering the most recent DATA
    date (ORDER BY date_to), NOT the most recently COMPLETED job.

    Backfill scenario: a job completed most RECENTLY but covers an OLD window; a job
    completed EARLIER covers a NEWER window. The ledger query must order by date_to so the
    NEWER data window is reported as the last extract.
    """
    captured = {}

    class _CapturingCursor(_DispatchCursor):
        def execute(self, sql, params=None):
            # `distinct on` and not `pull_jobs` alone. Since AI-273 the card
            # evaluates its own freshness before returning, and that evaluator's
            # query names `app.pull_jobs` too -- so the looser predicate captured
            # it LAST and this test asserted an ORDER BY belonging to a different
            # question. The ledger query is the one that picks one row per
            # connection, which is what `DISTINCT ON` says and the other does not.
            if "distinct on" in sql.lower() and "pull_jobs" in sql.lower():
                captured["ledger_sql"] = sql
            super().execute(sql, params)

    connections = [("cA", "google-ads", "ok", _DT("2026-07-14T00:00:00+00:00"))]
    # DISTINCT ON returns the first row per connection under the ORDER BY. With the fixed
    # ordering (date_to DESC) the row covering 2026-07-13 (a later completed_at is not what
    # decides) is what the DB would surface. We assert the SQL orders by date_to first.
    extract = [("cA", "2026-07-13", _DT("2026-07-10T02:00:00+00:00"))]
    cur = _CapturingCursor(connections, extract, [])
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=_MockConn(cur))
    ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=ctx),
        patch("core.cards._reports_by_module", return_value={}),
    ):
        _s, envelope, _uri = cards_module.get_card([], "projA", template="connectors")

    sql = captured["ledger_sql"]
    lowered = " ".join(sql.lower().split())  # normalise whitespace
    # date_to must come BEFORE completed_at in the ORDER BY (the fix).
    order_clause = lowered.split("order by", 1)[1]
    assert "pj.date_to desc" in order_clause
    assert order_clause.index("date_to") < order_clause.index("completed_at")
    # The last_extract shown is the row's data-window end.
    rows = {r["connector"]: r for r in envelope["data"]["composition"][0]["data"]["rows"]}
    assert rows["google-ads"]["last_extract"] == "2026-07-13"


def test_get_card_connectors_db_down_designed_empty():
    """DB down => designed empty envelope (empty rows, empty comment) -- never crashes."""
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        summary, envelope, widget_uri = cards_module.get_card(
            [],
            "projA",
            template="connectors",
        )
    assert widget_uri == "ui://core/card-connectors"
    comp = envelope["data"]["composition"]
    assert comp[0]["data"]["rows"] == []
    # Comment is the designed empty-inventory line, never blank.
    assert comp[1]["data"]["text"].strip()
    assert "unavailable" in summary


def test_get_card_connectors_envelope_schema_valid():
    connections, extract, datastreams = _two_connectors_rows()
    with (
        patch(
            "core.db.get_connection",
            return_value=_mock_connection(connections, extract, datastreams),
        ),
        patch("core.cards._reports_by_module", return_value={}),
    ):
        _summary, envelope, _uri = cards_module.get_card(
            [],
            "projA",
            template="connectors",
            trace_id="b" * 32,
        )
    jsonschema.Draft202012Validator(_schema()).validate(envelope)


# ---------------------------------------------------------------------------
# flow.report card_template honored by get_card(report_ref=...)
# ---------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, name, reports):
        self.name = name
        self.reports = reports


def _report_rows():
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
    ]


def test_flow_card_template_honored_by_get_card():
    """A flow.report card_template pins the card served for report_ref (no explicit tpl)."""
    module = _FakeModule(
        "google-analytics",
        [
            {"id": "overview", "metrics": ["sessions"], "dimensions": ["device_category"]},
        ],
    )
    with (
        patch("core.cards._flow_preferred_template", return_value="kpi"),
        patch("core.warehouse.query_report", return_value=_report_rows()),
        patch("core.cards._fetch_r6", return_value=(None, None)),
        patch("core.cards._fetch_card_config", return_value={}),
        patch(
            "core.reports.find_report",
            return_value={"metrics": ["sessions"], "dimensions": ["device_category"]},
        ),
    ):
        _summary, envelope, widget_uri = cards_module.get_card(
            [module],
            "projA",
            report_ref="google-analytics/overview",
        )
    assert widget_uri == "ui://core/card-kpi"
    assert envelope["meta"]["card_selection"]["chosen"] == "kpi"


def test_explicit_template_overrides_flow_card_template():
    """An explicit LLM template argument wins over the flow's card_template."""
    module = _FakeModule(
        "google-analytics",
        [
            {"id": "overview", "metrics": ["sessions"], "dimensions": ["device_category"]},
        ],
    )
    # Flow pins "conversions" but the caller explicitly asks for "kpi".
    called = {"flow_lookup": False}

    def _flow_pref(*a, **k):
        called["flow_lookup"] = True
        return "conversions"

    with (
        patch("core.cards._flow_preferred_template", side_effect=_flow_pref),
        patch("core.warehouse.query_report", return_value=_report_rows()),
        patch("core.cards._fetch_r6", return_value=(None, None)),
        patch("core.cards._fetch_card_config", return_value={}),
        patch(
            "core.reports.find_report",
            return_value={"metrics": ["sessions"], "dimensions": ["device_category"]},
        ),
    ):
        _summary, envelope, widget_uri = cards_module.get_card(
            [module],
            "projA",
            template="kpi",
            report_ref="google-analytics/overview",
        )
    # Explicit template short-circuits before the flow lookup runs.
    assert not called["flow_lookup"]
    assert widget_uri == "ui://core/card-kpi"


def test_unknown_flow_card_template_warns_and_falls_back(caplog):
    """An unknown card_template on the flow is ignored (warning) -> default suggestion."""
    module = _FakeModule(
        "google-analytics",
        [
            {"id": "overview", "metrics": ["sessions"], "dimensions": ["device_category"]},
        ],
    )
    import logging

    with (
        caplog.at_level(logging.WARNING, logger="core.cards"),
        patch("core.cards._flow_preferred_template", return_value="does-not-exist"),
        patch("core.warehouse.query_report", return_value=_report_rows()),
        patch("core.cards._fetch_r6", return_value=(None, None)),
        patch("core.cards._fetch_card_config", return_value={}),
        patch(
            "core.reports.find_report",
            return_value={"metrics": ["sessions"], "dimensions": ["device_category"]},
        ),
    ):
        _summary, envelope, widget_uri = cards_module.get_card(
            [module],
            "projA",
            report_ref="google-analytics/overview",
        )
    # Fell back to the server suggestion (kpi is the universal fallback here).
    assert envelope["meta"]["card_selection"]["chosen"] in {"kpi", "usertypes"}
    assert any("flow_card_template_ignored" in r.message for r in caplog.records)
