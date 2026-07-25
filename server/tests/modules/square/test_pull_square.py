"""Tests for the Square pull() / discover_accounts() functions.

Uses respx to mock the Square Connect v2 REST API (/v2/payments, /v2/locations). No test
contacts the real API. Real Square E2E is a human gate (BLOCKED: no Square test/sandbox
account available -- see memory no-connector-test-accounts; verification: blocked).

Coverage:
  * parse: centimes -> units EXPLICIT conversion (incl. zero-decimal JPY), fee SUMMED over
    processing_fee[], order_id/location_id extraction (nullable), RFC 3339 created_at -> date.
  * pull: window params (begin_time/end_time RFC 3339, sort, limit, optional location_id),
    rows land in raw_square_payments with the canonical rename (amount->revenue,
    refunded->refunds, fee_amount->fees), refunds/fees in DEDICATED columns.
  * bounded pagination via `cursor` + anti-loop guard (non-progressing cursor).
  * 429 -> RateLimitError("square", retry_after).
  * error_map: 401 ACCESS_TOKEN_REVOKED -> AuthRevokedError (the key distinction vs
    ACCESS_TOKEN_EXPIRED -> AuthExpiredError); both proven from Square's LIST error shape.
  * discover_accounts: GET /v2/locations -> [{id,label}] flat topology list.
  * AD-3: token never stored in the return value or logged; nango provider='square', Bearer header.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "square" / "connector.py"
)

_PAYMENTS_URL = "https://connect.squareup.com/v2/payments"
_LOCATIONS_URL = "https://connect.squareup.com/v2/locations"


def _payment(
    pid: str,
    created_at: str,
    *,
    amount: int,
    refunded: int | None = None,
    fees: object = None,
    currency: str = "EUR",
    location_id: str | None = "L_PARIS",
    order_id: str | None = None,
) -> dict:
    """Build a mock Square Payment (amounts in MINOR units / centimes -- AI-53 shape)."""
    p: dict = {
        "id": pid,
        "created_at": created_at,
        "status": "COMPLETED",
        "amount_money": {"amount": amount, "currency": currency},
    }
    if location_id is not None:
        p["location_id"] = location_id
    if order_id is not None:
        p["order_id"] = order_id
    if refunded is not None:
        p["refunded_money"] = {"amount": refunded, "currency": currency}
    if fees is not None:
        fee_list = fees if isinstance(fees, list) else [fees]
        p["processing_fee"] = [
            {"type": "INITIAL", "amount_money": {"amount": f, "currency": currency}}
            for f in fee_list
        ]
    return p


def _envelope(payments: list[dict], *, cursor: str | None = None) -> dict:
    d: dict = {"payments": payments}
    if cursor:
        d["cursor"] = cursor
    return d


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_square_pull", _TOOROW_PATH)
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
    """JPY is zero-decimal -> already in units, NOT divided by 100."""
    assert connector._amount_to_units(15000, "jpy") == pytest.approx(15000.0)
    assert connector._amount_to_units(15000, "JPY") == pytest.approx(15000.0)
    # A decimal currency IS divided (contrast).
    assert connector._amount_to_units(15000, "usd") == pytest.approx(150.0)


def test_parse_payment_maps_source_fields_and_converts(connector):
    """_parse_payment extracts source names + converts centimes; keeps SOURCE metric names."""
    row = connector._parse_payment(
        _payment(
            "sqpmt_x", "2026-07-02T11:05:00Z", amount=21090, refunded=4218,
            fees=[500, 137], location_id="L_LYON", order_id="sqord_x",
        )
    )
    assert row["date"] == "2026-07-02"
    assert row["payment_id"] == "sqpmt_x"
    assert row["order_id"] == "sqord_x"
    assert row["location_id"] == "L_LYON"
    # SOURCE names (renamed to canonical only by transform()).
    assert row["amount"] == pytest.approx(210.90)
    assert row["refunded"] == pytest.approx(42.18)
    # fees SUMMED across processing_fee[] (500 + 137 = 637 centimes -> 6.37).
    assert row["fee_amount"] == pytest.approx(6.37)
    assert row["revenue_source_currency"] == "EUR"


def test_parse_payment_nullable_order_id_and_missing_refund(connector):
    """order_id absent -> None; refunded_money absent -> 0.0."""
    row = connector._parse_payment(
        _payment("sqpmt_y", "2026-07-02T20:30:45Z", amount=3999, fees=141, order_id=None)
    )
    assert row["order_id"] is None
    assert row["refunded"] == 0.0
    assert row["amount"] == pytest.approx(39.99)


def test_transform_golden_matches_expected(connector):
    """transform(golden_pull) equals expected_facts byte-for-byte (AI-54 honest)."""
    import json  # noqa: PLC0415

    d = Path(_TOOROW_PATH).parent / "tests" / "fixtures"
    golden = json.loads((d / "golden_pull.json").read_text(encoding="utf-8"))
    expected = json.loads((d / "expected_facts.json").read_text(encoding="utf-8"))
    assert connector.transform(golden) == expected


# ---------------------------------------------------------------------------
# pull() — HTTP mocked with respx
# ---------------------------------------------------------------------------

def test_pull_calls_payments_endpoint_with_window(connector, tmp_path, monkeypatch):
    """pull sends begin_time/end_time RFC 3339, sort, limit; Bearer + Square-Version headers."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq.duckdb"))

    with respx.mock:
        route = respx.get(_PAYMENTS_URL).mock(
            return_value=httpx.Response(200, json=_envelope([_payment("p1", "2026-07-01T10:15:00Z", amount=12850, fees=398)]))  # noqa: E501
        )
        with patch("core.nango_client.get_fresh_token", return_value="fake-token") as tok:
            out = connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID1")

    assert out["row_count"] == 1
    assert tok.call_args.kwargs.get("provider") == "square"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer fake-token"
    assert req.headers["Square-Version"]
    assert "begin_time=2026-07-01" in str(req.url)
    assert "end_time=2026-07-01" in str(req.url)
    assert "sort_field=CREATED_AT" in str(req.url)


