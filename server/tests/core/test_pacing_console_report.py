"""Story 67.26 -- plan-versus-actual reaches the console, and states its currency.

TWO DEFECTS, ONE STORY, and they are the same defect at two removes.

1. `analyze-and-test.md:162` has said since Epic 22 that "Console parity is owed
   and not delivered ... no console route reads the pacing Result", and `:510`
   makes it a criterion. The cause was measurable and small: `get_card` has taken
   `plan_id` since story 22.5, and its REST mirror `cards_api` never forwarded it,
   so `mediaplan_pacing` -- which refuses without one -- answered 422 to every
   console caller. The route existed and could not be addressed.

2. The amendment "the currency of a pacing figure" (story 61.4) landed in the
   marts and in the Datastream Workbench sentence, and never reached the card
   envelope. The two table blocks labelled every amount `Budget (€)` / `Reste (€)`
   / `Extrapolated (€)` and dropped all seven currency columns the marts compute.
   A plan in USD on a Project reporting in EUR was drawn under a euro sign nothing
   had earned -- the exact figure 61.4 was ratified to stop.

The second is why the first could not simply be plumbed: exposing the card to the
console without the currency would have shipped the lie to a second surface.

The stand-in warehouse below carries the REAL mart columns (`plan_currency`,
`actual_currency`, `reporting_currency`, `money_gap_code`, `money_is_composable`,
`actual_withheld`, `fx_*`), which the older pacing fixtures predate.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402

pytest.importorskip("duckdb")

from starlette.routing import Router  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PROJECT_ID = "default"
PLAN_ID = "00000000-0000-4000-8000-000000000067"
VERSION_ID = "00000000-0000-4000-8000-000000000671"

#: Faithful to `dbt/models/marts/plan_pacing_by_line.sql` -- the currency columns
#: are the point of the fixture, so they are not trimmed for convenience.
_DDL = """
CREATE SCHEMA IF NOT EXISTS main_marts;

CREATE TABLE main_marts.plan_pacing_by_line (
    project_id VARCHAR, plan_id VARCHAR, plan_version_id VARCHAR, line_key VARCHAR,
    label VARCHAR, channel VARCHAR, currency VARCHAR,
    plan_currency VARCHAR, actual_currency VARCHAR, reporting_currency VARCHAR,
    money_policy_version_id VARCHAR, money_gap_code VARCHAR,
    money_is_composable BOOLEAN, actual_withheld BOOLEAN,
    native_currency VARCHAR, fx_as_of_date_min DATE, fx_as_of_date_max DATE,
    fx_source VARCHAR, fx_tier VARCHAR, fx_method VARCHAR,
    budget DOUBLE, line_start_date DATE, line_end_date DATE,
    is_plan_only BOOLEAN, sort_order INTEGER,
    as_of_day DATE, days_total INTEGER, days_elapsed INTEGER,
    allocated_to_date DOUBLE, actual_to_date DOUBLE, consumed_pct DOUBLE, pace DOUBLE,
    remaining_budget DOUBLE, extrapolated_spend DOUBLE,
    actual_pull_id_min VARCHAR, actual_pull_id_max VARCHAR, actual_pull_id_count BIGINT
);

CREATE TABLE main_marts.plan_pacing_by_channel (
    project_id VARCHAR, plan_id VARCHAR, plan_version_id VARCHAR, channel VARCHAR,
    currency VARCHAR, plan_currency VARCHAR, actual_currency VARCHAR,
    reporting_currency VARCHAR, money_gap_code VARCHAR, money_is_composable BOOLEAN,
    actual_withheld BOOLEAN,
    budget DOUBLE, allocated_to_date DOUBLE, actual_to_date DOUBLE,
    remaining_budget DOUBLE, consumed_pct DOUBLE, pace DOUBLE, extrapolated_spend DOUBLE,
    plan_only_line_count BIGINT, paceable_line_count BIGINT,
    actual_pull_id_min VARCHAR, actual_pull_id_max VARCHAR, actual_pull_id_count BIGINT
);

