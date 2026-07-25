"""Story 26.3 -- catalog_driven execution: pull_catalog_daily runs the async
report cycle EXCLUSIVELY through core.async_reports.run_async_report (26.1
socle): claim/dedup on the deterministic request_hash, re-poll resumption
after a deferred run, single resubmission on EXPIRED, download IMMEDIATELY
post-COMPLETED (5-minute link). All timing is fake-clock (no real sleeps);
respx-mocked HTTP; live probe deferred (AI-13 / 25.6).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from core.async_reports import InMemoryReportRefStore
from core.pull_errors import InvalidRequestError, ProviderTransientError

from .conftest import AD_ACCOUNT, API_BASE

_REPORTS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/reports"
_DOWNLOAD_URL = "https://pinterest-report-export.test/report.json"

_DAY = (date.today() - timedelta(days=2)).isoformat()

_SELECTION = {
    "metrics": ["SPEND_IN_MICRO_DOLLAR", "TOTAL_IMPRESSION"],
    "dimensions": ["date", "CAMPAIGN_ID", "CAMPAIGN_NAME"],
}

_REPORT_ROWS = [
    {
        "DATE": _DAY,
        "CAMPAIGN_ID": "626744128982",
        "CAMPAIGN_NAME": "Prospecting - FR",
        "SPEND_IN_MICRO_DOLLAR": 118400000,
        "TOTAL_IMPRESSION": 12040,
    }
]


class FakeClock:
    """Monotonic fake clock; the sleeper advances it (26.1 test pattern)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(connector, store, clock, **overrides):
    kwargs = dict(
        connection_id="c",
        date_from=_DAY,
        date_to=_DAY,
        project_id="p",
        pull_id="pull_pin_cat",
        selection=dict(_SELECTION),
        ad_account_id=AD_ACCOUNT,
        report_store=store,
        clock=clock,
        sleeper=clock.sleep,
        deadline_seconds=300,
    )
    kwargs.update(overrides)
    return connector.pull_catalog_daily(**kwargs)


def _poll_response(status: str, url: str | None = None) -> httpx.Response:
    payload: dict = {"report_status": status, "token": "tok123"}
    if url is not None:
        payload["url"] = url
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------------------
# Happy path: request shape + immediate download + LONG landing.
# ---------------------------------------------------------------------------


@respx.mock
def test_report_spec_built_from_selection_and_download_is_immediate(
    connector, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "pin_cat.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    poll = respx.get(_REPORTS_URL).mock(
        return_value=_poll_response("FINISHED", _DOWNLOAD_URL)
    )
    download = respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, json=_REPORT_ROWS)
    )

    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run(connector, store, clock)

    assert result["status"] == "completed"
    assert result["row_count"] == 2  # 2 metrics landed long
    assert result["resumed"] is False

    # Report spec: columns EXACTLY from the selection (date is the response
    # grain, never a requested column), level whitelisted, granularity DAY,
    # attribution PINNED explicit.
    body = json.loads(create.calls.last.request.content)
    assert body["columns"] == [
        "CAMPAIGN_ID", "CAMPAIGN_NAME", "SPEND_IN_MICRO_DOLLAR",
        "TOTAL_IMPRESSION",
    ]
    assert "DATE" not in body["columns"] and "date" not in body["columns"]
    assert body["level"] == "CAMPAIGN"
    assert body["granularity"] == "DAY"
    assert body["start_date"] == _DAY and body["end_date"] == _DAY
    assert body["click_window_days"] == 30
    assert body["engagement_window_days"] == 30
    assert body["view_window_days"] == 1
    assert body["conversion_report_time"] == "TIME_OF_AD_ACTION"
    assert body["report_format"] == "JSON"

    # Download happened IMMEDIATELY after the FINISHED poll (5-minute link):
    # the very next HTTP call after the last poll is the download.
    assert create.call_count == 1
    assert poll.call_count == 1
    assert download.call_count == 1
    all_calls = respx.calls
    assert str(all_calls[-2].request.url).startswith(_REPORTS_URL)
    assert str(all_calls[-1].request.url) == _DOWNLOAD_URL

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, data_level, campaign_id, date"
        " FROM raw_pinterest_ads_daily WHERE pull_id = 'pull_pin_cat'"
        " ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r for r in rows}
    # Catalog-driven micros rule at landing.
    assert by_metric["cost"][1] == pytest.approx(118.4)
    assert by_metric["impressions"][1] == 12040
    assert all(r[2] == "AD" or r[2] == "CAMPAIGN" for r in rows)
    assert all(r[2] == "CAMPAIGN" for r in rows)
    assert all(r[3] == "626744128982" and r[4] == _DAY for r in rows)


