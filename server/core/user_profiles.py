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

# app.user_profiles.display_name is TEXT but the API caps it at 255; a name from a
# third-party token must obey the same cap rather than fail the write downstream.
_DISPLAY_NAME_MAX = 255


def display_name_from_claims(claims: object) -> str | None:
    """The person's name as the identity provider states it, or None.

    Chosen order: ``name`` (what the provider itself considers the display form),
    then ``given_name``/``family_name`` recomposed -- either alone is accepted,
    because plenty of people have only one.

    The header of this module says an avatar is never populated from the Google
    DATA callback, and that remains true: this reads the claims of the IDENTITY
    token the console authenticates with (issuer accounts.google.com), which is a
    different thing entirely and is exactly the provider this file anticipated.

    Returns None rather than a placeholder whenever nothing usable is present, so
    the caller can tell "the provider gave us a name" apart from "we must ask" --
    the whole point of the flow: invitation -> ask ONLY if the token has no name.
    Deliberately total: any shape of input yields a name or None, never raises,
    because it runs on a path that must not be able to fail an acceptance.
    """
    if not isinstance(claims, dict):
        return None

    def _clean(key: str) -> str:
        value = claims.get(key)
        return value.strip() if isinstance(value, str) else ""

    name = _clean("name") or " ".join(p for p in (_clean("given_name"), _clean("family_name")) if p)
    if not name:
        return None
    # Truncate rather than refuse: a 300-character name is still that person's
    # name, and refusing it would send them to a form to retype what we already
    # have. Cut on a word boundary when there is one nearby.
    if len(name) > _DISPLAY_NAME_MAX:
        cut = name[:_DISPLAY_NAME_MAX]
        space = cut.rfind(" ")
        name = (cut[:space] if space > _DISPLAY_NAME_MAX // 2 else cut).strip()
    return name or None


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
