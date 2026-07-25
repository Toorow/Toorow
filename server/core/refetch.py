"""toorow -- Tiered refetch ladder for conversion restatements (Story 26.1, C).

Every ad platform restates past days (conversions credited to the click/view
date, invalid-traffic cleanups, moving attribution windows). A single flat
nightly window either re-pulls too little (missed restatements) or too much
(quota burn every night). The ladder declares three depths ONCE in the
module's manifest and core widens the nightly re-pull on schedule:

    "refetch": {"nightly_days": N, "weekly_days": M, "monthly_days": P}

  - every nightly run re-pulls the last N days;
  - the first run of a Monday widens to M days;
  - the first run of the month widens to P days (wins over Monday).

The ladder only ever WIDENS the window (review 26.1 F-7, product decision):
the effective depth is max(per-datastream configured window, ladder rung).
A user's AI-46 ``date_window_days`` override is never narrowed by a rung.

Windows are returned as individually queueable {"date_from", "date_to"} dicts
(<= 31 days each -- the Story 25.5 windowed-backfill pattern), oldest first.
Idempotence needs nothing new: enqueue_pull dedups active windows and the
AD-7 append-only + QUALIFY supersede makes every re-pull safe.

Backward compatibility (AD-22): the manifest block is OPTIONAL and additive
(no schema_version bump -- the manifest schema accepts additional top-level
keys). A module without the block keeps the scheduler's current behaviour
bit-identical; ``windows_for_nightly_dispatch`` then returns exactly the
default window it was given.

AD-2: this file contains zero provider names. The ladder values live in each
module's manifest; core only reads the generic contract.
ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# The manifest key that opts a module into the refetch ladder.
REFETCH_KEY = "refetch"

CADENCE_NIGHTLY = "nightly"
CADENCE_WEEKLY = "weekly"
CADENCE_MONTHLY = "monthly"

_CADENCE_DAYS_KEY = {
    CADENCE_NIGHTLY: "nightly_days",
    CADENCE_WEEKLY: "weekly_days",
    CADENCE_MONTHLY: "monthly_days",
}

# Individual window ceiling -- mirrors account_topology.BACKFILL_WINDOW_DAYS
# (Story 25.5): each queued window spans at most 31 days.
REFETCH_WINDOW_DAYS = 31

# Sanity ceiling for a single ladder depth (matches the backfill bound).
REFETCH_MAX_DAYS = 365


# ---------------------------------------------------------------------------
# Manifest contract (mirror of account_topology.validate_topology/get_topology)
# ---------------------------------------------------------------------------


def validate_refetch(refetch) -> list[str]:
    """Return contract-violation strings for a *refetch* block ([] when valid).

    Contract: {"nightly_days": int, "weekly_days": int, "monthly_days": int},
    each in [1, 365], monotonically non-decreasing (a ladder that narrows as
    it deepens is a declaration mistake, refused loudly).
    """
    errors: list[str] = []
    if not isinstance(refetch, dict):
        return ["refetch must be an object"]

    values: dict[str, int] = {}
    for key in ("nightly_days", "weekly_days", "monthly_days"):
        value = refetch.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"refetch.{key} must be an integer")
            continue
        if value < 1 or value > REFETCH_MAX_DAYS:
            errors.append(f"refetch.{key} must be in [1, {REFETCH_MAX_DAYS}]")
            continue
        values[key] = value

    if len(values) == 3 and not (
        values["nightly_days"] <= values["weekly_days"] <= values["monthly_days"]
    ):
        errors.append(
            "refetch ladder must be non-decreasing: "
            "nightly_days <= weekly_days <= monthly_days"
        )
    return errors


def get_refetch(manifest) -> dict | None:
    """Return the validated ``refetch`` block from *manifest*, or None.

    None means the module does NOT participate (backward compatible). A
    PRESENT-but-INVALID block returns None and logs a warning -- malformed
    must not silently behave like valid, but must not crash dispatch either
    (same policy as account_topology.get_topology).
    """
    if not isinstance(manifest, dict):
        return None
    refetch = manifest.get(REFETCH_KEY)
    if refetch is None:
        return None
    errors = validate_refetch(refetch)
    if errors:
        logger.warning(
            "refetch: invalid contract in manifest name=%s errors=%s",
            manifest.get("name", "?"),
            "; ".join(errors),
        )
        return None
    return refetch


def get_refetch_for_provider(provider: str) -> dict | None:
    """Return the validated refetch block for a loaded *provider*, or None.

    Uses the core.main public accessor (never imports server/modules/* --
    AD-2). Any registry failure returns None (dispatch falls back to the
    default window; a lookup hiccup must never block the nightly run).
    """
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        for loaded in get_loaded_modules():
            if loaded.name == provider:
                return get_refetch(getattr(loaded, "manifest", None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("refetch: module registry unavailable: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Cadence + window computation (pure functions)
# ---------------------------------------------------------------------------


def resolve_refetch_cadence(today: date) -> str:
    """Return the ladder cadence for a run happening on *today*.

    First run of the month (day == 1) -> monthly (the deepest rung wins even
    when the 1st is a Monday); first run of a Monday -> weekly; else nightly.
    """
    if today.day == 1:
        return CADENCE_MONTHLY
    if today.weekday() == 0:
        return CADENCE_WEEKLY
    return CADENCE_NIGHTLY


def compute_refetch_windows(
    today: date,
    refetch: dict,
    cadence: str | None = None,
) -> list[dict]:
    """Return the re-pull windows for *cadence* as queueable window dicts.

    The span covers the last ``<cadence>_days`` days ENDING YESTERDAY (the
    nightly convention), split into contiguous, non-overlapping windows of at
    most REFETCH_WINDOW_DAYS days, oldest first (the Story 25.5 backfill
    pattern -- each window is one enqueue_pull job, individually retryable).

    Args:
        today:   The run's reference date (usually date.today()).
        refetch: A VALIDATED ladder block (get_refetch output).
        cadence: One of nightly/weekly/monthly; None -> resolved from *today*.

    Returns:
        [{"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}, ...]

    Raises:
        ValueError: unknown cadence string (a caller bug, loud).
    """
    if cadence is None:
        cadence = resolve_refetch_cadence(today)
    days_key = _CADENCE_DAYS_KEY.get(cadence)
    if days_key is None:
        raise ValueError(
            f"unknown refetch cadence {cadence!r}; "
            f"expected one of {tuple(_CADENCE_DAYS_KEY)}"
        )
    days = int(refetch[days_key])

    overall_to = today - timedelta(days=1)
    overall_from = overall_to - timedelta(days=days - 1)

    windows: list[dict] = []
    cursor = overall_from
    while cursor <= overall_to:
        window_to = min(cursor + timedelta(days=REFETCH_WINDOW_DAYS - 1), overall_to)
        windows.append(
            {"date_from": cursor.isoformat(), "date_to": window_to.isoformat()}
        )
        cursor = window_to + timedelta(days=1)
    return windows


# ---------------------------------------------------------------------------
# Scheduler hook (the single, minimal branch point -- Story 26.1 C)
# ---------------------------------------------------------------------------


def _window_length_days(window: dict) -> int | None:
    """Length in days of a {"date_from","date_to"} window, or None if unreadable."""
    try:
        window_from = date.fromisoformat(str(window["date_from"]))
        window_to = date.fromisoformat(str(window["date_to"]))
    except (KeyError, TypeError, ValueError):
        return None
    length = (window_to - window_from).days + 1
    return length if length >= 1 else None


def windows_for_nightly_dispatch(
    provider: str,
    as_of_date: date,
    default_window: dict,
) -> list[dict]:
    """Return the windows the nightly dispatch should enqueue for *provider*.

    - Module manifest declares a valid ``refetch`` block -> the EFFECTIVE
      depth is max(per-datastream window, ladder rung) -- review 26.1 F-7
      (product decision, per-project-preferences doctrine): the ladder
      WIDENS a user's configured window (AI-46 ``date_window_days``), it
      NEVER narrows it. Applies to every rung (nightly/weekly/monthly).
      When the ladder rung is deeper, the ladder windows for the cadence
      resolved from *as_of_date* are returned; when the user's window is
      already at least as deep, ``default_window`` is returned unchanged
      (identity object).
    - No block / invalid block / ANY resolution failure -> exactly
      ``[default_window]``, preserving the scheduler's current behaviour
      bit-identically (AD-22). A refetch problem must never block dispatch.
    """
    try:
        refetch = get_refetch_for_provider(provider)
        if refetch is None:
            return [default_window]
        cadence = resolve_refetch_cadence(as_of_date)
        ladder_days = int(refetch[_CADENCE_DAYS_KEY[cadence]])
        default_days = _window_length_days(default_window)
        if default_days is not None and ladder_days <= default_days:
            # F-7: the user's per-datastream window is already at least as
            # deep as this rung -- keep the user's window (never narrow it).
            return [default_window]
        return compute_refetch_windows(as_of_date, refetch, cadence)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "refetch: window_resolution_failed provider=%s: %s -- "
            "falling back to the default window",
            provider,
            exc,
        )
        return [default_window]
