"""Project-level Global versus Local markets posture (CAP-27).

A tracked market is a stable id, an operator label, and **one or more** ISO
3166-1 alpha-2 codes (Story 37.8).  The platform holds no opinion whatsoever on
what a market contains: ``France`` may be exactly ``FR`` for one client and
``FR`` plus other territories for another, and both are correct.  Nothing in
this module widens, narrows, seeds, presets or "corrects" a client definition.

The only constraints imposed here are the ones without which the reporting
arithmetic breaks or the aggregate becomes unaddressable:

* **disjunction** — a country code belongs to at most one market, otherwise
  additive reconciliation double-counts;
* **non-emptiness** — a market carries >= 1 code, ``local_markets`` carries
  >= 1 market;
* **stable, unique ids** — bindings (budgets, objectives) survive a relabel;
* **known vocabulary** — a member code must exist in the canonical seed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Collection, Iterable, Mapping, Sequence

GLOBAL = "global"
LOCAL_MARKETS = "local_markets"
ALLOWED_MODES = frozenset({GLOBAL, LOCAL_MARKETS})
_CODE_RE = re.compile(r"^[A-Z]{2}$")
# Deliberately permissive: an id is an opaque stable handle chosen by the
# operator (or derived from a legacy flat code), never a platform taxonomy.
_MARKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_LABEL_CHARS = 120


class InvalidGeographicPosture(ValueError):
    """A geographic preference payload violates the CAP-27 aggregate."""


@dataclass(frozen=True, slots=True, order=False)
class Market:
    """One client-defined market: stable id + label + >= 1 canonical country code."""

    id: str
    label: str
    country_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "country_codes": list(self.country_codes),
        }

    @property
    def is_single_country(self) -> bool:
        return len(self.country_codes) == 1

    @property
    def single_country_code(self) -> str | None:
        """The member code when the market holds exactly one — else ``None``.

        A single-country market is a complete, first-class market; this accessor
        exists only so read-layer payloads can keep their historical
        ``market_code`` field, not to mark the market as degenerate.
        """

        return self.country_codes[0] if self.is_single_country else None


def _market_from_object(value: object) -> Market:
    if isinstance(value, Market):
        return value
    if not isinstance(value, Mapping):
        raise InvalidGeographicPosture(
            "each local market must be an object with 'id', 'label' and 'country_codes'"
        )
    raw_id = value.get("id")
    market_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) else ""
    if not market_id:
        raise InvalidGeographicPosture("each local market requires a stable 'id'")
    if not _MARKET_ID_RE.fullmatch(market_id):
        raise InvalidGeographicPosture(
            f"invalid market id: {market_id!r}; use 1-64 letters, digits, '.', '_' or '-'"
        )
    raw_label = value.get("label")
    label = str(raw_label).strip() if isinstance(raw_label, str) else ""
    if not label:
        raise InvalidGeographicPosture(f"market {market_id!r} requires a non-empty 'label'")
    if len(label) > _MAX_LABEL_CHARS:
        raise InvalidGeographicPosture(
            f"market {market_id!r} label exceeds {_MAX_LABEL_CHARS} characters"
        )
    codes = value.get("country_codes")
    if not isinstance(codes, Sequence) or isinstance(codes, (str, bytes)):
        raise InvalidGeographicPosture(
            f"market {market_id!r} country_codes must be an array of ISO alpha-2 codes"
        )
    return Market(id=market_id, label=label, country_codes=tuple(codes))


def _normalize_markets(
    raw_markets: Iterable[object],
    allowed: Collection[str],
) -> tuple[Market, ...]:
    """Validate the operator's market list without judging its composition."""

    markets: list[Market] = []
    ids_seen: dict[str, str] = {}
    code_owner: dict[str, str] = {}

    for raw in raw_markets:
        market = _market_from_object(raw)
        lowered_id = market.id.lower()
        if lowered_id in ids_seen:
            raise InvalidGeographicPosture(
                f"duplicate market id: {market.id!r} (already used by {ids_seen[lowered_id]!r})"
            )
        ids_seen[lowered_id] = market.id

        codes: list[str] = []
        seen_in_market: set[str] = set()
        for raw_code in market.country_codes:
            if not isinstance(raw_code, str):
                raise InvalidGeographicPosture(
                    f"market {market.id!r} country_codes must contain ISO alpha-2 strings"
                )
            code = raw_code.strip().upper()
            if not _CODE_RE.fullmatch(code):
                raise InvalidGeographicPosture(f"invalid ISO alpha-2 country code: {raw_code!r}")
            if code not in allowed:
                raise InvalidGeographicPosture(f"unsupported canonical country code: {code}")
            if code in seen_in_market:
                raise InvalidGeographicPosture(
                    f"duplicate country code after normalization: {code}"
                )
            owner = code_owner.get(code)
            if owner is not None:
                # Arithmetic, not an opinion: overlapping markets double-count
                # every additive total.
                raise InvalidGeographicPosture(
                    f"country code {code} belongs to both market {owner!r} and market "
                    f"{market.id!r}; a country belongs to at most one market — remove it "
                    "from one of them"
                )
            code_owner[code] = market.id
            seen_in_market.add(code)
            codes.append(code)

        if not codes:
            raise InvalidGeographicPosture(
                f"market {market.id!r} requires at least one country code"
            )
        markets.append(Market(id=market.id, label=market.label, country_codes=tuple(sorted(codes))))

    if not markets:
        raise InvalidGeographicPosture("local_markets requires at least one tracked market")
    return tuple(sorted(markets, key=lambda item: (item.id.lower(), item.id)))


