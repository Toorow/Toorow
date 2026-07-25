"""toorow -- quarantine storage seam for inbound delivery bytes (Epic 38, Story 38.6).

Raw attachment bytes from inbound deliveries are written here immediately after
receipt verification and read back by a downstream worker for parsing / DQ.

Design rules:
  - AD-2: source-agnostic -- no provider vocabulary anywhere.
  - Protocol + concrete impls + factory (mirrors warehouse_write.py pattern).
  - The internet-facing receipt process holds objectCreator only; it may call
    ``put`` but NEVER ``get`` (enforced by GCS IAM, not by this module).
  - ``partition`` is an OPAQUE non-enumerating key supplied by the caller
    (it will be a token HASH, not a raw address/token) -- not interpreted here.
  - Object key layout: ``inbound/<partition>/<message_id>/<safe_filename>``
    where safe_filename is sanitised before use (strip path separators, cap
    length) so an attacker-controlled filename cannot escape the key prefix.
  - GCS impl lazy-imports google.cloud.storage so the module is importable
    offline and in tests without the SDK installed.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default maximum object size (bytes). Matches INBOUND_MAX_BODY_BYTES default.
DEFAULT_MAX_SIZE = 25 * 1024 * 1024  # 25 MiB

#: Maximum length for the sanitised filename component of the object key.
_MAX_FILENAME_LEN = 255

#: Characters that are unsafe in a GCS object-key component or local filename.
#: We keep only printable ASCII minus the path-separator set and NUL.
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:\x00]')


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class QuarantineError(Exception):
    """Base error raised by any QuarantineStore operation."""


class QuarantineSizeError(QuarantineError):
    """Raised when the data payload exceeds the configured size limit."""


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QuarantineObject:
    """Opaque reference returned by a successful ``put``.

    ``uri`` is backend-specific (``gs://`` or ``file://``) and is the only
    pointer a worker needs to call ``get`` later.
    """
    uri: str   # e.g. "gs://bucket/inbound/<p>/<mid>/<name>" or "file:///..."
    size: int  # bytes actually stored


# ---------------------------------------------------------------------------
# Protocol (the seam)
# ---------------------------------------------------------------------------

class QuarantineStore(Protocol):
    """Write-once / read-back storage for raw inbound bytes.

    Implementations must be safe to call from multiple threads (GCS client is
    thread-safe; LocalFs uses atomic rename-on-write via Python builtins).
    """

    def put(
        self,
        *,
        partition: str,
        message_id: str,
        filename: str,
        data: bytes,
        content_type: str | None,
    ) -> QuarantineObject:
        """Persist *data* and return an opaque ``QuarantineObject``.

        Parameters
        ----------
        partition:
            Opaque, non-enumerating caller-supplied scope key (e.g. a HASH of
            the inbound routing token). Never interpreted by this module.
        message_id:
            Unique delivery identifier chosen by the caller.
        filename:
            Suggested filename. Will be sanitised before use; the caller must
            NOT assume the stored name matches this value.
        data:
            Raw bytes to store. Raises ``QuarantineSizeError`` when
            ``len(data) > max_size``.
        content_type:
            Optional MIME type hint stored as object metadata.
        """
        ...

    def get(self, uri: str) -> bytes:
        """Retrieve previously stored bytes by their opaque *uri*.

        Raises ``QuarantineError`` when the object does not exist or cannot
        be read.
        """
        ...


# ---------------------------------------------------------------------------
# Key helpers (shared)
# ---------------------------------------------------------------------------

def _safe_filename(raw: str) -> str:
    """Sanitise an untrusted filename so it is safe for use as a key suffix.

    - Strips path separators (``/``, ``\\``, ``:``), NUL bytes, and leading dots.
    - Collapses runs of whitespace to a single underscore.
    - Caps total length at ``_MAX_FILENAME_LEN`` characters.
    - Falls back to ``"attachment"`` when nothing safe remains.

    A LEADING underscore is preserved (only leading/trailing dots are stripped),
    so a caller-controlled reserved name such as ``_manifest.json`` survives
    sanitisation intact -- it is how the delivery manifest stays distinguishable
    from an untrusted attachment named ``manifest.json``.
    """
    name = _UNSAFE_FILENAME_RE.sub("_", raw)
    name = re.sub(r'\s+', '_', name).strip('.')
    name = name[:_MAX_FILENAME_LEN]
    # A name that reduces to only underscores (i.e. was entirely unsafe chars)
    # carries no meaning -> fall back to the neutral default.
    if not name or set(name) <= {"_"}:
        return "attachment"
    return name


def _object_key(partition: str, message_id: str, filename: str) -> str:
    """Build the canonical key: ``inbound/<partition>/<message_id>/<safe_filename>``."""
    safe = _safe_filename(filename)
    return f"inbound/{partition}/{message_id}/{safe}"


# ---------------------------------------------------------------------------
# Local filesystem implementation (dev / tests)
# ---------------------------------------------------------------------------

class LocalFsQuarantineStore:
    """QuarantineStore backed by the local filesystem.

    Objects are written under *root*; directories are created as needed.
    Intended for development and offline tests; not suitable for a
    multi-process production deployment.

    Parameters
    ----------
    root:
        Absolute path to the base directory.  All objects live under
        ``<root>/inbound/<partition>/<message_id>/``.
    max_size:
        Upper bound on ``len(data)`` for a single ``put`` call.
    """

    def __init__(self, root: str, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self._root = pathlib.Path(root).resolve()
        self._max_size = max_size

    def put(
        self,
        *,
        partition: str,
        message_id: str,
        filename: str,
        data: bytes,
        content_type: str | None = None,
    ) -> QuarantineObject:
        if len(data) > self._max_size:
            raise QuarantineSizeError(
                f"payload {len(data)} bytes exceeds max_size {self._max_size}"
            )
        key = _object_key(partition, message_id, filename)
        dest = (self._root / key).resolve()

        # Ensure the resolved path is under root (defence in depth; the key
        # builder already sanitises the components but belt+suspenders).
        try:
            dest.relative_to(self._root)
        except ValueError as exc:
            raise QuarantineError(
                f"resolved path escapes quarantine root: {dest}"
            ) from exc

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return QuarantineObject(uri=dest.as_uri(), size=len(data))

    def get(self, uri: str) -> bytes:
        if not uri.startswith("file://"):
            raise QuarantineError(f"uri scheme not supported by LocalFsQuarantineStore: {uri!r}")
        # Proper inverse of pathlib.Path.as_uri() (handles Windows drive paths:
        # file:///C:/... must decode to C:\... not /C:/...). urlparse + url2pathname
        # is the cross-platform round-trip.
        from urllib.parse import unquote, urlparse  # noqa: PLC0415
        from urllib.request import url2pathname  # noqa: PLC0415

        parsed = urlparse(uri)
        path = pathlib.Path(url2pathname(unquote(parsed.path))).resolve()
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise QuarantineError(f"quarantine object not found: {uri!r}") from exc
        except OSError as exc:
            raise QuarantineError(f"quarantine read error: {exc}") from exc


# ---------------------------------------------------------------------------
# Google Cloud Storage implementation (prod)
# ---------------------------------------------------------------------------

class GcsQuarantineStore:
    """QuarantineStore backed by a GCS bucket.

    The SA running the receipt service holds ``roles/storage.objectCreator``
    only (append-only posture enforced by Terraform; see inbound_runtime.tf).
    Consequently:
      - ``put`` uses ``upload_from_string`` (object-create, no read-back).
      - ``get`` requires broader read permissions and is intended for the
        downstream worker, which runs under a different identity.

    ``google.cloud.storage`` is imported LAZILY inside each method so the
    module can be imported without the SDK installed (offline / test envs).

    Parameters
    ----------
    bucket:
        GCS bucket name (without ``gs://`` prefix).
    max_size:
        Upper bound on ``len(data)`` for a single ``put`` call.
    """

    def __init__(self, bucket: str, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self._bucket = bucket
        self._max_size = max_size

    def put(
        self,
        *,
        partition: str,
        message_id: str,
        filename: str,
        data: bytes,
        content_type: str | None = None,
    ) -> QuarantineObject:
        if len(data) > self._max_size:
            raise QuarantineSizeError(
                f"payload {len(data)} bytes exceeds max_size {self._max_size}"
            )
        try:
            from google.cloud import storage as gcs  # noqa: PLC0415
        except ImportError as exc:
            raise QuarantineError(
                "google-cloud-storage is not installed; "
                "set INBOUND_QUARANTINE_LOCAL_ROOT to use the local backend"
            ) from exc

        key = _object_key(partition, message_id, filename)
        client = gcs.Client()
        bucket = client.bucket(self._bucket)
        blob = bucket.blob(key)
        extra: dict = {}
        if content_type:
            extra["content_type"] = content_type
        # upload_from_string is a create-only operation: it does not read the
        # existing object first, so objectCreator is sufficient.
        blob.upload_from_string(data, **extra)
        uri = f"gs://{self._bucket}/{key}"
        return QuarantineObject(uri=uri, size=len(data))

    def get(self, uri: str) -> bytes:
        prefix = f"gs://{self._bucket}/"
        if not uri.startswith(prefix):
            raise QuarantineError(
                f"uri does not belong to bucket {self._bucket!r}: {uri!r}"
            )
        try:
            from google.cloud import storage as gcs  # noqa: PLC0415
        except ImportError as exc:
            raise QuarantineError(
                "google-cloud-storage is not installed"
            ) from exc

        key = uri[len(prefix):]
        client = gcs.Client()
        bucket = client.bucket(self._bucket)
        blob = bucket.blob(key)
        try:
            return blob.download_as_bytes()
        except Exception as exc:  # noqa: BLE001
            raise QuarantineError(f"GCS read error for {uri!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def open_quarantine_store(max_size: int = DEFAULT_MAX_SIZE) -> QuarantineStore:
    """Return the appropriate QuarantineStore selected by environment.

    Selection order:
    1. ``INBOUND_QUARANTINE_BUCKET`` set -> ``GcsQuarantineStore``
       (prod Cloud Run; the SA holds objectCreator on that bucket).
    2. ``INBOUND_QUARANTINE_LOCAL_ROOT`` set -> ``LocalFsQuarantineStore``
       (dev machine or self-hosted without GCS).
    3. Neither set -> ``LocalFsQuarantineStore`` under a stable dev default
       inside the system temp directory (``<tmp>/toorow-quarantine``).
       A WARNING is emitted once so operators notice this fallback in logs.

    The ``max_size`` parameter flows through to the chosen store.
    """
    bucket = os.environ.get("INBOUND_QUARANTINE_BUCKET", "").strip()
    if bucket:
        logger.info("quarantine: backend=gcs bucket=%s", bucket)
        return GcsQuarantineStore(bucket=bucket, max_size=max_size)

    local_root = os.environ.get("INBOUND_QUARANTINE_LOCAL_ROOT", "").strip()
    if local_root:
        logger.info("quarantine: backend=local root=%s", local_root)
        return LocalFsQuarantineStore(root=local_root, max_size=max_size)

    default_root = str(pathlib.Path(tempfile.gettempdir()) / "toorow-quarantine")
    logger.warning(
        "quarantine: neither INBOUND_QUARANTINE_BUCKET nor INBOUND_QUARANTINE_LOCAL_ROOT "
        "is set -- using dev default root=%s (not suitable for production)",
        default_root,
    )
    return LocalFsQuarantineStore(root=default_root, max_size=max_size)
