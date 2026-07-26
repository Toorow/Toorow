"""Tests for the Stripe pull_catalog_daily() — Story 25.9.

PROJECTION STYLE: the pull fetches the same /v1/charges + expand[]=data.balance_transaction
as pull(); the selection controls which catalog fields are projected into landed rows at parse
time. No new Stripe endpoints are called.

Coverage:
  * projection lands exactly the selected metric and dimension fields per charge.
  * unselected fields are absent from the landed rows.
  * dotted-path extraction works (e.g. outcome.risk_score, billing_details.address.country).
  * BALANCE TRANSACTION fields read from charge.balance_transaction sub-object.
  * PLATFORM CONTRACT fields (revenue/refunds/fees) use _parse_charge centimes->units.
  * None selection falls back to catalog tier-core default.
  * AD-22: existing pull() profile (payments_daily) is bit-identical (legacy green).
  * AI-58 dispatch: get_module_pull_fn / manifest dispatch resolves pull_catalog_daily.
  * catalog_sources.json declares exposure_policy=catalog_driven + excluded_sections for
    CUSTOMER, INVOICE, SUBSCRIPTION, REFUND, PAYOUT.
  * api_catalog.json: zero planned fields; excluded sections have exclusion_reason.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[3] / "modules" / "stripe"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_CHARGES_URL = "https://api.stripe.com/v1/charges"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_stripe_catalog", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@pytest.fixture(scope="module")
def api_catalog():
    return json.loads((_MODULE_DIR / "api_catalog.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog_sources():
    return json.loads(
        (_MODULE_DIR / "catalog_sources" / "catalog_sources.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def manifest():
    return json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Charge builders
# ---------------------------------------------------------------------------

_D1 = 1782900900  # 2026-07-01 10:15:00 UTC
_D2 = 1782987300  # 2026-07-02 10:15:00 UTC


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
    outcome: dict | None = None,
    billing_country: str | None = None,
    charge_status: str = "succeeded",
) -> dict:
    """Build a mock Stripe Charge (amounts in minor units / centimes)."""
    c: dict = {
        "object": "charge",
        "id": charge_id,
        "created": created,
        "amount": amount,
        "amount_refunded": amount_refunded,
        "currency": currency,
        "status": charge_status,
    }
    if payment_intent is not None:
        c["payment_intent"] = payment_intent
    if client_reference_id is not None:
        c["metadata"] = {"client_reference_id": client_reference_id}
    if fee is not None:
        c["balance_transaction"] = {
            "object": "balance_transaction",
            "fee": fee,
            "net": amount - amount_refunded - fee,
            "status": "available",
            "type": "charge",
            "reporting_category": "charge",
            "id": f"txn_{charge_id}",
        }
    if outcome is not None:
        c["outcome"] = outcome
    if billing_country is not None:
        c["billing_details"] = {"address": {"country": billing_country}}
    return c


def _envelope(charges: list[dict], *, has_more: bool = False) -> dict:
    return {"object": "list", "data": charges, "has_more": has_more}


# ---------------------------------------------------------------------------
# catalog_sources.json contract
# ---------------------------------------------------------------------------

def test_catalog_sources_exposure_policy_catalog_driven(catalog_sources):
    """catalog_sources.json must declare exposure_policy=catalog_driven (Story 25.9)."""
    assert catalog_sources.get("exposure_policy") == "catalog_driven"


def test_catalog_sources_excluded_sections_present(catalog_sources):
    """excluded_sections must be declared for all non-fetchable object types."""
    excluded = catalog_sources.get("excluded_sections", {})
    for section in ("CUSTOMER", "INVOICE", "SUBSCRIPTION", "REFUND", "PAYOUT"):
        assert section in excluded, f"excluded_sections missing entry for {section}"
        assert excluded[section], f"excluded_sections[{section!r}] must have a non-empty reason"


def test_catalog_sources_excluded_reasons_mention_daily_pull(catalog_sources):
    """Each excluded section reason must be honest about the daily pull constraint."""
    excluded = catalog_sources.get("excluded_sections", {})
    for section, reason in excluded.items():
        assert "daily pull" in reason.lower() or "not fetched" in reason.lower(), (
            f"excluded_sections[{section!r}] reason should explain why the daily pull "
            f"cannot reach the section, got: {reason!r}"
        )


# ---------------------------------------------------------------------------
# api_catalog.json contract — zero planned, excluded have reasons
# ---------------------------------------------------------------------------

def test_api_catalog_zero_planned_fields(api_catalog):
    """Story 25.9 gate: zero fields with exposure='planned' — every field is
    exposed, catalog_driven, or excluded with a reason."""
    planned = [f["field_id"] for f in api_catalog["fields"] if f.get("exposure") == "planned"]
    assert planned == [], f"Fields still at 'planned': {planned}"


def test_api_catalog_excluded_fields_have_reason(api_catalog):
    """Every excluded field must carry an exclusion_reason (honest precise reason)."""
    missing = [
        f["field_id"]
        for f in api_catalog["fields"]
        if f.get("exposure") == "excluded" and not f.get("exclusion_reason")
    ]
    assert missing == [], f"Excluded fields missing exclusion_reason: {missing}"


def test_api_catalog_fetchable_sections_are_catalog_driven_or_exposed(api_catalog):
    """Fields in CHARGE / BALANCE TRANSACTION / PLATFORM CONTRACT must be
    'catalog_driven' or 'exposed' (never 'planned' or 'excluded')."""
    fetchable_sections = {"CHARGE", "BALANCE TRANSACTION", "PLATFORM CONTRACT"}
    bad = [
        f["field_id"]
        for f in api_catalog["fields"]
        if f["section"] in fetchable_sections
        and f.get("exposure") not in ("catalog_driven", "exposed")
    ]
    assert bad == [], (
        f"Fields in fetchable sections must be catalog_driven or exposed; "
        f"bad fields: {bad}"
    )


def test_api_catalog_excluded_sections_all_excluded(api_catalog):
    """Fields in CUSTOMER / INVOICE / SUBSCRIPTION / REFUND / PAYOUT must all be 'excluded'."""
    excluded_sections = {"CUSTOMER", "INVOICE", "SUBSCRIPTION", "REFUND", "PAYOUT"}
    bad = [
        f["field_id"]
        for f in api_catalog["fields"]
        if f["section"] in excluded_sections and f.get("exposure") != "excluded"
    ]
    assert bad == [], f"Fields in excluded sections not marked 'excluded': {bad}"


# ---------------------------------------------------------------------------
# manifest.json — catalog_daily profile declared
# ---------------------------------------------------------------------------

def test_manifest_has_catalog_daily_report_profile(manifest):
    """manifest.json must declare a catalog_daily report profile."""
    profiles = {rp["id"]: rp for rp in manifest.get("report_profiles", [])}
    assert "catalog_daily" in profiles, "catalog_daily must be in report_profiles"


def test_manifest_catalog_daily_in_source_capabilities(manifest):
    """manifest.json source_capabilities.reports must include catalog_daily with
    selection_mode=catalog_driven and dispatch.callable=pull_catalog_daily."""
    reports = {
        r["id"]: r
        for r in manifest.get("source_capabilities", {}).get("reports", [])
    }
    assert "catalog_daily" in reports, (
        "catalog_daily must appear in source_capabilities.reports"
    )
    cd = reports["catalog_daily"]
    assert cd.get("selection_mode") == "catalog_driven"
    assert cd.get("dispatch", {}).get("callable") == "pull_catalog_daily"


def test_manifest_payments_daily_unchanged(manifest):
    """AD-22: the legacy payments_daily profile is bit-identical (untouched)."""
    reports = {
        r["id"]: r
        for r in manifest.get("source_capabilities", {}).get("reports", [])
    }
    assert "payments_daily" in reports
    pd = reports["payments_daily"]
    assert pd["selection_mode"] == "exact_bundle"
    assert pd["dispatch"]["callable"] == "pull"
    assert set(pd["metrics"]) == {"revenue", "refunds", "fees", "transaction_count", "order_count"}


# ---------------------------------------------------------------------------
# _project_charge unit tests (no HTTP)
# ---------------------------------------------------------------------------

def test_project_charge_selected_metrics_land(connector):
    """Projection lands exactly the selected metric fields — revenue, fees."""
    catalog_fields = {
        "revenue": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "fees": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "transaction_count": {"section": "CHARGE", "kind": "metric"},
    }
    selection = {
        "metrics": ["revenue", "fees"],
        "dimensions": [],
        "source_fields": {"revenue": "amount", "fees": "fee_amount"},
        "_catalog_fields": catalog_fields,
    }
    charge = _charge("ch_1", _D1, amount=12850, fee=398, payment_intent="pi_1")
    rows = connector._project_charge(charge, selection)

    field_ids = [r["field_id"] for r in rows]
    assert "revenue" in field_ids
    assert "fees" in field_ids
    # unselected metric must not appear
    assert "transaction_count" not in field_ids


def test_project_charge_unselected_fields_absent(connector):
    """Unselected fields are strictly absent — projection is not a pass-through."""
    catalog_fields = {
        "revenue": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "refunds": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "fees": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "transaction_count": {"section": "CHARGE", "kind": "metric"},
        "order_count": {"section": "CHARGE", "kind": "metric"},
        "charge_id": {"section": "CHARGE", "kind": "dimension"},
        "payment_intent_id": {"section": "CHARGE", "kind": "dimension"},
    }
    selection = {
        "metrics": ["revenue"],
        "dimensions": ["charge_id"],
        "source_fields": {"revenue": "amount", "charge_id": "charge_id"},
        "_catalog_fields": catalog_fields,
    }
    charge = _charge("ch_sel", _D1, amount=10000, fee=300, payment_intent="pi_sel")
    rows = connector._project_charge(charge, selection)

    field_ids = [r["field_id"] for r in rows]
    assert "revenue" in field_ids
    assert "charge_id" in field_ids
    # these must be absent
    for absent in ("refunds", "fees", "transaction_count", "order_count", "payment_intent_id"):
        assert absent not in field_ids, f"{absent!r} should not appear in projection"


def test_project_charge_platform_contract_revenue_centimes_to_units(connector):
    """PLATFORM CONTRACT revenue value uses _parse_charge centimes->units conversion."""
    catalog_fields = {
        "revenue": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "refunds": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "fees": {"section": "PLATFORM CONTRACT", "kind": "metric"},
    }
    selection = {
        "metrics": ["revenue", "refunds", "fees"],
        "dimensions": [],
        "source_fields": {"revenue": "amount", "refunds": "amount_refunded", "fees": "fee_amount"},
        "_catalog_fields": catalog_fields,
    }
    # 21090 centimes = 210.90 EUR; 4218 centimes refunded = 42.18; fee 637 centimes = 6.37
    charge = _charge("ch_pc", _D1, amount=21090, amount_refunded=4218, fee=637)
    rows = connector._project_charge(charge, selection)
    by_field = {r["field_id"]: r["value"] for r in rows}

    assert by_field["revenue"] == pytest.approx(210.90)
    assert by_field["refunds"] == pytest.approx(42.18)
    assert by_field["fees"] == pytest.approx(6.37)


def test_project_charge_dotted_path_extraction(connector):
    """Dotted source_field paths (outcome.risk_score, billing_details.address.country)
    are extracted from the charge object via _extract_dotted."""
    catalog_fields = {
        "charge_outcome_risk_score": {"section": "CHARGE", "kind": "metric"},
        "billing_country": {"section": "CHARGE", "kind": "dimension"},
    }
    selection = {
        "metrics": ["charge_outcome_risk_score"],
        "dimensions": ["billing_country"],
        "source_fields": {
            "charge_outcome_risk_score": "outcome.risk_score",
            "billing_country": "billing_details.address.country",
        },
        "_catalog_fields": catalog_fields,
    }
    charge = _charge(
        "ch_dot", _D1, amount=5000,
        outcome={"risk_score": 42, "risk_level": "normal", "type": "authorized"},
        billing_country="FR",
    )
    rows = connector._project_charge(charge, selection)
    by_field = {r["field_id"]: r["value"] for r in rows}

    assert by_field["charge_outcome_risk_score"] == 42
    assert by_field["billing_country"] == "FR"


def test_project_charge_balance_transaction_fields(connector):
    """BALANCE TRANSACTION fields are read from charge.balance_transaction sub-dict."""
    catalog_fields = {
        "bt_status": {"section": "BALANCE TRANSACTION", "kind": "dimension"},
        "bt_type": {"section": "BALANCE TRANSACTION", "kind": "dimension"},
    }
    selection = {
        "metrics": [],
        "dimensions": ["bt_status", "bt_type"],
        "source_fields": {"bt_status": "status", "bt_type": "type"},
        "_catalog_fields": catalog_fields,
    }
    charge = _charge("ch_bt", _D1, amount=5000, fee=150)
    # fee is set, so balance_transaction sub-object is on the charge
    rows = connector._project_charge(charge, selection)
    by_field = {r["field_id"]: r["value"] for r in rows}

    assert by_field["bt_status"] == "available"
    assert by_field["bt_type"] == "charge"


def test_project_charge_date_is_always_in_rows(connector):
    """Every projected row carries a non-empty date extracted from charge.created."""
    catalog_fields = {
        "revenue": {"section": "PLATFORM CONTRACT", "kind": "metric"},
    }
    selection = {
        "metrics": ["revenue"],
        "dimensions": [],
        "source_fields": {"revenue": "amount"},
        "_catalog_fields": catalog_fields,
    }
    charge = _charge("ch_date", _D1, amount=1000)
    rows = connector._project_charge(charge, selection)
    for row in rows:
        assert row["date"] == "2026-07-01", (
            f"Row for {row['field_id']} should carry date=2026-07-01"
        )


# ---------------------------------------------------------------------------
# _extract_dotted unit tests
# ---------------------------------------------------------------------------

def test_extract_dotted_simple_key(connector):
    """Single-key path extracts directly."""
    assert connector._extract_dotted({"status": "succeeded"}, "status") == "succeeded"


def test_extract_dotted_nested_path(connector):
    """Dotted path traverses nested dicts."""
    obj = {"outcome": {"risk_score": 72, "type": "authorized"}}
    assert connector._extract_dotted(obj, "outcome.risk_score") == 72


def test_extract_dotted_missing_intermediate_key_returns_none(connector):
    """Missing intermediate key returns None (no KeyError)."""
    assert connector._extract_dotted({"a": {}}, "a.b.c") is None


def test_extract_dotted_none_value_returns_none(connector):
    """A None value at any level returns None."""
    assert connector._extract_dotted({"outcome": None}, "outcome.risk_score") is None


# ---------------------------------------------------------------------------
# pull_catalog_daily() integration tests (respx mock)
# ---------------------------------------------------------------------------

@respx.mock
def test_pull_catalog_daily_lands_selected_fields_only(connector, tmp_path, monkeypatch):
    """pull_catalog_daily projects only selected fields; others are absent from raw table."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cd_sel.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    charges = _envelope([
        _charge("ch_a", _D1, amount=10000, fee=300, payment_intent="pi_a"),
        _charge("ch_b", _D2, amount=20000, fee=600, payment_intent="pi_b",
                client_reference_id="REF-1"),
    ])
    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=charges))

    # Select only revenue + charge_id; refunds / fees / transaction_count must not appear.
    catalog_fields = {
        "revenue": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "refunds": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "fees": {"section": "PLATFORM CONTRACT", "kind": "metric"},
        "transaction_count": {"section": "CHARGE", "kind": "metric"},
        "charge_id": {"section": "CHARGE", "kind": "dimension"},
        "payment_intent_id": {"section": "CHARGE", "kind": "dimension"},
    }
    selection = {
        "metrics": ["revenue"],
        "dimensions": ["charge_id"],
        "source_fields": {"revenue": "amount", "charge_id": "charge_id"},
        "_catalog_fields": catalog_fields,
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_catalog_daily(
            connection_id="conn_cd", date_from="2026-07-01", date_to="2026-07-02",
            project_id="proj_cd", pull_id="pull_cd_1",
            selection=selection,
        )

    assert result["pull_id"] == "pull_cd_1"
    # 2 charges x 2 selected fields (1 metric + 1 dimension) = 4 rows
    assert result["row_count"] == 4

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed_fields = {
        r[0] for r in con.execute(
            "SELECT DISTINCT field_id FROM raw_stripe_catalog_daily "
            "WHERE pull_id = 'pull_cd_1'"
        ).fetchall()
    }
    con.close()

    assert landed_fields == {"revenue", "charge_id"}
    # unselected fields must be absent
    for absent in ("refunds", "fees", "transaction_count", "payment_intent_id"):
        assert absent not in landed_fields, f"{absent!r} must not appear in landed rows"


@respx.mock
def test_pull_catalog_daily_revenue_values_correct(connector, tmp_path, monkeypatch):
    """pull_catalog_daily projects correct revenue values (centimes->units via _parse_charge)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cd_rev.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    charges = _envelope([
        _charge("ch_r1", _D1, amount=12850, fee=398),   # 128.50 EUR
        _charge("ch_r2", _D2, amount=21090, fee=637),   # 210.90 EUR
    ])
    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=charges))

    catalog_fields = {
        "revenue": {"section": "PLATFORM CONTRACT", "kind": "metric"},
    }
    selection = {
        "metrics": ["revenue"],
        "dimensions": [],
        "source_fields": {"revenue": "amount"},
        "_catalog_fields": catalog_fields,
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        connector.pull_catalog_daily(
            connection_id="conn_rev", date_from="2026-07-01", date_to="2026-07-02",
            project_id="proj_rev", pull_id="pull_cd_rev",
            selection=selection,
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT charge_id, CAST(value AS DOUBLE) "
        "FROM raw_stripe_catalog_daily "
        "WHERE pull_id = 'pull_cd_rev' AND field_id = 'revenue' "
        "ORDER BY charge_id"
    ).fetchall()
    con.close()

    by_charge = {r[0]: r[1] for r in rows}
    assert by_charge["ch_r1"] == pytest.approx(128.50)
    assert by_charge["ch_r2"] == pytest.approx(210.90)


@respx.mock
def test_pull_catalog_daily_dotted_path_projected(connector, tmp_path, monkeypatch):
    """pull_catalog_daily projects dotted-path fields (outcome.risk_score) correctly."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cd_dot.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    charge_with_outcome = _charge(
        "ch_ors", _D1, amount=5000,
        outcome={"risk_score": 55, "risk_level": "elevated", "type": "authorized"},
    )
    respx.get(_CHARGES_URL).mock(
        return_value=httpx.Response(200, json=_envelope([charge_with_outcome]))
    )

    catalog_fields = {
        "charge_outcome_risk_score": {"section": "CHARGE", "kind": "metric"},
        "charge_outcome_risk_level": {"section": "CHARGE", "kind": "dimension"},
    }
    selection = {
        "metrics": ["charge_outcome_risk_score"],
        "dimensions": ["charge_outcome_risk_level"],
        "source_fields": {
            "charge_outcome_risk_score": "outcome.risk_score",
            "charge_outcome_risk_level": "outcome.risk_level",
        },
        "_catalog_fields": catalog_fields,
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        connector.pull_catalog_daily(
            connection_id="conn_dot", date_from="2026-07-01", date_to="2026-07-01",
            project_id="proj_dot", pull_id="pull_cd_dot",
            selection=selection,
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT field_id, value FROM raw_stripe_catalog_daily "
            "WHERE pull_id = 'pull_cd_dot'"
        ).fetchall()
    }
    con.close()

    assert rows["charge_outcome_risk_score"] == "55"
    assert rows["charge_outcome_risk_level"] == "elevated"


