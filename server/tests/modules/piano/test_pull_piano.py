"""Story 28.1 -- piano getData pull mechanics (respx-mocked, no real API).

Covers: the getData request SHAPE (columns from the exact_bundle profile,
space.s=[site_id], period p1 D, sort, max-results/page-num), the LONG landing
(non-additive flag on visit metrics, no micro-currency conversion), the
BODY-CODE error taxonomy (401 BadAuthentication -> auth_expired), the
non-numeric-value never-dropped rule, pre-call refusals, and the x-api-key
header form. Live probe deferred (25.6 -- ROLLOUT_NOTES).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import AuthExpiredError, InvalidRequestError
from core.quota import RateLimitError

from .conftest import GETDATA_URL, SITE_ID

_DAY = (date.today() - timedelta(days=2)).isoformat()


def _datafeed(rows):
    return {"DataFeed": [{"Context": {}, "Columns": [], "Rows": rows, "totals": {}}]}


_BASELINE_ROW = {
    "date": _DAY,
    "device_type": "desktop",
    "geo_country": "France",
    "m_visits": 1240,
    "m_unique_visitors": 980,
    "m_page_loads": 4300,
}


def _run(connector, fn="pull_baseline_daily", **overrides):
    kwargs = dict(
        connection_id="c",
        date_from=_DAY,
        date_to=_DAY,
        project_id="p",
        pull_id="pull_piano_t1",
        site_id=SITE_ID,
    )
    kwargs.update(overrides)
    return getattr(connector, fn)(**kwargs)


@respx.mock
def test_getdata_request_shape_and_long_landing(connector, tmp_path, monkeypatch):
    """Columns come from the manifest bundle (AD-2); space/period/sort correct;
    visits/unique_visitors carry non_additive=TRUE; NO micro conversion."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "piano.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(200, json=_datafeed([_BASELINE_ROW]))
    )
    result = _run(connector)

    # 3 bundle metrics landed long for the single row.
    assert result["row_count"] == 3
    assert result["status"] == "completed"
    assert result["truncated"] is False
    assert result["non_numeric_landed"] == 0

    body = json.loads(route.calls.last.request.content)
    # columns = the exact_bundle dims + metrics (date IS a real column for Piano).
    assert body["columns"] == [
        "date", "device_type", "geo_country",
        "m_visits", "m_unique_visitors", "m_page_loads",
    ]
    assert body["space"] == {"s": [int(SITE_ID)]}
    assert body["period"] == {"p1": [{"type": "D", "start": _DAY, "end": _DAY}]}
    # sort key is the primary metric DESC and IS one of columns (Piano rule).
    assert body["sort"] == ["-m_visits"]
    assert body["max-results"] == 10000 and body["page-num"] == 1
    # x-api-key header = <access>_<secret>, never a bare key; never logged.
    assert route.calls.last.request.headers["x-api-key"] == "acc_sec"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, non_additive, site_id, segments_json"
        " FROM raw_piano_daily WHERE pull_id = 'pull_piano_t1' ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r for r in rows}
    # Canonical names, plain counts (no micros/1e6 division for Piano).
    assert by_metric["visits"][1] == 1240
    assert by_metric["unique_visitors"][1] == 980
    assert by_metric["page_loads"][1] == 4300
    assert "m_visits" not in by_metric
    # AD-4: visit/visitor metrics flagged non_additive; page_loads is additive.
    assert by_metric["visits"][2] is True
    assert by_metric["unique_visitors"][2] is True
    assert by_metric["page_loads"][2] is False
    assert all(r[3] == SITE_ID for r in rows)
    # Non-key properties (device_type/geo_country) live in segments_json grain.
    segments = json.loads(by_metric["visits"][4])
    assert segments["device_type"] == "desktop"
    assert segments["geo_country"] == "France"


