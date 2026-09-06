"""meta-ads never invents a currency for a spend it did not measure (AD-9).

WHY THIS TEST EXISTS
    Until 2026-08-17 this connector declared its raw currency column as
    ``cost_source_currency VARCHAR DEFAULT 'USD'`` and read the value as
    ``api_row.get("account_currency", "USD")``. Both halves of the money contract
    broke on that default:

      * ``revenue is never a naked number`` was satisfied in APPEARANCE only --
        every amount carried a currency, but for the accounts whose currency the
        Graph API did not return, that currency was a guess;
      * ``core.currency_refusal`` can refuse an UNKNOWN currency
        (``UNKNOWN_CURRENCY_GAP``) and it cannot doubt a STATED one. A fabricated
        ``'USD'`` therefore did not degrade the answer, it replaced it: the FX
        join found a dollar rate and converted a euro amount into the project's
        canonical total at the dollar rate, silently, with full provenance.

    An absent currency is a gap. The gap has a name downstream (``fx_gap_code``
    in ``stg_meta_ads_daily``, ``UNKNOWN_CURRENCY_GAP`` in the refusal engine),
    and both need the NULL to fire.

WHAT IT PINS
    The three places the default used to live -- the landing DDL, the additive
    ALTER for pre-existing tables, and the row mapper -- and the seed loader,
    which mirrors the landing table and must not re-introduce it.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "meta-ads"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_SEED_PATH = _MODULE_DIR / "seeds" / "load_meta_seed.py"


@pytest.fixture(scope="module")
def connector():
    spec = importlib.util.spec_from_file_location("meta_ads_currency", _CONNECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_absent_account_currency_maps_to_none_not_to_dollars(connector):
    """The API row says nothing about currency -> the canonical row says nothing."""
    row = connector._parse_insight_row(
        {
            "date_start": "2026-07-01",
            "campaign_id": "23851234500010001",
            "campaign_name": "Summer Sale - Prospecting",
            "spend": "250.75",
            "impressions": "48200",
            "clicks": "1310",
            "conversions": "42",
        }
    )
    assert row["cost_source_currency"] is None, (
        "an absent account_currency must land NULL. A default here is a currency "
        "nobody measured, and every downstream refusal keyed on the NULL stops "
        "firing the moment it is filled in."
    )


def test_a_stated_account_currency_is_preserved_exactly(connector):
    """The guard must not have turned into 'always None'."""
    row = connector._parse_insight_row(
        {
            "date_start": "2026-07-01",
            "campaign_id": "23851234500010001",
            "spend": "250.75",
            "account_currency": "EUR",
        }
    )
    assert row["cost_source_currency"] == "EUR"


def test_an_empty_account_currency_is_a_gap_not_an_empty_currency(connector):
    """``""`` is the API saying nothing, not a currency named empty string."""
    row = connector._parse_insight_row(
        {"date_start": "2026-07-01", "campaign_id": "1", "account_currency": ""}
    )
    assert row["cost_source_currency"] is None


@pytest.mark.parametrize(
    "path",
    [_CONNECTOR_PATH, _SEED_PATH],
    ids=["connector", "seed_loader"],
)
def test_no_sql_default_currency_anywhere_in_the_module(path: Path):
    """No DDL of this module may supply a currency the data did not carry.

    Read from the SQL text rather than from a live DuckDB file so the check
    covers the ``CREATE TABLE`` and the additive ``ALTER TABLE`` alike -- the
    ALTER was the copy most likely to be forgotten, being the one that only ever
    runs against tables that already exist.
    """
    text = path.read_text(encoding="utf-8")
    sql_only = re.sub(r"#[^\n]*", "", text)  # python comments may DISCUSS the default
    offenders = re.findall(
        r"cost_source_currency[^\n,)]*DEFAULT[^\n,)]*", sql_only, re.IGNORECASE
    )
    assert not offenders, (
        f"{path.name}: cost_source_currency carries a SQL DEFAULT again "
        f"({offenders}). AD-9: an absent currency is a gap, never a guess."
    )
