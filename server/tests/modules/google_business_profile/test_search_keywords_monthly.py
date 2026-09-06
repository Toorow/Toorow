"""Story 30.1 -- `search_keywords_monthly`, and the floor that must stay a floor.

Decoched on 2026-07-31 after measurement: the profile did not exist. Two things
make it worth building rather than merely present.

  (a) THE GRAIN IS OBSERVED, NOT ASSUMED. The endpoint takes a monthly RANGE and
      answers one aggregate per keyword over the whole range -- the response
      carries no month. So the only way a month column can be a fact is to
      request one month at a time and stamp the answer with the month asked for.
  (b) THE PRIVACY FLOOR SURVIVES. Google returns EITHER an exact `value` OR a
      `threshold` ("fewer than N"). Coercing the second into the first fabricates
      precision the source explicitly refuses to give, and it becomes invisible
      the moment it enters a SUM.

Plus the shared 0-QPM access gate: a 403 is an expected provisioning state and
comes back as an explicit skip.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-business-profile"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_FIXTURES_DIR = _MODULE_DIR / "tests" / "fixtures"

_KEYWORDS_URL = (
    "https://businessprofileperformance.googleapis.com/v1/locations/2222"
    "/searchkeywords/impressions/monthly"
)


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location("connector_gbp_keywords", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _golden() -> dict:
    return json.loads(
        (_FIXTURES_DIR / "golden_search_keywords.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# (a) The grain
# ---------------------------------------------------------------------------


def test_months_in_range_is_inclusive_at_both_ends(connector):
    assert connector._months_in_range("2026-07-04", "2026-07-28") == [(2026, 7)]
    assert connector._months_in_range("2026-11-01", "2027-02-15") == [
        (2026, 11), (2026, 12), (2027, 1), (2027, 2)
    ]


@respx.mock
def test_one_request_per_month_and_the_month_is_the_one_requested(connector):
    """The month column is a fact only because it was the unit asked for."""
    route = respx.get(_KEYWORDS_URL).mock(return_value=httpx.Response(200, json=_golden()))

    with patch("core.nango_client.get_fresh_token", return_value="tok"), \
         patch.object(connector, "_land_rows", return_value=6) as land:
        connector.pull_search_keywords_monthly(
            "conn_1", "2026-06-10", "2026-07-20", "proj_1", "pull_1",
            location_id="locations/2222",
        )

    assert route.call_count == 2
    asked = [dict(call.request.url.params) for call in route.calls]
    assert [(p["monthlyRange.startMonth.year"], p["monthlyRange.startMonth.month"])
            for p in asked] == [("2026", "6"), ("2026", "7")]
    # Each request asks for a SINGLE month (start == end): a wider range would
    # come back as one aggregate with no month, and the column would be a guess.
    for params in asked:
        assert params["monthlyRange.startMonth.year"] == params["monthlyRange.endMonth.year"]
        assert params["monthlyRange.startMonth.month"] == params["monthlyRange.endMonth.month"]

    rows = land.call_args[0][2]
    assert sorted({row["month"] for row in rows}) == ["2026-06", "2026-07"]


@respx.mock
def test_pagination_walks_every_page_of_a_month(connector):
    respx.get(_KEYWORDS_URL).mock(
        side_effect=[
            httpx.Response(200, json={
                "searchKeywordsCounts": [
                    {"searchKeyword": "k1", "insightsValue": {"value": "10"}}
                ],
                "nextPageToken": "p2",
            }),
            httpx.Response(200, json={
                "searchKeywordsCounts": [
                    {"searchKeyword": "k2", "insightsValue": {"value": "20"}}
                ],
            }),
        ]
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"), \
         patch.object(connector, "_land_rows", return_value=2) as land:
        connector.pull_search_keywords_monthly(
            "conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1",
            location_id="locations/2222",
        )

    rows = land.call_args[0][2]
    assert [row["search_keyword"] for row in rows] == ["k1", "k2"]


# ---------------------------------------------------------------------------
# (b) The privacy floor
# ---------------------------------------------------------------------------


def test_a_threshold_never_becomes_a_count(connector):
    rows = connector.transform_search_keywords(
        _golden()["searchKeywordsCounts"], "2026-07", location_id="locations/2222"
    )
    by_keyword = {row["search_keyword"]: row for row in rows}

    exact = by_keyword["bakery near me"]
    assert exact["search_keyword_impressions"] == 2410
    assert exact["search_keyword_impressions_threshold"] is None
    assert exact["is_thresholded"] is False

    floored = by_keyword["gluten free croissant"]
    # The count column stays EMPTY. Writing 15 there would state a measurement
    # Google refused to make, and no consumer could tell afterwards.
    assert floored["search_keyword_impressions"] is None
    assert floored["search_keyword_impressions_threshold"] == 15
    assert floored["is_thresholded"] is True


def test_the_two_impression_columns_are_mutually_exclusive(connector):
    rows = connector.transform_search_keywords(
        _golden()["searchKeywordsCounts"], "2026-07"
    )
    for row in rows:
        exact = row["search_keyword_impressions"]
        floor = row["search_keyword_impressions_threshold"]
        assert (exact is None) != (floor is None), row


def test_an_entry_without_a_keyword_is_dropped(connector):
    """The keyword is the grain key; a row without one could not be superseded."""
    assert connector.transform_search_keywords(
        [{"insightsValue": {"value": "99"}}], "2026-07"
    ) == []


# ---------------------------------------------------------------------------
# (c) The shared 0-QPM access gate
# ---------------------------------------------------------------------------


@respx.mock
def test_403_is_the_quota_gate_and_comes_back_as_a_skip(connector):
    respx.get(_KEYWORDS_URL).mock(
        return_value=httpx.Response(403, json={"error": {"code": 403,
                                                          "status": "PERMISSION_DENIED"}})
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"), \
         patch.object(connector, "_land_rows") as land:
        result = connector.pull_search_keywords_monthly(
            "conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1",
            location_id="locations/2222",
        )

    assert result["prevented"] is True
    assert result["prevented_reason"] == "google_access_pending"
    assert result["row_count"] == 0
    assert {"pull_id", "row_count", "date_from", "date_to"} <= set(result)
    # AI-307: the gesture, not the cause. "0 QPM" told a person nothing they
    # could do; "request Business Profile API quota for this project" does.
    assert "quota" in result["message"].lower()
    assert "re-ask" in result["message"].lower()
    land.assert_not_called()


def test_pull_search_keywords_refuses_to_run_without_a_selected_location(connector):
    with pytest.raises(ValueError, match="location_id"):
        connector.pull_search_keywords_monthly(
            "conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1"
        )
