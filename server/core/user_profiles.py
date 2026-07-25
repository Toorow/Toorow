"""toorow -- global per-identity user profiles (Story 21.2, Epic 21, FR37/CAP-25).

A user profile is GLOBAL to an identity (the AD-14 opaque subject), not scoped to
an org: the same display name / avatar follows a person across every organization
they belong to. Stored in ``app.user_profiles`` (migration 036).

IMPORTANT (design decision, Story 21.2): the platform's Google OAuth flow is a
DATA-access flow that does NOT request identity scopes (openid/email/profile) --
see ``core.google_oauth`` ("authorizes DATA access, not identity"). So an avatar
is NEVER populated from the Google data callback. It is set by the user
(self-service, ``avatar_source='self'``) or by the real identity provider via
``upsert_user_profile`` the day it exposes a picture claim.

AD-8: Postgres is the sole writer; no secret is stored here.
"""

from __future__ import annotations

_PROFILE_COLUMNS = ("identity", "display_name", "email", "avatar_url", "avatar_source")


def fetch_user_profile(identity: str, conn) -> dict:
    """Return the profile for *identity*, or a default (all-None) shape if absent.

    Never raises on a missing row -- an identity with no profile yet is a normal
    state (returns the identity with null fields).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT identity, display_name, email, avatar_url, avatar_source, updated_at
            FROM app.user_profiles WHERE identity = %s
            """,
            (identity,),
        )
        row = cur.fetchone()
    if row is None:
        return {
            "identity": identity,
            "display_name": None,
            "email": None,
            "avatar_url": None,
            "avatar_source": None,
            "updated_at": None,
        }
    return {
        "identity": row[0],
        "display_name": row[1],
        "email": row[2],
        "avatar_url": row[3],
        "avatar_source": row[4],
        "updated_at": row[5].isoformat() if row[5] is not None else None,
    }


def upsert_user_profile(
    identity: str,
    conn,
    *,
    display_name: str | None = None,
    email: str | None = None,
    avatar_url: str | None = None,
    avatar_source: str | None = None,
) -> dict:
    """Insert or update the profile for *identity*, touching only provided fields.

    Reusable integration point: the identity provider (or the self-service API)
    calls this. Do NOT call it from the Google DATA callback -- that flow carries
    no identity claims (see module docstring).

    Only keyword fields that are not None are written; the rest are left as-is on
    update (so a partial patch does not clobber existing values). ``conn`` is an
    open psycopg connection owned by the caller (the caller commits).
    """
    provided: dict[str, object] = {}
    for col, val in (
        ("display_name", display_name),
        ("email", email),
        ("avatar_url", avatar_url),
        ("avatar_source", avatar_source),
    ):
        if val is not None:
            provided[col] = val

    with conn.cursor() as cur:
        if not provided:
            # No-op upsert: ensure a row exists, change nothing else.
            cur.execute(
                "INSERT INTO app.user_profiles (identity) VALUES (%s) "
                "ON CONFLICT (identity) DO NOTHING",
                (identity,),
            )
        else:
            cols = list(provided.keys())
            insert_cols = ["identity", *cols]
            placeholders = ", ".join(["%s"] * len(insert_cols))
            update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            params = [identity, *[provided[c] for c in cols]]
            cur.execute(
                f"INSERT INTO app.user_profiles ({', '.join(insert_cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (identity) DO UPDATE SET {update_set}, updated_at = NOW()",
                params,
            )
    return fetch_user_profile(identity, conn)
