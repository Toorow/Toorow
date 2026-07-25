"""Story 26.5 -- multi-region routing (risk #1): 3 isolated hosts.

An EU profile NEVER calls the NA (or FE) host; discovery fans out over the 3
hosts and tolerates per-host failures (warning, never a global failure --
a single-region advertiser legitimately gets [] elsewhere).
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx

from .conftest import HOSTS

_DATE_FROM = (date.today() - timedelta(days=2)).isoformat()
_DATE_TO = (date.today() - timedelta(days=1)).isoformat()

_ROWS = [
    {
        "date": _DATE_FROM,
        "campaignId": 1,
        "campaignName": "EU camp",
        "campaignStatus": "ENABLED",
        "campaignBudgetCurrencyCode": "EUR",
        "impressions": 10,
        "clicks": 1,
        "cost": 0.5,
    }
]


def _gzip_body(rows) -> bytes:
    return gzip.compress(json.dumps(rows).encode("utf-8"))


@respx.mock
def test_eu_profile_never_calls_the_na_or_fe_host(connector, async_hooks):
    """Story DoD: profile EU n'appelle jamais le host NA."""
    s3 = "https://offline-report-storage.s3.amazonaws.com/eu.json.gz"
    # Catch-alls on the OTHER hosts: any call there fails the test.
    na_route = respx.route(host="advertising-api.amazon.com").mock(
        return_value=httpx.Response(500)
    )
    fe_route = respx.route(host="advertising-api-fe.amazon.com").mock(
        return_value=httpx.Response(500)
    )
    respx.post(f"{HOSTS['EU']}/reporting/reports").mock(
        return_value=httpx.Response(200, json={"reportId": "r-eu", "status": "PENDING"})
    )
    respx.get(f"{HOSTS['EU']}/reporting/reports/r-eu").mock(
        return_value=httpx.Response(
            200, json={"reportId": "r-eu", "status": "COMPLETED", "url": s3}
        )
    )
    respx.get(s3).mock(return_value=httpx.Response(200, content=_gzip_body(_ROWS)))

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with patch.object(connector, "_insert_raw_rows", return_value=3):
            result = connector.pull_sp_campaigns_daily(
                connection_id="conn-eu",
                date_from=_DATE_FROM,
                date_to=_DATE_TO,
                project_id="p",
                pull_id="pull_az_eu",
                account_id="EU:777",
                **async_hooks,
            )
    assert result["row_count"] == 3
    assert na_route.call_count == 0
    assert fe_route.call_count == 0


def test_account_id_must_carry_a_known_region(connector):
    with pytest.raises(ValueError, match="REGION"):
        connector._split_selected_account("777")
    with pytest.raises(ValueError, match="REGION"):
        connector._split_selected_account("XX:777")
    assert connector._split_selected_account("eu:777") == ("EU", "777")


@respx.mock
def test_discover_accounts_fans_out_and_tolerates_a_failing_host(
    connector, caplog
):
    """One host down = WARNING + the other hosts' profiles are still returned."""
    respx.get(f"{HOSTS['NA']}/v2/profiles").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "profileId": 1111,
                    "countryCode": "US",
                    "currencyCode": "USD",
                    "timezone": "America/Los_Angeles",
                    "accountInfo": {"id": "A1", "type": "seller", "name": "Acme US"},
                },
                {
                    "profileId": 2222,
                    "countryCode": "CA",
                    "currencyCode": "CAD",
                    "timezone": "America/Toronto",
                    "accountInfo": {
                        "id": "A2", "type": "vendor", "subType": "KDP_AUTHOR",
                        "name": "Acme Books",
                    },
                },
            ],
        )
    )
    respx.get(f"{HOSTS['EU']}/v2/profiles").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{HOSTS['FE']}/v2/profiles").mock(
        return_value=httpx.Response(500, json={"code": "500", "details": "down"})
    )

    with caplog.at_level(logging.WARNING):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            regions = connector.discover_accounts("conn-az")

    assert "amazon_ads_discovery_host_failed" in caplog.text
    assert [r["id"] for r in regions] == ["NA"]
    children = regions[0]["children"]
    assert {c["id"] for c in children} == {"NA:1111", "NA:2222"}
    # Every profile persists its region on the node (routing key).
    assert all(c["region"] == "NA" for c in children)
    # KDP authors are flagged (no Sponsored Brands access).
    kdp = next(c for c in children if c["id"] == "NA:2222")
    assert "KDP author" in kdp["label"]
    # The request carried the profile filters (dossier 1.5).
    req = respx.calls[0].request
    assert "profileTypeFilter=seller%2Cvendor" in str(req.url)
    assert "apiProgram=report" in str(req.url)


@respx.mock
def test_discover_accounts_all_hosts_down_raises(connector):
    for host in HOSTS.values():
        respx.get(f"{host}/v2/profiles").mock(
            return_value=httpx.Response(500, json={"code": "500", "details": "down"})
        )
    from core.pull_errors import ProviderTransientError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ProviderTransientError):
            connector.discover_accounts("conn-az")


@respx.mock
def test_check_account_access_probes_the_profile_region_host(connector):
    """Access check = ONE report-creation POST on the profile's own host;
    a 425 (identical probe already in flight) ALSO proves access."""
    route = respx.post(f"{HOSTS['EU']}/reporting/reports").mock(
        return_value=httpx.Response(425, json={"detail": "Duplicate request"})
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.check_account_access("conn-az", "EU:777")
    assert result["ok"] is True
    assert result["region"] == "EU"
    req = route.calls.last.request
    assert req.headers["amazon-advertising-api-scope"] == "777"
    body = json.loads(req.content)
    assert body["configuration"]["reportTypeId"] == "spCampaigns"


@respx.mock
def test_check_account_access_wrong_region_is_typed(connector):
    from core.pull_errors import PermissionDeniedError

    respx.post(f"{HOSTS['FE']}/reporting/reports").mock(
        return_value=httpx.Response(
            403, json={"code": "403", "details": "Not authorized to access scope"}
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(PermissionDeniedError):
            connector.check_account_access("conn-az", "FE:999")