def markets_from_country_codes(codes: Iterable[str]) -> tuple[Market, ...]:
    """Read legacy flat codes as the equivalent single-country markets.

    Backward compatibility rule (Story 37.8): id **and** label are the ISO code
    itself, so the reporting output of an already-configured project is
    unchanged.  No display name is substituted and no territory is added — the
    platform must not invent a definition where the operator never gave one.
    """

    normalized = sorted({str(code).strip().upper() for code in codes if str(code).strip()})
    return tuple(
        Market(id=code, label=code, country_codes=(code,)) for code in normalized
    )


@dataclass(frozen=True, slots=True)
class GeographicPosture:
    """The project aggregate.

    ``markets`` is the source of truth.  ``country_codes`` remains available as
    the derived union of every market's members (the tracked set), so callers
    and stored rows written before Story 37.8 keep working; constructing a
    posture from flat ``country_codes`` alone yields the equivalent
    single-country markets.
    """

    mode: str = GLOBAL
    country_codes: tuple[str, ...] = ()
    markets: tuple[Market, ...] = field(default=())

    def __post_init__(self) -> None:
        markets = tuple(self.markets)
        if markets:
            # One canonical order everywhere (fingerprints, diffs, descriptors).
            # Ordering is presentation determinism, never a statement about
            # composition or importance.
            object.__setattr__(
                self,
                "markets",
                tuple(sorted(markets, key=lambda item: (item.id.lower(), item.id))),
            )
            object.__setattr__(
                self,
                "country_codes",
                tuple(sorted({code for market in markets for code in market.country_codes})),
            )
            return
        derived = markets_from_country_codes(self.country_codes)
        object.__setattr__(self, "markets", derived)
        object.__setattr__(
            self, "country_codes", tuple(market.country_codes[0] for market in derived)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "geographic_mode": self.mode,
            "local_markets": [market.as_dict() for market in self.markets],
            "local_market_country_codes": list(self.country_codes),
        }

    def market_by_id(self, market_id: object) -> Market | None:
        wanted = str(market_id or "").strip().lower()
        if not wanted:
            return None
        return next(
            (market for market in self.markets if market.id.lower() == wanted),
            None,
        )

    def market_for_country(self, code: object) -> Market | None:
        wanted = str(code or "").strip().upper()
        if not wanted:
            return None
        return next(
            (market for market in self.markets if wanted in market.country_codes),
            None,
        )


@dataclass(frozen=True, slots=True)
class GeographicReportContext:
    posture: GeographicPosture
    coverage: Mapping[str, object]

    @property
    def mode(self) -> str:
        return self.posture.mode

    @property
    def country_codes(self) -> tuple[str, ...]:
        return self.posture.country_codes

    @property
    def markets(self) -> tuple[Market, ...]:
        return self.posture.markets

    def market_by_id(self, market_id: object) -> Market | None:
        return self.posture.market_by_id(market_id)

    def market_for_country(self, code: object) -> Market | None:
        return self.posture.market_for_country(code)


