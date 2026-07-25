"""toorow -- Render snapshot store (Story 13.5 volet a).

Persistance des envelopes AD-1 au moment du rendu (get_card / get_report).
Lecture pour la galerie console « Rendus ».

DESIGN :
  - Chaque rendu persisté est un snapshot immuable de l'envelope telle que
    renvoyée au client MCP. Aucune transformation : AD-9 (fraicheur et
    provenance préservées intactes).
  - La rétention est bornée et configurable via la variable d'environnement
    RENDER_SNAPSHOT_RETENTION_DAYS (défaut 30 jours). La purge s'exécute
    inline à chaque write, best-effort (jamais bloquante).
  - Tout accès en lecture est scopé par project_id (AD-5) ; l'appelant doit
    avoir vérifié l'accès avant d'appeler list_render_snapshots /
    get_render_snapshot.

ASCII-only stdout (L-3). Copie FR accentuée dans les exceptions.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Nombre de jours de rétention par défaut.
_DEFAULT_RETENTION_DAYS = 30

# Longueur maximale du summary_snippet stocké.
_SUMMARY_SNIPPET_MAX = 500


def _retention_days() -> int:
    """Lire RENDER_SNAPSHOT_RETENTION_DAYS depuis l'environnement."""
    raw = os.environ.get("RENDER_SNAPSHOT_RETENTION_DAYS", "")
    try:
        days = int(raw)
        if days > 0:
            return days
    except (ValueError, TypeError):
        pass
    return _DEFAULT_RETENTION_DAYS


def _mint_id() -> str:
    """Minter un ULID préfixé 'rsn_'."""
    from ulid import ULID  # noqa: PLC0415

    return f"rsn_{ULID()}"