# ---------------------------------------------------------------------------
# Fake-clock resumption: deferred run -> retryable typed transient (review
# 26.3 F-2: NEVER a 0-row "done"), then re-poll, NEVER a second submit.
# ---------------------------------------------------------------------------


@respx.mock
def test_deadline_defers_then_next_run_resumes_by_repoll(
    connector, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_defer.duckdb"))

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    poll = respx.get(_REPORTS_URL).mock(
        return_value=_poll_response("IN_PROGRESS")
    )

    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)

    # Run 1: provider never finishes inside the budget -> the DEFERRED
    # outcome is surfaced as a RETRYABLE typed transient (review 26.3 F-2,
    # amazon-ads pattern): the queue retry policy re-runs the pull; the
    # reference stays persisted in flight. It is NEVER a completed 0-row pull.
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ProviderTransientError) as deferred_exc:
            _run(connector, store, clock, deadline_seconds=60)
    assert deferred_exc.value.retryable is True
    assert "deferred" in str(deferred_exc.value)
    assert create.call_count == 1
    polls_after_first = poll.call_count
    assert polls_after_first >= 1

    # Run 2 (same request shape => same request_hash): RESUMES by re-poll --
    # submit is NOT called again -- and completes with immediate download.
    poll.mock(return_value=_poll_response("FINISHED", _DOWNLOAD_URL))
    respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, json=_REPORT_ROWS)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        second = _run(connector, store, clock, deadline_seconds=60)

    assert second["status"] == "completed"
    assert second["resumed"] is True
    assert second["row_count"] == 2
    assert create.call_count == 1, "resumption must NEVER resubmit blindly"
    # Deterministic request hash is the dedup key across the two runs -- the
    # transient message carries it for the queue/audit trail.
    assert second["request_hash"] in str(deferred_exc.value)


@respx.mock
def test_expired_report_is_resubmitted_exactly_once(
    connector, tmp_path, monkeypatch
):
    """EXPIRED/CANCELLED/DOES_NOT_EXIST -> canonical expired: ONE resubmission
    (26.1 policy), then success here; a second expiry would be a typed
    ProviderTransientError (covered by the core 26.1 suite)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_exp.duckdb"))

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    respx.get(_REPORTS_URL).mock(
        side_effect=[
            _poll_response("EXPIRED"),
            _poll_response("FINISHED", _DOWNLOAD_URL),
        ]
    )
    respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, json=_REPORT_ROWS)
    )

    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run(connector, store, clock)

    assert result["status"] == "completed"
    assert create.call_count == 2, "exactly one resubmission after EXPIRED"


@respx.mock
def test_failed_report_raises_typed_provider_transient(
    connector, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_fail.duckdb"))
    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    respx.get(_REPORTS_URL).mock(return_value=_poll_response("FAILED"))

    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ProviderTransientError):
            _run(connector, store, clock)


# ---------------------------------------------------------------------------
# Pre-call typed refusals (no respx mock active: an HTTP attempt would raise
# ConnectError, not InvalidRequestError -- proving the refusal is PRE-CALL).
# ---------------------------------------------------------------------------


def _refusal_kwargs(selection=None, **overrides):
    kwargs = dict(
        connection_id="c",
        date_from=_DAY,
        date_to=_DAY,
        project_id="p",
        pull_id="pull_pin_refuse",
        selection=selection,
        ad_account_id=AD_ACCOUNT,
    )
    kwargs.update(overrides)
    return kwargs


@respx.mock
def test_device_path_column_exposed_and_submitted(connector, tmp_path, monkeypatch):
    """Review 26.3 F-1: the 99 CONVERSION DEVICE PATH columns are EXPOSED --
    they ride the exact same async request shape as every other async column
    (no operational reason to block them). The row-shape check is a probe
    CONTROL (ROLLOUT_NOTES), not an exposure gate."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_dp.duckdb"))

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    respx.get(_REPORTS_URL).mock(
        return_value=_poll_response("FINISHED", _DOWNLOAD_URL)
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, json=[]))

    selection = {
        "metrics": ["TOTAL_CHECKOUT_MOBILE_ACTION_TO_DESKTOP_CONVERSION"],
        "dimensions": ["date", "CAMPAIGN_ID"],
    }
    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run(connector, store, clock, selection=selection)
    assert result["status"] == "completed"
    body = json.loads(create.calls.last.request.content)
    assert "TOTAL_CHECKOUT_MOBILE_ACTION_TO_DESKTOP_CONVERSION" in body["columns"]


