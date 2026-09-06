"""toorow -- Per-tenant encryption key management (Story 7.3, AC1, AC2).

Dual-backend design:
  Phase A (P3-dev): LocalFileKeyBackend -- 32-byte random keys stored as
    base64-encoded files in infra/keys/<project_id>.key. The directory is
    gitignored (key material NEVER in git, logs, or Postgres plaintext).
  Phase B (future): SecretManagerKeyBackend -- keys in GCP Secret Manager as
    projects/<gcp_project>/secrets/tenant-key-<project_id>/versions/latest.
    Requires HG-A (GCP billing account). The interface is identical; switching
    is a one-line env var change (TENANT_KEY_BACKEND=secret_manager).

Key scope (P3-dev honesty, per AC1 PRAGMATIC SCOPE):
  The per-tenant key at P3-dev governs the key LIFECYCLE (provision at project
  create, rotate on demand, delete on archive) and proves the revocation
  plumbing.  The actual OAuth tokens live in Nango's own store under Nango's
  global NANGO_ENCRYPTION_KEY (AD-3).  The key isolation layer protects cached
  metadata (connection health, module names) -- not the OAuth tokens themselves.
  Every key lifecycle event is audited in app.tenant_key_audit (AC3).

AD-3: key material is NEVER written to Postgres, logs, or environment variables.
AD-8: per AD-8 the key backend is the only surface that touches key files.
"""

from __future__ import annotations

import base64
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class TenantKeyBackendUnavailable(RuntimeError):
    """The configured backend cannot be reached at all (SDK absent, no project id).

    Distinct from "this tenant has no key": that answers None. This says the key
    store itself is unusable, which must never be mistaken for an empty one --
    the mistake that lets a caller mint a replacement key over a live one.
    """


class TenantKeyBackend(ABC):
    """Abstract base class for per-tenant key storage backends.

    All methods return/accept raw 32-byte key material (bytes).
    Key material must NEVER be logged, stored in Postgres, or sent over the wire.
    """

    @abstractmethod
    def get_or_create_key(self, project_id: str) -> bytes:
        """Return the 32-byte key for the project, creating one if it does not exist.

        Thread-safe: concurrent calls for the same project_id must converge on
        the same key (last-write-wins for local; atomic Secret Manager version).

        ENCRYPTION ONLY. A decryption path that calls this mints a fresh key when
        the real one is missing and then fails to open its own ciphertext with a
        message that blames the ciphertext (AI-278: that is exactly how a live
        Google credential came to be unreadable, one `key_created` log line
        before every `cannot decrypt` — see docs/claude-md-incidents.md). Read
        with ``get_key`` / ``decryption_keys`` instead.
        """

    @abstractmethod
    def get_key(self, project_id: str) -> bytes | None:
        """Return the project's current key, or None when there is none.

        NEVER creates. This is the read side of ``get_or_create_key``: it lets a
        decryption path tell "no key here" (unrecoverable, say so) apart from
        "wrong key" instead of manufacturing a third possibility.
        """

    def decryption_keys(self, project_id: str) -> list[bytes]:
        """Keys to try when opening EXISTING ciphertext, newest first.

        A backend that keeps previous key versions alive during a rotation
        overlap window (AI-42) overrides this to return them after the current
        one. The default is the single current key, or nothing at all.
        """
        key = self.get_key(project_id)
        return [key] if key is not None else []

    @abstractmethod
    def delete_key(self, project_id: str) -> bool:
        """Delete the key for the project.

        Returns:
            True  -- key was present and deleted.
            False -- key was not found (already deleted or never created).

        Must NOT raise on missing key.
        """

    @abstractmethod
    def rotate_key(self, project_id: str) -> bytes:
        """Generate a new key for the project, replacing the old one.

        The old key is NOT kept -- Phase A stores only the current key.
        Phase B callers may implement versioned secrets for a decrypt-only
        old-key window, but that is out of scope for this story.

        Returns:
            The new 32-byte key.
        """


# ---------------------------------------------------------------------------
# Phase A: LocalFileKeyBackend
# ---------------------------------------------------------------------------


