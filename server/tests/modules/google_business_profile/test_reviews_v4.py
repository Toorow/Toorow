"""Story 30.1 -- the `reviews` profile, and the two things it must not do.

The profile was decoched on 2026-07-31 after measurement: it did not exist. What
follows pins the behaviour that made it worth building, not that it merely runs.

  (a) transform_reviews() -- the pure mapping. Star ratings become 1..5 LEVELS,
      the UNSPECIFIED sentinel becomes NULL (never 0, which is off the scale),
      an anonymous reviewer loses their display name, the reviewer photo is
      never persisted at all, and a review with no reviewId is dropped rather
      than landed without its supersede key.
  (b) The _reviews_v4 adapter -- pagination on the legacy host, and the fact
      that the v4 URL shape lives in exactly one place.
  (c) THE PRECONDITION. A 403 from the allowlist-gated v4 host is an expected
      provisioning state of a freshly installed connector, so pull_reviews
      returns an explicit SKIP envelope and never raises. This is the case the
      story called its central doctrinal nuance and the one the audit found
      missing: without it, the gate produces exactly the crash-loop it promises
      to avoid.

No test contacts the real API (respx) or a real warehouse (landing mocked).
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

_PARENT = "accounts/1111/locations/2222"
_REVIEWS_URL = f"https://mybusiness.googleapis.com/v4/{_PARENT}/reviews"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location("connector_gbp_reviews", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _golden() -> dict:
    return json.loads((_FIXTURES_DIR / "golden_reviews.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) transform_reviews() -- pure mapping
# ---------------------------------------------------------------------------


def test_transform_reviews_maps_star_ratings_to_levels(connector):
    rows = connector.transform_reviews(
        _golden(), account_id="accounts/1111", location_id="locations/2222"
    )
    by_id = {row["review_id"]: row for row in rows}

    assert by_id["rev_aaa111"]["review_star_rating"] == 5
    assert by_id["rev_bbb222"]["review_star_rating"] == 3
    # STAR_RATING_UNSPECIFIED is an ABSENT rating, not a zero: 0 is not a value
    # on a 1..5 scale and would drag any average it entered.
    assert by_id["rev_ccc333"]["review_star_rating"] is None


def test_transform_reviews_carries_the_list_level_aggregates_on_every_row(connector):
    """averageRating / totalReviewCount are LEVELS of the location, repeated.

    Repeated deliberately: the rollup reads them last-value without a second
    table, and a level that only existed on one row could not be read at all.
    """
    rows = connector.transform_reviews(_golden(), location_id="locations/2222")
    assert {row["average_rating"] for row in rows} == {4.2}
    assert {row["total_review_count"] for row in rows} == {137}


def test_transform_reviews_honors_anonymity_and_never_persists_the_photo(connector):
    rows = connector.transform_reviews(_golden(), location_id="locations/2222")
    by_id = {row["review_id"]: row for row in rows}

    anonymous = by_id["rev_bbb222"]
    assert anonymous["reviewer_is_anonymous"] is True
    # The provider still sends a display name; honouring isAnonymous means not
    # keeping it, not keeping whatever placeholder it happened to send.
    assert anonymous["reviewer_display_name"] is None

    named = by_id["rev_aaa111"]
    assert named["reviewer_is_anonymous"] is False
    assert named["reviewer_display_name"] == "Camille D."

    # The photo URL is in the fixture and in no landed row (PII, lean).
    for row in rows:
        assert "profilePhotoUrl" not in row
        assert not any(
            isinstance(value, str) and "googleusercontent" in value
            for value in row.values()
        )


def test_transform_reviews_skips_a_review_without_its_supersede_key(connector):
    """No reviewId means no upsert key -- landing it would create a row nothing
    could ever supersede. Dropped with a warning, not landed half-identified."""
    listing = {"reviews": [{"starRating": "FOUR", "createTime": "2026-07-01T00:00:00Z"}]}
    assert connector.transform_reviews(listing) == []


def test_transform_reviews_keeps_the_reply_and_its_own_freshness(connector):
    rows = connector.transform_reviews(_golden(), location_id="locations/2222")
    by_id = {row["review_id"]: row for row in rows}

    assert by_id["rev_aaa111"]["review_reply_comment"] == "Thank you for the kind words."
    # The reply's updateTime is the freshness of the RESPONSE, not of the review.
    assert by_id["rev_aaa111"]["review_reply_update_time"] == "2026-07-03T08:00:00Z"
    assert by_id["rev_aaa111"]["review_update_time"] == "2026-07-02T09:14:00Z"
    assert by_id["rev_bbb222"]["review_reply_comment"] is None


# ---------------------------------------------------------------------------
# (b) The _reviews_v4 adapter -- the isolated legacy seam
# ---------------------------------------------------------------------------


@respx.mock
def test_reviews_v4_adapter_walks_every_page(connector):
    respx.get(_REVIEWS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "reviews": [{"reviewId": "r1", "starRating": "FIVE"}],
                    "averageRating": 4.0,
                    "totalReviewCount": 2,
                    "nextPageToken": "page2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "reviews": [{"reviewId": "r2", "starRating": "ONE"}],
                    "averageRating": 4.1,
                    "totalReviewCount": 2,
                },
            ),
        ]
    )

    listing = connector._reviews_v4.list_reviews(_PARENT, "tok")

    assert [r["reviewId"] for r in listing["reviews"]] == ["r1", "r2"]
    # The aggregates come from the LAST page -- the freshest statement of a level.
    assert listing["averageRating"] == 4.1
    assert listing["totalReviewCount"] == 2


def test_the_v4_host_is_named_in_exactly_one_place_for_reviews(connector):
    """The migration risk is contained iff one object knows the legacy shape.

    v4 is deprecated with no v1 replacement, so this connector will have to move.
    The adapter is only worth its name if the swap is a single class -- pinned
    here rather than trusted.
    """
    assert connector._reviews_v4.base == connector.GBP_V4_BASE
    assert connector._reviews_v4.surface == "reviews_v4"
    # v4 caps reviews.list at 50 rows per page (research dossier, section 9).
    assert connector._reviews_v4.page_size == 50

    source = _CONNECTOR_PATH.read_text(encoding="utf-8")
    # "/reviews" is built inside the adapter and nowhere else.
    assert source.count('/{parent}/reviews') == 1


# ---------------------------------------------------------------------------
# (c) The precondition: an allowlist gate is not a failure
# ---------------------------------------------------------------------------

_FORBIDDEN = {
    "error": {
        "code": 403,
        "message": "The caller does not have permission",
        "status": "PERMISSION_DENIED",
    }
}


@respx.mock
def test_403_on_reviews_is_prevented_not_raised(connector):
    """The central nuance of Story 30.1, exercised end to end.

    The v4 reviews host is allowlisted separately from the quota grant, so a
    brand-new connection gets 403 here while the daily profile works. Raising
    would requeue a call that cannot succeed until a human at Google acts; the
    profile therefore reports zero rows and says why.
    """
    respx.get(_REVIEWS_URL).mock(return_value=httpx.Response(403, json=_FORBIDDEN))

    with patch.object(connector, "_resolve_location_parent", return_value=_PARENT), \
         patch("core.nango_client.get_fresh_token", return_value="tok"), \
         patch.object(connector, "_land_rows") as land:
        result = connector.pull_reviews(
            "conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1",
            location_id="locations/2222",
        )

    assert result["prevented"] is True
    assert result["prevented_reason"] == "reviews_access_pending"
    assert result["row_count"] == 0
    # The four documented envelope keys survive the refusal (pull_contract).
    assert {"pull_id", "row_count", "date_from", "date_to"} <= set(result)
    # AI-307: the sentence names the GESTURE that releases the gate. The worker
    # writes it on the window, so this is the string a person ends up reading.
    assert "allowlist" in result["message"].lower()
    assert "re-ask" in result["message"].lower()
    # A skip lands NOTHING -- an empty write would look like "no reviews exist".
    land.assert_not_called()


@respx.mock
def test_401_on_reviews_still_raises_auth_expired(connector):
    """The skip is for a PROVISIONING gate, not for every error.

    A 401 means the credential stopped working, which the person can fix by
    reconnecting -- swallowing it would hide a broken connection behind a
    profile that quietly reports zero reviews forever.
    """
    respx.get(_REVIEWS_URL).mock(
        return_value=httpx.Response(401, json={"error": {"code": 401}})
    )

    with patch.object(connector, "_resolve_location_parent", return_value=_PARENT), \
         patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(Exception) as exc_info:
            connector.pull_reviews(
                "conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1",
                location_id="locations/2222",
            )

    assert exc_info.value.error_class == "auth_expired"
    assert connector.precondition_of(exc_info.value) is None


@respx.mock
def test_pull_reviews_lands_the_stream_when_access_is_granted(connector):
    respx.get(_REVIEWS_URL).mock(return_value=httpx.Response(200, json=_golden()))

    with patch.object(connector, "_resolve_location_parent", return_value=_PARENT), \
         patch("core.nango_client.get_fresh_token", return_value="tok"), \
         patch.object(connector, "_land_rows", return_value=3) as land:
        result = connector.pull_reviews(
            "conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1",
            location_id="locations/2222",
        )

    assert result["row_count"] == 3
    assert "prevented" not in result
    assert "skipped" not in result

    table, columns, rows, project_id = land.call_args[0][:4]
    assert table == "raw_gbp_review"
    assert project_id == "proj_1"
    assert [row["account_id"] for row in rows] == ["accounts/1111"] * 3
    assert [row["location_id"] for row in rows] == ["locations/2222"] * 3
    # Every landed row carries every declared column -- a column the connector
    # forgets lands NULL forever and nothing else would notice.
    for row in rows:
        assert set(name for name, _ in columns) <= set(row)


def test_pull_reviews_refuses_to_run_without_a_selected_location(connector):
    """Doctrine 25.5+: no env-var fallback for the account/entity selection."""
    with pytest.raises(ValueError, match="location_id"):
        connector.pull_reviews("conn_1", "2026-07-01", "2026-07-31", "proj_1", "pull_1")
