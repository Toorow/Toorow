"""Mocked tests for the Google Ad Manager REST pull() + discover_accounts().

AI-13: these encode the HTTP contract (submit -> run -> poll -> fetchRows +
networks) and the money/marginal doctrine. They are OFFLINE (respx intercepts
httpx at the transport level) and do NOT touch a live GAM network — the live
contract stays 'blocked' until a real network ratifies the response shapes.

Key assertions:
  * MONEY (ad_revenue) is KEPT IN MICROS (5_400_000, not /1e6) — division is at read.
  * Wide GAM rows expand to LONG marginal fact rows (one per metric x breakdown dim).
  * currency + report_timezone from networks.get are captured and passed to the insert.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
import respx

# .../server/modules/google-ad-manager/tests/test_pull_gam.py -> server/ is parents[3]
_SERVER_DIR = Path(__file__).parents[3]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


_FAKE_TOKEN = "ya29.fake_access_token"  # noqa: S105 -- test fixture, not a secret
_BASE = "https://admanager.googleapis.com/v1"
_NETWORK = "123"


# The module dir has a hyphen ("google-ad-manager") — not importable as a dotted
# path, so load connector.py by file location.
def _load_connector():
    path = Path(__file__).parents[1] / "connector.py"
    spec = importlib.util.spec_from_file_location("gam_connector_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def connector(monkeypatch: pytest.MonkeyPatch):
    mod = _load_connector()
    monkeypatch.setattr(
        "core.nango_client.get_fresh_token",
        lambda connection_id, provider=None: _FAKE_TOKEN,
        raising=False,
    )
    return mod


def _mock_report_flow(rows_payload: dict) -> None:
    respx.get(f"{_BASE}/networks/{_NETWORK}").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": f"networks/{_NETWORK}",
                "displayName": "Publisher Network",
                "currencyCode": "EUR",
                "timeZone": "Europe/Paris",
            },
        )
    )
    respx.post(f"{_BASE}/networks/{_NETWORK}/reports").mock(
        return_value=httpx.Response(200, json={"name": f"networks/{_NETWORK}/reports/999"})
    )
    respx.post(f"{_BASE}/networks/{_NETWORK}/reports/999:run").mock(
        return_value=httpx.Response(
            200, json={"name": f"networks/{_NETWORK}/operations/reports/999/runs/1"}
        )
    )
    respx.get(f"{_BASE}/networks/{_NETWORK}/operations/reports/999/runs/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "done": True,
                "response": {"reportResult": f"networks/{_NETWORK}/reportResults/555"},
            },
        )
    )
    respx.route(
        method="GET", url__startswith=f"{_BASE}/networks/{_NETWORK}/reportResults/555:fetchRows"
    ).mock(return_value=httpx.Response(200, json=rows_payload))


@respx.mock
def test_pull_expands_marginals_and_keeps_money_in_micros(
    connector, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def _fake_insert(rows, pull_id, project_id, currency=None, report_timezone=None):
        captured["rows"] = rows
        captured["currency"] = currency
        captured["report_timezone"] = report_timezone
        return len(rows)

    monkeypatch.setattr(connector, "_insert_raw_rows", _fake_insert)

    # One wide GAM row: DATE x AD_UNIT_NAME x LINE_ITEM_NAME with 3 metrics.
    rows_payload = {
        "rows": [
            {
                "dimensionValues": [
                    {"dateValue": {"year": 2026, "month": 7, "day": 1}},
                    {"stringValue": "Homepage_Leaderboard"},
                    {"stringValue": "LI_Summer"},
                ],
                "metricValueGroups": [
                    {
                        "primaryValues": [
                            {"intValue": "12000"},
                            {"intValue": "84"},
                            {"doubleValue": 5400000},
                        ]
                    }
                ],
            }
        ]
    }
    _mock_report_flow(rows_payload)

    result = connector.pull(
        connection_id="conn_1",
        date_from="2026-07-01",
        date_to="2026-07-01",
        project_id="default",
        pull_id="pull_gam_001",
        network_code=_NETWORK,
    )

    # 3 metrics x 2 breakdown dims (ad_unit_name, line_item_name) = 6 marginal rows.
    assert result["row_count"] == 6
    assert captured["currency"] == "EUR"
    assert captured["report_timezone"] == "Europe/Paris"

    rows = captured["rows"]
    assert {r["breakdown_dimension"] for r in rows} == {"ad_unit_name", "line_item_name"}
    assert {r["metric"] for r in rows} == {"impressions", "clicks", "ad_revenue"}

    # MONEY kept in MICROS (not divided) — division happens once at read.
    revenue_rows = [r for r in rows if r["metric"] == "ad_revenue"]
    assert revenue_rows and all(r["value"] == 5400000 for r in revenue_rows)

    # Marginals share the day total: impressions sum per breakdown axis == 12000.
    for axis in ("ad_unit_name", "line_item_name"):
        imps = sum(
            r["value"]
            for r in rows
            if r["metric"] == "impressions" and r["breakdown_dimension"] == axis
        )
        assert imps == 12000


@respx.mock
def test_discover_accounts_lists_networks(connector) -> None:
    respx.get(f"{_BASE}/networks").mock(
        return_value=httpx.Response(
            200,
            json={
                "networks": [
                    {"name": "networks/123", "displayName": "Publisher Network"},
                    {"name": "networks/456", "networkCode": "456", "displayName": "Second Net"},
                ]
            },
        )
    )

    accounts = connector.discover_accounts("conn_1")

    assert accounts == [
        {"id": "123", "label": "Publisher Network", "level": "network"},
        {"id": "456", "label": "Second Net", "level": "network"},
    ]
