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


# ---------------------------------------------------------------------------
# L'organisation arrive par le parametre declare, jamais par `selection`.
#
# `selection` (celui du PLAN, core/schemas/datastream-intent.schema.json,
# $defs.selection, additionalProperties: false) ne porte que selection_mode /
# metrics / dimensions / grain / filters -- aucune organisation. Et
# core/queue.py ne remplit jamais job["selection"]. L'URN choisie par
# l'operateur arrive par account_topology.pull_parameter.
#
# Forme de l'identifiant : LinkedIn adresse organizationalEntity par une URN
# COMPLETE (`urn:li:organization:<id>`), jamais par un entier nu -- la research
# le dit ("Select an organization URN and URL-encode URNs"). Le connecteur ne
# doit donc rien reconstruire a partir d'un id nu.
# ---------------------------------------------------------------------------

ORG_URN = "urn:li:organization:123"


@pytest.fixture()
def http(connector, monkeypatch, tmp_path):
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(connector, "_token", lambda connection_id: "t")
    client = MagicMock()
    client.request.return_value = Response(
        payload={"elements": [{"followerGains": {"organicFollowerGain": 4}}], "paging": {}}
    )
    monkeypatch.setattr(connector.httpx, "Client", lambda *a, **k: client)
    return client


def test_manifest_declares_the_pull_parameter(connector):
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    declared = manifest["account_topology"].get("pull_parameter")
    assert declared == "organization_urn", (
        "Les trois endpoints organiques filtrent sur q=organizationalEntity + "
        "une URN d'organisation ; le manifeste doit nommer ce parametre pour que "
        "core/queue.py::_account_kwargs sache le remplir."
    )
    import inspect

    for name in (
        "pull",
        "pull_follower_statistics",
        "pull_page_statistics",
        "pull_organic_share_statistics",
    ):
        params = inspect.signature(getattr(connector, name)).parameters
        assert declared in params, f"{name}() n'accepte pas {declared!r}"
        assert params[declared].default is None, (
            f"{name}(): {declared} doit etre OPTIONNEL -- le worker ne passe le "
            f"compte que si une selection existe."
        )


def test_topology_contract_is_valid(connector):
    from core.account_topology import get_topology, validate_topology

    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert validate_topology(manifest["account_topology"]) == []
    assert get_topology(manifest) is not None


def test_discovery_id_is_the_full_urn(connector):
    """core ne retient que `id` (_flatten_account_ids). Un id synthetique
    ('linkedin_pages_selection_1') serait stocke tel quel puis repasse a pull()
    -- et LinkedIn refuserait un organizationalEntity qui n'est pas une URN."""
    client = MagicMock()
    client.request.return_value = Response(
        payload={"elements": [{"organization": ORG_URN, "organizationName": "Page"}]}
    )
    row = connector.discover_accounts("c", _client=client, _token_value="t")[0]
    assert row["id"] == ORG_URN
    assert row["label"] == "Page"


def test_pull_targets_the_organization_passed_as_a_parameter(connector, http):
    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-01",
        "proj",
        "pull-1",
        organization_urn=ORG_URN,
        selection={"lifetime": True},
    )
    params = http.request.call_args.kwargs["params"]
    assert params["organizationalEntity"] == ORG_URN
    assert params["q"] == "organizationalEntity"


def test_pull_without_an_organization_raises_a_named_typed_error(connector, http):
    with pytest.raises(connector.LinkedInPagesOnboardingError) as raised:
        connector.pull("conn", "2026-07-01", "2026-07-01", "proj", "pull-1")
    assert "organization_urn" in str(raised.value)
    http.request.assert_not_called()


def test_a_bare_id_is_refused_not_rebuilt_into_an_urn(connector, http):
    """Ne pas fabriquer 'urn:li:organization:' + id : une URN d'organizationBrand
    ou une URN d'un autre type se ferait passer pour une organisation."""
    with pytest.raises(connector.LinkedInPagesCompatibilityError) as raised:
        connector.pull(
            "conn", "2026-07-01", "2026-07-01", "proj", "pull-1", organization_urn="123"
        )
    assert "urn:li:" in str(raised.value)
    http.request.assert_not_called()


def test_the_report_selection_can_no_longer_smuggle_an_organization(connector, http):
    with pytest.raises(connector.LinkedInPagesOnboardingError):
        connector.pull(
            "conn",
            "2026-07-01",
            "2026-07-01",
            "proj",
            "pull-1",
            selection={"organization_urn": ORG_URN, "lifetime": True},
        )
    http.request.assert_not_called()


def test_report_selection_keys_still_reach_the_request(connector, http):
    connector.pull_page_statistics(
        "conn",
        "2026-07-01",
        "2026-07-01",
        "proj",
        "pull-1",
        organization_urn=ORG_URN,
        selection={"lifetime": True, "facet": "country"},
    )
    params = http.request.call_args.kwargs["params"]
    assert params["facet"] == "country"
    assert params["organizationalEntity"] == ORG_URN


def test_no_environment_fallback_for_the_organization(connector, http, monkeypatch):
    monkeypatch.setenv("LINKEDIN_ORGANIZATION_URN", ORG_URN)
    with pytest.raises(connector.LinkedInPagesOnboardingError):
        connector.pull("conn", "2026-07-01", "2026-07-01", "proj", "pull-1")
    source = (MODULE_DIR / "connector.py").read_text(encoding="utf-8")
    assert "LINKEDIN_ORGANIZATION_URN" not in source


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
