"""toorow -- LEGACY snapshot sharing, reduced to revocation and history (Story 50.7).

RETIRED, NOT DELETED. This module used to be the whole public-sharing contract, and
its own docstring stated the design defect out loud: *"le token EST l'autorisation"*
-- the plaintext token in the URL path WAS the authorization. Three things followed
from that, and all three are why it is gone:

  * The token lived in the URL PATH, so every browser history entry, referrer,
    proxy log and ASGI access log held a live bearer, before any application code
    ran. No handler-level redaction can reach the request line.
  * It was stored in the CLEAR (`app.render_snapshot_shares.share_token`), and
    returned in the console listing on purpose -- the old docstring argued that was
    acceptable because the caller is authenticated.
  * `app.render_snapshot_shares` has no `project_id`, NO EXPIRY COLUMN AT ALL, and
    CASCADE-deletes with a snapshot the retention purge removes. So a grant could
    never expire, and the proof that it had been revoked could be deleted by a
    background job.

WHAT SURVIVES HERE, and only this: `revoke_share` and a token-free `list_shares`,
so existing grants can still be closed and their history read. `_mint_token`,
`create_share` and `get_shared_snapshot` are DELETED, and migration 162 adds a
trigger refusing any INSERT into the table -- so a remounted route fails loudly at
the database rather than quietly minting a new plaintext bearer.

The replacement is `core.render_shares`: one revocable, EXPIRING, audited grant to
one immutable `app.renders` row, with a 256-bit bearer stored only as a peppered
HMAC and delivered in a URL fragment that never reaches a server.

Neither the table nor its rows are dropped. They are the only proof that the open
grants were closed (CLAUDE.md anti-drift rule 3).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Story 50.7: `_TOKEN_BYTES` and `_mint_token` are removed. Nothing in this
# module mints a bearer any more.


def _mint_share_id() -> str:
    """Minter un ULID prefixe 'rss_'."""
    from ulid import ULID  # noqa: PLC0415

    return f"rss_{ULID()}"


# ---------------------------------------------------------------------------
# `create_share` is DELETED by Story 50.7. It INSERTed a plaintext 192-bit
# token. Its replacement is `core.render_shares.create_share`: a 256-bit
# bearer, stored only as hmac(pepper, "render-share-bearer:" || bearer), a
# mandatory expiry, and creation governed through `execute_operation` so the
# grant and its audit row commit together.
# ---------------------------------------------------------------------------


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
    """List a snapshot's LEGACY shares -- for revocation and history only.

    Returns {id, snapshot_id, shared_at, shared_by, revoked_at}. The token is NOT
    returned and is no longer selected at all. The previous version of this
    docstring argued the opposite -- that including it was fine because the caller
    is authenticated -- which is how a live public grant ended up in every console
    screenshot, browser cache entry and support-ticket paste.

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
                SELECT s.id, s.snapshot_id,
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
                SELECT s.id, s.snapshot_id,
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
# `get_shared_snapshot` is DELETED by Story 50.7.
#
# It looked a snapshot up by PLAINTEXT token equality, filtered only on
# `revoked_at IS NULL` -- there was no expiry check because there was no
# expiry column. Replaced by the AD-30 exchange: the bearer is consumed once
# in a POST body, and the resulting session is revalidated against the
# Share's live state on EVERY call, not only at exchange.
# ---------------------------------------------------------------------------
