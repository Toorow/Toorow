"""Tests for the Stripe pull() function — Story 15.7 (Epic 15).

Uses respx to mock the Stripe REST /v1/charges API. No test contacts the real API.
Real Stripe E2E is a human gate (BLOCKED Phase B, AI-08/AI-13: live keys; the Stripe TEST
mode may serve as a first live pass in Phase B).

Coverage:
  * parse: centimes -> units EXPLICIT conversion (incl. zero-decimal JPY), fee via
    balance_transaction, client_reference_id extraction (metadata / payment_intent.metadata),
    payment_intent id (string vs expanded object).
  * pull: window params (created[gte]/created[lte], expand, limit), rows land in
    raw_stripe_payments with the canonical rename (amount->revenue, amount_refunded->refunds,
    fee_amount->fees), refunds/fees in DEDICATED columns (never netted into revenue).
  * bounded pagination via starting_after cursor + anti-loop guard (non-progressing cursor).
  * 429 -> RateLimitError("stripe", retry_after); non-200 -> RuntimeError.
  * AD-3: token never stored in the return value or logged; nango provider='stripe', Bearer header.
  * golden fixture routed through parse+transform (AI-54 honest).
  * seam AI-56: multi-day values assert on VALUES (revenue/refunds/fees), not just HTTP 200.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "stripe" / "connector.py"
)

_CHARGES_URL = "https://api.stripe.com/v1/charges"


def _charge(
    charge_id: str,
    created: int,
    *,
    amount: int,
    amount_refunded: int = 0,
    fee: int | None = None,
    currency: str = "eur",
    payment_intent: object = None,
    client_reference_id: str | None = None,
) -> dict:
    """Build a mock Stripe Charge (amounts in MINOR units / centimes -- AI-53 shape)."""
    c: dict = {
        "object": "charge",
        "id": charge_id,
        "created": created,
        "amount": amount,
        "amount_refunded": amount_refunded,
        "currency": currency,
    }
    if payment_intent is not None:
        c["payment_intent"] = payment_intent
    if client_reference_id is not None:
        c["metadata"] = {"client_reference_id": client_reference_id}
    if fee is not None:
        c["balance_transaction"] = {"object": "balance_transaction", "fee": fee}
    return c


def _envelope(charges: list[dict], *, has_more: bool = False) -> dict:
    return {"object": "list", "data": charges, "has_more": has_more}


# Fixed UTC epochs across two days (verified: datetime.fromtimestamp(x, UTC).date()).
_D1 = 1782900900  # 2026-07-01 10:15:00 UTC
_D2 = 1782987300  # 2026-07-02 10:15:00 UTC

_BASIC = _envelope(
    [
        _charge("ch_1", _D1, amount=12850, fee=398, payment_intent="pi_1",
                client_reference_id="1000000001"),
        _charge("ch_2", _D1, amount=6400, fee=211, payment_intent="pi_2"),
        _charge("ch_3", _D2, amount=21090, amount_refunded=4218, fee=637, payment_intent="pi_3"),
    ]
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_stripe_pull", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# parse / conversion unit tests (no HTTP)
# ---------------------------------------------------------------------------

def test_amount_conversion_centimes_to_units(connector):
    """EUR amounts (centimes) are divided by 100 -- EXPLICIT conversion (not silent)."""
    assert connector._amount_to_units(12850, "eur") == pytest.approx(128.50)
    assert connector._amount_to_units(398, "EUR") == pytest.approx(3.98)
    assert connector._amount_to_units(0, "eur") == 0.0
    assert connector._amount_to_units(None, "eur") == 0.0


def test_amount_conversion_zero_decimal_currency_not_divided(connector):
    """JPY/KRW are zero-decimal -> already in units, NOT divided by 100."""
    assert connector._amount_to_units(15000, "jpy") == pytest.approx(15000.0)
    assert connector._amount_to_units(15000, "JPY") == pytest.approx(15000.0)
    # A decimal currency IS divided (contrast).
    assert connector._amount_to_units(15000, "usd") == pytest.approx(150.0)


def test_parse_charge_maps_source_fields_and_converts(connector):
    """_parse_charge extracts source names + converts centimes; keeps SOURCE metric names."""
    row = connector._parse_charge(
        _charge("ch_x", _D1, amount=21090, amount_refunded=4218, fee=637,
                payment_intent="pi_x", client_reference_id="ORD-9")
    )
    assert row["date"] == "2026-07-01"
    assert row["charge_id"] == "ch_x"
    assert row["payment_intent_id"] == "pi_x"
    assert row["client_reference_id"] == "ORD-9"
    # SOURCE names (renamed to canonical only by transform()).
    assert row["amount"] == pytest.approx(210.90)
    assert row["amount_refunded"] == pytest.approx(42.18)
    assert row["fee_amount"] == pytest.approx(6.37)
    assert row["revenue_source_currency"] == "EUR"


def test_parse_client_reference_id_from_payment_intent_metadata(connector):
    """client_reference_id can also come from an EXPANDED payment_intent.metadata."""
    charge = {
        "object": "charge",
        "id": "ch_pi",
        "created": _D1,
        "amount": 1000,
        "currency": "eur",
        "payment_intent": {"id": "pi_expanded", "metadata": {"client_reference_id": "REF-42"}},
    }
    row = connector._parse_charge(charge)
    assert row["payment_intent_id"] == "pi_expanded"
    assert row["client_reference_id"] == "REF-42"


def test_parse_client_reference_id_absent_is_none(connector):
    """A raw Charge with no client_reference_id -> None (honest NULL, souvent absent AI-53)."""
    row = connector._parse_charge(_charge("ch_bare", _D1, amount=999, payment_intent="pi_bare"))
    assert row["client_reference_id"] is None


# ---------------------------------------------------------------------------
# pull() — window, landing, rename, dedicated columns
# ---------------------------------------------------------------------------

@respx.mock
def test_pull_calls_charges_endpoint_with_window(connector, tmp_path, monkeypatch):
    """pull hits /v1/charges with created[gte]/created[lte], limit, expand balance_transaction."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st.duckdb"))

    route = respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=_BASIC))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-st", pull_id="pull_st_p1",
        )

    assert route.called
    params = route.calls.last.request.url.params
    assert "created[gte]" in params and "created[lte]" in params
    assert params["limit"] == "100"
    assert params["expand[]"] == "data.balance_transaction"