@respx.mock
def test_default_pull_is_baseline_daily(connector, tmp_path, monkeypatch):
    """AI-58: profile-less pull() dispatches to the baseline grain."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "piano_def.duckdb"))
    route = respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(200, json=_datafeed([_BASELINE_ROW]))
    )
    result = _run(connector, fn="pull")
    assert result["row_count"] == 3
    body = json.loads(route.calls.last.request.content)
    assert "m_visits" in body["columns"]


def test_no_site_selected_is_a_hard_error_without_env_fallback(connector):
    """PIANO_*_SITE_ID env fallbacks are forbidden: unselected site = error."""
    with patch(
        "core.token_service.resolve_connection_by_nango_id", return_value=None
    ):
        with pytest.raises(ValueError) as exc:
            _run(connector, site_id=None)
    assert "topology" in str(exc.value)


def test_missing_api_key_is_a_hard_error(connector, monkeypatch):
    monkeypatch.delenv("PIANO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("PIANO_SECRET_KEY", raising=False)
    with respx.mock:
        respx.post(GETDATA_URL).mock(
            return_value=httpx.Response(200, json=_datafeed([]))
        )
        with pytest.raises(ValueError) as exc:
            _run(connector)
    assert "PIANO_ACCESS_KEY" in str(exc.value)


def test_malformed_date_refused_typed_pre_call(connector):
    with pytest.raises(InvalidRequestError):
        _run(connector, date_from="07/01/2026", date_to=_DAY)


def test_reversed_window_refused_typed_pre_call(connector):
    later = (date.today() - timedelta(days=1)).isoformat()
    earlier = (date.today() - timedelta(days=5)).isoformat()
    with pytest.raises(InvalidRequestError):
        _run(connector, date_from=later, date_to=earlier)


@respx.mock
def test_401_bad_authentication_maps_to_auth_expired(connector):
    """The Piano API-Code drives classification (dossier s.7): 401
    BadAuthentication_NoHeader -> auth_expired."""
    body = {"ErrorCode": "BadAuthentication_NoHeader",
            "ErrorMessage": "Incorrect login or password"}
    respx.post(GETDATA_URL).mock(return_value=httpx.Response(401, json=body))
    with pytest.raises(AuthExpiredError) as exc:
        _run(connector)
    assert exc.value.error_class == "auth_expired"
    assert exc.value.provider_status == 401


@respx.mock
def test_403_unauthorized_site_maps_to_permission_denied(connector):
    from core.pull_errors import PermissionDeniedError

    body = {"ErrorCode": "UnauthorizedSite",
            "ErrorMessage": "You are not authorised to access this site."}
    respx.post(GETDATA_URL).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(PermissionDeniedError):
        _run(connector)


@respx.mock
def test_403_invalid_space_prefix_maps_to_permission_denied(connector):
    """F-3: 403 InvalidSpace_NoActiveSite reduces to the InvalidSpace Category
    prefix, which the error_map declares explicitly -> permission_denied (not a
    bare-HTTP fallback)."""
    from core.pull_errors import PermissionDeniedError

    body = {"ErrorCode": "InvalidSpace_NoActiveSite",
            "ErrorMessage": "No active site in the requested space."}
    respx.post(GETDATA_URL).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(PermissionDeniedError):
        _run(connector)


@respx.mock
def test_509_quotas_exceeded_raises_rate_limit_breaker(connector):
    """509 QuotasExceeded stays on the RateLimitError breaker path (concurrency
    throttle), NOT the error_map."""
    body = {"ErrorCode": "QuotasExceeded_TooManyRequests",
            "ErrorMessage": "Too many simultaneous calls"}
    respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(509, json=body, headers={"Retry-After": "12"})
    )
    with pytest.raises(RateLimitError) as exc:
        _run(connector)
    assert exc.value.retry_after == 12


@respx.mock
def test_5xx_unknown_error_is_provider_transient(connector):
    from core.pull_errors import ProviderTransientError

    body = {"ErrorCode": "UnknownError", "ErrorMessage": "Unknown error"}
    respx.post(GETDATA_URL).mock(return_value=httpx.Response(500, json=body))
    with pytest.raises(ProviderTransientError):
        _run(connector)


@respx.mock
def test_non_numeric_metric_value_lands_in_segments_not_dropped(
    connector, tmp_path, monkeypatch, caplog
):
    """A non-numeric metric value is NEVER silently dropped: it lands in
    segments_json (key = the canonical metric name), a warning is logged, and
    the pull result counts it (non_numeric_landed)."""
    import logging

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "piano_nn.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    poisoned = dict(_BASELINE_ROW)
    poisoned["m_page_loads"] = "n/a"
    respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(200, json=_datafeed([poisoned]))
    )
    with caplog.at_level(logging.WARNING):
        result = _run(connector)

    assert result["non_numeric_landed"] == 1
    assert result["row_count"] == 2  # the poisoned metric landed no value row
    assert "piano_non_numeric_metric_landed" in caplog.text

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, segments_json FROM raw_piano_daily"
        " WHERE pull_id = 'pull_piano_t1'"
    ).fetchall()
    con.close()
    metrics = {r[0] for r in rows}
    assert "page_loads" not in metrics  # no corrupted value_num row
    for _metric, segments_raw in rows:
        segments = json.loads(segments_raw)
        assert segments["page_loads"] == "n/a"  # observable, not dropped
