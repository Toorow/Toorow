"""The Google Ad Manager report read -- it used to be a TODO in the envelope.

`get_google_ad_manager_report` returned `metrics: {}` and a shipped alert saying
`TODO: implement mart query`. That is not an unfinished function: it is a
placeholder a reader cannot tell from "this ad server delivered nothing", served
through the MCP surface as if it were an answer.

What these tests hold is the doctrine the TODO itself wrote down, now that there
is code to hold it against:

  * MONEY goes through the platform adapter and nowhere else. `ad_revenue` is
    declared `decimal` in `dbt/seeds/money_metric_units.csv` because
    `stg_google_ad_manager_daily` already divided the GAM micros once, at
    staging. `read_units` must therefore NOT divide again -- and the test that
    proves it is the one that would catch a hand-written `/1e6`.
  * A monetary total NAMES ITS CURRENCY, carried from the mart row, never
    assumed; two currencies in one window are said, not summed away.
  * RATIOS ARE RECONSTRUCTED from additive components and never stored -- and a
    zero denominator yields NO ratio rather than a zero, because a zero reads as
    "nobody clicked" when the truth is "nobody was shown anything".
  * An unreadable warehouse and an empty window are DIFFERENT ANSWERS.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).parents[3]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


def _load_connector():
    path = Path(__file__).parents[1] / "connector.py"
    spec = importlib.util.spec_from_file_location("gam_report_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def connector():
    return _load_connector()


def _row(metric, value, *, dimension="ad_unit", breakdown="Homepage", currency=None):
    return {
        "metric": metric,
        "breakdown_dimension": dimension,
        "breakdown_value": breakdown,
        "value": value,
        "native_currency": currency,
        "pull_id": "pull_01GAM",
        "freshness": "2026-08-18T00:00:00Z",
    }


def _envelope(connector, rows, **kwargs):
    return connector._build_envelope(
        rows,
        kwargs.get("report_profile", "historical_daily"),
        kwargs.get("date_from", "2026-08-01"),
        kwargs.get("date_to", "2026-08-07"),
        kwargs.get("project_id", "proj_EXAMPLE"),
    )


def test_the_report_carries_the_metrics_it_read(connector):
    envelope = _envelope(
        connector,
        [_row("impressions", 12_000.0), _row("clicks", 240.0)],
    )

    assert envelope["schema_version"] == "1"
    assert envelope["meta"]["provenance"] == {
        "source_system": "google-ad-manager",
        "source_field": "fact_daily_kpi",
        "pull_id": "pull_01GAM",
    }
    assert envelope["data"]["metrics"]["impressions"][0]["value"] == 12_000.0
    assert envelope["data"]["metrics"]["clicks"][0]["value"] == 240.0


def test_money_is_not_divided_a_second_time(connector):
    """The staging model already divided the GAM micros once.

    A hand-written `/1e6` here would report 5.4 as 0.0000054. The adapter is
    asked, and for a metric the seed declares `decimal` it answers "do not
    divide" -- which is exactly why the call is not ceremony.
    """
    envelope = _envelope(connector, [_row("ad_revenue", 5.40, currency="EUR")])

    revenue = envelope["data"]["metrics"]["ad_revenue"][0]
    assert revenue["value"] == pytest.approx(5.40)


def test_a_monetary_total_names_its_currency(connector):
    envelope = _envelope(connector, [_row("ad_revenue", 5.40, currency="EUR")])

    assert envelope["data"]["metrics"]["ad_revenue"][0]["currency"] == "EUR"
    assert envelope["data"]["currency"] == "EUR"
    # A count is not money and carries no currency tag at all.
    counts = _envelope(connector, [_row("impressions", 10.0)])
    assert "currency" not in counts["data"]["metrics"]["impressions"][0]


def test_two_currencies_are_said_not_summed(connector):
    envelope = _envelope(
        connector,
        [
            _row("ad_revenue", 5.40, breakdown="Homepage", currency="EUR"),
            _row("ad_revenue", 7.10, breakdown="Sport", currency="USD"),
        ],
    )

    assert envelope["data"]["currency"] is None, "one currency was picked for two"
    warning = envelope["meta"]["alerts"][0]
    assert warning["level"] == "warning"
    assert "EUR" in warning["message"] and "USD" in warning["message"]
    # The values stay per breakdown, each with its own tag -- nothing collapsed.
    revenue_rows = envelope["data"]["metrics"]["ad_revenue"]
    tagged = {r["breakdown_value"]: r["currency"] for r in revenue_rows}
    assert tagged == {"Homepage": "EUR", "Sport": "USD"}


def test_the_three_ratios_are_reconstructed_from_additive_components(connector):
    envelope = _envelope(
        connector,
        [
            _row("impressions", 10_000.0),
            _row("clicks", 250.0),
            _row("ad_revenue", 40.0, currency="EUR"),
        ],
    )

    metrics = envelope["data"]["metrics"]
    assert metrics["ctr"][0]["value"] == pytest.approx(0.025)
    # eCPM is per MILLE: 40 / 10 000 * 1000.
    assert metrics["ecpm"][0]["value"] == pytest.approx(4.0)
    assert metrics["cpc"][0]["value"] == pytest.approx(0.16)


def test_a_zero_denominator_yields_no_ratio_rather_than_a_zero(connector):
    """A zero CTR reads as "nobody clicked"; the truth is "nobody was shown"."""
    envelope = _envelope(
        connector,
        [_row("impressions", 0.0), _row("clicks", 0.0), _row("ad_revenue", 0.0)],
    )

    metrics = envelope["data"]["metrics"]
    assert "ctr" not in metrics
    assert "ecpm" not in metrics
    assert "cpc" not in metrics


def test_a_ratio_is_never_emitted_for_a_breakdown_that_lacks_a_component(connector):
    envelope = _envelope(
        connector,
        [
            _row("impressions", 1_000.0, breakdown="Homepage"),
            _row("impressions", 500.0, breakdown="Sport"),
            _row("clicks", 50.0, breakdown="Homepage"),
        ],
    )

    ctr = {r["breakdown_value"]: r["value"] for r in envelope["data"]["metrics"]["ctr"]}
    assert ctr == {"Homepage": pytest.approx(0.05)}, "a ratio was invented for Sport"


def test_an_unreadable_warehouse_is_not_an_empty_report(connector, monkeypatch):
    def _raise(*_a, **_kw):
        raise RuntimeError("mart unreachable")

    monkeypatch.setattr(connector, "_query_mart", _raise)

    answer = connector.get_google_ad_manager_report(
        project_id="proj_EXAMPLE", date_from="2026-08-01", date_to="2026-08-07"
    )

    assert answer["data"]["metrics"] is None, "an unread window rendered as zero metrics"
    alert = answer["meta"]["alerts"][0]
    assert alert["level"] == "error"
    assert "could not be read" in alert["message"]
    # The message names the gesture that repairs it, never the exception.
    assert "daily build" in alert["message"]
    assert "RuntimeError" not in alert["message"]


def test_an_empty_window_says_the_warehouse_answered(connector, monkeypatch):
    monkeypatch.setattr(connector, "_query_mart", lambda *_a, **_kw: [])

    answer = connector.get_google_ad_manager_report(
        project_id="proj_EXAMPLE", date_from="2026-08-01", date_to="2026-08-07"
    )

    assert answer["data"]["metrics"] == {}, "an answered-but-empty window lost its shape"
    assert answer["meta"]["alerts"][0]["level"] == "info"
    assert "had nothing for these dates" in answer["meta"]["alerts"][0]["message"]


def test_no_placeholder_survives_in_the_served_envelope(connector, monkeypatch):
    """The class, not the instance: no TODO may reach a reader through this tool."""
    monkeypatch.setattr(
        connector,
        "_query_mart",
        lambda *_a, **_kw: [_row("impressions", 1.0)],
    )

    answer = connector.get_google_ad_manager_report(project_id="proj_EXAMPLE")

    assert "TODO" not in repr(answer)


def test_the_mart_query_scopes_the_connector_and_never_reads_raw(connector):
    """AD-12, read off the statement itself."""
    sql = connector._MART_QUERY

    assert "fact_daily_kpi" in connector._get_mart_table("duckdb", "proj_EXAMPLE")
    assert "connector = 'google-ad-manager'" in sql
    assert "raw_" not in sql
    # The window and the project are bound, never interpolated into the string.
    assert "{p_project}" in sql and "{p_from}" in sql and "{p_to}" in sql
