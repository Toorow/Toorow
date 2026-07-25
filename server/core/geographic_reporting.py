"""Project-level Global versus Local markets posture (CAP-27)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Collection, Mapping, Sequence

GLOBAL = "global"
LOCAL_MARKETS = "local_markets"
ALLOWED_MODES = frozenset({GLOBAL, LOCAL_MARKETS})
_CODE_RE = re.compile(r"^[A-Z]{2}$")


class InvalidGeographicPosture(ValueError):
    """A geographic preference payload violates the CAP-27 aggregate."""


@dataclass(frozen=True, slots=True)
class GeographicPosture:
    mode: str = GLOBAL
    country_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "geographic_mode": self.mode,
            "local_market_country_codes": list(self.country_codes),
        }


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


def normalize_geographic_posture(
    mode: object,
    country_codes: object,
    supported_codes: Collection[str],
) -> GeographicPosture:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ALLOWED_MODES:
        raise InvalidGeographicPosture("geographic_mode must be 'global' or 'local_markets'")

    if normalized_mode == GLOBAL:
        return GeographicPosture()

    if not isinstance(country_codes, Sequence) or isinstance(country_codes, (str, bytes)):
        raise InvalidGeographicPosture(
            "local_market_country_codes must be an array of ISO alpha-2 codes"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    allowed = {str(code).upper() for code in supported_codes}
    for raw_code in country_codes:
        if not isinstance(raw_code, str):
            raise InvalidGeographicPosture(
                "local_market_country_codes must contain ISO alpha-2 strings"
            )
        code = raw_code.strip().upper()
        if not _CODE_RE.fullmatch(code):
            raise InvalidGeographicPosture(f"invalid ISO alpha-2 country code: {raw_code!r}")
        if code in seen:
            raise InvalidGeographicPosture(f"duplicate country code after normalization: {code}")
        if code not in allowed:
            raise InvalidGeographicPosture(f"unsupported canonical country code: {code}")
        seen.add(code)
        normalized.append(code)

    if not normalized:
        raise InvalidGeographicPosture("local_markets requires at least one tracked country")
    return GeographicPosture(
        mode=LOCAL_MARKETS,
        country_codes=tuple(sorted(normalized)),
    )


def merge_geographic_patch(
    current: GeographicPosture,
    body: Mapping[str, object],
    supported_codes: Collection[str],
) -> GeographicPosture:
    has_mode = "geographic_mode" in body
    has_codes = "local_market_country_codes" in body
    if not (has_mode or has_codes):
        return current

    mode = body.get("geographic_mode", current.mode)
    codes = body.get("local_market_country_codes", current.country_codes)
    normalized_mode = str(mode or "").strip().lower()

    if not has_mode and current.mode == GLOBAL and has_codes and codes:
        raise InvalidGeographicPosture(
            "local_market_country_codes cannot be set while geographic_mode is global"
        )
    if normalized_mode == GLOBAL:
        codes = ()
    return normalize_geographic_posture(mode, codes, supported_codes)


def fetch_project_geographic_posture(
    project_id: str,
    conn: object,
    *,
    for_update: bool = False,
) -> GeographicPosture:
    """Read the project-owned posture, defaulting legacy preference rows to Global."""

    from core.country_vocabulary import get_supported_country_codes

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
            "SELECT geographic_mode, local_market_country_codes "
            "FROM app.project_preferences WHERE project_id = %s" + suffix,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return GeographicPosture()
    return normalize_geographic_posture(
        row[0],
        row[1],
        get_supported_country_codes(),
    )


def persist_project_geographic_posture(
    project_id: str,
    posture: GeographicPosture,
    conn: object,
) -> None:
    """Write the project aggregate on the caller-owned transaction."""

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            INSERT INTO app.project_preferences
                (project_id, geographic_mode, local_market_country_codes)
            VALUES (%s, %s, %s)
            ON CONFLICT (project_id) DO UPDATE SET
                geographic_mode = EXCLUDED.geographic_mode,
                local_market_country_codes = EXCLUDED.local_market_country_codes,
                updated_at = NOW()
            """,
            (project_id, posture.mode, list(posture.country_codes)),
        )


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