@respx.mock
def test_pull_catalog_daily_none_selection_falls_back_to_core_default(
    connector, tmp_path, monkeypatch
):
    """None selection falls back to tier-core catalog default (revenue, transaction_count,
    order_count, charge_id, date, payment_intent_id, client_reference_id, etc.)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cd_def.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    charges = _envelope([
        _charge("ch_def", _D1, amount=8000, fee=240, payment_intent="pi_def"),
    ])
    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=charges))

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_catalog_daily(
            connection_id="conn_def", date_from="2026-07-01", date_to="2026-07-01",
            project_id="proj_def", pull_id="pull_cd_def",
            selection=None,  # triggers tier-core default
        )

    # At minimum the core tier fields (revenue, fees, refunds, transaction_count,
    # order_count, charge_id, date, payment_intent_id, ...) should be present.
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed_fields = {
        r[0] for r in con.execute(
            "SELECT DISTINCT field_id FROM raw_stripe_catalog_daily "
            "WHERE pull_id = 'pull_cd_def'"
        ).fetchall()
    }
    con.close()

    # Core-tier fields must always land when selection=None
    for expected in ("revenue", "fees", "refunds", "charge_id"):
        assert expected in landed_fields, f"{expected!r} must land with None selection"

    assert result["row_count"] > 0


# ---------------------------------------------------------------------------
# AD-22: legacy pull() profile green (bit-identical, untouched)
# ---------------------------------------------------------------------------

@respx.mock
def test_ad22_legacy_pull_still_green(connector, tmp_path, monkeypatch):
    """AD-22: the existing pull() function is untouched; its behavior is identical
    after adding pull_catalog_daily. Rows land in raw_stripe_payments as before."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "ad22.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    charges = _envelope([
        _charge("ch_legacy", _D1, amount=12850, fee=398, payment_intent="pi_legacy",
                client_reference_id="REF-LEGACY"),
    ])
    respx.get(_CHARGES_URL).mock(return_value=httpx.Response(200, json=charges))

    with patch("core.nango_client.get_fresh_token", return_value="fake-legacy"):
        result = connector.pull(
            connection_id="conn_legacy", date_from="2026-07-01", date_to="2026-07-01",
            project_id="proj_legacy", pull_id="pull_legacy",
        )

    assert result["row_count"] == 1

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT revenue, fees, client_reference_id "
        "FROM raw_stripe_payments WHERE pull_id = 'pull_legacy'"
    ).fetchone()
    con.close()

    assert row is not None
    assert row[0] == pytest.approx(128.50)  # revenue
    assert row[1] == pytest.approx(3.98)    # fees
    assert row[2] == "REF-LEGACY"           # client_reference_id preserved


# ---------------------------------------------------------------------------
# AI-58: dispatch resolves pull_catalog_daily
# ---------------------------------------------------------------------------

def test_ai58_dispatch_resolves_pull_catalog_daily():
    """AI-58: the manifest dispatch for catalog_daily resolves to pull_catalog_daily,
    and the connector module exposes that callable."""
    import types

    from core.main import get_module_pull_fn

    st_mod = _import_connector()
    # pull_catalog_daily must be present on the module
    assert hasattr(st_mod, "pull_catalog_daily"), (
        "connector must expose pull_catalog_daily"
    )

    # Verify dispatch via manifest
    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    reports = {
        r["id"]: r
        for r in manifest.get("source_capabilities", {}).get("reports", [])
    }
    assert reports["catalog_daily"]["dispatch"]["callable"] == "pull_catalog_daily"

    # get_module_pull_fn for 'stripe' (default profile = pull)
    loaded = types.SimpleNamespace(name="stripe", connector_module=st_mod)
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("stripe")
    assert fn is st_mod.pull

    # Verify pull_catalog_daily is callable directly
    assert callable(st_mod.pull_catalog_daily)
