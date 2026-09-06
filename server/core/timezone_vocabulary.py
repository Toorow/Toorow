"""The governed IANA time-zone vocabulary and pinned tzdb version (Story 48.3).

:mod:`core.report_timezone` already answers *is this string a valid IANA zone?*
via :func:`~core.report_timezone.is_valid_iana`. What was missing, and what
AC1/AC7 require, is everything around that yes/no:

* an **enumerable** set a selector can search, instead of a free-text field
  whose only feedback is a later failure;
* the **tzdb version** the answer was computed against, recorded on every
  derivation. Time zone rules change several times a year: a ``reporting_date``
  derived under ``2024a`` and one derived under ``2026c`` are not necessarily
  the same date, and a derivation that cannot say which one it used is not
  reproducible;
* the **canonical vs link** distinction. ``Europe/Kiev`` and ``Europe/Kyiv``
  both resolve; only one is canonical. Storing whichever one arrived makes two
  Projects with the same boundary look different.

No network call is ever made. The zone set comes from the stdlib
``zoneinfo`` database (the ``tzdata`` wheel on Windows, the OS store elsewhere),
which is the same store every derivation in this repository resolves against --
so the vocabulary cannot drift away from the resolver that uses it.

The stored governance version is an immutable
``app.master_data_vocabulary_versions`` row, exactly like Country (Story 48.2)
and Currency, minted by ``scripts/export_timezone_vocabulary.py``.
"""

from __future__ import annotations

import csv
import io
import zoneinfo
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any, Mapping, Sequence

TIMEZONE_VOCABULARY_KEY = "timezone"

IANA_AUTHORITY = "IANA Time Zone Database"
IANA_REFERENCE = "https://www.iana.org/time-zones"

#: Zones the vocabulary retains but a Project may not select as its reporting
#: boundary. ``UTC`` is deliberately NOT here -- reporting in UTC is a legitimate
#: explicit choice; what the story forbids is *assuming* it (AC7).
_NON_SELECTABLE_PREFIXES = ("Etc/", "SystemV/", "US/", "Canada/", "Mexico/", "Brazil/", "Chile/")
_NON_SELECTABLE_EXACT = frozenset({"localtime", "Factory", "GMT0", "GMT-0", "GMT+0", "Zulu"})

SEED_COLUMNS = ("zone", "canonical_zone", "area", "is_canonical", "selectable")


class TimezoneVocabularyError(RuntimeError):
    """Raised when the local tz database cannot be trusted."""


@dataclass(frozen=True, slots=True)
class TimeZone:
    zone: str
    #: The canonical name this zone resolves to. Equal to ``zone`` when the
    #: identifier is itself canonical; different when it is a backward link.
    canonical_zone: str
    area: str
    is_canonical: bool
    selectable: bool


def tzdb_version() -> str:
    """The exact tz database release the runtime resolves against.

    Read from the ``tzdata`` distribution when present (the Windows path and the
    pinned CI path), else from the OS store's ``tzdata.zi`` header. Raises rather
    than returning ``"unknown"``: a derivation that cannot name its tzdb version
    must fail closed, not record a placeholder (AC7).
    """

    try:
        import tzdata  # noqa: PLC0415 -- optional; present on every supported target

        version = str(getattr(tzdata, "IANA_VERSION", "")).strip()
        if version:
            return version
    except ImportError:
        pass
    for key in zoneinfo.TZPATH:
        candidate = f"{key}/tzdata.zi"
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                first = handle.readline().strip()
        except OSError:
            continue
        # The header line is "# version 2026c".
        if first.startswith("#") and "version" in first:
            return first.rsplit(" ", 1)[-1].strip()
    raise TimezoneVocabularyError(
        "no tz database version could be read; a time derivation cannot be pinned"
    )


def _is_selectable(zone: str) -> bool:
    if zone in _NON_SELECTABLE_EXACT:
        return False
    if zone.startswith(_NON_SELECTABLE_PREFIXES):
        return False
    # A selectable reporting zone names a real place: Area/Location.
    return "/" in zone or zone == "UTC"


