"""Transactional, fail-closed canonical application identity resolution.

The only automatic identity key is the exact inbound ``(issuer, subject)``
pair. A normalized verified email is stored as a claim but is never queried to
select or merge a person.
"""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass

from ulid import ULID


class CanonicalIdentityValidationError(ValueError):
    """Inbound identity claims are missing or malformed."""


class CanonicalIdentityUnavailable(RuntimeError):
    """Canonical identity could not be resolved safely."""

    def __init__(self, message: str, *, correlation_id: str | None = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id or f"identity_{secrets.token_hex(12)}"


class CanonicalIdentityClaimConflict(CanonicalIdentityUnavailable):
    """A stable identity returned a different verified-email claim."""


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    person_id: str
    issuer: str
    subject: str
    verified_email: str | None
    created: bool


def _stable_claim(name: str, value: str, *, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CanonicalIdentityValidationError(f"{name} must be a bounded non-empty string")
    if value != value.strip() or any(not char.isprintable() for char in value):
        raise CanonicalIdentityValidationError(f"{name} contains invalid whitespace")
    return value


def normalize_verified_email(value: str) -> str:
    """Normalize a provider-verified email without treating it as an identity key."""

    if not isinstance(value, str):
        raise CanonicalIdentityValidationError("verified_email must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or len(normalized) > 320 or normalized.count("@") != 1:
        raise CanonicalIdentityValidationError("verified_email is malformed")
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or any(char.isspace() for char in normalized):
        raise CanonicalIdentityValidationError("verified_email is malformed")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise CanonicalIdentityValidationError("verified_email is malformed") from exc
    if not ascii_domain or len(ascii_domain) > 255:
        raise CanonicalIdentityValidationError("verified_email is malformed")
    return f"{local}@{ascii_domain}"


def _touch_identity(cur, issuer: str, subject: str, verified_email: str | None) -> tuple:
    cur.execute(
        """
        UPDATE app.person_identities
        SET verified_email = CASE
                WHEN verified_email IS NULL THEN %s
                ELSE verified_email
            END,
            verified_email_at = CASE
                WHEN verified_email IS NULL AND %s IS NOT NULL THEN NOW()
                ELSE verified_email_at
            END,
            last_seen_at = NOW()
        WHERE issuer = %s AND subject = %s
          AND (%s IS NULL OR verified_email IS NULL OR verified_email = %s)
        RETURNING person_id, verified_email
        """,
        (
            verified_email,
            verified_email,
            issuer,
            subject,
            verified_email,
            verified_email,
        ),
    )
    row = cur.fetchone()
    if isinstance(row, (tuple, list)) and len(row) >= 2:
        return row
    cur.execute(
        "SELECT 1 FROM app.person_identities WHERE issuer = %s AND subject = %s",
        (issuer, subject),
    )
    if cur.fetchone() is not None:
        raise CanonicalIdentityClaimConflict("verified identity claim changed")
    raise CanonicalIdentityUnavailable("canonical identity returned an invalid result")


def resolve_canonical_identity(
    conn,
    *,
    issuer: str,
    subject: str,
    verified_email: str | None = None,
) -> CanonicalIdentity:
    """Find or create one person for an exact stable inbound identity.

    The caller owns the connection and outer transaction. ``conn.transaction``
    creates a transaction or savepoint, so a partial person/identity pair can
    never escape. Database failures are converted to a sanitized fail-closed
    error; raw claims are never included in diagnostics.
    """

    stable_issuer = _stable_claim("issuer", issuer)
    stable_subject = _stable_claim("subject", subject)
    normalized_email = (
        normalize_verified_email(verified_email) if verified_email is not None else None
    )
    transaction = getattr(conn, "transaction", None)
    if not callable(transaction):
        raise CanonicalIdentityUnavailable("transactional identity resolution unavailable")

    try:
        with transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT person_id
                    FROM app.person_identities
                    WHERE issuer = %s AND subject = %s
                    FOR UPDATE
                    """,
                    (stable_issuer, stable_subject),
                )
                if cur.fetchone() is not None:
                    row = _touch_identity(cur, stable_issuer, stable_subject, normalized_email)
                    return CanonicalIdentity(
                        person_id=str(row[0]),
                        issuer=stable_issuer,
                        subject=stable_subject,
                        verified_email=str(row[1]) if row[1] is not None else None,
                        created=False,
                    )

                person_id = f"person_{ULID()}"
                identity_id = f"pident_{ULID()}"
                cur.execute("INSERT INTO app.persons (id) VALUES (%s)", (person_id,))
                cur.execute(
                    """
                    INSERT INTO app.person_identities
                        (id, person_id, issuer, subject, verified_email, verified_email_at)
                    VALUES (%s, %s, %s, %s, %s,
                            CASE WHEN %s IS NULL THEN NULL ELSE NOW() END)
                    ON CONFLICT (issuer, subject) DO NOTHING
                    RETURNING person_id, verified_email
                    """,
                    (
                        identity_id,
                        person_id,
                        stable_issuer,
                        stable_subject,
                        normalized_email,
                        normalized_email,
                    ),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    return CanonicalIdentity(
                        person_id=str(inserted[0]),
                        issuer=stable_issuer,
                        subject=stable_subject,
                        verified_email=(str(inserted[1]) if inserted[1] is not None else None),
                        created=True,
                    )

                # A concurrent transaction won the unique (issuer, subject)
                # insert. Remove our now-unreferenced candidate and resolve the
                # winner under lock. No email-based lookup is ever performed.
                cur.execute("DELETE FROM app.persons WHERE id = %s", (person_id,))
                row = _touch_identity(cur, stable_issuer, stable_subject, normalized_email)
                return CanonicalIdentity(
                    person_id=str(row[0]),
                    issuer=stable_issuer,
                    subject=stable_subject,
                    verified_email=str(row[1]) if row[1] is not None else None,
                    created=False,
                )
    except CanonicalIdentityUnavailable:
        raise
    except Exception as exc:
        raise CanonicalIdentityUnavailable("canonical identity resolution failed") from exc