@respx.mock
def test_pull_lands_rows_with_refunds_and_fees_dedicated_columns(connector, tmp_path, monkeypatch):
    """Rows land in raw_stripe_payments; refunds and fees are DEDICATED positive columns
    (never subtracted from revenue -- decision de story 15.7). Canonical rename applied."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "st_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=_BASIC))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-st", pull_id="pull_st_p2",
        )

    assert result["row_count"] == 3

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT charge_id, date, revenue, refunds, fees, client_reference_id "
        "FROM raw_stripe_payments WHERE pull_id = 'pull_st_p2' ORDER BY charge_id"
    ).fetchall()
    con.close()

    assert len(rows) == 3
    by_charge = {r[0]: r for r in rows}
    # gross revenue preserved (refund NOT netted into revenue).
    assert by_charge["ch_3"][2] == pytest.approx(210.90)
    assert by_charge["ch_3"][3] == pytest.approx(42.18)   # refunds in own column
    assert by_charge["ch_1"][4] == pytest.approx(3.98)    # fees in own column
    # created epoch -> day-grain date (never '').
    assert by_charge["ch_1"][1] == "2026-07-01"
    assert all(r[1] != "" for r in rows)
    # client_reference_id kept when present, NULL when absent.
    assert by_charge["ch_1"][5] == "1000000001"
    assert by_charge["ch_2"][5] is None


@respx.mock
def test_pull_ai56_seam_multi_day_values(connector, tmp_path, monkeypatch):
    """AI-56 seam: multi-day seed, assert on VALUES (per-day revenue/refunds), not just HTTP 200."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "st_seam.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=_BASIC))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-st", pull_id="pull_st_seam",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    per_day = dict(
        con.execute(
            "SELECT date, SUM(revenue) FROM raw_stripe_payments "
            "WHERE pull_id = 'pull_st_seam' GROUP BY date ORDER BY date"
        ).fetchall()
    )
    day2_refunds = con.execute(
        "SELECT SUM(refunds) FROM raw_stripe_payments "
        "WHERE pull_id = 'pull_st_seam' AND date = '2026-07-02'"
    ).fetchone()[0]
    con.close()

    # 2026-07-01: 128.50 + 64.00 = 192.50 ; 2026-07-02: 210.90.
    assert per_day["2026-07-01"] == pytest.approx(192.50)
    assert per_day["2026-07-02"] == pytest.approx(210.90)
    assert day2_refunds == pytest.approx(42.18)


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