def _looks_like_market_list(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    return any(isinstance(item, (Mapping, Market)) for item in value)


def normalize_geographic_posture(
    mode: object,
    country_codes: object = (),
    supported_codes: Collection[str] = (),
    *,
    local_markets: object | None = None,
) -> GeographicPosture:
    """Validate and canonicalize a posture payload.

    ``country_codes`` accepts either the legacy flat ISO list or a market list,
    so pre-37.8 callers and stored rows keep working unchanged.  Pass
    ``local_markets`` explicitly for the market shape.
    """

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ALLOWED_MODES:
        raise InvalidGeographicPosture("geographic_mode must be 'global' or 'local_markets'")

    if normalized_mode == GLOBAL:
        return GeographicPosture()

    allowed = {str(code).upper() for code in supported_codes}

    if local_markets is not None:
        if not isinstance(local_markets, Sequence) or isinstance(local_markets, (str, bytes)):
            raise InvalidGeographicPosture("local_markets must be an array of market objects")
        return GeographicPosture(
            mode=LOCAL_MARKETS,
            markets=_normalize_markets(local_markets, allowed),
        )

    if _looks_like_market_list(country_codes):
        return GeographicPosture(
            mode=LOCAL_MARKETS,
            markets=_normalize_markets(country_codes, allowed),  # type: ignore[arg-type]
        )

    if not isinstance(country_codes, Sequence) or isinstance(country_codes, (str, bytes)):
        raise InvalidGeographicPosture(
            "local_market_country_codes must be an array of ISO alpha-2 codes"
        )

    # Legacy flat shape: each code is read as its own single-country market,
    # which is a complete first-class answer, not a degenerate group.
    flat_markets: list[dict[str, object]] = []
    for raw_code in country_codes:
        if not isinstance(raw_code, str):
            raise InvalidGeographicPosture(
                "local_market_country_codes must contain ISO alpha-2 strings"
            )
        code = raw_code.strip().upper()
        if not _CODE_RE.fullmatch(code):
            raise InvalidGeographicPosture(f"invalid ISO alpha-2 country code: {raw_code!r}")
        if any(item["id"] == code for item in flat_markets):
            raise InvalidGeographicPosture(f"duplicate country code after normalization: {code}")
        flat_markets.append({"id": code, "label": code, "country_codes": [code]})

    if not flat_markets:
        raise InvalidGeographicPosture("local_markets requires at least one tracked country")
    return GeographicPosture(
        mode=LOCAL_MARKETS,
        markets=_normalize_markets(flat_markets, allowed),
    )


def merge_geographic_patch(
    current: GeographicPosture,
    body: Mapping[str, object],
    supported_codes: Collection[str],
) -> GeographicPosture:
    has_mode = "geographic_mode" in body
    has_markets = "local_markets" in body
    has_codes = "local_market_country_codes" in body
    if not (has_mode or has_markets or has_codes):
        return current

    mode = body.get("geographic_mode", current.mode)
    normalized_mode = str(mode or "").strip().lower()

    if not has_mode and current.mode == GLOBAL:
        if has_markets and body.get("local_markets"):
            raise InvalidGeographicPosture(
                "local_markets cannot be set while geographic_mode is global"
            )
        if has_codes and body.get("local_market_country_codes"):
            raise InvalidGeographicPosture(
                "local_market_country_codes cannot be set while geographic_mode is global"
            )

    if normalized_mode == GLOBAL:
        return normalize_geographic_posture(mode, (), supported_codes)

    if has_markets:
        return normalize_geographic_posture(
            mode, (), supported_codes, local_markets=body.get("local_markets")
        )
    if has_codes:
        return normalize_geographic_posture(
            mode, body.get("local_market_country_codes"), supported_codes
        )
    return normalize_geographic_posture(
        mode,
        (),
        supported_codes,
        local_markets=[market.as_dict() for market in current.markets],
    )


def bindable_markets(posture: GeographicPosture) -> tuple[Market, ...]:
    """Markets a budget or objective may bind to (never Other markets/Unknown)."""

    return posture.markets if posture.mode == LOCAL_MARKETS else ()


def resolve_market_binding(posture: GeographicPosture, market_id: object) -> Market:
    """Resolve a geographic binding by **market id**, never by country code.

    Binding on the id is what makes a relabel non-breaking, and what keeps the
    synthetic ``Other markets`` / ``Unknown`` groupings out of budgeting.
    """

    from core.geographic_semantics import OTHER_MARKETS, UNKNOWN_MARKET  # noqa: PLC0415

    wanted = str(market_id or "").strip()
    if wanted in {OTHER_MARKETS, UNKNOWN_MARKET}:
        raise InvalidGeographicPosture(
            f"{wanted} is a reporting grouping, not a budgetable market"
        )
    market = posture.market_by_id(wanted)
    if market is None:
        raise InvalidGeographicPosture(f"unknown market id: {wanted!r}")
    return market


def market_diff(
    previous: GeographicPosture, target: GeographicPosture
) -> dict[str, list[dict[str, object]] | list[str]]:
    """Diff two postures as **markets**, not only as codes."""

    previous_by_id = {market.id: market for market in previous.markets}
    target_by_id = {market.id: market for market in target.markets}

    added = [
        target_by_id[market_id].as_dict()
        for market_id in sorted(set(target_by_id) - set(previous_by_id))
    ]
    removed = [
        previous_by_id[market_id].as_dict()
        for market_id in sorted(set(previous_by_id) - set(target_by_id))
    ]
    relabelled: list[dict[str, object]] = []
    recomposed: list[dict[str, object]] = []
    for market_id in sorted(set(previous_by_id) & set(target_by_id)):
        before = previous_by_id[market_id]
        after = target_by_id[market_id]
        if before.label != after.label:
            relabelled.append(
                {"id": market_id, "previous_label": before.label, "new_label": after.label}
            )
        if before.country_codes != after.country_codes:
            before_codes = set(before.country_codes)
            after_codes = set(after.country_codes)
            recomposed.append(
                {
                    "id": market_id,
                    "label": after.label,
                    "added_country_codes": sorted(after_codes - before_codes),
                    "removed_country_codes": sorted(before_codes - after_codes),
                }
            )

    previous_codes = set(previous.country_codes)
    target_codes = set(target.country_codes)
    moved = [
        {
            "country_code": code,
            "previous_market_id": previous.market_for_country(code).id,  # type: ignore[union-attr]
            "new_market_id": target.market_for_country(code).id,  # type: ignore[union-attr]
        }
        for code in sorted(previous_codes & target_codes)
        if previous.market_for_country(code) is not None
        and target.market_for_country(code) is not None
        and previous.market_for_country(code).id != target.market_for_country(code).id  # type: ignore[union-attr]
    ]

    return {
        "added_markets": added,
        "removed_markets": removed,
        "relabelled_markets": relabelled,
        "recomposed_markets": recomposed,
        "moved_country_codes": moved,
        "added_country_codes": sorted(target_codes - previous_codes),
        "removed_country_codes": sorted(previous_codes - target_codes),
    }


def fetch_project_geographic_posture(
    project_id: str,
    conn: object,
    *,
    for_update: bool = False,
) -> GeographicPosture:
    """Read the project-owned posture, defaulting legacy preference rows to Global."""

    suffix = " FOR UPDATE" if for_update else ""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        if for_update:
            # A bare "FOR UPDATE" on project_preferences locks nothing when the
            # row is still absent (brand-new project), which would let two
            # concurrent first-writes both read None and lost-update. Lock the
            # always-present parent project row to serialize them.
            cur.execute(
                "SELECT id FROM app.projects WHERE id = %s FOR UPDATE",
                (project_id,),
            )
        cur.execute(
            "SELECT geographic_mode, local_market_country_codes, local_markets "
            "FROM app.project_preferences WHERE project_id = %s" + suffix,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return GeographicPosture()
    return posture_from_stored_row(row[0], row[1], row[2] if len(row) > 2 else None)


def posture_from_stored_row(
    mode: object,
    country_codes: object,
    local_markets: object = None,
) -> GeographicPosture:
    """Rebuild the aggregate from a stored preference row.

    Rows written before Story 37.8 carry only the flat code array; they read
    back as the equivalent single-country markets so their reporting output is
    unchanged.
    """

    from core.country_vocabulary import get_supported_country_codes  # noqa: PLC0415

    supported = get_supported_country_codes()
    if isinstance(local_markets, str):
        import json  # noqa: PLC0415

        try:
            local_markets = json.loads(local_markets)
        except ValueError:
            local_markets = None
    if isinstance(local_markets, Sequence) and not isinstance(local_markets, (str, bytes)):
        if len(local_markets) > 0:
            return normalize_geographic_posture(
                mode, (), supported, local_markets=list(local_markets)
            )
    return normalize_geographic_posture(mode, country_codes, supported)


def _persist_params(project_id: str, posture: GeographicPosture) -> tuple[object, ...]:
    import json  # noqa: PLC0415

    return (
        project_id,
        posture.mode,
        list(posture.country_codes),
        json.dumps([market.as_dict() for market in posture.markets]),
    )


_UPSERT_POSTURE_SQL = """
    INSERT INTO app.project_preferences
        (project_id, geographic_mode, local_market_country_codes, local_markets)
    VALUES (%s, %s, %s, %s::jsonb)
    ON CONFLICT (project_id) DO UPDATE SET
        geographic_mode = EXCLUDED.geographic_mode,
        local_market_country_codes = EXCLUDED.local_market_country_codes,
        local_markets = EXCLUDED.local_markets,
        updated_at = NOW()
"""


def persist_project_geographic_posture(
    project_id: str,
    posture: GeographicPosture,
    conn: object,
) -> None:
    """Write the project aggregate on the caller-owned transaction.

    The flat ``local_market_country_codes`` column stays in sync as the derived
    union of every market's members, so pre-37.8 readers and the existing
    CHECK constraint keep working.
    """

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(_UPSERT_POSTURE_SQL, _persist_params(project_id, posture))


def fetch_project_geographic_coverage(
    project_id: str,
    posture: GeographicPosture,
    conn: object,
) -> dict[str, object]:
    """Derive honest coverage from current plans and published execution provenance."""

    if posture.mode == GLOBAL:
        return {"status": "consolidated", "datastreams": []}

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT ds.id, ds.name, ds.current_plan_version_id,
                   pv.normalized_payload, published.plan_version_id,
                   published_plan.normalized_payload
            FROM app.datastreams ds
            LEFT JOIN app.datastream_plan_versions pv
              ON pv.id = ds.current_plan_version_id
             AND pv.datastream_id = ds.id
             AND pv.project_id = ds.project_id
            LEFT JOIN app.datastream_executions published
              ON published.id = ds.current_published_execution_id
             AND published.datastream_id = ds.id
             AND published.project_id = ds.project_id
             AND published.state = 'published'
            LEFT JOIN app.datastream_plan_versions published_plan
              ON published_plan.id = published.plan_version_id
             AND published_plan.datastream_id = published.datastream_id
             AND published_plan.project_id = published.project_id
            WHERE ds.project_id = %s AND ds.archived_at IS NULL
            ORDER BY ds.id
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    items: list[dict[str, object]] = []
    for (
        datastream_id,
        name,
        current_plan_id,
        payload,
        published_plan_id,
        published_payload,
    ) in rows:
        geographic = payload.get("geographic", {}) if isinstance(payload, Mapping) else {}
        compilation = geographic.get("compilation_status")
        if compilation == "blocked":
            state = "unavailable"
        elif compilation in {"country_complete", "preserved_full_grain"}:
            published_geographic = (
                published_payload.get("geographic", {})
                if isinstance(published_payload, Mapping)
                else {}
            )
            published_compilation = published_geographic.get("compilation_status")
            published_has_country = published_compilation in {
                "country_complete",
                "preserved_full_grain",
            }
            state = (
                "complete"
                if published_plan_id == current_plan_id or published_has_country
                else "partial"
            )
        else:
            state = "unavailable"
        items.append(
            {
                "datastream_id": datastream_id,
                "datastream_name": name,
                "current_plan_version_id": current_plan_id,
                "published_plan_version_id": published_plan_id,
                "state": state,
            }
        )

    states = [str(item["state"]) for item in items]
    overall = (
        "unavailable"
        if not states or "unavailable" in states
        else "partial"
        if "partial" in states
        else "complete"
    )
    return {"status": overall, "datastreams": items}
