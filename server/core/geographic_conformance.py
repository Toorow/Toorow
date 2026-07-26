"""The MDM bridge for geography (Story 37.9).

`Unknown` used to be a dead end: ``country_vocabulary.normalize_country_value``
returned ``None`` for a spelling the shared seed does not carry, and
``geographic_semantics`` emitted a ``country_value_unmapped`` data-quality
evidence that nothing could act on.

This module turns that dead end into the **existing governed repair path**:

* resolution reads the conformed-dimension layer (``core.dimension_conformance``)
  in cascade PROJECT > ORG > PLATFORM, so a client's spelling decision is scoped
  to that client and never leaks into the shared seed;
* an unresolved value becomes a ``proposed`` suggestion in the SAME
  suggest -> confirm/reject -> manual flow, with a difflib score as its evidence;
* nothing is ever auto-confirmed. Without evidence, a value stays unresolved and
  keeps grouping under ``Unknown``. We never invent a resolution;
* a confirmed mapping reclassifies retained rows on the NEXT READ — no fact is
  rewritten, because resolution happens in the read layer.

Anti-hardcode rule (Story 37.9 AC5): this module ships **no** client alias, no
market composition and no preset. Every candidate it proposes is derived at call
time from the governed ISO seed, and every decision is stored per client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from core.country_vocabulary import (
    CANONICAL_COUNTRY_DIMENSION,
    get_country_vocabulary,
    get_supported_country_codes,
    normalize_country_value,
)

logger = logging.getLogger(__name__)

# The DQ evidence code already emitted by the read layer, kept verbatim so the
# report envelope contract does not move.
EVIDENCE_UNMAPPED = "country_value_unmapped"
# A confirmed mapping that points OUTSIDE the legal ISO set is refused rather
# than trusted: the seed owns the legal set, the client owns the spelling.
EVIDENCE_OUTSIDE_VOCABULARY = "country_mapping_outside_vocabulary"

# The alert type under which unmapped geography reaches the Epic 13 monitors and
# ``get_data_quality_report``. The ``dq_`` prefix is what those surfaces filter on.
DQ_GEOGRAPHY_ALERT_TYPE = "dq_geography"

# Prudent default: below this difflib ratio we propose NOTHING rather than a
# plausible-looking guess. Same spirit as dimension_conformance's own threshold.
DEFAULT_CANDIDATE_THRESHOLD = 0.88
MAX_CANDIDATES = 3

# Resolution states reported for one unresolved source value.
STATE_SUGGESTED = "suggested"           # a scored candidate was persisted as 'proposed'
STATE_UNRESOLVED = "unresolved"         # no evidence -> nothing persisted, stays Unknown


# ---------------------------------------------------------------------------
# Pure candidate scoring (offline-testable, no DB, no I/O beyond the seed).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CountryCandidate:
    """One evidence-backed proposal for an unrecognized spelling."""

    canonical_code: str
    display_name: str
    method: str
    score: float

    def as_dict(self) -> dict[str, object]:
        return {
            "canonical_code": self.canonical_code,
            "display_name": self.display_name,
            "method": self.method,
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class UnmappedCountryValue:
    """An aggregated unresolved source value, with its evidence and state."""

    connector: str
    source_value: str
    occurrences: int
    candidates: tuple[CountryCandidate, ...] = ()
    state: str = STATE_UNRESOLVED

    def as_dict(self) -> dict[str, object]:
        return {
            "connector": self.connector,
            "source_value": self.source_value,
            "occurrences": self.occurrences,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "state": self.state,
        }


def _normalize_options():
    from core.dimension_conformance import NormalizeOptions  # noqa: PLC0415

    return NormalizeOptions()


def _normalized(value: str) -> str:
    from core.dimension_conformance import normalize_value  # noqa: PLC0415

    return normalize_value(value, _normalize_options())


def score_country_candidates(
    raw_value: object,
    *,
    threshold: float = DEFAULT_CANDIDATE_THRESHOLD,
    limit: int = MAX_CANDIDATES,
) -> tuple[CountryCandidate, ...]:
    """Score an unrecognized spelling against the governed ISO seed (PURE).

    Compares the normalized form of *raw_value* to the normalized form of every
    canonical code, display name and shipped alias, and returns the candidates
    at or above *threshold*, best first.  Below the threshold: **nothing**, not a
    best-effort guess — an honest empty result is what keeps ``Unknown`` honest.
    """

    from core.dimension_conformance import (  # noqa: PLC0415
        METHOD_NORMALIZED,
        METHOD_SIMILARITY,
        similarity,
    )

    if not isinstance(raw_value, str):
        return ()
    normalized = _normalized(raw_value)
    if not normalized:
        return ()

    best: dict[str, CountryCandidate] = {}
    for country in get_country_vocabulary():
        forms = {country.code, country.display_name, *country.aliases}
        for form in forms:
            form_normalized = _normalized(form)
            if not form_normalized:
                continue
            score = similarity(normalized, form_normalized)
            if score < threshold:
                continue
            method = METHOD_NORMALIZED if score >= 1.0 else METHOD_SIMILARITY
            current = best.get(country.code)
            if current is None or score > current.score:
                best[country.code] = CountryCandidate(
                    canonical_code=country.code,
                    display_name=country.display_name,
                    method=method,
                    score=score,
                )
    ranked = sorted(best.values(), key=lambda item: (-item.score, item.canonical_code))
    return tuple(ranked[: max(0, limit)])


# ---------------------------------------------------------------------------
# Resolution: seed first, then the governed per-client conformance layer.
# ---------------------------------------------------------------------------

CountryResolver = Callable[[object, object], str | None]


def _validated_canonical(value: object) -> str | None:
    """Accept a governed mapping ONLY when it lands inside the legal ISO set."""

    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if code in get_supported_country_codes():
        return code
    return None


def load_project_country_conformance(project_id: str) -> dict[tuple[str, str], str]:
    """Load the CONFIRMED (connector, source_value) -> ISO code map for a project.

    One query, cascade PROJECT > ORG > PLATFORM, delegated entirely to
    ``dimension_conformance.resolve_dimension_conformance``.  Fail-soft: an
    unreachable store yields ``{}`` and every unrecognized spelling simply stays
    ``Unknown`` (never a guess, never a crash).
    """

    from core.dimension_conformance import resolve_dimension_conformance  # noqa: PLC0415

    try:
        resolved = resolve_dimension_conformance(project_id, CANONICAL_COUNTRY_DIMENSION)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "geographic_conformance: conformance load failed project=%s: %s", project_id, exc
        )
        return {}
    result: dict[tuple[str, str], str] = {}
    for (connector, source_value), canonical in resolved.items():
        code = _validated_canonical(canonical)
        if code is None:
            logger.warning(
                "geographic_conformance: %s project=%s connector=%s value=%r -> %r (ignored)",
                EVIDENCE_OUTSIDE_VOCABULARY,
                project_id,
                connector,
                source_value,
                canonical,
            )
            continue
        result[(str(connector), str(source_value))] = code
    return result


def make_country_resolver(
    *,
    project_id: str | None = None,
    conformance: Mapping[tuple[str, str], str] | None = None,
) -> CountryResolver:
    """Build a ``(raw_value, connector) -> ISO code | None`` read-layer resolver.

    Order, and it matters:

    1. the governed seed (the legal set, identical for every client);
    2. the client's CONFIRMED conformance mappings for this project.

    A ``proposed`` or ``rejected`` mapping resolves nothing — that is
    ``dimension_conformance``'s own contract (AD-9), not a rule re-implemented
    here.  When neither step answers, the caller keeps ``Unknown``.
    """

    table: dict[tuple[str, str], str]
    if conformance is not None:
        # Same fail-closed rule as the DB loader: a confirmed mapping that lands
        # outside the legal ISO set resolves NOTHING. The seed owns the legal set.
        table = {}
        for (connector, value), canonical in conformance.items():
            code = _validated_canonical(canonical)
            if code is None:
                logger.warning(
                    "geographic_conformance: %s connector=%s value=%r -> %r (ignored)",
                    EVIDENCE_OUTSIDE_VOCABULARY,
                    connector,
                    value,
                    canonical,
                )
                continue
            table[(str(connector), str(value))] = code
    elif project_id:
        table = load_project_country_conformance(project_id)
    else:
        table = {}

    # Case-insensitive fallback index. The exact key always wins; this only
    # spares the operator from confirming 'Espagne' and 'ESPAGNE' separately.
    folded: dict[tuple[str, str], str] = {}
    for (connector, value), code in table.items():
        folded.setdefault((connector, value.casefold()), code)

    def _resolve(raw_value: object, connector: object = None) -> str | None:
        seeded = normalize_country_value(raw_value)
        if seeded is not None:
            return seeded
        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip()
        if not value:
            return None
        key_connector = str(connector or "")
        exact = table.get((key_connector, value))
        if exact is not None:
            return exact
        return folded.get((key_connector, value.casefold()))

    return _resolve


# ---------------------------------------------------------------------------
# Aggregation + governed suggestion raising.
# ---------------------------------------------------------------------------


def aggregate_unmapped_evidence(
    evidence: Iterable[Mapping[str, object]],
    *,
    threshold: float = DEFAULT_CANDIDATE_THRESHOLD,
) -> tuple[UnmappedCountryValue, ...]:
    """Fold raw ``country_value_unmapped`` evidence rows into scored items (PURE).

    Accepts the evidence dicts produced by ``geographic_semantics`` (``raw_value``
    + ``connector``) as well as pre-aggregated rows carrying ``occurrences``.
    """

    counts: dict[tuple[str, str], int] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("raw_value", item.get("source_value"))
        if not isinstance(raw, str) or not raw.strip():
            continue
        connector = str(item.get("connector") or "")
        occurrences = item.get("occurrences")
        increment = int(occurrences) if isinstance(occurrences, int) and occurrences > 0 else 1
        key = (connector, raw.strip())
        counts[key] = counts.get(key, 0) + increment

    items: list[UnmappedCountryValue] = []
    for (connector, source_value), occurrences in sorted(counts.items()):
        candidates = score_country_candidates(source_value, threshold=threshold)
        items.append(
            UnmappedCountryValue(
                connector=connector,
                source_value=source_value,
                occurrences=occurrences,
                candidates=candidates,
                state=STATE_SUGGESTED if candidates else STATE_UNRESOLVED,
            )
        )
    return tuple(items)


def build_country_suggestions(items: Sequence[UnmappedCountryValue]) -> list:
    """Turn scored items into ``dimension_conformance.Suggestion`` rows (PURE).

    Only items that carry evidence produce a suggestion, and the produced status
    is always ``proposed`` at persistence time.  An item with no candidate
    produces NOTHING: ``canonical_value`` is NOT NULL in the store and writing
    the source value back as its own canonical would fabricate a resolution.
    """

    from core.dimension_conformance import METHOD_SIMILARITY, Suggestion  # noqa: PLC0415

    suggestions = []
    for item in items:
        if not item.candidates:
            continue
        best = item.candidates[0]
        suggestions.append(
            Suggestion(
                connector=item.connector,
                source_value=item.source_value,
                canonical_value=best.canonical_code,
                method=best.method or METHOD_SIMILARITY,
                score=best.score,
                normalized_form=_normalized(item.source_value),
            )
        )
    return suggestions


def register_unmapped_country_values(
    project_id: str,
    items: Sequence[UnmappedCountryValue],
    *,
    identity: str = "system",
) -> dict[str, int]:
    """Persist the evidenced items as PROJECT-scoped ``proposed`` mappings.

    Scoping is PROJECT so one client's spelling decision never becomes another
    client's default (that is the whole point of Story 37.9).  Persistence
    delegates to ``dimension_conformance.persist_suggestions``, which is
    idempotent, never overwrites a human decision, and audits every real change.
    Returns the persistence counts plus ``unresolved`` (items with no evidence).
    """

    counts = {"proposed": 0, "skipped": 0, "unchanged": 0, "unresolved": 0}
    counts["unresolved"] = sum(1 for item in items if not item.candidates)
    suggestions = build_country_suggestions(items)
    if not suggestions:
        return counts

    from core.dimension_conformance import SCOPE_PROJECT, persist_suggestions  # noqa: PLC0415

    try:
        persisted = persist_suggestions(
            suggestions,
            canonical_dimension=CANONICAL_COUNTRY_DIMENSION,
            scope_level=SCOPE_PROJECT,
            org_id=None,
            project_id=project_id,
            identity=identity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "geographic_conformance: suggestion persistence failed project=%s: %s",
            project_id,
            exc,
        )
        return counts
    for key in ("proposed", "skipped", "unchanged"):
        counts[key] = int(persisted.get(key, 0))
    return counts


# ---------------------------------------------------------------------------
# DQ supervision surfacing.
# ---------------------------------------------------------------------------


def build_dq_firing_payload(
    items: Sequence[UnmappedCountryValue],
    *,
    datastream_id: str = "",
    datastream_name: str = "",
    module_name: str = "",
    window_date: str = "",
) -> dict[str, object] | None:
    """Compose the message + metadata of the geography DQ firing (PURE).

    Returns ``None`` when there is nothing to report — an empty firing would be
    noise, and a fabricated green is worse than silence.
    """

    if not items:
        return None
    distinct = len(items)
    occurrences = sum(item.occurrences for item in items)
    unresolved = sum(1 for item in items if not item.candidates)
    message = (
        f"Geographie non resolue: {distinct} valeur(s) pays distincte(s) "
        f"({occurrences} ligne(s)) ne correspondent a aucun code ISO canonique; "
        f"{distinct - unresolved} suggestion(s) de conformance a valider, "
        f"{unresolved} sans candidat."
    )
    return {
        "message": message,
        "metadata": {
            "datastream_id": datastream_id,
            "datastream_name": datastream_name,
            "module_name": module_name,
            "window_date": window_date,
            "evidence_code": EVIDENCE_UNMAPPED,
            "canonical_dimension": CANONICAL_COUNTRY_DIMENSION,
            "distinct_unmapped_values": distinct,
            "unmapped_row_count": occurrences,
            "without_candidate": unresolved,
            "values": [item.as_dict() for item in items[:20]],
            "repair": {
                "surface": "dimension_conformance",
                "scope_level": "PROJECT",
                "canonical_dimension": CANONICAL_COUNTRY_DIMENSION,
            },
        },
    }


def emit_country_dq_firing(
    project_id: str,
    items: Sequence[UnmappedCountryValue],
    *,
    datastream_id: str = "",
    datastream_name: str = "",
    module_name: str = "",
    window_date: str = "",
) -> bool:
    """Write ONE ``dq_geography`` firing so the gap reaches the DQ surfaces.

    Epic 13's monitors and ``get_data_quality_report`` both read
    ``app.alert_firings`` filtered on ``type LIKE 'dq_%'``; writing there is what
    makes an agent asked "is this data trustworthy?" see the geographic hole
    instead of assuming completeness. Never raises.
    """

    payload = build_dq_firing_payload(
        items,
        datastream_id=datastream_id,
        datastream_name=datastream_name,
        module_name=module_name,
        window_date=window_date,
    )
    if payload is None:
        return False
    try:
        from core import infra_alerts  # noqa: PLC0415

        infra_alerts.write_infra_firing(
            alert_type=DQ_GEOGRAPHY_ALERT_TYPE,
            project_id=project_id,
            metric=EVIDENCE_UNMAPPED,
            severity="warning",
            message=str(payload["message"]),
            metadata=payload["metadata"],  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "geographic_conformance: dq firing failed project=%s: %s", project_id, exc
        )
        return False
    return True


def record_unmapped_country_evidence(
    project_id: str,
    evidence: Iterable[Mapping[str, object]],
    *,
    identity: str = "system",
    datastream_id: str = "",
    datastream_name: str = "",
    module_name: str = "",
    window_date: str = "",
    threshold: float = DEFAULT_CANDIDATE_THRESHOLD,
    emit_firing: bool = True,
) -> dict[str, object]:
    """THE single entry point: evidence -> governed suggestions + DQ firing.

    Called by the read layer and by the scheduled monitor alike, so both routes
    produce the same repair path. Best-effort by construction: a reporting read
    must never fail because the repair path is momentarily unavailable.

    ``emit_firing=False`` is what the read layer passes: a report may be rendered
    many times a day and one alert per render would be noise. The suggestion
    upsert is idempotent, so the repair path appears immediately; the DQ firing
    is raised once per evaluation by the scheduled monitor.
    """

    items = aggregate_unmapped_evidence(evidence, threshold=threshold)
    if not items:
        return {"items": (), "counts": {"proposed": 0, "skipped": 0, "unchanged": 0,
                                        "unresolved": 0}, "fired": False}
    counts = register_unmapped_country_values(project_id, items, identity=identity)
    if not emit_firing:
        return {"items": items, "counts": counts, "fired": False}
    fired = emit_country_dq_firing(
        project_id,
        items,
        datastream_id=datastream_id,
        datastream_name=datastream_name,
        module_name=module_name,
        window_date=window_date,
    )
    return {"items": items, "counts": counts, "fired": fired}
