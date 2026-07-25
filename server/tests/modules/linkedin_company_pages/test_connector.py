from __future__ import annotations

import importlib.util
import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parents[4]
MODULE_DIR = ROOT / "server" / "modules" / "linkedin-company-pages"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_linkedin_pages", MODULE_DIR / "connector.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload


def test_separate_from_ads_and_version_headers(connector):
    assert "linkedin-ads" not in (MODULE_DIR / "connector.py").read_text()
    # Version is env-var overridable; default is 202604. Assert the header key is set.
    version = connector.LINKEDIN_VERSION
    assert connector._headers("t")["Linkedin-Version"] == version
    assert connector._headers("t")["X-Restli-Protocol-Version"] == "2.0.0"
    # Env-var override works
    os.environ["LINKEDIN_COMPANY_PAGES_API_VERSION"] = "202605"
    import importlib
    _spec = importlib.util.spec_from_file_location(
        "connector_linkedin_pages_v", MODULE_DIR / "connector.py"
    )
    reloaded = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(reloaded)
    assert reloaded.LINKEDIN_VERSION == "202605"
    del os.environ["LINKEDIN_COMPANY_PAGES_API_VERSION"]


def test_sunset_gate(connector):
    # Version is 202604 by default. July 2026 is 3 months after April 2026 → age=3.
    assert connector.check_version_support(date(2026, 7, 1)) == 3
    # 12+ months after 202604 should raise.
    with pytest.raises(RuntimeError):
        connector.check_version_support(date(2027, 6, 1))


def test_discovery_acl_and_encoded_urn(connector):
    client = MagicMock()
    client.request.return_value = Response(
        payload={
            "elements": [{"organization": "urn:li:organization:1", "organizationName": "Page"}]
        }
    )
    row = connector.discover_accounts("c", _client=client, _token_value="t")[0]
    assert row["organization_path"] == "urn%3Ali%3Aorganization%3A1"
    assert client.request.call_args.kwargs["params"]["role"] == "ADMINISTRATOR"


def test_empty_access_actionable(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"elements": []})
    with pytest.raises(connector.LinkedInPagesOnboardingError):
        connector.discover_accounts("c", _client=client, _token_value="t")


def test_window_lag_retention_and_demographic_refusal(connector):
    connector.validate_window(
        "2025-07-22", "2026-07-20", lifetime=False, facet=None, today=date(2026, 7, 22)
    )
    with pytest.raises(connector.LinkedInPagesCompatibilityError):
        connector.validate_window(
            "2026-07-01", "2026-07-20", lifetime=False, facet="country", today=date(2026, 7, 22)
        )
    with pytest.raises(connector.LinkedInPagesCompatibilityError):
        connector.validate_window(
            "2025-07-19", "2026-07-20", lifetime=False, facet=None, today=date(2026, 7, 22)
        )


def test_restli_pagination(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"elements": [{"id": 1}], "paging": {"total": 2}}),
        Response(payload={"elements": [{"id": 2}], "paging": {"total": 2}}),
    ]
    assert [x["id"] for x in connector.paginate_restli(client, "t", "/x", params={}, count=1)] == [
        1,
        2,
    ]


def test_429_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(429, {}, {"Retry-After": "7"}))
    assert raised.value.retry_after == 7


def test_catalog_zero_planned_video_unavailable_and_negative_likes():
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    assert not [f for f in catalog["fields"] if f["exposure"] == "planned"]
    assert (
        next(r for r in manifest["source_capabilities"]["reports"] if r["id"] == "video_analytics")[
            "availability"
        ]["status"]
        == "unavailable"
    )
    fixture = json.loads((MODULE_DIR / "tests" / "fixtures" / "golden_pull.json").read_text())
    assert fixture[0]["likes"] == "-1"


def test_401_raises_auth_expired(connector):
    """H-2: A 401 response must surface as AuthExpiredError (auth_expired classification)."""
    from core.pull_errors import AuthExpiredError

    with pytest.raises(AuthExpiredError):
        connector._raise_response(
            Response(401, {"message": "Unauthorized", "code": "UNAUTHORIZED"})
        )


def test_land_page_statistics_produces_nonzero_rows(connector, tmp_path, monkeypatch):
    """C-1: _land() with a realistic page_statistics payload must produce non-zero rows.

    LinkedIn's organizationPageStatistics response nests views inside
    totalPageStatistics.views.<ViewType>.pageViews — the previous isinstance guard
    silently dropped all of them. This test asserts rows are written for all four
    declared page_statistics metrics.
    """
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "test.duckdb"))

    # Realistic API response for one time-interval row from organizationPageStatistics
    api_row = {
        "organizationalEntity": "urn:li:organization:123",
        "timeRange": {"start": 1753056000000, "end": 1753142400000},
        "totalPageStatistics": {
            "views": {
                "allPageViews": {"pageViews": 420},
                "allDesktopPageViews": {"pageViews": 310},
                "allMobilePageViews": {"pageViews": 110},
            },
            "clicks": {
                "mobileCustomButtonClickCounts": 5,
                "careersPageClicks": 12,
                "jobsPageClicks": 3,
            },
        },
    }

    context = {
        "profile": "page_statistics",
        "organization_urn": "urn:li:organization:123",
        "date_from": "2026-07-21",
        "date_to": "2026-07-21",
        "lifetime": False,
        "facet": "",
        "pull_id": "pull-test-001",
        "project_id": "proj-test",
    }

    count = connector._land([api_row], context)

    assert count > 0, "page_statistics landing must produce at least one row"

    # Verify the exact metrics landed
    import duckdb

    con = duckdb.connect(str(tmp_path / "test.duckdb"))
    rows = con.execute(
        "SELECT metric, value FROM raw_linkedin_company_pages_daily ORDER BY metric"
    ).fetchall()
    con.close()

    metric_map = {r[0]: r[1] for r in rows}
    assert "page_views" in metric_map, "page_views must be landed"
    assert metric_map["page_views"] == 420.0
    assert "desktop_page_views" in metric_map
    assert metric_map["desktop_page_views"] == 310.0
    assert "mobile_page_views" in metric_map
    assert metric_map["mobile_page_views"] == 110.0
    assert "clicks" in metric_map
    assert metric_map["clicks"] == 20.0  # 5 + 12 + 3
