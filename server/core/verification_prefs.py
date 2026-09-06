"""The per-connector verification preferences: read one, write one.

AD-43, 2026-08-12. Two functions, one table, no route. They close over nothing at
all -- which is what a service looks like when it has been in the wrong file for
a while rather than genuinely entangled with one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("core.admin_api")

def _upsert_verification_prefs(
    project_id: str,
    fields: dict,
    conn: object,
) -> None:
    """Story 17.1: upsert des colonnes source de vérification dans app.project_preferences.

    Crée la ligne si elle n'existe pas (ON CONFLICT DO UPDATE), met à jour uniquement
    les colonnes présentes dans `fields`.

    AD-8 : Postgres est le seul writer. La propagation au miroir se fait via
    mirror_sync.py (SELECT * FROM app.project_preferences).
    """
    if not fields:
        return

    allowed = {"verification_source_type", "verification_source_id", "lead_event_name"}
    update_fields = {k: v for k, v in fields.items() if k in allowed}
    if not update_fields:
        return

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        # Ensure a project_preferences row exists (may not for old projects).
        cur.execute(
            """
            INSERT INTO app.project_preferences (project_id)
            VALUES (%s)
            ON CONFLICT (project_id) DO NOTHING
            """,
            (project_id,),
        )
        set_parts = [f"{col} = %s" for col in update_fields]
        params = list(update_fields.values()) + [project_id]
        cur.execute(
            "UPDATE app.project_preferences SET "
            + ", ".join(set_parts)
            + ", updated_at = NOW() WHERE project_id = %s",
            params,
        )

def _fetch_verification_prefs(project_id: str, conn: object) -> dict:
    """Story 17.1: lit les 3 colonnes source de vérification depuis project_preferences.

    Retourne un dict avec les 3 clés (valeur None si absent ou non configuré).
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT verification_source_type, verification_source_id, lead_event_name
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {
            "verification_source_type": None,
            "verification_source_id": None,
            "lead_event_name": None,
        }
    return {
        "verification_source_type": row[0],
        "verification_source_id": row[1],
        "lead_event_name": row[2],
    }
