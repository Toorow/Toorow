"""Story 28.1 -- piano catalog_daily (the dynamic-catalog surface).

THE central behaviour: field_discovery=dynamic. A user selection may reference
a per-site CUSTOM key absent from the baseline catalog. That key is NOT refused
in advance -- it is SENT to getData; if the site does not define it the API
returns a 400 InvalidColumns_* which the connector surfaces as
InvalidRequestError + the pull_invalid_request drift signal. NEVER a crash,
NEVER a silent drop. A None selection falls back to the tier-core baseline,
PRUNED to numeric metrics.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import httpx
import pytest
import respx
from core.pull_errors import InvalidRequestError

from .conftest import GETDATA_URL, SITE_ID

_DAY = (date.today() - timedelta(days=2)).isoformat()


def _datafeed(rows):
    return {"DataFeed": [{"Rows": rows}]}


def _run_catalog(connector, selection, **overrides):
    kwargs = dict(
        connection_id="c",
        date_from=_DAY,
        date_to=_DAY,
        project_id="p",
        pull_id="pull_piano_cat",
        selection=selection,
        site_id=SITE_ID,
    )
    kwargs.update(overrides)
    return connector.pull_catalog_daily(**kwargs)


@respx.mock
def test_user_selection_builds_getdata_columns(connector, tmp_path, monkeypatch):
    """A user selection of baseline keys builds the columns array verbatim."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat.duckdb"))
    route = respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(
            200,
            json=_datafeed(
                [{"date": _DAY, "page": "/home", "m_page_loads": 55, "m_visits": 40}]
            ),
        )
    )
    selection = {"metrics": ["m_page_loads", "m_visits"], "dimensions": ["date", "page"]}
    result = _run_catalog(connector, selection)
    assert result["status"] == "completed"
    body = json.loads(route.calls.last.request.content)
    assert set(body["columns"]) == {"date", "page", "m_page_loads", "m_visits"}
    # dimensions precede metrics (grain-first column order).
    assert body["columns"][:2] == ["date", "page"]


@respx.mock
def test_unknown_custom_key_is_sent_then_400_becomes_invalid_request_drift(
    connector, caplog
):
    """A key UNKNOWN to the baseline (a candidate custom key) is NOT refused in
    advance: it is SENT. When the site does not define it, the API returns
    400 InvalidColumns_* -> InvalidRequestError + pull_invalid_request_drift."""
    body = {"ErrorCode": "InvalidColumns_UnknownColumn",
            "ErrorMessage": "Unknown column 'custom_not_a_real_key'"}
    route = respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(400, json=body)
    )
    selection = {
        "metrics": ["m_visits"],
        "dimensions": ["date", "custom_not_a_real_key"],
    }
    with caplog.at_level(logging.WARNING):
        with pytest.raises(InvalidRequestError) as exc:
            _run_catalog(connector, selection)

    assert exc.value.error_class == "invalid_request"
    assert exc.value.provider_status == 400
    # The unknown key WAS sent (not pre-refused) -- proves the dynamic surface.
    sent = json.loads(route.calls.last.request.content)
    assert "custom_not_a_real_key" in sent["columns"]
    # The drift signal fired (never a silent drop, never a crash).
    assert "pull_invalid_request_drift" in caplog.text


def test_unknown_key_charset_still_enforced_pre_call(connector):
    """A candidate custom key must still pass the column charset (no injection):
    a key with illegal characters is refused BEFORE any API call."""
    selection = {"metrics": ["m_visits"], "dimensions": ["date", "bad key!"]}
    with pytest.raises(InvalidRequestError) as exc:
        _run_catalog(connector, selection)
    assert "charset" in str(exc.value)


def test_excluded_baseline_field_refused_pre_call(connector):
    """A baseline field with exposure=excluded (AV QoS / MV / consent) is a
    typed pre-call refusal (no silent drop)."""
    # Pick an excluded field from the catalog dynamically.
    import json as _json

    from .conftest import MODULE_DIR

    cat = _json.loads((MODULE_DIR / "api_catalog.json").read_text())
    excluded = next(
        f for f in cat["fields"]
        if f["exposure"] == "excluded" and f["kind"] == "metric"
    )
    selection = {"metrics": ["m_visits", excluded["field_id"]], "dimensions": ["date"]}
    with pytest.raises(InvalidRequestError) as exc:
        _run_catalog(connector, selection)
    assert "excluded" in str(exc.value)


