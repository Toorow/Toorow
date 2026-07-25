"""Story 26.4 -- Customer -> Account topology discovery + access check.

discover_accounts = User/Query then Accounts/Search paginated (Customer
Management v13 REST); nodes are customers with advertiser-account children
carrying the composite opaque id '<account_id>@<customer_id>'. Access check =
SubmitGenerateReport of a 1-day tier-core campaign report (submit success =
access proven). respx-mocked (AI-13: live probe deferred to 25.6).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import PermissionDeniedError

from .conftest import (
    ACCOUNT_ID,
    COMPOSITE_ACCOUNT,
    CUSTOMER_BASE,
    CUSTOMER_ID,
    SUBMIT_URL,
    fault_body,
)

_USER_QUERY_URL = f"{CUSTOMER_BASE}/User/Query"
_ACCOUNTS_SEARCH_URL = f"{CUSTOMER_BASE}/Accounts/Search"


def _account(acct_id, name, customer_id, status="Active"):
    return {
        "Id": int(acct_id),
        "Name": name,
        "Number": f"X{acct_id}",
        "ParentCustomerId": int(customer_id),
        "AccountLifeCycleStatus": status,
        "CurrencyCode": "EUR",
    }


@respx.mock
def test_discover_accounts_builds_customer_tree_with_composite_ids(connector):
    respx.post(_USER_QUERY_URL).mock(
        return_value=httpx.Response(200, json={"User": {"Id": 987654}})
    )
    search = respx.post(_ACCOUNTS_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "Accounts": [
                    _account(ACCOUNT_ID, "Toorow FR", CUSTOMER_ID),
                    _account("246813579", "Toorow US", CUSTOMER_ID),
                    _account("111222333", "Old", CUSTOMER_ID, status="Inactive"),
                    _account("999888777", "Agency Client", "360054321"),
                ]
            },
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        nodes = connector.discover_accounts("conn-1")

    by_id = {node["id"]: node for node in nodes}
    assert set(by_id) == {CUSTOMER_ID, "360054321"}
    assert all(node["level"] == "customer" for node in nodes)

    children = by_id[CUSTOMER_ID]["children"]
    # Inactive account filtered; composite id embeds the parent CustomerId.
    assert [c["id"] for c in children] == [
        f"{ACCOUNT_ID}@{CUSTOMER_ID}",
        f"246813579@{CUSTOMER_ID}",
    ]
    assert children[0]["label"] == "Toorow FR"
    assert children[0]["level"] == "account"
    assert children[0]["currency_code"] == "EUR"

    # Discovery request shape: UserId predicate + page info + devtoken header.
    req = search.calls.last.request
    body = json.loads(req.content)
    assert body["Predicates"] == [
        {"Field": "UserId", "Operator": "Equals", "Value": "987654"}
    ]
    assert body["PageInfo"]["Size"] == 1000
    assert req.headers["developertoken"] == "dev-token-test"
    assert req.headers["authorization"] == "Bearer tok"


@respx.mock
def test_discovery_paginates_accounts_search(connector):
    respx.post(_USER_QUERY_URL).mock(
        return_value=httpx.Response(200, json={"User": {"Id": 987654}})
    )
    full_page = {
        "Accounts": [
            _account(str(100000000 + i), f"Acct {i}", CUSTOMER_ID)
            for i in range(1000)
        ]
    }
    last_page = {"Accounts": [_account("200000001", "Tail", CUSTOMER_ID)]}
    search = respx.post(_ACCOUNTS_SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=full_page),
            httpx.Response(200, json=last_page),
        ]
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        nodes = connector.discover_accounts("conn-1")
    assert search.call_count == 2
    second_body = json.loads(search.calls[1].request.content)
    assert second_body["PageInfo"]["Index"] == 1
    assert sum(len(n["children"]) for n in nodes) == 1001


@respx.mock
def test_discovery_page_cap_with_full_last_page_warns_truncated(
    connector, monkeypatch, caplog
):
    """F-5 (review 26.4): when the hard page cap is hit and the last page is
    STILL FULL, the account list is likely incomplete -- an explicit warning
    is the observable signal (the topology node-list shape has no side
    channel for a truncated flag; live >1000-account pagination is a
    ROLLOUT_NOTES probe item)."""
    import logging

    monkeypatch.setattr(connector, "_MAX_ACCOUNT_PAGES", 2)
    respx.post(_USER_QUERY_URL).mock(
        return_value=httpx.Response(200, json={"User": {"Id": 987654}})
    )

    def _full_page(request):
        index = json.loads(request.content)["PageInfo"]["Index"]
        return httpx.Response(200, json={
            "Accounts": [
                _account(str(100000000 + index * 1000 + i), f"A{index}-{i}",
                         CUSTOMER_ID)
                for i in range(1000)
            ]
        })

    search = respx.post(_ACCOUNTS_SEARCH_URL).mock(side_effect=_full_page)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with caplog.at_level(logging.WARNING):
            nodes = connector.discover_accounts("conn-1")

    assert search.call_count == 2  # capped -- never a runaway loop
    assert sum(len(n["children"]) for n in nodes) == 2000
    truncation_warnings = [
        rec for rec in caplog.records
        if "microsoft_ads_discover_accounts_truncated" in rec.getMessage()
    ]
    assert truncation_warnings and truncation_warnings[0].levelno >= logging.WARNING
    assert "INCOMPLETE" in truncation_warnings[0].getMessage()


@respx.mock
def test_access_check_submits_one_day_tier_core_report(connector):
    """Access check = SubmitGenerateReport (1-day, tier-core campaign columns)
    with BOTH customer headers; success returns the request id as evidence."""
    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_probe"})
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.check_account_access("conn-1", COMPOSITE_ACCOUNT)

    assert result["ok"] is True
    assert result["customer_account_id"] == ACCOUNT_ID
    assert result["customer_id"] == CUSTOMER_ID
    assert result["report_request_id"] == "rr_probe"

    req = submit.calls.last.request
    assert req.headers["customerid"] == CUSTOMER_ID
    assert req.headers["customeraccountid"] == ACCOUNT_ID
    body = json.loads(req.content)["ReportRequest"]
    assert body["Type"] == "CampaignPerformanceReportRequest"
    assert body["Scope"] == {"AccountIds": [int(ACCOUNT_ID)]}
    start = body["Time"]["CustomDateRangeStart"]
    end = body["Time"]["CustomDateRangeEnd"]
    assert start == end  # 1-day probe window
    # Tier-core probe columns only (catalog_daily bundle).
    assert {"TimePeriod", "AccountId", "CampaignId", "Spend", "Impressions",
            "Clicks", "Conversions"} <= set(body["Columns"])


@respx.mock
def test_access_check_permission_denied_is_typed(connector):
    """403 body code 2003 (ReportingServiceAccountNotAuthorized) -> typed
    permission_denied with the provider payload preserved."""
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(
            403, json=fault_body(2003, "ReportingServiceAccountNotAuthorized")
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(PermissionDeniedError) as exc:
            connector.check_account_access("conn-1", COMPOSITE_ACCOUNT)
    assert "ReportingServiceAccountNotAuthorized" in str(exc.value.provider_payload)