def test_pull_lands_rows_with_refunds_and_fees_dedicated_columns(connector, tmp_path, monkeypatch):
    """Rows land canonical-renamed; refunds/fees are DEDICATED columns (never netted)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "sq_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    payments = [_payment("p3", "2026-07-02T11:05:00Z", amount=21090, refunded=4218, fees=[500, 137])]  # noqa: E501
    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(return_value=httpx.Response(200, json=_envelope(payments)))
        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            connector.pull("conn-1", "2026-07-02", "2026-07-02", "default", "pull_ULID2")

    import duckdb  # noqa: PLC0415

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT revenue, refunds, fees FROM raw_square_payments WHERE payment_id='p3'"
    ).fetchone()
    con.close()
    assert row == (pytest.approx(210.90), pytest.approx(42.18), pytest.approx(6.37))


def test_pull_follows_cursor_pagination(connector, tmp_path, monkeypatch):
    """pull follows the `cursor` field across pages and lands all payments."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq_page.duckdb"))

    page1 = _envelope([_payment("pa", "2026-07-01T10:15:00Z", amount=1000, fees=30)], cursor="CUR2")
    page2 = _envelope([_payment("pb", "2026-07-01T12:15:00Z", amount=2000, fees=60)])
    responses = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]

    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(side_effect=responses)
        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            out = connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID3")

    assert out["row_count"] == 2


