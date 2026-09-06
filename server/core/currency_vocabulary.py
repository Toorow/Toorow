"""The governed ISO 4217 currency vocabulary (Story 48.3, AC1 / AC3).

Before this module, "a valid currency" was whatever string reached a text field.
``app.project_preferences.canonical_currency`` was free text with an ``'EUR'``
column default, ``fx_helper`` carried ``DEFAULT_REPORTING_CURRENCY = "EUR"``, and
nothing anywhere could say how many minor units a currency has -- so no code
could round a converted amount correctly, and nothing could tell ``JPY`` (0
decimals) from ``EUR`` (2) from ``BHD`` (3).

The shape mirrors :mod:`core.country_vocabulary` and :mod:`core.country_registry`
exactly, and for the same reason: the runtime vocabulary is owned by Governance
(``app.master_data_vocabulary_versions``, immutable, content-hashed) and the dbt
seed is a *rendered projection* of the stored version, never a second editable
authority. ``scripts/export_currency_vocabulary.py`` is the one path between them.

Three properties this file exists to guarantee:

* **No runtime reference call.** No ISO service is contacted, ever. Refreshing
  the snapshot is a deliberate build operation someone reviews.
* **Minor unit is data, not a guess.** ``minor_unit`` comes from the snapshot.
  A currency whose minor unit is unknown (``XAU``, ``XDR``) is *not* given 2 by
  default -- it is excluded from exact monetary rounding, which is honest.
* **Reporting currency is a narrower set than the vocabulary.** Metals, test
  codes, fund codes and ``XXX`` are retained so an inbound native value still
  resolves, but :func:`selectable_reporting_currencies` excludes them: a Project
  does not report in Palladium.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

_CODE_RE = re.compile(r"^[A-Z]{3}$")
_NUMERIC_RE = re.compile(r"^[0-9]{3}$")
_REPO_ROOT = Path(__file__).parents[2]

CURRENCY_VOCABULARY_KEY = "currency"

ISO_AUTHORITY = "ISO 4217"
ISO_REFERENCE = "https://www.iso.org/iso-4217-currency-codes.html"

#: The four kinds the snapshot distinguishes. Only ``tender`` may be chosen as a
#: Project reporting currency; the rest exist so an inbound native value has
#: somewhere honest to land instead of becoming an unknown-currency gap.
KIND_TENDER = "tender"
KIND_FUND = "fund"
KIND_METAL = "metal"
KIND_TEST = "test"
KIND_NO_CURRENCY = "no_currency"
KINDS = (KIND_TENDER, KIND_FUND, KIND_METAL, KIND_TEST, KIND_NO_CURRENCY)

SEED_COLUMNS = (
    "code",
    "numeric_code",
    "minor_unit",
    "display_name",
    "kind",
    "status",
    "aliases",
)


class CurrencyVocabularyError(RuntimeError):
    """Raised when the canonical currency snapshot cannot be trusted."""


@dataclass(frozen=True, slots=True)
class Currency:
    code: str
    numeric_code: str
    #: ``None`` means the standard assigns no minor unit (``XAU``, ``XDR``).
    #: It is deliberately not coerced to 2 -- see the module docstring.
    minor_unit: int | None
    display_name: str
    kind: str
    status: str
    aliases: tuple[str, ...] = ()

    @property
    def is_tender(self) -> bool:
        return self.kind == KIND_TENDER and self.status == "active"

    @property
    def supports_exact_rounding(self) -> bool:
        """True when a converted amount can be rounded to a defined precision."""
        return self.minor_unit is not None


def default_currency_seed_path() -> Path:
    seed_dir = Path(os.environ.get("TOOROW_DBT_SEEDS_DIR", str(_REPO_ROOT / "dbt" / "seeds")))
    return seed_dir / "dim_currency.csv"


def load_currency_vocabulary(path: Path | None = None) -> tuple[Currency, ...]:
    """Parse the snapshot, refusing anything ambiguous rather than repairing it."""

    seed_path = path or default_currency_seed_path()
    try:
        with seed_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(SEED_COLUMNS) <= set(reader.fieldnames):
                raise CurrencyVocabularyError(
                    "currency vocabulary requires columns: " + ", ".join(SEED_COLUMNS)
                )
            records: list[Currency] = []
            seen: set[str] = set()
            for row in reader:
                records.append(_row_to_currency(row, seen))
    except FileNotFoundError as exc:
        raise CurrencyVocabularyError(f"currency vocabulary not found at {seed_path}") from exc
    if not records:
        raise CurrencyVocabularyError("currency vocabulary is empty")
    return tuple(sorted(records, key=lambda item: item.code))


def _row_to_currency(row: Mapping[str, Any], seen: set[str]) -> Currency:
    code = (row.get("code") or "").strip().upper()
    if not _CODE_RE.fullmatch(code):
        raise CurrencyVocabularyError(f"invalid ISO 4217 alpha code: {code!r}")
    if code in seen:
        raise CurrencyVocabularyError(f"duplicate currency code: {code}")
    seen.add(code)
    numeric = (row.get("numeric_code") or "").strip()
    if not _NUMERIC_RE.fullmatch(numeric):
        raise CurrencyVocabularyError(f"invalid ISO 4217 numeric code for {code}: {numeric!r}")
    raw_minor = (row.get("minor_unit") or "").strip()
    if raw_minor == "":
        minor: int | None = None
    else:
        try:
            minor = int(raw_minor)
        except ValueError as exc:
            raise CurrencyVocabularyError(
                f"non-integer minor unit for {code}: {raw_minor!r}"
            ) from exc
        if minor < 0 or minor > 6:
            raise CurrencyVocabularyError(f"minor unit out of range for {code}: {minor}")
    name = (row.get("display_name") or "").strip()
    if not name:
        raise CurrencyVocabularyError(f"missing display name for currency {code}")
    kind = (row.get("kind") or "").strip()
    if kind not in KINDS:
        raise CurrencyVocabularyError(f"unknown currency kind for {code}: {kind!r}")
    status = (row.get("status") or "").strip() or "active"
    aliases = tuple(
        sorted({alias.strip() for alias in (row.get("aliases") or "").split("|") if alias.strip()})
    )
    return Currency(
        code=code,
        numeric_code=numeric,
        minor_unit=minor,
        display_name=name,
        kind=kind,
        status=status,
        aliases=aliases,
    )


@lru_cache(maxsize=1)
def _index() -> dict[str, Currency]:
    index: dict[str, Currency] = {}
    for currency in load_currency_vocabulary():
        index[currency.code] = currency
        for alias in currency.aliases:
            index.setdefault(alias.strip().upper(), currency)
    return index


def reset_cache() -> None:
    """Drop the memoized index. Tests that point at a fixture seed call this."""
    _index.cache_clear()


def resolve_currency(value: str | None) -> Currency | None:
    """Resolve a code or a known alias, or ``None``. Never guesses."""

    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    if not candidate:
        return None
    return _index().get(candidate)


def is_valid_currency(value: str | None) -> bool:
    """True iff *value* resolves against the governed snapshot."""
    return resolve_currency(value) is not None


def minor_unit(value: str | None) -> int | None:
    """The currency's minor unit, or ``None`` when the standard assigns none.

    ``None`` is a real answer, not a failure: the caller must not round to two
    decimals because the code happened to be unknown.
    """
    currency = resolve_currency(value)
    return currency.minor_unit if currency is not None else None


def selectable_reporting_currencies() -> tuple[Currency, ...]:
    """The set a Project may confirm as its reporting currency.

    Active legal tender only. A fund code, a metal, ``XTS`` and ``XXX`` are all
    resolvable as a *native* currency and none of them is a reporting currency.
    """
    return tuple(item for item in load_currency_vocabulary() if item.is_tender)


def search_currencies(query: str | None, *, limit: int = 25) -> tuple[Currency, ...]:
    """Rank selectable currencies for a typeahead. Deterministic, offline.

    Exact code first, then code prefix, then alias, then name substring -- and
    within each band by code, so the same query always returns the same order.
    """

    pool = selectable_reporting_currencies()
    if not query or not query.strip():
        return pool[:limit]
    needle = query.strip().upper()

    def band(item: Currency) -> int | None:
        if item.code == needle:
            return 0
        if item.code.startswith(needle):
            return 1
        if any(alias.upper().startswith(needle) for alias in item.aliases):
            return 2
        if needle in item.display_name.upper():
            return 3
        return None

    scored = [(band(item), item) for item in pool]
    matches = sorted(
        ((rank, item) for rank, item in scored if rank is not None),
        key=lambda pair: (pair[0], pair[1].code),
    )
    return tuple(item for _, item in matches[:limit])


# ---------------------------------------------------------------------------
# The governed version: import, render, pin. Mirrors core.country_registry.
# ---------------------------------------------------------------------------


def vocabulary_entries(currencies: Sequence[Currency] | None = None) -> list[dict[str, Any]]:
    """Turn the canonical currency set into governed vocabulary entries.

    Sorted by code with deduplicated aliases, so identical input always yields
    an identical content hash -- the property that lets the runtime version and
    the warehouse seed be *proven* the same snapshot rather than assumed to be.
    """

    source = list(currencies) if currencies is not None else list(load_currency_vocabulary())
    return [
        {
            "code": item.code,
            "display_name": item.display_name,
            "numeric_code": item.numeric_code,
            "minor_unit": item.minor_unit,
            "kind": item.kind,
            "status": item.status,
            "aliases": list(item.aliases),
        }
        for item in sorted(source, key=lambda entry: entry.code)
    ]


def import_currency_vocabulary(
    conn,
    *,
    actor: str,
    source_version: str,
    effective_date: date | str,
    currencies: Sequence[Currency] | None = None,
) -> dict[str, Any]:
    """Store the canonical currency snapshot as an immutable Governance version."""

    from core.master_data import import_vocabulary_version  # noqa: PLC0415

    return import_vocabulary_version(
        conn,
        vocabulary_key=CURRENCY_VOCABULARY_KEY,
        source_authority=ISO_AUTHORITY,
        source_version=source_version,
        source_reference=ISO_REFERENCE,
        effective_date=effective_date,
        entries=vocabulary_entries(currencies),
        actor=actor,
    )


def render_seed_csv(entries: Sequence[Mapping[str, Any]]) -> str:
    """Render the warehouse projection of one vocabulary version."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SEED_COLUMNS)
    for entry in sorted(entries, key=lambda item: str(item["code"])):
        minor = entry.get("minor_unit")
        writer.writerow(
            [
                entry["code"],
                entry["numeric_code"],
                "" if minor is None else int(minor),
                entry["display_name"],
                entry.get("kind", KIND_TENDER),
                entry.get("status", "active"),
                "|".join(entry.get("aliases") or ()),
            ]
        )
    return buffer.getvalue()


def seed_provenance(vocabulary: Mapping[str, Any]) -> dict[str, Any]:
    """The pin that makes the seed traceable to the exact governed version."""

    return {
        "generated_from": "app.master_data_vocabulary_versions",
        "vocabulary_key": vocabulary["vocabulary_key"],
        "vocabulary_version_id": vocabulary["id"],
        "content_hash": vocabulary["content_hash"],
        "source_authority": vocabulary["source_authority"],
        "source_version": vocabulary["source_version"],
        "source_reference": vocabulary.get("source_reference"),
        "effective_date": str(vocabulary["effective_date"]),
        "entry_count": vocabulary["entry_count"],
        "note": (
            "Generated projection. Governance owns the runtime vocabulary; editing "
            "this file by hand makes the warehouse disagree with the pinned version."
        ),
    }