CREATE TABLE main_marts.plan_pacing_by_plan (
    project_id VARCHAR, plan_id VARCHAR, plan_version_id VARCHAR, currency VARCHAR,
    plan_currency VARCHAR, actual_currency VARCHAR, reporting_currency VARCHAR,
    money_policy_version_id VARCHAR, money_gap_code VARCHAR,
    as_of_day DATE, budget DOUBLE, allocated_to_date DOUBLE, actual_to_date DOUBLE,
    remaining_budget DOUBLE, consumed_pct DOUBLE, pace DOUBLE, extrapolated_spend DOUBLE,
    plan_only_line_count BIGINT, paceable_line_count BIGINT,
    actual_pull_id_min VARCHAR, actual_pull_id_max VARCHAR, actual_pull_id_count BIGINT
);
"""


def _seed(path: str, *, mixed: bool) -> None:
    """Seed one plan. `mixed=True` is the 61.4 case: budget USD, spend EUR.

    In the mixed case the mart has ALREADY suppressed every composed figure
    (`consumed_pct`, `pace`, `remaining_budget` are NULL and
    `money_is_composable` is false). That suppression is not re-done in Python
    anywhere -- the test asserts the card LABELS it, which is the half that was
    missing.
    """
    import duckdb  # noqa: PLC0415

    plan_ccy = "USD" if mixed else "EUR"
    single = None if mixed else "EUR"
    composable = not mixed
    consumed = None if mixed else 1250.0 / 3000.0
    pace = None if mixed else 0.25
    remaining = None if mixed else 1750.0

    con = duckdb.connect(path)
    try:
        con.execute(_DDL)
        con.execute(
            "INSERT INTO main_marts.plan_pacing_by_line VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                PROJECT_ID, PLAN_ID, VERSION_ID, "line-digital-a",
                "Digital A", "digital", single,
                plan_ccy, "EUR", "EUR", "mp_v1", None,
                composable, False,
                "USD", "2026-03-01", "2026-03-10", "ecb", "reference", "direct",
                3000.0, "2026-03-01", "2026-03-30",
                False, 0,
                "2026-03-10", 30, 10,
                1000.0, 1250.0, consumed, pace,
                remaining, 3750.0,
                "pull_a", "pull_b", 2,
            ],
        )
        con.execute(
            "INSERT INTO main_marts.plan_pacing_by_channel VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                PROJECT_ID, PLAN_ID, VERSION_ID, "digital",
                single, plan_ccy, "EUR", "EUR", None, composable, False,
                3000.0, 1000.0, 1250.0, remaining, consumed, pace, 3750.0,
                0, 1, "pull_a", "pull_b", 2,
            ],
        )
        con.execute(
            "INSERT INTO main_marts.plan_pacing_by_plan VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                PROJECT_ID, PLAN_ID, VERSION_ID, single,
                plan_ccy, "EUR", "EUR", "mp_v1", None,
                "2026-03-10", 3000.0, 1000.0, 1250.0, remaining, consumed, pace, 3750.0,
                0, 1, "pull_a", "pull_b", 2,
            ],
        )
    finally:
        con.close()


def _cards_module(tmp_path, monkeypatch, *, mixed: bool):
    import unittest.mock as _mock  # noqa: PLC0415

    db_file = tmp_path / f"pacing_{'mixed' if mixed else 'aligned'}.duckdb"
    _seed(str(db_file), mixed=mixed)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")

    from core import warehouse  # noqa: PLC0415
    importlib.reload(warehouse)

    def _stub_get_plan(conn, *, plan_id):  # noqa: ARG001
        return {
            "id": plan_id,
            "project_id": PROJECT_ID,
            "name": "Plan 67.26",
            "currency": "USD" if mixed else "EUR",
            "active_version": {"id": VERSION_ID},
            "lines": [],
        }

    monkeypatch.setattr("core.mediaplan_store.get_plan", _stub_get_plan)
    _fake_ctx = _mock.MagicMock()
    _fake_ctx.__enter__ = _mock.MagicMock(return_value=_mock.MagicMock())
    _fake_ctx.__exit__ = _mock.MagicMock(return_value=False)
    monkeypatch.setattr("core.db.get_connection", lambda: _fake_ctx)

    from core import cards  # noqa: PLC0415
    importlib.reload(cards)
    return cards


def _envelope(cards_mod):
    tpl = cards_mod.get_template("mediaplan_pacing")
    _summary, envelope, _uri = cards_mod._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None
    )
    return envelope


def _block(envelope, source):
    for b in envelope["data"]["composition"]:
        if (b.get("binding") or {}).get("source") == source:
            return b
    raise AssertionError(f"no block bound to {source}")


# ---------------------------------------------------------------------------
# 61.4 -- the Result states the currency that produced each amount.
# ---------------------------------------------------------------------------


def test_no_column_label_hard_codes_a_currency_glyph(tmp_path, monkeypatch):
    """The defect, stated as a test: a header may not name a currency.

    A header can only ever state ONE currency for a whole table, and a plan whose
    lines were bought in two has no such single one. This guards the class, not
    the euro: any glyph in any pacing column label fails here.
    """
    cards_mod = _cards_module(tmp_path, monkeypatch, mixed=False)
    envelope = _envelope(cards_mod)
    for source in ("plan_lines", "plan_channels"):
        for column in _block(envelope, source)["data"]["columns"]:
            for glyph in ("€", "$", "£", "¥"):
                assert glyph not in column["label"], (
                    f"{source}.{column['key']} hard-codes {glyph} in its header"
                )


def test_every_amount_row_carries_the_currency_that_produced_it(tmp_path, monkeypatch):
    cards_mod = _cards_module(tmp_path, monkeypatch, mixed=False)
    envelope = _envelope(cards_mod)
    for source in ("plan_lines", "plan_channels"):
        block = _block(envelope, source)
        assert any(c["key"] == "currency" for c in block["data"]["columns"])
        for row in block["data"]["rows"]:
            assert row["currency"] == "EUR"


def test_two_currencies_state_both_and_compose_nothing(tmp_path, monkeypatch):
    """61.4's rule, end to end: both amounts, no composed figure, and it says so."""
    cards_mod = _cards_module(tmp_path, monkeypatch, mixed=True)
    envelope = _envelope(cards_mod)

    row = _block(envelope, "plan_lines")["data"]["rows"][0]
    # Both sides are named ...
    assert row["plan_currency"] == "USD"
    assert row["actual_currency"] == "EUR"
    # ... under no single currency ...
    assert row["currency"] is None
    # ... and nothing composed of the two is stated. NULL, never 0: a zero pace
    # on a campaign that spent normally is the firing 61.4 was ratified to stop.
    assert row["consumed_pct"] is None
    assert row["pace_pct"] is None
    assert row["remaining_budget"] is None
    # ... while both raw amounts survive, each under its own currency.
    assert row["budget"] == 3000.0
    assert row["actual_to_date"] == 1250.0

    money = envelope["data"]["money"]
    assert money["comparable"] is False
    assert money["plan_currency"] == ["USD"]
    assert money["actual_currency"] == ["EUR"]