def _tzif_bytes(zone: str) -> bytes | None:
    """The compiled TZif payload for *zone*, or ``None`` when it is unreadable.

    Two identifiers are the SAME zone iff their compiled rule data is
    byte-identical -- that is what a tz database ``Link`` produces. Comparing
    resolved offsets instead would be wrong in a way that matters: ``Europe/Paris``
    and ``Arctic/Longyearbyen`` agree on every present-day offset and are two
    different zones with different histories, so an offset fingerprint merges
    them and silently makes one of them unselectable.
    """

    for root in zoneinfo.TZPATH:
        try:
            with open(f"{root}/{zone}", "rb") as handle:
                return handle.read()
        except OSError:
            continue
    try:
        import importlib.resources as resources  # noqa: PLC0415

        return resources.files("tzdata.zoneinfo").joinpath(zone).read_bytes()
    except (ImportError, ModuleNotFoundError, OSError, FileNotFoundError):
        return None


def _declared_canonical_zones() -> frozenset[str]:
    """The zones the tz database itself declares canonical, from ``zone1970.tab``.

    That file lists one row per *primary* zone; every other identifier reaching
    the same rules is a ``Link``. Deriving canonicality any other way gets it
    backwards -- ``Europe/Monaco`` sorts before ``Europe/Paris`` and
    ``Europe/Kiev`` before ``Europe/Kyiv``, so a lexicographic rule promotes the
    link and demotes the real zone. Returns an empty set when the table is not
    shipped, in which case the byte-groups below fall back to a stated,
    reproducible tie-break.
    """

    for opener in (
        lambda: _tzif_bytes("zone1970.tab"),
        lambda: _tzif_bytes("zone.tab"),
    ):
        raw = opener()
        if not raw:
            continue
        zones: set[str] = set()
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            columns = line.split("\t")
            if len(columns) >= 3 and columns[2].strip():
                zones.add(columns[2].strip())
        if zones:
            # `zone1970.tab` lists zones by country and therefore omits UTC. It is
            # nonetheless the canonical spelling of that boundary, and reporting in
            # UTC is a legitimate explicit choice -- what AC7 forbids is assuming it.
            return frozenset(zones | {"UTC"})
    return frozenset()


@lru_cache(maxsize=1)
def load_timezone_vocabulary() -> tuple[TimeZone, ...]:
    """The full local IANA set, canonicalised and sorted. Offline, deterministic."""

    names = sorted(zoneinfo.available_timezones())
    if not names:
        raise TimezoneVocabularyError("the local tz database exposes no zones")

    declared = _declared_canonical_zones()
    by_payload: dict[bytes, list[str]] = {}
    unreadable: list[str] = []
    for name in names:
        payload = _tzif_bytes(name)
        if payload is None:
            # Retained as its own canonical identity rather than guessed into a
            # group: an unreadable payload is unknown, not equal to something.
            unreadable.append(name)
            continue
        by_payload.setdefault(payload, []).append(name)

    canonical_for: dict[str, str] = {name: name for name in unreadable}
    for group in by_payload.values():
        primary = sorted(name for name in group if name in declared)
        if primary:
            preferred = primary[0]
        else:
            # No declared primary in this group: prefer an Area/Location name,
            # then lexicographic. Stated rather than silent, and reproducible.
            preferred = sorted(group, key=lambda item: (0 if "/" in item else 1, item))[0]
        for name in group:
            canonical_for[name] = preferred

    records = [
        TimeZone(
            zone=name,
            canonical_zone=canonical_for[name],
            area=name.split("/", 1)[0] if "/" in name else "Other",
            # `declared` is the authority when it is available: a zone the tz
            # database lists is canonical even if an alias shares its payload.
            is_canonical=(name in declared) if declared else canonical_for[name] == name,
            selectable=_is_selectable(name),
        )
        for name in names
    ]
    return tuple(sorted(records, key=lambda item: item.zone))