@respx.mock
def test_pull_follows_starting_after_pagination(connector, tmp_path, monkeypatch):
    """pull advances via starting_after cursor across pages while has_more is true (multi-date)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "st_page.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    page1 = _envelope([_charge("ch_p1", _D1, amount=10000, fee=100, payment_intent="pi_p1")],
                      has_more=True)
    page2 = _envelope([_charge("ch_p2", _D2, amount=20000, fee=200, payment_intent="pi_p2")],
                      has_more=False)
    route = respx.get(_CHARGES_URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-st", pull_id="pull_st_pg",
        )

    assert route.call_count == 2, "page 2 must be fetched exactly once"
    assert result["row_count"] == 2
    # second request carries starting_after = last id of page 1.
    assert route.calls[1].request.url.params["starting_after"] == "ch_p1"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    dates = sorted(
        r[0] for r in con.execute(
            "SELECT DISTINCT date FROM raw_stripe_payments WHERE pull_id = 'pull_st_pg'"
        ).fetchall()
    )
    con.close()
    assert dates == ["2026-07-01", "2026-07-02"]


@respx.mock
def test_pull_pagination_guard_stops_on_non_progressing_cursor(connector, tmp_path, monkeypatch):
    """A server that keeps returning has_more=true with the SAME last id must be bounded.

    The connector's seen-cursor guard breaks when starting_after repeats -- it never hangs.
    Same charge id every page -> starting_after='ch_loop' on call 2, already seen -> stop.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_loop.duckdb"))

    def _always_more(request: httpx.Request):
        return httpx.Response(200, json=_envelope(
            [_charge("ch_loop", _D1, amount=100, fee=1, payment_intent="pi_loop")],
            has_more=True,
        ))

    route = respx.get(_CHARGES_URL).mock(side_effect=_always_more)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-st", pull_id="pull_st_loop",
        )

    # Bounded: page 1 fetches, page 2 detects the repeated cursor and stops. Never infinite.
    assert route.call_count == 2


@respx.mock
def test_pull_pagination_hard_cap(connector, tmp_path, monkeypatch):
    """Even with UNIQUE progressing cursors forever, the _MAX_PAGES hard cap bounds the loop."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_cap.duckdb"))
    monkeypatch.setattr(connector, "_MAX_PAGES", 3)

    counter = {"n": 0}

    def _unique_more(request: httpx.Request):
        counter["n"] += 1
        cid = f"ch_cap_{counter['n']}"
        return httpx.Response(200, json=_envelope(
            [_charge(cid, _D1, amount=100, fee=1, payment_intent="pi_cap")],
            has_more=True,
        ))

    route = respx.get(_CHARGES_URL).mock(side_effect=_unique_more)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-st", pull_id="pull_st_cap",
        )

    assert route.call_count == 3, "hard cap _MAX_PAGES must bound a forever-progressing cursor"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------

@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('stripe', retry_after) on a 429 (rate_limit)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_429.duckdb"))

    respx.get(_CHARGES_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "4"}, json={"error": {}})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-st", pull_id="pull_st_429",
            )

    assert exc_info.value.platform == "stripe"
    assert exc_info.value.retry_after == 4


@respx.mock
def test_pull_non_200_error_raises_runtime_error(connector, tmp_path, monkeypatch):
    """pull raises RuntimeError on non-200, non-429 responses."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_500.duckdb"))

    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError, match="500"):
            connector.pull(
                connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-st", pull_id="pull_st_500",
            )