def test_aligned_plan_is_comparable_and_carries_fx_provenance(tmp_path, monkeypatch):
    cards_mod = _cards_module(tmp_path, monkeypatch, mixed=False)
    money = _envelope(cards_mod)["data"]["money"]

    assert money["comparable"] is True
    assert money["reporting_currency"] == "EUR"
    assert money["money_policy_version_id"] == "mp_v1"

    fx = money["fx_evidence"]
    assert fx["native_currency"] == ["USD"]
    assert fx["as_of_start"] == "2026-03-01"
    assert fx["as_of_end"] == "2026-03-10"
    assert fx["source"] == ["ecb"]
    assert fx["tier"] == ["reference"]
    assert fx["method"] == ["direct"]


def test_the_rate_itself_is_not_carried_at_the_to_date_grain(tmp_path, monkeypatch):
    """61.4 states this refusal explicitly; a test keeps it from being helpfully added.

    "The rate itself is deliberately not carried at the to-date grain -- it
    varies day by day, and one rate printed over a period is a day's measurement
    wearing the period's clothes."
    """
    cards_mod = _cards_module(tmp_path, monkeypatch, mixed=False)
    fx = _envelope(cards_mod)["data"]["money"]["fx_evidence"]
    assert "rate" not in fx
    assert not any("rate" in k for k in fx)


# ---------------------------------------------------------------------------
# Console parity -- the REST mirror can be addressed at all.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _allowed_db():
    yield object()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """The REST mirror, over the same stand-in warehouse the card resolver reads."""
    _cards_module(tmp_path, monkeypatch, mixed=False)
    from core.cards_api import CARDS_ROUTES  # noqa: PLC0415

    app = Router(routes=CARDS_ROUTES)
    with patch(
        "core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))
    ), patch("core.db.get_connection", _allowed_db), patch(
        "core.project_access.identity_can_read_project", return_value=True
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def test_rest_mirror_forwards_plan_id_and_serves_the_pacing_result(api):
    """The one line that made console parity impossible for four epics."""
    response = api.get(
        f"/api/cards?project_id={PROJECT_ID}&template=mediaplan_pacing&plan_id={PLAN_ID}"
    )
    assert response.status_code == 200, response.text
    data = response.json()["envelope"]["data"]
    assert data["plan_id"] == PLAN_ID
    assert data["money"]["reporting_currency"] == "EUR"
    assert any(
        (b.get("binding") or {}).get("source") == "plan_lines" for b in data["composition"]
    )


def test_rest_mirror_without_plan_id_refuses_and_names_what_is_missing(api):
    """Absent stays absent. A card about an object refuses rather than guessing one."""
    response = api.get(f"/api/cards?project_id={PROJECT_ID}&template=mediaplan_pacing")
    assert response.status_code == 422
    assert "plan_id" in response.json()["missing"]
