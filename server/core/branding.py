"""toorow — org branding resolution for rendered envelopes (Story 23.1, AC1).

Resolves the branding of the organization that owns a project so the widget
shell can merge the org colors into its single ThemeProvider (AD-11). The
branding columns were introduced by Story 21.2 (migration 036) and validated
there (#RRGGBB hex, API + SQL CHECK) — no re-validation happens here.

Contract (AI-31 additive meta key):
  - Branding defined  -> dict {org_id, brand_primary, brand_secondary,
                               brand_accent, logo_url}
  - No org / no color / ANY resolution error -> None (the caller omits the
    key; the widget renders the default toorow theme). Branding NEVER fails
    a render — degradation is the default theme, not an error.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# THIS QUERY HAD NEVER BEEN EXECUTED AGAINST A SCHEMA. Two of its three column
# references did not exist: `app.organizations` keys on `id` (not `org_id`, named
# twice), and `app.projects` keys on `id` (not `project_id`). Postgres raises
# `UndefinedColumn` on the first one, the degradation contract below catches it,
# and EVERY report has been served with the default theme -- the only trace a
# WARNING nobody reads. Measured 2026-08-23 by driving the eval loop against a
# migrated database; the second error only surfaced after the first was fixed,
# because the parser stops at the first.
#
# Neither spelling appears anywhere else: the ten other call sites on
# `app.organizations` say `o.id`, and every other `FROM app.projects p` filters
# on `p.id`. The alias keeps the returned key name `org_id` -- that is the
# envelope's vocabulary, and it does not change.
#
# `test_branding_sql_runs_against_a_real_schema_pg` is what makes this provable:
# the five tests that guarded this module all mocked `get_connection`, so the
# SQL string was the one thing they could not judge.
_BRANDING_SQL = """
    SELECT o.id AS org_id, o.brand_primary, o.brand_secondary, o.brand_accent, o.logo_url
    FROM app.projects p
    JOIN app.organizations o ON o.id = p.org_id
    WHERE p.id = %s
"""


def resolve_org_branding(project_id: str | None) -> dict | None:
    """Return the org branding dict for *project_id*, or None (see module doc).

    None on: missing/empty project_id, project without org, org with all three
    colors NULL, or any DB error (logged at debug — never raised: AC1).
    """
    if not project_id:
        return None
    try:
        from core.db import get_connection  # noqa: PLC0415 — optional dep (psycopg)

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(_BRANDING_SQL, (project_id,))
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 — degradation = default theme, by contract
        logger.warning(
            "resolve_org_branding: DB resolution failed for %s", project_id, exc_info=True
        )
        return None

    if row is None:
        return None
    # Shape guard. This function's contract is "any DB error -> None, never
    # raised (AC1)", and every caller relies on it: `core.main.get_daily_report`
    # calls it inline with no handler of its own. The unpack below used to sit
    # OUTSIDE the try, so a row of another arity raised straight through the
    # report -- `get_daily_report` returned "not enough values to unpack
    # (expected 5, got 3)" instead of a report with the default theme. A branding
    # lookup must never be able to take a report down; degradation IS the
    # contract. Same posture as `health_enrichment._worst_health`, which already
    # declines to enrich rather than raising inside a report.
    if not isinstance(row, (tuple, list)) or len(row) != 5:
        logger.warning(
            "resolve_org_branding: unexpected row shape for %s (%d fields, expected 5)"
            " -- falling back to the default theme",
            project_id,
            len(row) if isinstance(row, (tuple, list)) else -1,
        )
        return None
    org_id, primary, secondary, accent, logo_url = row
    if primary is None and secondary is None and accent is None:
        # Org exists but defined no branding — same as absent (AC1).
        return None
    return {
        "org_id": org_id,
        "brand_primary": primary,
        "brand_secondary": secondary,
        "brand_accent": accent,
        "logo_url": logo_url,
    }