@respx.mock
def test_none_selection_falls_back_to_pruned_tier_core_baseline(
    connector, tmp_path, monkeypatch
):
    """selection=None -> the catalog tier-core default, PRUNED to numeric
    metrics (the LONG landing carries value_num)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_def.duckdb"))
    route = respx.post(GETDATA_URL).mock(
        return_value=httpx.Response(200, json=_datafeed([]))
    )
    result = _run_catalog(connector, None)
    assert result["status"] == "completed"
    body = json.loads(route.calls.last.request.content)
    # The default carries tier-core metrics + dimensions (non-empty columns).
    assert "m_visits" in body["columns"]
    assert "date" in body["columns"]


@respx.mock
def test_none_default_is_bounded_to_max_columns_single_call(
    connector, tmp_path, monkeypatch
):
    """F-1(b): the raw tier-core default is ~61 columns (> max_columns 50). The
    None fallback MUST be pruned to <= 50 columns so it fits ONE getData call
    (no chunk-merge for the default). 'date' is always kept."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_bound.duckdb"))
    calls = []

    def _responder(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_datafeed([]))

    respx.post(GETDATA_URL).mock(side_effect=_responder)
    result = _run_catalog(connector, None)
    assert result["status"] == "completed"
    # Default fits a single call, bounded at or under the ceiling.
    assert len(calls) == 1
    max_columns = connector._max_columns()
    assert len(calls[0]["columns"]) <= max_columns
    assert "date" in calls[0]["columns"]
    assert "m_visits" in calls[0]["columns"]


@respx.mock
def test_wide_selection_is_chunk_merged_on_the_grain(
    connector, tmp_path, monkeypatch
):
    """F-1(a): a selection wider than max_columns is split into several getData
    calls SHARING the same dimensions + a stable sort; the per-chunk wide rows
    MERGE on the (period, property-tuple) grain and the result is the UNION of
    the metric columns. Two chunks here, one merged grain."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_chunk.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    max_columns = connector._max_columns()
    # 2 dimensions + enough metrics to force exactly 2 chunks.
    dims = ["date", "device_type"]
    metric_budget = max_columns - len(dims)  # metrics per chunk
    metrics = [f"m_num_{i}" for i in range(metric_budget + 1)]  # forces 2 chunks

    # Chunk A carries the first metric_budget metrics; chunk B carries the last.
    # Both share the SAME grain (date, device_type) so they merge into one row.
    grain = {"date": _DAY, "device_type": "desktop"}

    def _responder(request):
        body = json.loads(request.content)
        cols = body["columns"]
        row = dict(grain)
        for c in cols:
            if c.startswith("m_num_"):
                row[c] = 10  # each metric present in its chunk
        return httpx.Response(200, json=_datafeed([row]))

    route = respx.post(GETDATA_URL).mock(side_effect=_responder)
    selection = {"metrics": metrics, "dimensions": dims}
    result = _run_catalog(connector, selection)
    assert result["status"] == "completed"

    # Exactly 2 getData calls (2 chunks), each sharing the dimension columns.
    assert result["requests_made"] == 2
    assert len(route.calls) == 2
    for call in route.calls:
        cols = json.loads(call.request.content)["columns"]
        assert "date" in cols and "device_type" in cols
        assert len(cols) <= max_columns
        # Stable dimension-ASC sort shared across chunks (grains align).
        assert json.loads(call.request.content)["sort"] == ["date"]

    # The merged grain landed the UNION of the metric columns (all N metrics for
    # the single grain -- no cell dropped across chunks).
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed = con.execute(
        "SELECT DISTINCT metric FROM raw_piano_daily WHERE pull_id ="
        " 'pull_piano_cat'"
    ).fetchall()
    con.close()
    assert {m[0] for m in landed} == set(metrics)


def test_wide_selection_that_cannot_be_chunked_is_typed_refusal_not_drift(
    connector,
):
    """F-1(c): a selection > max_columns whose DIMENSIONS alone leave no room to
    split metrics cannot be chunked -> a typed pre-call refusal DISTINCT from the
    unknown-column drift: provider_status is None (no API round-trip) and the
    message says the selection exceeds max_columns."""
    max_columns = connector._max_columns()
    # More dimensions than the ceiling: no room for even one metric per chunk.
    dims = ["date"] + [f"dim_{i}" for i in range(max_columns)]
    selection = {"metrics": ["m_visits"], "dimensions": dims}
    with pytest.raises(InvalidRequestError) as exc:
        _run_catalog(connector, selection)
    # Distinct from drift: no provider round-trip (status None), clear message.
    assert exc.value.provider_status is None
    msg = str(exc.value)
    assert "max_columns" in msg
    assert "reduce or split" in msg