@respx.mock
def test_pull_401_api_key_expired_raises_auth_expired_via_error_map(
    connector, tmp_path, monkeypatch
):
    """Story 25.7 (AC: error_map wired): a 401 with Stripe error.code='api_key_expired'
    routes to AuthExpiredError via the manifest error_map (not just the pure-HTTP
    class which would also be auth_expired, but this proves the error_map is loaded
    and the Stripe error.code payload is preserved as evidence).

    Stripe error response shape: {error: {type, code, message}}.
    core._extract_provider_code reads error.code -> "api_key_expired" -> matched to
    manifest entry "401:api_key_expired" -> "auth_expired" class.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_401.duckdb"))

    stripe_error = {
        "error": {
            "code": "api_key_expired",
            "doc_url": "https://stripe.com/docs/error-codes/api-key-expired",
            "message": "The API key provided has expired.",
            "type": "invalid_request_error",
        }
    }
    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(401, json=stripe_error))

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-st", pull_id="pull_st_401",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # Stripe error payload preserved as evidence.
    assert err.provider_payload["error"]["code"] == "api_key_expired"


def test_manifest_error_map_shape(connector):
    """Story 25.7: manifest.json carries a well-formed Stripe error_map.

    Keys are '<status>:<code>'; values are canonical pull_errors classes.
    The _error_map_note key (non-functional justification) must be excluded
    from actual routing (prefixed with '_').
    """
    from core.pull_errors import ERROR_CLASSES

    manifest = json.loads(
        (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    )
    error_map = manifest.get("error_map")
    assert isinstance(error_map, dict) and error_map, "manifest must declare error_map"
    # All routing entries (not notes) must map to valid canonical classes.
    for key, value in error_map.items():
        if key.startswith("_"):
            continue  # justification note, not a routing entry
        assert value in ERROR_CLASSES, (
            f"error_map[{key!r}] = {value!r} is not a canonical error class"
        )


# ---------------------------------------------------------------------------
# AD-3 secret discipline
# ---------------------------------------------------------------------------

@respx.mock
def test_pull_token_not_stored_or_logged(connector, tmp_path, monkeypatch, caplog):
    """AD-3: the secret key must not appear in the return value or any log record."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_tok.duckdb"))

    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=_BASIC))

    secret = "sk_test_super-secret-stripe-key-98765"
    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value=secret):
            result = connector.pull(
                connection_id="conn_st", date_from="2026-07-01", date_to="2026-07-03",
                project_id="jean-st", pull_id="pull_st_tok",
            )

    assert secret not in str(result)
    for record in caplog.records:
        assert secret not in record.getMessage()


@respx.mock
def test_pull_respects_nango_provider_and_bearer_header(connector, tmp_path, monkeypatch):
    """AD-3: token via nango_client.get_fresh_token(provider='stripe'), Authorization: Bearer."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "st_auth.duckdb"))

    captured: list[dict] = []

    def _capture(request: httpx.Request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_envelope([]))

    respx.get(_CHARGES_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="sk_bearer_tok") as mock_nango:
        connector.pull(
            connection_id="conn_st_auth", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-st", pull_id="pull_st_auth",
        )
        mock_nango.assert_called_once_with("conn_st_auth", provider="stripe")

    assert captured[0].get("authorization") == "Bearer sk_bearer_tok"
    # AI-53: Stripe-Version header pinned for field stability.
    assert "stripe-version" in {k.lower() for k in captured[0]}


# ---------------------------------------------------------------------------
# golden fixture (AI-54 honest) + dispatch
# ---------------------------------------------------------------------------

def test_golden_pull_through_parse_and_transform(connector):
    """AI-54 honest: golden_pull.json is the REAL Charge API shape ({object:'charge', created,
    amount(centimes), balance_transaction.fee, ...}). transform() routes API-shaped rows through
    _parse_charge (structural extraction + EXPLICIT centimes->units) THEN applies the manifest
    canonical rename (amount->revenue, amount_refunded->refunds, fee_amount->fees) and must produce
    expected_facts.json. Exercises _parse_charge on the fixture, not just the respx mocks."""
    fixtures = _TOOROW_PATH.parent / "tests" / "fixtures"
    golden = json.loads((fixtures / "golden_pull.json").read_text(encoding="utf-8"))
    expected = json.loads((fixtures / "expected_facts.json").read_text(encoding="utf-8"))
    assert connector.transform(golden) == expected
    # The zero-decimal JPY charge (last row) is NOT divided by 100.
    jpy = next(r for r in expected if r["revenue_source_currency"] == "JPY")
    assert jpy["revenue"] == pytest.approx(15000.0)


def test_dispatch_resolves_default_pull_fn():
    """AI-58: get_module_pull_fn('stripe') resolves the connector's pull()."""
    import types

    from core.main import get_module_pull_fn

    st_mod = _import_connector()
    loaded = types.SimpleNamespace(name="stripe", connector_module=st_mod)
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("stripe")
    assert fn is st_mod.pull


# ---------------------------------------------------------------------------
# Real Stripe E2E — BLOCKED Phase B (AI-08/AI-13): live keys (TEST mode may be first pass).
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Real Stripe E2E — BLOCKED Phase B (live/test keys, AI-08/AI-13)")
def test_pull_e2e_with_real_stripe():  # pragma: no cover
    """Placeholder for the live Stripe API smoke test (Phase B; Stripe TEST mode first pass)."""
