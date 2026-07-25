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

_BRANDING_SQL = """
    SELECT o.org_id, o.brand_primary, o.brand_secondary, o.brand_accent, o.logo_url
    FROM app.projects p
    JOIN app.organizations o ON o.org_id = p.org_id
    WHERE p.project_id = %s
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