def reset_cache() -> None:
    """Drop the memoized vocabulary. Tests that stub the tz store call this."""
    load_timezone_vocabulary.cache_clear()
    _zone_index.cache_clear()


@lru_cache(maxsize=1)
def _zone_index() -> dict[str, TimeZone]:
    return {item.zone: item for item in load_timezone_vocabulary()}


def resolve_timezone(value: str | None) -> TimeZone | None:
    """Resolve an identifier against the governed set, or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    return _zone_index().get(candidate)


def canonical_zone(value: str | None) -> str | None:
    """The canonical name for *value*, or ``None`` when it does not resolve."""
    resolved = resolve_timezone(value)
    return resolved.canonical_zone if resolved is not None else None


def selectable_timezones() -> tuple[TimeZone, ...]:
    """Zones a Project may confirm as its reporting boundary."""
    return tuple(
        item for item in load_timezone_vocabulary() if item.selectable and item.is_canonical
    )


def search_timezones(query: str | None, *, limit: int = 25) -> tuple[TimeZone, ...]:
    """Rank selectable zones for a typeahead. Deterministic, offline."""

    pool = selectable_timezones()
    if not query or not query.strip():
        return pool[:limit]
    needle = query.strip().lower().replace(" ", "_")

    def band(item: TimeZone) -> int | None:
        lowered = item.zone.lower()
        if lowered == needle:
            return 0
        location = lowered.split("/", 1)[-1]
        if location.startswith(needle):
            return 1
        if lowered.startswith(needle):
            return 2
        if needle in lowered:
            return 3
        return None

    matches = sorted(
        ((band(item), item) for item in pool if band(item) is not None),
        key=lambda pair: (pair[0], pair[1].zone),
    )
    return tuple(item for _, item in matches[:limit])


# ---------------------------------------------------------------------------
# The governed version: import, render, pin. Mirrors core.currency_vocabulary.
# ---------------------------------------------------------------------------


def vocabulary_entries(zones: Sequence[TimeZone] | None = None) -> list[dict[str, Any]]:
    source = list(zones) if zones is not None else list(load_timezone_vocabulary())
    return [
        {
            "code": item.zone,
            "display_name": item.zone.replace("_", " "),
            "canonical_zone": item.canonical_zone,
            "area": item.area,
            "is_canonical": item.is_canonical,
            "selectable": item.selectable,
            "status": "active",
        }
        for item in sorted(source, key=lambda entry: entry.zone)
    ]


def import_timezone_vocabulary(
    conn,
    *,
    actor: str,
    effective_date: date | str,
    source_version: str | None = None,
    zones: Sequence[TimeZone] | None = None,
) -> dict[str, Any]:
    """Store the local tz snapshot as an immutable Governance version.

    ``source_version`` defaults to the tzdb release actually read, so the stored
    version and the runtime resolver can never disagree about which release the
    entries came from.
    """

    from core.master_data import import_vocabulary_version  # noqa: PLC0415

    return import_vocabulary_version(
        conn,
        vocabulary_key=TIMEZONE_VOCABULARY_KEY,
        source_authority=IANA_AUTHORITY,
        source_version=source_version or tzdb_version(),
        source_reference=IANA_REFERENCE,
        effective_date=effective_date,
        entries=vocabulary_entries(zones),
        actor=actor,
    )


def render_seed_csv(entries: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SEED_COLUMNS)
    for entry in sorted(entries, key=lambda item: str(item["code"])):
        writer.writerow(
            [
                entry["code"],
                entry.get("canonical_zone", entry["code"]),
                entry.get("area", "Other"),
                "true" if entry.get("is_canonical", True) else "false",
                "true" if entry.get("selectable", False) else "false",
            ]
        )
    return buffer.getvalue()


def seed_provenance(vocabulary: Mapping[str, Any]) -> dict[str, Any]:
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