def test_pull_pagination_guard_stops_on_non_progressing_cursor(connector, tmp_path, monkeypatch):
    """A server returning the SAME cursor forever must not hang the worker."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq_loop.duckdb"))

    stuck = _envelope([_payment("pz", "2026-07-01T10:15:00Z", amount=500, fees=15)], cursor="SAME")
    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(return_value=httpx.Response(200, json=stuck))
        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            out = connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID4")

    # Terminates (guard) rather than looping forever.
    assert out["pull_id"] == "pull_ULID4"


def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('square', retry_after) on a 429 (RATE_LIMITED)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq_429.duckdb"))

    from core.quota import RateLimitError  # noqa: PLC0415

    body = {"errors": [{"category": "RATE_LIMITED", "code": "RATE_LIMITED"}]}
    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}, json=body))  # noqa: E501
        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            with pytest.raises(RateLimitError) as exc_info:
                connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID5")

    assert exc_info.value.retry_after == 7


def test_pull_401_access_token_revoked_maps_to_auth_revoked(connector, tmp_path, monkeypatch):
    """Square's LIST error {errors:[{code:ACCESS_TOKEN_REVOKED}]} on 401 routes to AuthRevokedError.

    Proves _square_error_payload surfaces the code so the manifest error_map refines: a bare
    401 would fall back to auth_expired, but ACCESS_TOKEN_REVOKED must classify as auth_revoked.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq_401.duckdb"))

    body = {"errors": [{"category": "AUTHENTICATION_ERROR", "code": "ACCESS_TOKEN_REVOKED", "detail": "revoked"}]}  # noqa: E501
    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(return_value=httpx.Response(401, json=body))
        from core.pull_errors import AuthRevokedError  # noqa: PLC0415

        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            with pytest.raises(AuthRevokedError) as exc_info:
                connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID6")

    assert exc_info.value.error_class == "auth_revoked"


def test_pull_401_access_token_expired_maps_to_auth_expired(connector, tmp_path, monkeypatch):
    """ACCESS_TOKEN_EXPIRED on 401 routes to AuthExpiredError (refreshable)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq_401b.duckdb"))

    body = {"errors": [{"category": "AUTHENTICATION_ERROR", "code": "ACCESS_TOKEN_EXPIRED"}]}
    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(return_value=httpx.Response(401, json=body))
        from core.pull_errors import AuthExpiredError  # noqa: PLC0415

        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            with pytest.raises(AuthExpiredError) as exc_info:
                connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID7")

    assert exc_info.value.error_class == "auth_expired"


def test_error_payload_preserves_evidence(connector):
    """_square_error_payload surfaces the first code AND preserves the original errors array."""
    resp = httpx.Response(403, json={"errors": [{"category": "AUTHENTICATION_ERROR", "code": "INSUFFICIENT_SCOPES"}]})  # noqa: E501
    norm = connector._square_error_payload(resp)
    assert norm["code"] == "INSUFFICIENT_SCOPES"
    assert isinstance(norm["errors"], list) and norm["errors"][0]["code"] == "INSUFFICIENT_SCOPES"


# ---------------------------------------------------------------------------
# discover_accounts() — account topology
# ---------------------------------------------------------------------------

def test_discover_accounts_lists_locations(connector):
    """discover_accounts returns the flat [{id,label}] topology list from GET /v2/locations."""
    body = {
        "locations": [
            {"id": "L_PARIS", "name": "Paris Flagship", "status": "ACTIVE", "currency": "EUR"},
            {"id": "L_LYON", "name": "Lyon", "status": "ACTIVE", "currency": "EUR"},
            {"id": "L_NONAME"},
        ]
    }
    with respx.mock:
        route = respx.get(_LOCATIONS_URL).mock(return_value=httpx.Response(200, json=body))
        with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
            accounts = connector.discover_accounts("conn-1")

    assert {"id": "L_PARIS", "label": "Paris Flagship"} in accounts
    assert {"id": "L_LYON", "label": "Lyon"} in accounts
    # A location with no name falls back to id as label.
    assert {"id": "L_NONAME", "label": "L_NONAME"} in accounts
    assert route.calls[0].request.headers["Square-Version"]


# ---------------------------------------------------------------------------
# AD-3 — token hygiene
# ---------------------------------------------------------------------------

def test_pull_token_not_stored_or_logged(connector, tmp_path, monkeypatch, caplog):
    """The access token must never appear in the return value or the logs (AD-3)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "sq_tok.duckdb"))

    secret = "super-secret-square-token"
    with respx.mock:
        respx.get(_PAYMENTS_URL).mock(return_value=httpx.Response(200, json=_envelope([_payment("p1", "2026-07-01T10:15:00Z", amount=1000, fees=30)])))  # noqa: E501
        with caplog.at_level(logging.INFO):
            with patch("core.nango_client.get_fresh_token", return_value=secret):
                out = connector.pull("conn-1", "2026-07-01", "2026-07-01", "default", "pull_ULID8")

    assert secret not in repr(out)
    assert secret not in caplog.text