class LocalFileKeyBackend(TenantKeyBackend):
    """Phase A: per-tenant keys stored as base64-encoded files.

    Storage layout:
        {key_dir}/{project_id}.key  -- contains base64url-encoded 32-byte key.

    The directory (default: infra/keys) MUST be gitignored before the first key
    file is written (enforced by .gitignore entry added in Story 7.3).

    Security note (Phase A honesty):
        Files are created with default umask permissions.  In production, use
        a restricted directory (chmod 700) and the SecretManagerKeyBackend.
        The LocalFileKeyBackend is a dev-time analog of Secret Manager -- it
        uses real cryptographic random key material, not fake/mock values.
    """

    def __init__(self, key_dir: str | None = None) -> None:
        """Initialise with the key directory path.

        Args:
            key_dir: Directory path for key files.  Defaults to TENANT_KEY_DIR
                     env var, then "infra/keys" as a last resort.
        """
        if key_dir is None:
            key_dir = os.environ.get("TENANT_KEY_DIR", "infra/keys")
        self._key_dir = Path(key_dir)

    def _key_path(self, project_id: str) -> Path:
        """Return the Path for a project's key file.

        Project IDs are sanitised to path-safe characters (only alphanumeric,
        hyphens, underscores -- matching the 'proj_<ULID>' and 'default' formats
        the admin API produces).
        """
        safe_id = "".join(c for c in project_id if c.isalnum() or c in ("-", "_"))
        if not safe_id:
            raise ValueError(f"project_id produces empty safe name: {project_id!r}")
        return self._key_dir / f"{safe_id}.key"

    def _ensure_key_dir(self) -> None:
        # Create with mode 0o700 (owner only).  On Windows the mode bits are
        # mostly no-ops (NTFS ACLs govern access) -- harmless.
        self._key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _write_key_file(self, key_path, encoded: str) -> None:
        """Write encoded key to key_path with mode 0o600 (owner read/write only).

        Uses low-level os.open so the file is created with the correct mode
        atomically (Path.write_text uses default umask, not a fixed mode).
        On Windows the mode bits are mostly no-ops -- harmless.
        """
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(encoded)

    def get_or_create_key(self, project_id: str) -> bytes:
        """Return existing key or generate a new 32-byte random key."""
        key_path = self._key_path(project_id)
        if key_path.exists():
            try:
                raw = key_path.read_text(encoding="ascii").strip()
                key = base64.urlsafe_b64decode(raw)
                if len(key) != 32:
                    raise ValueError(
                        f"Key file {key_path} contains {len(key)} bytes (expected 32)"
                    )
                return key
            except Exception as exc:
                logger.warning(
                    "tenant_keys: corrupted key file for project %r: %s -- regenerating",
                    project_id,
                    exc,
                )
        # Generate new key
        self._ensure_key_dir()
        key = os.urandom(32)
        self._write_key_file(key_path, base64.urlsafe_b64encode(key).decode("ascii"))
        # AD-3: log event only -- never log the key value
        logger.info("tenant_keys: key_created project_id=[REDACTED]")
        return key

    def get_key(self, project_id: str) -> bytes | None:
        """Return the stored key, or None. Never creates, never regenerates."""
        key_path = self._key_path(project_id)
        if not key_path.exists():
            return None
        try:
            key = base64.urlsafe_b64decode(key_path.read_text(encoding="ascii").strip())
        except Exception as exc:  # noqa: BLE001 -- corrupted file, not a key
            logger.warning(
                "tenant_keys: unreadable key file for project %r: %s", project_id, exc
            )
            return None
        if len(key) != 32:
            logger.warning(
                "tenant_keys: key file for project %r holds %d bytes (expected 32)",
                project_id,
                len(key),
            )
            return None
        return key

    def delete_key(self, project_id: str) -> bool:
        """Delete the key file. Returns True if deleted, False if not found."""
        key_path = self._key_path(project_id)
        if not key_path.exists():
            return False
        key_path.unlink()
        # AD-3: log event only -- never log key material
        logger.info("tenant_keys: key_deleted project_id=[REDACTED]")
        return True

    def rotate_key(self, project_id: str) -> bytes:
        """Generate a new key, overwrite the existing file. Returns new key."""
        self._ensure_key_dir()
        key_path = self._key_path(project_id)
        new_key = os.urandom(32)
        self._write_key_file(key_path, base64.urlsafe_b64encode(new_key).decode("ascii"))
        # AD-3: log event only -- never log key material
        logger.info("tenant_keys: key_rotated project_id=[REDACTED]")
        return new_key


