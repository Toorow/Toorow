"""toorow -- confidence lookup: completeness, freshness, provenance (AD-9).

Extracted verbatim from ``core.main.get_daily_report`` in Story 5.1 (AI-27
decomposition). Extended with freshness and provenance terms (G-Confidence-3 fix):

  score = completeness * freshness * provenance

Terms:
  completeness: ratio from app.pull_verifications (existing behaviour, preserved).
  freshness:    1.0 when the most-recent loaded_at is within 48 h of date_to;
                degrades linearly to 0.0 over the next 7 days. Derived from rows'
                loaded_at values passed by the caller (no extra DB round-trip).
  provenance:   fraction of rows carrying a non-empty pull_id (AD-9: traceability).

Return shapes (backward-compatible -- existing consumers read ``completeness``):
  * Single connector requested -> ``{"completeness": float, "freshness": float,
                                      "provenance": float, "score": float}``.
  * Multiple / all connectors  -> ``{"per_connector": {name: ratio, ...},
                                      "freshness": float, "provenance": float,
                                      "score": float}``
                                   where score uses mean(per_connector) as completeness.
  * No data / DB error         -> ``None`` (best-effort; never blocks the report).

AD-2: connector resolution uses generic ``app.connection_ref`` columns
(connector_name). No module-specific names are hardcoded here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Freshness cadence constants (AD-9 / G-Confidence-3):
_FRESHNESS_GRACE_HOURS = 48   # within this window of date_to: freshness = 1.0
_FRESHNESS_DECAY_DAYS = 7     # after grace + this many days: freshness = 0.0


def _compute_freshness(rows: list[dict], date_to: str) -> float:
    """Compute freshness term [0.0, 1.0] from loaded_at values in *rows* (AD-9).

    Freshness is 1.0 when the latest loaded_at timestamp is within
    ``_FRESHNESS_GRACE_HOURS`` hours of ``date_to`` (interpreted as midnight UTC),
    degrading linearly to 0.0 over ``_FRESHNESS_DECAY_DAYS`` days beyond the grace
    window. Returns 1.0 when no loaded_at values are present (best-effort: no rows
    = no penalty, since the report was already empty-stated by the caller).
    """
    if not rows:
        return 1.0

    loaded_ats = []
    for row in rows:
        raw = row.get("loaded_at")
        if raw is None:
            continue
        if isinstance(raw, datetime):
            dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            loaded_ats.append(dt)
        else:
            try:
                s = str(raw).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                loaded_ats.append(dt)
            except (ValueError, TypeError):
                continue

    if not loaded_ats:
        return 1.0

    latest = max(loaded_ats)

    try:
        # date_to is the report end-date (YYYY-MM-DD); interpret as midnight UTC.
        end_midnight = datetime.fromisoformat(date_to + "T00:00:00+00:00")
    except (ValueError, TypeError):
        end_midnight = datetime.now(tz=timezone.utc)

    # Age = how far the latest loaded_at is behind end_midnight.
    age_seconds = (end_midnight - latest).total_seconds()
    grace_seconds = _FRESHNESS_GRACE_HOURS * 3600
    decay_seconds = _FRESHNESS_DECAY_DAYS * 86400

    if age_seconds <= grace_seconds:
        return 1.0
    if age_seconds >= grace_seconds + decay_seconds:
        return 0.0
    # Linear decay from 1.0 to 0.0 over the decay window.
    elapsed_decay = age_seconds - grace_seconds
    return round(1.0 - elapsed_decay / decay_seconds, 4)


def _compute_provenance(rows: list[dict]) -> float:
    """Compute provenance term [0.0, 1.0]: fraction of rows with a non-empty pull_id.

    A pull_id on every row means full traceability (AD-9). Returns 1.0 when there
    are no rows (best-effort: empty result has no provenance gap to penalise).
    """
    if not rows:
        return 1.0
    with_pull = sum(1 for r in rows if r.get("pull_id"))
    return round(with_pull / len(rows), 4)


def compute_confidence(
    project_id: str,
    effective_connectors: list[str] | None,
    *,
    rows: list[dict] | None = None,
    date_to: str | None = None,
) -> dict | None:
    """Return the confidence dict for the report, or None.

    Args:
        project_id:          The project to scope the completeness lookup.
        effective_connectors: Connectors in scope (None = all).
        rows:                Optional list of fact rows used to derive freshness
                             and provenance terms. When omitted the two new terms
                             default to 1.0 (backward-compatible no-op).
        date_to:             Report end date (ISO YYYY-MM-DD), used for freshness
                             age calculation. When omitted, today UTC is used.

    Best-effort: any DB error yields None (no ``confidence`` key in meta). See the
    module docstring for the return shapes.
    """
    _rows = rows or []
    _date_to = date_to or datetime.now(tz=timezone.utc).date().isoformat()

    freshness = _compute_freshness(_rows, _date_to)
    provenance = _compute_provenance(_rows)

    try:
        from core.db import get_connection as _get_pg_conn  # noqa: PLC0415

        with _get_pg_conn() as _pg_conn:
            with _pg_conn.cursor() as _pg_cur:
                if effective_connectors and len(effective_connectors) == 1:
                    # Single-connector request: scope to that connector's connection_ref_id.
                    # Resolve connection_ref_id for this connector in this project (AD-2:
                    # uses generic column name -- connector_name stores the module kebab-case name).
                    _pg_cur.execute(
                        """
                        SELECT pv.completeness_ratio
                        FROM app.pull_verifications pv
                        JOIN app.connection_ref r ON r.id = pv.connection_ref_id
                        WHERE r.project_id = %s
                          AND r.connector_name = %s
                        ORDER BY pv.verified_at DESC
                        LIMIT 1
                        """,
                        (project_id, effective_connectors[0]),
                    )
                    _conf_row = _pg_cur.fetchone()
                    if _conf_row:
                        completeness = float(_conf_row[0])
                        score = round(completeness * freshness * provenance, 4)
                        return {
                            "completeness": completeness,
                            "freshness": freshness,
                            "provenance": provenance,
                            "score": score,
                        }
                    return None

                # Multi-connector or all-connectors: return per-connector confidence map.
                # Each connector gets its own most-recent completeness ratio.
                # Returns {connector_name: completeness_ratio} dict.
                _connector_filter = effective_connectors  # None = all connectors
                if _connector_filter:
                    _pg_cur.execute(
                        """
                        SELECT r.connector_name, pv.completeness_ratio
                        FROM app.pull_verifications pv
                        JOIN app.connection_ref r ON r.id = pv.connection_ref_id
                        WHERE r.project_id = %s
                          AND r.connector_name = ANY(%s)
                        ORDER BY r.connector_name, pv.verified_at DESC
                        """,
                        (project_id, list(_connector_filter)),
                    )
                else:
                    _pg_cur.execute(
                        """
                        SELECT r.connector_name, pv.completeness_ratio
                        FROM app.pull_verifications pv
                        JOIN app.connection_ref r ON r.id = pv.connection_ref_id
                        WHERE r.project_id = %s
                        ORDER BY r.connector_name, pv.verified_at DESC
                        """,
                        (project_id,),
                    )
                _per_connector: dict[str, float] = {}
                for _conn_name, _ratio in _pg_cur.fetchall():
                    # Keep only the most-recent row per connector (query is ordered DESC)
                    if _conn_name not in _per_connector:
                        _per_connector[_conn_name] = float(_ratio)
                if _per_connector:
                    mean_completeness = sum(_per_connector.values()) / len(_per_connector)
                    score = round(mean_completeness * freshness * provenance, 4)
                    return {
                        "per_connector": _per_connector,
                        "freshness": freshness,
                        "provenance": provenance,
                        "score": score,
                    }
                return None
    except Exception:
        return None  # confidence is best-effort; never block the report
