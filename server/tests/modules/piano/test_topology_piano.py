"""Story 28.1 -- piano site topology (organization -> site).

Piano has NO site-enumeration endpoint: discovery VERIFIES candidate site ids
via a 1-day getData access-check probe and returns only the reachable ones.
The access check is a getData probe; a 403 UnauthorizedSite drops the site.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from core.account_topology import validate_topology
from core.pull_errors import PermissionDeniedError

from .conftest import GETDATA_URL, MANIFEST, SITE_ID


def _datafeed(rows):
    return {"DataFeed": [{"Rows": rows}]}


def test_manifest_topology_is_valid_site_level():
    topo = MANIFEST["account_topology"]
    assert validate_topology(topo) == []
    assert topo["selection_level"] == "site"
    assert {lvl["id"] for lvl in topo["levels"]} == {"organization", "site"}
    assert topo["discovery"]["callable"] == "discover_accounts"


@respx.mock
def test_discover_accounts_verifies_candidates_drops_unreachable(connector):
    """Two candidates: one reachable (200), one UnauthorizedSite (403). Only the
    reachable one is returned."""
    ok_body = _datafeed([{"m_visits": 1}])
    bad_body = {"ErrorCode": "UnauthorizedSite", "ErrorMessage": "no access"}

    def _responder(request):
        import json

        body = json.loads(request.content)
        site = body["space"]["s"][0]
        if site == int(SITE_ID):
            return httpx.Response(200, json=ok_body)
        return httpx.Response(403, json=bad_body)

    respx.post(GETDATA_URL).mock(side_effect=_responder)

    accounts = connector.discover_accounts(
        "c", candidate_site_ids=[SITE_ID, "999999"]
    )
    ids = {a["id"] for a in accounts}
    assert ids == {SITE_ID}
    assert accounts[0]["level"] == "site"


@respx.mock
def test_check_account_access_probes_getdata_one_day(connector):
    """The access check is a 1-day getData probe (m_visits, max-results 1) on
    the selected site."""
    import json

    route = respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(200, json=_datafeed([{"m_visits": 5}]))
    )
    result = connector.check_account_access("c", SITE_ID)
    assert result == {"account_id": SITE_ID, "ok": True}
    body = json.loads(route.calls.last.request.content)
    assert body["columns"] == ["m_visits"]
    assert body["space"] == {"s": [int(SITE_ID)]}
    assert body["max-results"] == 1


@respx.mock
def test_check_account_access_403_raises_permission_denied(connector):
    body = {"ErrorCode": "UnauthorizedSite", "ErrorMessage": "no access"}
    respx.post(GETDATA_URL).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(PermissionDeniedError):
        connector.check_account_access("c", SITE_ID)