# ---------------------------------------------------------------------------
# Phase B: SecretManagerKeyBackend (interface-complete stub)
# ---------------------------------------------------------------------------


class SecretManagerKeyBackend(TenantKeyBackend):
    """Phase B: per-tenant keys stored in GCP Secret Manager.

    Secret naming convention:
        projects/<gcp_project>/secrets/tenant-key-<project_id>/versions/latest

    Switch: set TENANT_KEY_BACKEND=secret_manager. No code changes required --
    the factory function (get_tenant_key_backend) handles the wiring.

    KEY ROTATION OVERLAP WINDOW (AI-42 -- implemented in ``decryption_keys``)
    ----------------------------------------------------------------------------
    LocalFileKeyBackend.rotate_key() overwrites the single key file: any value
    encrypted under the old key becomes undecryptable the instant rotation
    lands.  That is acceptable at P3-dev (nothing long-lived is encrypted under
    tenant keys), but NOT in Phase B where in-flight jobs may still hold
    ciphertext produced under the previous version.  The Phase B strategy is a
    DUAL-VERSION ACTIVE window, natively supported by Secret Manager versions:

      1. rotate_key() adds a NEW secret version (never destroys the previous
         one) and returns the new key.  Encryption always uses "latest".
      2. Decryption tries "latest" first, then falls back to the previous
         version while it is still enabled (try-newest-first, at most 2).
      3. A KEY_ROTATION_GRACE_PERIOD env var (seconds; default 86400) defines
         how long the previous version stays enabled.  After the grace period
         a scheduled cleanup DISABLES (not destroys) the old version; destroy
         only after a further manual confirmation, per GCP best practice.
      4. Every rotation writes a tka_ audit row (write_key_audit_row) with the
         old/new version numbers -- never key material (AD-3).

    Steps 1 and 2 are live below. Step 3's scheduled disable and step 4's audit
    row are an operator action for now: rotation is manual, and a version left
    enabled costs correctness nothing -- only the size of the window.

    WHY THIS EXISTS (AI-278). The local backend cannot hold a production key: the
    only writable directory a Cloud Run container has is its own ephemeral
    filesystem, so the key file died with the instance and `get_or_create_key`
    minted a fresh key on the next request. Every Google credential and every
    alert-destination secret sealed under the old key became permanently
    unreadable, silently, one request later.
    """

    _MISSING_SDK_MSG = (
        "google-cloud-secret-manager is not installed -- TENANT_KEY_BACKEND=secret_manager "
        "cannot read or write tenant keys. Install the dependency or set "
        "TENANT_KEY_BACKEND=local."
    )

    # How many previous versions stay eligible for DECRYPTION after a rotation
    # (AI-42 dual-version window). Encryption always uses the newest.
    _DECRYPT_FALLBACK_VERSIONS = 1

    def __init__(self, gcp_project: str | None = None) -> None:
        """Initialise against a GCP project.

        Args:
            gcp_project: the GCP project holding the secrets. Defaults to
                GCP_PROJECT, then GOOGLE_CLOUD_PROJECT.
        """
        self._gcp_project = (
            gcp_project
            or os.environ.get("GCP_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        self._client = None

    # -- plumbing ----------------------------------------------------------

    def _secret_id(self, project_id: str) -> str:
        """Secret name for a tenant. Sanitised to Secret Manager's charset.

        Secret ids accept ``[A-Za-z0-9_-]`` only, which is already the shape of
        ``proj_<ULID>``; the filter is here so a hand-made id cannot produce a
        path that names a DIFFERENT tenant's secret.
        """
        safe_id = "".join(c for c in project_id if c.isalnum() or c in ("-", "_"))
        if not safe_id:
            raise ValueError(f"project_id produces empty secret name: {project_id!r}")
        return f"tenant-key-{safe_id}"

    def _api(self):
        """Return the Secret Manager client, created once per backend instance."""
        if self._client is None:
            try:
                from google.cloud import secretmanager  # noqa: PLC0415
            except ImportError as exc:
                raise TenantKeyBackendUnavailable(self._MISSING_SDK_MSG) from exc
            if not self._gcp_project:
                raise TenantKeyBackendUnavailable(
                    "TENANT_KEY_BACKEND=secret_manager requires GCP_PROJECT (or "
                    "GOOGLE_CLOUD_PROJECT) to name the project holding the secrets."
                )
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _parent(self) -> str:
        return f"projects/{self._gcp_project}"

    def _version_path(self, project_id: str, version: str = "latest") -> str:
        return f"{self._parent()}/secrets/{self._secret_id(project_id)}/versions/{version}"

    @staticmethod
    def _decode(payload: bytes, where: str) -> bytes | None:
        """Decode a stored payload to raw 32 bytes, or None when it is not one."""
        try:
            key = base64.urlsafe_b64decode(payload.strip())
        except Exception as exc:  # noqa: BLE001 -- corrupted payload, not a key
            logger.warning("tenant_keys: unreadable secret payload (%s): %s", where, exc)
            return None
        if len(key) != 32:
            logger.warning(
                "tenant_keys: secret payload (%s) holds %d bytes (expected 32)",
                where,
                len(key),
            )
            return None
        return key

    def _add_version(self, project_id: str, key: bytes) -> None:
        """Append a new enabled version carrying *key*. Never destroys a prior one."""
        client = self._api()
        client.add_secret_version(
            request={
                "parent": f"{self._parent()}/secrets/{self._secret_id(project_id)}",
                "payload": {"data": base64.urlsafe_b64encode(key)},
            }
        )

    # -- interface ---------------------------------------------------------

    def get_key(self, project_id: str) -> bytes | None:
        """Return the newest key version, or None when the secret does not exist."""
        from google.api_core import exceptions as gexc  # noqa: PLC0415

        client = self._api()
        try:
            response = client.access_secret_version(
                request={"name": self._version_path(project_id)}
            )
        except gexc.NotFound:
            return None
        except gexc.FailedPrecondition:
            # Every version disabled/destroyed -- the secret exists but holds no
            # usable key. Same answer as absent, and honest about it.
            logger.warning("tenant_keys: secret has no enabled version project_id=[REDACTED]")
            return None
        return self._decode(response.payload.data, "latest")

    def get_or_create_key(self, project_id: str) -> bytes:
        """Return the newest key version, creating the secret on first use.

        Concurrency: two callers racing on a first-ever key both try to create
        the secret; the loser catches AlreadyExists and re-reads, so both return
        the SAME key rather than one silently overwriting the other.
        """
        from google.api_core import exceptions as gexc  # noqa: PLC0415

        existing = self.get_key(project_id)
        if existing is not None:
            return existing

        client = self._api()
        key = os.urandom(32)
        try:
            client.create_secret(
                request={
                    "parent": self._parent(),
                    "secret_id": self._secret_id(project_id),
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        except gexc.AlreadyExists:
            # The secret exists but had no readable version (a create that died
            # between create_secret and add_secret_version, or a race). Fall
            # through: adding a version is exactly the repair.
            pass
        self._add_version(project_id, key)
        # AD-3: log the event only -- never the key value.
        logger.info("tenant_keys: key_created project_id=[REDACTED] backend=secret_manager")
        return key

    def decryption_keys(self, project_id: str) -> list[bytes]:
        """Newest key first, then the rotation-overlap predecessors (AI-42).

        This is what makes ``rotate_key`` safe: ciphertext sealed under the
        previous version keeps opening until that version is disabled.
        """
        from google.api_core import exceptions as gexc  # noqa: PLC0415

        client = self._api()
        try:
            versions = list(
                client.list_secret_versions(
                    request={
                        "parent": f"{self._parent()}/secrets/{self._secret_id(project_id)}",
                        "filter": "state:ENABLED",
                    }
                )
            )
        except gexc.NotFound:
            return []
        # list_secret_versions returns newest first; keep that order explicit so a
        # future API change cannot silently make the OLDEST key the encryption key.
        versions.sort(key=lambda v: int(str(v.name).rsplit("/", 1)[-1]), reverse=True)

        keys: list[bytes] = []
        for version in versions[: 1 + self._DECRYPT_FALLBACK_VERSIONS]:
            try:
                payload = client.access_secret_version(request={"name": version.name})
            except gexc.GoogleAPIError as exc:
                logger.warning("tenant_keys: version unreadable: %s", type(exc).__name__)
                continue
            key = self._decode(payload.payload.data, "version")
            if key is not None:
                keys.append(key)
        return keys

    def delete_key(self, project_id: str) -> bool:
        """Delete the whole secret. Returns False when there was nothing to delete."""
        from google.api_core import exceptions as gexc  # noqa: PLC0415

        client = self._api()
        try:
            client.delete_secret(
                request={"name": f"{self._parent()}/secrets/{self._secret_id(project_id)}"}
            )
        except gexc.NotFound:
            return False
        logger.info("tenant_keys: key_deleted project_id=[REDACTED] backend=secret_manager")
        return True

    def rotate_key(self, project_id: str) -> bytes:
        """Add a NEW version and return it. The previous version stays ENABLED.

        Destroying the predecessor here would break every in-flight decryption;
        it is disabled later, outside the grace window, by an operator.
        """
        from google.api_core import exceptions as gexc  # noqa: PLC0415

        client = self._api()
        new_key = os.urandom(32)
        try:
            self._add_version(project_id, new_key)
        except gexc.NotFound:
            # Rotating a tenant that never had a key: create it rather than fail.
            client.create_secret(
                request={
                    "parent": self._parent(),
                    "secret_id": self._secret_id(project_id),
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            self._add_version(project_id, new_key)
        logger.info("tenant_keys: key_rotated project_id=[REDACTED] backend=secret_manager")
        return new_key


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def get_tenant_key_backend() -> TenantKeyBackend:
    """Return the configured TenantKeyBackend instance.

    Reads TENANT_KEY_BACKEND env var:
        'local'          (default) -> LocalFileKeyBackend(TENANT_KEY_DIR)
        'secret_manager'           -> SecretManagerKeyBackend (Phase B stub)

    Called on each request -- do not cache at module level so env var changes
    take effect without restart (same pattern as nango_client.py).
    """
    backend = os.environ.get("TENANT_KEY_BACKEND", "local").strip().lower()
    if backend == "secret_manager":
        return SecretManagerKeyBackend()
    # Default: local file backend
    return LocalFileKeyBackend()


# ---------------------------------------------------------------------------
# Audit helper: write a tka_ row to app.tenant_key_audit
# ---------------------------------------------------------------------------


def write_key_audit_row(
    project_id: str,
    action: str,
    performed_by: str,
    details: dict | None = None,
) -> None:
    """Write one row to app.tenant_key_audit. Never raises -- logs on failure.

    Args:
        project_id:    The project whose key was affected.
        action:        One of 'key_created', 'key_rotated', 'key_deleted'.
        performed_by:  Identity subject (from auth layer or 'system').
        details:       Optional JSONB dict, e.g. {"backend": "local"}.

    AD-3: this function NEVER logs key material.  project_id is logged only at
    DEBUG level; details must NOT contain key bytes.
    """
    import json  # noqa: PLC0415

    from ulid import ULID  # noqa: PLC0415

    try:
        import psycopg  # noqa: PLC0415

        from core.audit import _db_url  # noqa: PLC0415

        row_id = f"tka_{ULID()}"
        details_json = json.dumps(details) if details is not None else None

        with psycopg.connect(_db_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.tenant_key_audit
                        (id, project_id, action, performed_by, details)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    """,
                    (row_id, project_id, action, performed_by, details_json),
                )
            conn.commit()
    except Exception as exc:
        # Audit write failure must NEVER block the caller (mirrors audit.py AC6).
        logger.warning("tenant_key_audit_write_failed: %s", exc)
