"""toorow — Window rule resolver (Story 6.5, AC2).

Stores and resolves relative time window rules for notebooks.
The key feature: every re-run applies the rule against the CURRENT date,
making notebooks 'living documents' (FR7).

Supported rules: last_7d, last_14d, last_30d, last_90d, last_180d, last_365d.

If the user wants to replay a historical window, they use the ``as_of``
parameter on ``run_notebook`` — but ``window_rule`` itself is always relative.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    import zoneinfo
except ImportError:  # pragma: no cover
    from backports import zoneinfo  # type: ignore[no-redef]

SUPPORTED_RULES = [
    "last_7d",
    "last_14d",
    "last_30d",
    "last_90d",
    "last_180d",
    "last_365d",
]

# Map rule -> number of days to subtract from today
_RULE_DAYS: dict[str, int] = {
    "last_7d": 7,
    "last_14d": 14,
    "last_30d": 30,
    "last_90d": 90,
    "last_180d": 180,
    "last_365d": 365,
}


def resolve_window_rule(
    rule: str,
    tz: str = "Europe/Paris",
    *,
    _today=None,  # injectable for tests
) -> tuple[str, str]:
    """Return (date_from_iso, date_to_iso) by resolving `rule` against today in `tz`.

    Parameters:
        rule: One of SUPPORTED_RULES (e.g. 'last_30d').
        tz:   IANA timezone string for the date boundary (default: 'Europe/Paris').
              The project's reporting timezone (AD-6) is passed here.
        _today: Override today's date (for testing). Must be a ``datetime.date`` object.

    Returns:
        Tuple of ISO-8601 date strings ``(date_from, date_to)`` where
        ``date_to = today`` and ``date_from = today - N days``.

    Raises:
        ValueError: if ``rule`` is not in SUPPORTED_RULES.
    """
    if rule not in SUPPORTED_RULES:
        raise ValueError(
            f"Unsupported window rule: {rule!r}. "
            f"Supported rules: {', '.join(SUPPORTED_RULES)}"
        )

    if _today is not None:
        today = _today
    else:
        # Resolve 'today' in the project's timezone so the day boundary is correct
        # (e.g. Europe/Paris midnight, not UTC midnight — AD-6).
        try:
            tz_info = zoneinfo.ZoneInfo(tz)
        except Exception:
            # Fallback to UTC if tz string is invalid
            tz_info = timezone.utc
        today = datetime.now(tz=tz_info).date()

    days = _RULE_DAYS[rule]
    date_to = today
    date_from = today - timedelta(days=days)

    return date_from.isoformat(), date_to.isoformat()