def persist_render_envelope(
    *,
    project_id: str,
    tool_name: str,
    envelope: dict,
    widget_uri: str | None,
    summary: str,
    tool_args: dict | None,
    identity: str | None,
    trace_id: str | None,
    conn,
) -> str | None:
    """Persister le snapshot d'un rendu dans app.render_snapshots.

    Retourne le snapshot_id inséré, ou None si la persistance échoue.
    N'est JAMAIS bloquante : les erreurs sont loggées en debug.

    La purge de rétention est exécutée dans la même transaction (best-effort).

    Args:
        project_id: Identifiant du projet (AD-5 scope).
        tool_name:  'get_card' ou 'get_report'.
        envelope:   Envelope AD-1 complète (schema_version + meta + data).
        widget_uri: URI du widget au moment du rendu.
        summary:    Résumé LLM (tronqué à _SUMMARY_SNIPPET_MAX).
        tool_args:  Dictionnaire des arguments passés à l'outil.
        identity:   Sujet OAuth de l'appelant.
        trace_id:   Identifiant de trace distribuée.
        conn:       Connexion psycopg ouverte.
    """
    try:
        snap_id = _mint_id()
        snippet = (summary or "")[:_SUMMARY_SNIPPET_MAX]

        # Extraire la question depuis tool_args (template -> answers_question N/A
        # ici, on stocke ce qu'on a : report_ref ou template id).
        question: str | None = None
        if isinstance(tool_args, dict):
            question = (
                tool_args.get("report_id")
                or tool_args.get("report_ref")
                or tool_args.get("template")
                or None
            )
            # Pour get_report le question peut être report_id passé en arg
            if question is None:
                question = str(tool_args.get("report_id", "")) or None

        retention_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_retention_days())

        with conn.cursor() as cur:
            # 1. Insérer le snapshot.
            # Les colonnes JSONB recoivent la chaine JSON avec le cast ::jsonb
            # (pattern psycopg3 verifie dans datastreams.py, dq_monitors.py, etc.)
            cur.execute(
                """
                INSERT INTO app.render_snapshots
                    (id, project_id, tool_name, tool_args, envelope,
                     widget_uri, summary_snippet, question, identity, trace_id)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    snap_id,
                    project_id,
                    tool_name,
                    json.dumps(tool_args) if tool_args is not None else None,
                    json.dumps(envelope),
                    widget_uri,
                    snippet or None,
                    question,
                    identity,
                    trace_id,
                ),
            )

            # 2. Purge de rétention : supprimer les snapshots trop anciens
            #    pour ce projet.
            cur.execute(
                """
                DELETE FROM app.render_snapshots
                WHERE project_id = %s
                  AND created_at < %s
                """,
                (project_id, retention_cutoff),
            )

        conn.commit()
        logger.debug(
            "snapshots: persisted snap_id=%s project=%s tool=%s",
            snap_id,
            project_id,
            tool_name,
        )
        return snap_id
    except Exception as exc:  # noqa: BLE001 -- best-effort, jamais bloquant
        logger.debug("snapshots: persist_failed project=%s tool=%s: %s", project_id, tool_name, exc)
        return None


def list_render_snapshots(
    project_id: str,
    conn,
    *,
    limit: int = 50,
    offset: int = 0,
    tool_name: str | None = None,
) -> list[dict]:
    """Lister les snapshots d'un projet (ordre anti-chronologique).

    Retourne une liste de dicts avec les champs de la galerie.
    Le champ 'envelope' N'EST PAS inclus dans la liste (trop volumineux) ;
    utiliser get_render_snapshot pour récupérer l'envelope complète.

    Args:
        project_id: Scope AD-5 (pré-vérifié par l'appelant).
        conn:       Connexion psycopg ouverte.
        limit:      Nombre maximum de résultats.
        offset:     Décalage de pagination.
        tool_name:  Filtre optionnel ('get_card' ou 'get_report').
    """
    limit = min(max(1, int(limit)), 200)
    offset = max(0, int(offset))

    tool_filter = tool_name if tool_name in ("get_card", "get_report") else None

    with conn.cursor() as cur:
        if tool_filter:
            cur.execute(
                """
                SELECT id, project_id, tool_name, tool_args,
                       widget_uri, summary_snippet, question,
                       identity, trace_id, created_at,
                       envelope -> 'meta' AS meta
                FROM app.render_snapshots
                WHERE project_id = %s
                  AND tool_name  = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (project_id, tool_filter, limit, offset),
            )
        else:
            cur.execute(
                """
                SELECT id, project_id, tool_name, tool_args,
                       widget_uri, summary_snippet, question,
                       identity, trace_id, created_at,
                       envelope -> 'meta' AS meta
                FROM app.render_snapshots
                WHERE project_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (project_id, limit, offset),
            )

        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            record: dict = {}
            for col, val in zip(cols, row):
                if col == "created_at" and val is not None:
                    record[col] = val.isoformat()
                elif col in ("tool_args", "meta") and isinstance(val, str):
                    try:
                        record[col] = json.loads(val)
                    except Exception:
                        record[col] = val
                else:
                    record[col] = val
            rows.append(record)
        return rows


def get_render_snapshot(
    snapshot_id: str,
    project_id: str,
    conn,
) -> dict | None:
    """Récupérer un snapshot complet (avec envelope) pour rouvrir le widget.

    Retourne None si le snapshot n'existe pas ou n'appartient pas au projet
    (scoping AD-5 — le résultat est identique dans les deux cas : 404
    non-disclosant côté appelant).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, tool_name, tool_args, envelope,
                   widget_uri, summary_snippet, question,
                   identity, trace_id, created_at
            FROM app.render_snapshots
            WHERE id         = %s
              AND project_id = %s
            """,
            (snapshot_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None

        cols = [d[0] for d in cur.description]
        record: dict = {}
        for col, val in zip(cols, row):
            if col == "created_at" and val is not None:
                record[col] = val.isoformat()
            elif col in ("tool_args", "envelope") and isinstance(val, str):
                try:
                    record[col] = json.loads(val)
                except Exception:
                    record[col] = val
            else:
                record[col] = val
        return record


def delete_render_snapshot(
    snapshot_id: str,
    project_id: str,
    conn,
) -> bool:
    """Supprimer un snapshot (action console, optionnelle).

    Retourne True si la suppression a eu lieu, False si introuvable.
    Scoping AD-5 : la clause WHERE project_id garantit qu'on ne peut pas
    supprimer un snapshot d'un autre projet (même si l'id est connu).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM app.render_snapshots
            WHERE id         = %s
              AND project_id = %s
            """,
            (snapshot_id, project_id),
        )
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted
