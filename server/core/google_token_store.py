"""toorow -- Encrypted Google token store (Story 18.1, AD-21).

The SINGLE writer/reader/decryptor of the Google direct-auth token blob stored in
``app.connection_ref.encrypted_token_blob`` (migration 029). This is the one
component allowed to touch that blob (AD-8 single ownership); no connector module
(GA4, GSC, Ads, Sheets) reads or writes it directly -- they all go through the
provider-aware ``get_fresh_token`` facade delivered in Story 18.3.

AD-21 (RATIFIED 2026-07-18): this module is the encrypted exception to AD-3,
strictly bounded to the Google stack. It preserves AD-3's intent under three
guarantees:
  1. Encryption at rest equivalent to Nango -- the blob is sealed with a key
     derived from ``tenant_keys`` (Secret Manager in Phase B), the same key class
     Nango uses. Postgres only ever sees opaque ciphertext (NFR3).
  2. Single writer -- this module is the only one that encrypts/decrypts/persists.
  3. Audit -- every emission/refresh/revocation writes an On-Behalf-Of row (AD-14).

SECURITY INVARIANTS (the 18.5 review is adversarial on these -- any plaintext
token leak = CRITICAL):
  * The decrypted token payload NEVER leaves this module except as the explicit
    return value of ``load_google_token`` (consumed by the 18.3 service). It is
    never logged, never put in an exception message, never in a fixture.
  * ``__repr__`` / ``__str__`` of every object defined here contain NO secret --
    only ids, lengths, expiry, and scope names.
  * Every exception raised here is rewritten WITHOUT the token contents
    (the ``token redacted`` pattern). We never let a stray ``ValueError`` from the
    crypto layer carry ciphertext or plaintext up the stack.
  * Logs carry ids and lengths only -- never token material.

Crypto choice (see the story Dev Agent Record for the full rationale):
  * Primitive: ``cryptography.fernet.Fernet`` (AES-128-CBC + HMAC-SHA256, an
    authenticated/AEAD-equivalent construction with a versioned token format and a
    built-in timestamp). Chosen over a hand-rolled AESGCM wrapper because Fernet
    ships a stable, misuse-resistant, self-describing container and ``cryptography``
    is already resolved in the lockfile.
  * Key: ``tenant_keys.get_tenant_key_backend().get_or_create_key(project_id)``
    returns raw 32 bytes. A Fernet key is exactly a 32-byte urlsafe-base64 value,
    so we derive it directly from the tenant key with ``urlsafe_b64encode`` -- the
    same key class that protects Nango's tokens. No trivial/derived key (the
    ineffective-encryption failure mode called out in the epic).
  * Blob format is VERSIONED so tenant-key rotation (AI-42 dual-version window) can
    be handled without breaking old blobs: the plaintext we encrypt is a JSON
    document ``{"v": 1, "access_token": ..., "refresh_token": ..., "metadata": ...}``.
    The wire format version lives INSIDE the sealed plaintext; the Fernet container
    itself also carries its own version byte. Bumping ``_BLOB_FORMAT_VERSION`` lets a
    future reader branch on the document shape after a clean decrypt.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from core.tenant_keys import get_tenant_key_backend

logger = logging.getLogger(__name__)

# Versioned blob document format. Bump when the decrypted JSON shape changes so a
# future reader can branch after a clean decrypt (tenant-key rotation stays
# orthogonal -- see module docstring).
_BLOB_FORMAT_VERSION = 1

# Redaction sentinel used everywhere a token value might otherwise surface.
_REDACTED = "[REDACTED]"


class GoogleTokenStoreError(Exception):
    """Raised for any token-store failure, ALWAYS with the token contents redacted.

    Never carries access_token, refresh_token, ciphertext, or the tenant key --
    only a short, safe message. Catch this to surface a re-consent / auth error to
    the operator without risking a plaintext leak (AD-21 / NFR3).
    """


@dataclass
class GoogleToken:
    """Decrypted Google token, returned ONLY by ``load_google_token``.

    This object crosses the module boundary intentionally (the 18.3 service uses it
    to mint fresh access tokens). Its ``__repr__``/``__str__`` are overridden so the
    secret fields never appear in a log line, a traceback frame, or an f-string.
    """

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_expiry: datetime | None = None
    granted_scopes: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # noqa: D105 -- secret-safe repr
        # NEVER include access_token / refresh_token / metadata values.
        return (
            "GoogleToken("
            f"access_token=<{len(self.access_token)} chars, redacted>, "
            f"refresh_token=<{len(self.refresh_token)} chars, redacted>, "
            f"token_expiry={self.token_expiry!r}, "
            f"granted_scopes={self.granted_scopes!r})"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Crypto core (pure functions -- no DB)
# ---------------------------------------------------------------------------


def _fernet_for(project_id: str) -> Fernet:
    """Build a Fernet from the project's tenant key.

    The tenant key is raw 32 bytes; a Fernet key is that same 32 bytes,
    urlsafe-base64-encoded. Same key class as Nango's token protection (AD-21).

    Raises:
        GoogleTokenStoreError: if the key cannot be obtained (message redacted --
            the tenant key value is NEVER included).
    """
    try:
        key = get_tenant_key_backend().get_or_create_key(project_id)
    except Exception as exc:  # rewrite without any secret material
        raise GoogleTokenStoreError(
            f"tenant key unavailable for project (token redacted): {type(exc).__name__}"
        ) from None
    # Fernet requires a 32-byte urlsafe-base64 key; tenant keys are raw 32 bytes.
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token_payload(payload: dict, project_id: str) -> bytes:
    """Encrypt a token payload dict into an opaque blob.

    Args:
        payload: dict carrying at least ``access_token`` and ``refresh_token``
                 (and optionally ``metadata``). NEVER logged.
        project_id: project whose tenant key seals the blob.

    Returns:
        Opaque ciphertext bytes -- NOT decryptable without the tenant key.

    Raises:
        GoogleTokenStoreError: on any failure, with token contents redacted.
    """
    fernet = _fernet_for(project_id)
    doc = {
        "v": _BLOB_FORMAT_VERSION,
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "metadata": payload.get("metadata") or {},
    }
    try:
        plaintext = json.dumps(doc).encode("utf-8")
        return fernet.encrypt(plaintext)
    except Exception as exc:  # never let the plaintext escape in the error
        raise GoogleTokenStoreError(
            f"token encryption failed (token redacted): {type(exc).__name__}"
        ) from None


def decrypt_token_payload(blob: bytes, project_id: str) -> dict:
    """Decrypt an opaque blob back into the token payload dict.

    Args:
        blob: ciphertext produced by ``encrypt_token_payload``.
        project_id: project whose tenant key unseals the blob.

    Returns:
        dict with ``access_token``, ``refresh_token``, ``metadata`` (and ``v``).

    Raises:
        GoogleTokenStoreError: if the blob is absent, tampered, or the key is
            wrong. The message is redacted -- it never echoes ciphertext or
            plaintext (a wrong-key attempt yields only a clean "cannot decrypt").
    """
    if not blob:
        raise GoogleTokenStoreError("no encrypted token blob (token redacted)")
    fernet = _fernet_for(project_id)
    try:
        plaintext = fernet.decrypt(bytes(blob))
    except InvalidToken:
        # Wrong key or tampered blob. Do NOT include the blob in the message.
        raise GoogleTokenStoreError(
            "cannot decrypt token blob: wrong key or tampered (token redacted)"
        ) from None
    except Exception as exc:
        raise GoogleTokenStoreError(
            f"token decryption failed (token redacted): {type(exc).__name__}"
        ) from None
    try:
        doc = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise GoogleTokenStoreError(
            f"decrypted token blob is not valid JSON (token redacted): {type(exc).__name__}"
        ) from None
    # review-18-1 F-2: the announced format-version guarantee must actually exist.
    # Fernet's HMAC proves integrity but NOT anti-rollback between two validly
    # sealed blobs -- an unknown/stale version is REJECTED, never silently accepted.
    if doc.get("v") != _BLOB_FORMAT_VERSION:
        raise GoogleTokenStoreError(
            "unsupported token blob format version (token redacted)"
        )
    return doc


# ---------------------------------------------------------------------------
# DB-backed store (single writer -- AD-8)
# ---------------------------------------------------------------------------


def _check_expected_project(actual_project_id: str, expected_project_id: str | None) -> None:
    """review-18-1 F-1 (defense in depth): reject cross-project id confusion.

    The AD-5 identity check lives in the caller (18.3 facade); this guard only
    catches a caller that ALREADY resolved a project but passes a foreign
    connection id -- it can never replace the identity check.
    """
    if expected_project_id is not None and actual_project_id != expected_project_id:
        raise GoogleTokenStoreError(
            "connection_ref does not belong to the expected project (token redacted)"
        )


def _project_id_for_connection(cur, connection_ref_id: str) -> str:
    """Resolve the project_id for a connection_ref row (needed to pick the key)."""
    cur.execute(
        "SELECT project_id FROM app.connection_ref WHERE id = %s",
        (connection_ref_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise GoogleTokenStoreError(
            f"connection_ref not found: id={connection_ref_id!r} (token redacted)"
        )
    return row[0]


def store_google_token(
    connection_ref_id: str,
    payload: dict,
    expiry: datetime | None,
    scopes: list[str] | None,
    expected_project_id: str | None = None,
) -> None:
    """Encrypt and persist a Google token onto an existing connection_ref row.

    Sets ``auth_path='google_direct'``, ``encrypted_token_blob`` (sealed),
    ``token_expiry`` (clear -- not a secret), and ``granted_scopes`` (clear).

    AD-5 CONTRACT (review-18-1 F-1): this store does NOT verify that the calling
    identity has access to the connection's project. The caller (the 18.3
    ``get_fresh_token`` facade / the 18.2 OAuth flow) MUST have validated
    ``identity_has_project_access`` BEFORE calling. Defense in depth: pass
    ``expected_project_id`` and the store rejects any mismatch with the row's
    actual project (cross-tenant id confusion becomes a hard error).

    Args:
        connection_ref_id: the ``conn_`` id of the target row (must already exist).
        payload: dict with ``access_token`` + ``refresh_token`` (+ optional
                 ``metadata``). NEVER logged. ``metadata`` must NEVER carry
                 duplicated token material (the anti-leak contract relies on it).
        expiry: access-token expiry for local health (clear).
        scopes: granted Google scopes (clear).
        expected_project_id: when provided, must equal the row's project_id.

    Raises:
        GoogleTokenStoreError: on failure (token contents redacted).
    """
    from core.db import get_connection  # local import: avoid import cycle at load

    scopes = scopes or []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                project_id = _project_id_for_connection(cur, connection_ref_id)
                _check_expected_project(project_id, expected_project_id)
                blob = encrypt_token_payload(payload, project_id)
                cur.execute(
                    """
                    UPDATE app.connection_ref
                       SET auth_path = 'google_direct',
                           encrypted_token_blob = %s,
                           token_expiry = %s,
                           granted_scopes = %s,
                           updated_at = now()
                     WHERE id = %s
                    """,
                    (blob, expiry, scopes, connection_ref_id),
                )
            conn.commit()
    except GoogleTokenStoreError:
        raise
    except Exception as exc:
        raise GoogleTokenStoreError(
            f"store_google_token failed (token redacted): {type(exc).__name__}"
        ) from None
    # AD-3/NFR3: log ids + lengths + expiry/scopes only -- NEVER the token.
    logger.info(
        "google_token_store: token_stored connection_ref_id=%s blob_bytes=%d "
        "scopes=%d expiry=%s",
        connection_ref_id,
        len(blob),
        len(scopes),
        expiry.isoformat() if expiry else "none",
    )


def load_google_token(
    connection_ref_id: str,
    expected_project_id: str | None = None,
) -> GoogleToken:
    """Load and decrypt the Google token for a connection.

    This is the ONE sanctioned exit point for the decrypted payload -- the 18.3
    service calls it to mint fresh access tokens. Callers must not log or persist
    the returned object's secret fields (AD-3 on the module side stays true).

    AD-5 CONTRACT (review-18-1 F-1): the caller MUST have validated
    ``identity_has_project_access`` before calling — this store does not
    re-check identity. ``expected_project_id`` adds defense in depth.

    Raises:
        GoogleTokenStoreError: if the connection is absent, has no blob, or the
            blob cannot be decrypted (message redacted).
    """
    from core.db import get_connection  # local import: avoid import cycle at load

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id, encrypted_token_blob, token_expiry,
                           granted_scopes
                      FROM app.connection_ref
                     WHERE id = %s
                    """,
                    (connection_ref_id,),
                )
                row = cur.fetchone()
    except GoogleTokenStoreError:
        raise
    except Exception as exc:
        raise GoogleTokenStoreError(
            f"load_google_token failed (token redacted): {type(exc).__name__}"
        ) from None

    if row is None:
        raise GoogleTokenStoreError(
            f"connection_ref not found: id={connection_ref_id!r} (token redacted)"
        )
    project_id, blob, expiry, scopes = row
    _check_expected_project(project_id, expected_project_id)
    doc = decrypt_token_payload(blob, project_id)
    # AD-3/NFR3: log the successful load with ids only -- never the token.
    logger.info(
        "google_token_store: token_loaded connection_ref_id=%s scopes=%d",
        connection_ref_id,
        len(scopes or []),
    )
    return GoogleToken(
        access_token=doc.get("access_token") or "",
        refresh_token=doc.get("refresh_token") or "",
        token_expiry=expiry,
        granted_scopes=list(scopes or []),
        metadata=doc.get("metadata") or {},
    )


def clear_google_token(
    connection_ref_id: str,
    performed_by: str = "system",
    expected_project_id: str | None = None,
) -> bool:
    """Purge the encrypted token blob for a connection and write an audit row.

    AD-5 CONTRACT (review-18-1 F-1): identity check is the CALLER's duty
    (18.3/18.4); ``expected_project_id`` adds defense in depth. review-18-1 F-6:
    ``performed_by`` MUST carry the real human identity on any user-triggered
    path — the 'system' default is for internal maintenance only; émission/refresh
    audit rows are delivered by 18.2/18.3 (this store audits revocation only).

    Nulls out ``encrypted_token_blob``, ``token_expiry``, ``granted_scopes`` and
    resets ``auth_path`` back to ``'nango'`` (the safe default -- no dangling
    google_direct row without a token). Idempotent: purging an already-clear row is
    a no-op that still returns cleanly.

    Args:
        connection_ref_id: the ``conn_`` id to purge.
        performed_by: identity subject for the On-Behalf-Of audit row (AD-14).

    Returns:
        True if a row was updated, False if the connection did not exist.

    Raises:
        GoogleTokenStoreError: on DB failure (token contents redacted).
    """
    from core.db import get_connection  # local import: avoid import cycle at load

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if expected_project_id is not None:
                    actual = _project_id_for_connection(cur, connection_ref_id)
                    _check_expected_project(actual, expected_project_id)
                cur.execute(
                    """
                    UPDATE app.connection_ref
                       SET encrypted_token_blob = NULL,
                           token_expiry = NULL,
                           granted_scopes = NULL,
                           auth_path = 'nango',
                           updated_at = now()
                     WHERE id = %s
                    """,
                    (connection_ref_id,),
                )
                updated = cur.rowcount
            conn.commit()
    except GoogleTokenStoreError:
        raise
    except Exception as exc:
        raise GoogleTokenStoreError(
            f"clear_google_token failed (token redacted): {type(exc).__name__}"
        ) from None

    if updated == 0:
        return False

    # AD-14 On-Behalf-Of audit row for the purge (best-effort -- never blocks).
    try:
        from core.audit import ACTION_CONNECTION_REVOKED, write_audit_row

        write_audit_row(
            identity=performed_by,
            action=ACTION_CONNECTION_REVOKED,
            provider_account="google_direct",
            connection_ref=connection_ref_id,
            metadata={"event": "google_token_cleared"},
        )
    except Exception as exc:  # pragma: no cover -- audit never blocks the purge
        logger.warning("google_token_store: audit_write_failed: %s", type(exc).__name__)

    logger.info(
        "google_token_store: token_cleared connection_ref_id=%s", connection_ref_id
    )
    return True
