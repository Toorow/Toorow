"""toorow -- Partage tokenise des snapshots rendus (Story 13.5 volet b).

Modalite O1 (ratifiee AD-20) :
  Un partage = un GRANT sur UN snapshot FIGE. URL tokenisee read-only publique.
  Aucun re-run, aucun acces aux donnees live : le endpoint public ne touche que
  app.render_snapshot_shares et app.render_snapshots -- jamais les marts ni le projet.

DESIGN (voir migration 054) :
  - Table dediee app.render_snapshot_shares (id, snapshot_id FK, share_token UNIQUE,
    shared_at, shared_by, revoked_at).
  - Un snapshot peut avoir plusieurs partages (re-partage possible, historique).
  - Révocation = revoked_at non-NULL (jamais de DELETE, historique conserve).
  - Token : secrets.token_urlsafe(24) -> 192 bits, URL-safe (~32 chars).
  - Lecture publique : get_shared_snapshot(token) filtre sur revoked_at IS NULL.
    Ne touche QUE les deux tables render_snapshot_shares + render_snapshots (O1 strict).

AD-5 : create_share / revoke_share / list_shares scopees projet (appelant verifie
  l'acces avant d'appeler). get_shared_snapshot est intentionnellement non-scope :
  le token EST l'autorisation (O1).
AD-9 : shared_at honnete = horodatage de creation du partage (pas modifie).
       stale_since = freshness gelee = envelope.meta.freshness tel que stocke.

ASCII-only stdout (L-3). Copie FR accentuee dans les exceptions.
"""

from __future__ import annotations

import json
import logging
import secrets

logger = logging.getLogger(__name__)

# Longueur de token en bytes -> 24 bytes = 192 bits -> ~32 chars URL-safe base64.
_TOKEN_BYTES = 24


def _mint_share_id() -> str:
    """Minter un ULID prefixe 'rss_'."""
    from ulid import ULID  # noqa: PLC0415

    return f"rss_{ULID()}"


def _mint_token() -> str:
    """Generer un token URL-safe 192 bits (pattern identique Story 6.6)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


# ---------------------------------------------------------------------------
# create_share
# ---------------------------------------------------------------------------


def create_share(
    snapshot_id: str,
    project_id: str,
    identity: str | None,
    conn,
) -> tuple[str, str] | None:
    """Creer un partage tokenise pour un snapshot (O1).

    Verifie que le snapshot appartient bien a project_id (AD-5 -- l'appelant
    doit egalement avoir passe la verification d'acces au projet, mais on
    double-check ici via la jointure).

    Retourne (share_id, share_token) en cas de succes, None si le snapshot
    n'existe pas ou n'appartient pas au projet.

    Args:
        snapshot_id: Identifiant du snapshot a partager.
        project_id:  Projet proprietaire du snapshot (scope AD-5).
        identity:    Sujet OAuth de l'auteur du partage.
        conn:        Connexion psycopg ouverte.
    """
    # Verifier que le snapshot existe et appartient au projet.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.render_snapshots WHERE id = %s AND project_id = %s",
            (snapshot_id, project_id),
        )
        if cur.fetchone() is None:
            return None

    share_id = _mint_share_id()
    token = _mint_token()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.render_snapshot_shares
                (id, snapshot_id, share_token, shared_at, shared_by)
            VALUES (%s, %s, %s, NOW(), %s)
            """,
            (share_id, snapshot_id, token, identity),
        )
    conn.commit()

    logger.debug(
        "snapshot_shares: created share_id=%s snapshot=%s project=%s",
        share_id,
        snapshot_id,
        project_id,
    )
    return share_id, token


# ---------------------------------------------------------------------------
# revoke_share
# ---------------------------------------------------------------------------


def revoke_share(
    share_id: str,
    project_id: str,
    conn,
) -> bool:
    """Revoquer un partage (revoked_at = NOW()).

    Verifie que le partage appartient bien a un snapshot du projet (AD-5).
    Retourne True si la revocation a eu lieu, False si introuvable ou deja revoque.

    Args:
        share_id:   Identifiant du partage (rss_...).
        project_id: Projet proprietaire (scope AD-5).
        conn:       Connexion psycopg ouverte.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.render_snapshot_shares s
            SET revoked_at = NOW()
            FROM app.render_snapshots r
            WHERE s.id         = %s
              AND s.snapshot_id = r.id
              AND r.project_id  = %s
              AND s.revoked_at IS NULL
            """,
            (share_id, project_id),
        )
        updated = cur.rowcount > 0
    conn.commit()

    if updated:
        logger.debug(
            "snapshot_shares: revoked share_id=%s project=%s",
            share_id,
            project_id,
        )
    return updated