def test_excluded_product_item_column_refused_with_reason(connector):
    """The 11 PRODUCT_ITEM_* columns stay excluded as a REAL operational v1
    refusal (review 26.3 F-4: unreachable at any whitelisted level)."""
    selection = {
        "metrics": ["SPEND_IN_MICRO_DOLLAR"],
        "dimensions": ["date", "CAMPAIGN_ID", "PRODUCT_ITEM_NAME"],
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(**_refusal_kwargs(selection))
    assert exc.value.error_class == "invalid_request"
    assert "PRODUCT_ITEM_NAME" in str(exc.value)
    assert "excluded" in str(exc.value)


def test_sync_only_quiz_column_refused_on_async_path(connector):
    selection = {
        "metrics": ["QUIZ_PIN_RESULT_OPEN"],
        "dimensions": ["date", "CAMPAIGN_ID"],
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(**_refusal_kwargs(selection))
    assert "SYNC-ONLY" in str(exc.value)


def test_level_incompatible_dimension_refused_by_core_engine(connector):
    """AD_ID at level CAMPAIGN violates the selectable_set rule -- refused by
    core.field_compat.enforce_selection with the rule id in the message."""
    selection = {
        "metrics": ["SPEND_IN_MICRO_DOLLAR"],
        "dimensions": ["date", "CAMPAIGN_ID", "AD_ID"],
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(**_refusal_kwargs(selection, level="CAMPAIGN"))
    assert "level_selectable_campaign" in str(exc.value)
    assert "AD_ID" in str(exc.value)


@respx.mock
def test_same_dimension_legal_at_its_own_level(
    connector, tmp_path, monkeypatch
):
    """AD_ID IS selectable at level PIN_PROMOTION: the refusal above is level
    scoping, not a blanket ban -- the spec is submitted with the ad columns
    and the AD data_level stamp (level -> data_level map)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_lvl.duckdb"))

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    respx.get(_REPORTS_URL).mock(
        return_value=_poll_response("FINISHED", _DOWNLOAD_URL)
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, json=[]))

    selection = {
        "metrics": ["SPEND_IN_MICRO_DOLLAR"],
        "dimensions": ["date", "CAMPAIGN_ID", "AD_GROUP_ID", "AD_ID"],
    }
    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run(
            connector, store, clock, selection=selection, level="PIN_PROMOTION"
        )
    assert result["status"] == "completed"
    body = json.loads(create.calls.last.request.content)
    assert body["level"] == "PIN_PROMOTION"
    assert "AD_ID" in body["columns"]


def test_unknown_field_refused_as_catalog_drift(connector):
    selection = {
        "metrics": ["NOT_A_REAL_COLUMN"],
        "dimensions": ["date", "CAMPAIGN_ID"],
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(**_refusal_kwargs(selection))
    assert "NOT_A_REAL_COLUMN" in str(exc.value)


def test_non_whitelisted_level_refused(connector):
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            **_refusal_kwargs(dict(_SELECTION), level="PRODUCT_ITEM")
        )
    assert "PRODUCT_ITEM" in str(exc.value)


def test_keyword_level_refused_without_identity_key(connector):
    """Review 26.3 F-3: KEYWORD is OUT of the v1 whitelist -- the enum has no
    observable keyword identity column, so N keywords of an ad group would
    collapse onto one grain key (QUALIFY keeps one arbitrary row). Probe-only
    ratification (ROLLOUT_NOTES) if the response carries an identity key."""
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            **_refusal_kwargs(dict(_SELECTION), level="KEYWORD")
        )
    assert "KEYWORD" in str(exc.value)


def test_request_hash_is_column_order_insensitive(connector):
    """Review 26.3 F-7: two selections differing only by column ORDER are the
    same provider report -> same request hash (dedup/resume, no double
    submit); a different column SET still yields a different hash."""
    base = {"start_date": _DAY, "end_date": _DAY, "granularity": "DAY",
            "level": "CAMPAIGN", "report_format": "JSON"}
    spec_a = dict(base, columns=["TOTAL_IMPRESSION", "SPEND_IN_MICRO_DOLLAR"])
    spec_b = dict(base, columns=["SPEND_IN_MICRO_DOLLAR", "TOTAL_IMPRESSION"])
    spec_c = dict(base, columns=["SPEND_IN_MICRO_DOLLAR"])
    hash_a = connector._report_request_hash(AD_ACCOUNT, spec_a)
    assert hash_a == connector._report_request_hash(AD_ACCOUNT, spec_b)
    assert hash_a != connector._report_request_hash(AD_ACCOUNT, spec_c)


def test_async_window_beyond_186_day_range_refused(connector):
    date_from = (date.today() - timedelta(days=250)).isoformat()
    date_to = (date.today() - timedelta(days=2)).isoformat()
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            **_refusal_kwargs(dict(_SELECTION), date_from=date_from, date_to=date_to)
        )
    assert "186" in str(exc.value)


def test_async_window_beyond_914_days_back_refused(connector):
    date_from = (date.today() - timedelta(days=1000)).isoformat()
    date_to = (date.today() - timedelta(days=950)).isoformat()
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            **_refusal_kwargs(dict(_SELECTION), date_from=date_from, date_to=date_to)
        )
    assert "914" in str(exc.value)


# ---------------------------------------------------------------------------
# None selection -> tier-core default, PRUNED (26.2 review lesson).
# ---------------------------------------------------------------------------


@respx.mock
def test_none_selection_uses_pruned_tier_core_default(
    connector, catalog, tmp_path, monkeypatch
):
    """Default = catalog tier core, pruned to: DAY granularity, CAMPAIGN-level
    compatible dimensions only, numeric metric columns only. A user selection
    is never pruned (typed refusals above)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_def.duckdb"))

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(200, json={"report_status": "IN_PROGRESS",
                                               "token": "tok123"})
    )
    respx.get(_REPORTS_URL).mock(
        return_value=_poll_response("FINISHED", _DOWNLOAD_URL)
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, json=[]))

    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run(connector, store, clock, selection=None)
    assert result["status"] == "completed"

    body = json.loads(create.calls.last.request.content)
    columns = set(body["columns"])
    assert body["granularity"] == "DAY"

    # Core tier metrics are in (numeric by catalog physical_type).
    assert {"SPEND_IN_MICRO_DOLLAR", "TOTAL_IMPRESSION", "TOTAL_CLICKTHROUGH",
            "TOTAL_CONVERSIONS", "CTR"} <= columns
    # Campaign-lineage dims kept; deeper-level dims PRUNED (level=CAMPAIGN).
    assert {"CAMPAIGN_ID", "CAMPAIGN_NAME", "ADVERTISER_ID",
            "AD_ACCOUNT_ID"} <= columns
    for pruned in ("AD_GROUP_ID", "AD_GROUP_NAME", "AD_ID", "AD_NAME",
                   "PIN_ID", "PIN_PROMOTION_ID"):
        assert pruned not in columns, pruned
    # Excluded fields never enter the default; every sent column is a
    # numeric metric or a dimension per the catalog (no non-numeric metrics).
    by_id = {f["field_id"]: f for f in catalog["fields"]}
    for col in columns:
        entry = by_id[col]
        assert entry["exposure"] == "exposed", col
        if entry["kind"] == "metric":
            assert entry["physical_type"] in ("integer", "decimal"), col