# ---------------------------------------------------------------------------
# list_shares
# ---------------------------------------------------------------------------


def list_shares(
    snapshot_id: str,
    project_id: str,
    conn,
    *,
    active_only: bool = False,
) -> list[dict]:
    """Lister les partages d'un snapshot (console, scope projet AD-5).

    Retourne une liste de dicts {id, snapshot_id, share_token, shared_at,
    shared_by, revoked_at}. Les tokens sont INCLUS dans la liste console
    (l'identite a deja ete verifiee par l'appelant).

    Args:
        snapshot_id: Identifiant du snapshot.
        project_id:  Scope AD-5.
        conn:        Connexion psycopg ouverte.
        active_only: Si True, ne retourne que les partages non revoques.
    """
    with conn.cursor() as cur:
        if active_only:
            cur.execute(
                """
                SELECT s.id, s.snapshot_id, s.share_token,
                       s.shared_at, s.shared_by, s.revoked_at
                FROM app.render_snapshot_shares s
                JOIN app.render_snapshots r ON r.id = s.snapshot_id
                WHERE s.snapshot_id = %s
                  AND r.project_id  = %s
                  AND s.revoked_at IS NULL
                ORDER BY s.shared_at DESC
                """,
                (snapshot_id, project_id),
            )
        else:
            cur.execute(
                """
                SELECT s.id, s.snapshot_id, s.share_token,
                       s.shared_at, s.shared_by, s.revoked_at
                FROM app.render_snapshot_shares s
                JOIN app.render_snapshots r ON r.id = s.snapshot_id
                WHERE s.snapshot_id = %s
                  AND r.project_id  = %s
                ORDER BY s.shared_at DESC
                """,
                (snapshot_id, project_id),
            )

        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            record: dict = {}
            for col, val in zip(cols, row):
                if col in ("shared_at", "revoked_at") and val is not None:
                    record[col] = val.isoformat()
                else:
                    record[col] = val
            rows.append(record)
        return rows


# ---------------------------------------------------------------------------
# get_shared_snapshot (endpoint public O1 -- intentionnellement non-scope projet)
# ---------------------------------------------------------------------------


def get_shared_snapshot(token: str, conn) -> dict | None:
    """Lire un snapshot partage via son token (endpoint public, O1 strict).

    Retourne le snapshot fige + metadonnees de partage, ou None si :
      - Le token est inconnu.
      - Le partage est revoque (revoked_at IS NOT NULL).

    GARANTIE O1 : cette fonction ne touche QUE les tables
      app.render_snapshot_shares et app.render_snapshots.
      Elle n'accede JAMAIS aux marts, aux connexions, aux projets, ni a
      aucune autre table du schema app. Aucun re-run n'est effectue.

    Le resultat contient :
      - envelope       : l'envelope AD-1 gelee au moment du rendu (jamais
                         re-executee, AD-9).
      - widget_uri     : URI du widget au moment du rendu.
      - shared_at      : horodatage du partage (AD-9, "partage le").
      - stale_since    : freshness gelee = envelope.meta.freshness tel que
                         stocke (honnete, AD-9).
      - tool_name      : 'get_card' ou 'get_report'.
      - question       : question ou rapport (informatif).
      - summary_snippet: extrait LLM (informatif).

    Args:
        token: Token URL-safe du partage.
        conn:  Connexion psycopg ouverte.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id           AS share_id,
                   s.shared_at,
                   s.shared_by,
                   r.id           AS snapshot_id,
                   r.tool_name,
                   r.envelope,
                   r.widget_uri,
                   r.summary_snippet,
                   r.question,
                   r.created_at   AS rendered_at
            FROM app.render_snapshot_shares s
            JOIN app.render_snapshots r ON r.id = s.snapshot_id
            WHERE s.share_token = %s
              AND s.revoked_at IS NULL
            """,
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            return None

        cols = [d[0] for d in cur.description]
        record: dict = {}
        for col, val in zip(cols, row):
            if col in ("shared_at", "rendered_at") and val is not None:
                record[col] = val.isoformat()
            elif col == "envelope":
                if isinstance(val, str):
                    try:
                        record[col] = json.loads(val)
                    except Exception:
                        record[col] = val
                else:
                    record[col] = val
            else:
                record[col] = val

    # Extraire stale_since depuis l'envelope gelee (AD-9 : freshness figee).
    envelope = record.get("envelope") or {}
    meta = envelope.get("meta") or {}
    record["stale_since"] = meta.get("freshness")

    return record
