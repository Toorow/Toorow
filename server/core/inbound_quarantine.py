"""Immutable quarantine storage seam for inbound delivery bytes.

The public receipt process may create objects but never reads them. Callers
supply opaque non-enumerating partitions and collision-proof reserved-safe
object names. Local writes use exclusive creation with byte comparison on
replay; GCS writes use a create-only generation precondition. The SDK import is
lazy so offline tests do not require cloud dependencies.
"""

from __future__ import annotations

import hmac
import json
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
_UNSAFE_FILENAME_RE = re.compile(r"[/\\:\x00]")


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class QuarantineError(Exception):
    """Base error raised by any QuarantineStore operation."""


class QuarantineSizeError(QuarantineError):
    """Raised when the data payload exceeds the configured size limit."""

    def __init__(
        self,
        message: str,
        *,
        observed_size: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.observed_size = observed_size
        self.max_bytes = max_bytes


class QuarantineConflictError(QuarantineError):
    """An immutable object key already exists with different bytes."""


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuarantineObject:
    """Opaque reference returned by a successful ``put``.

    ``uri`` is backend-specific (``gs://`` or ``file://``) and is the only
    pointer a worker needs to call ``get`` later.
    """

    uri: str  # e.g. "gs://bucket/inbound/<p>/<mid>/<name>" or "file:///..."
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
        metadata: dict[str, str] | None = None,
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

    def get(
        self,
        uri: str,
        *,
        expected_org_id: str | None = None,
        expected_datastream_id: str | None = None,
        expected_content_hash: str | None = None,
    ) -> bytes:
        """Retrieve bytes, optionally enforcing their full content scope.

        Raises ``QuarantineError`` when the object does not exist or cannot
        be read.
        """
        ...

    def get_bounded(
        self,
        uri: str,
        *,
        max_bytes: int,
        expected_org_id: str | None = None,
        expected_datastream_id: str | None = None,
        expected_content_hash: str | None = None,
    ) -> bytes:
        """Read at most max_bytes + 1 and refuse an oversized object."""
        ...

    def set_legal_hold(
        self,
        uri: str,
        *,
        enabled: bool,
        expected_org_id: str,
        expected_datastream_id: str,
        expected_content_hash: str,
    ) -> None:
        """Synchronize the backend's deletion hold for one scoped object."""
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
    name = re.sub(r"\s+", "_", name).strip(".")
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


def content_scope_partition(*, org_id: str, datastream_id: str) -> str:
    """Return the validated organization/Datastream object-key partition."""
    for label, value in (("org_id", org_id), ("datastream_id", datastream_id)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Za-z_.-]{1,128}", value):
            raise QuarantineError(f"{label} is not a safe object-key component")
    return f"{org_id}/{datastream_id}"


def _expected_key_prefix(*, org_id: str, datastream_id: str, content_hash: str) -> str:
    partition = content_scope_partition(org_id=org_id, datastream_id=datastream_id)
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash or ""):
        raise QuarantineError("expected_content_hash must be a lowercase SHA-256")
    return f"inbound/{partition}/{content_hash}/"


def _safe_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    """Validate bounded non-secret object metadata before storage."""
    if metadata is None:
        return {}
    if not isinstance(metadata, dict) or len(metadata) > 16:
        raise QuarantineError("object metadata must contain at most 16 entries")
    safe: dict[str, str] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", key):
            raise QuarantineError("object metadata key is invalid")
        if not isinstance(value, str) or len(value.encode("utf-8")) > 1024:
            raise QuarantineError("object metadata value is invalid")
        safe[key] = value
    return safe


def _fs_path(p: pathlib.Path) -> pathlib.Path:
    """Prepare pathlib.Path for Windows filesystem operations (handling MAX_PATH > 260 chars)."""
    if os.name == "nt":
        s = str(p.resolve())
        if not s.startswith("\\\\?\\"):
            return pathlib.Path("\\\\?\\" + s)
    return p


# ---------------------------------------------------------------------------
# Local filesystem implementation (dev / tests)
# ---------------------------------------------------------------------------


class LocalFsQuarantineStore:
    """Local-filesystem implementation for dev / offline / test suites.

    Objects are written under *root*; directories are created as needed.
    Intended for development and offline tests; not suitable for a
    multi-process production deployment.

    Parameters
    ----------
    root:
        Absolute path to the base directory. All objects live under
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
        metadata: dict[str, str] | None = None,
    ) -> QuarantineObject:
        if len(data) > self._max_size:
            raise QuarantineSizeError(
                f"payload {len(data)} bytes exceeds max_size {self._max_size}"
            )
        safe_metadata = _safe_metadata(metadata)
        key = _object_key(partition, message_id, filename)
        dest = (self._root / key).resolve()

        # Ensure the resolved path is under root (defence in depth; the key
        # builder already sanitises the components but belt+suspenders).
        try:
            dest.relative_to(self._root)
        except ValueError as exc:
            raise QuarantineError(f"resolved path escapes quarantine root: {dest}") from exc

        fs_dest = _fs_path(dest)
        fs_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with fs_dest.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            try:
                existing = fs_dest.read_bytes()
            except OSError as exc:
                raise QuarantineError(
                    f"quarantine replay could not inspect immutable object: {dest}"
                ) from exc
            if not hmac.compare_digest(existing, data):
                raise QuarantineConflictError(
                    "immutable quarantine key is already bound to different bytes"
                )
        if safe_metadata:
            metadata_dest = dest.with_name(f"{dest.name}.metadata.json")
            fs_metadata_dest = _fs_path(metadata_dest)
            encoded = json.dumps(safe_metadata, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            try:
                with fs_metadata_dest.open("xb") as handle:
                    handle.write(encoded)
            except FileExistsError:
                try:
                    existing_metadata = fs_metadata_dest.read_bytes()
                except OSError as exc:
                    raise QuarantineError(
                        "quarantine replay could not inspect immutable metadata"
                    ) from exc
                if not hmac.compare_digest(existing_metadata, encoded):
                    raise QuarantineConflictError("immutable quarantine metadata differs on replay")
        return QuarantineObject(uri=dest.as_uri(), size=len(data))

    def get(
        self,
        uri: str,
        *,
        expected_org_id: str | None = None,
        expected_datastream_id: str | None = None,
        expected_content_hash: str | None = None,
    ) -> bytes:
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
            relative = path.relative_to(self._root).as_posix()
        except ValueError as exc:
            raise QuarantineError("quarantine uri escapes configured root") from exc
        expected_values = (expected_org_id, expected_datastream_id, expected_content_hash)
        if any(value is not None for value in expected_values):
            if not all(isinstance(value, str) and value for value in expected_values):
                raise QuarantineError("complete expected object scope is required")
            prefix = _expected_key_prefix(
                org_id=expected_org_id,
                datastream_id=expected_datastream_id,
                content_hash=expected_content_hash,
            )
            if not relative.startswith(prefix):
                raise QuarantineError("quarantine uri is outside expected object scope")
        try:
            return _fs_path(path).read_bytes()
        except FileNotFoundError as exc:
            raise QuarantineError(f"quarantine object not found: {uri!r}") from exc
        except OSError as exc:
            raise QuarantineError(f"quarantine read error: {exc}") from exc

    def get_bounded(
        self,
        uri: str,
        *,
        max_bytes: int,
        expected_org_id: str | None = None,
        expected_datastream_id: str | None = None,
        expected_content_hash: str | None = None,
    ) -> bytes:
        from urllib.parse import unquote, urlparse  # noqa: PLC0415
        from urllib.request import url2pathname  # noqa: PLC0415

        if not uri.startswith("file://"):
            raise QuarantineError("quarantine uri scheme is not supported")
        path = pathlib.Path(url2pathname(unquote(urlparse(uri).path))).resolve()
        try:
            relative = path.relative_to(self._root).as_posix()
        except ValueError as exc:
            raise QuarantineError("quarantine uri escapes configured root") from exc
        expected_values = (expected_org_id, expected_datastream_id, expected_content_hash)
        if any(value is not None for value in expected_values):
            if not all(isinstance(value, str) and value for value in expected_values):
                raise QuarantineError("complete expected object scope is required")
            prefix = _expected_key_prefix(
                org_id=expected_org_id,
                datastream_id=expected_datastream_id,
                content_hash=expected_content_hash,
            )
            if not relative.startswith(prefix):
                raise QuarantineError("quarantine uri is outside expected object scope")
        try:
            fs_p = _fs_path(path)
            observed_size = fs_p.stat().st_size
            if observed_size > max_bytes:
                raise QuarantineSizeError(
                    "quarantine object exceeds read limit",
                    observed_size=observed_size,
                    max_bytes=max_bytes,
                )
            with fs_p.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except QuarantineSizeError:
            raise
        except FileNotFoundError as exc:
            raise QuarantineError("quarantine object not found") from exc
        except OSError as exc:
            raise QuarantineError("quarantine read failed") from exc
        if len(data) > max_bytes:
            raise QuarantineSizeError(
                "quarantine object exceeds read limit",
                observed_size=len(data),
                max_bytes=max_bytes,
            )
        return data

    def set_legal_hold(
        self,
        uri: str,
        *,
        enabled: bool,
        expected_org_id: str,
        expected_datastream_id: str,
        expected_content_hash: str,
    ) -> None:
        # The scoped read proves the URI is rooted in the expected object.
        self.get(
            uri,
            expected_org_id=expected_org_id,
            expected_datastream_id=expected_datastream_id,
            expected_content_hash=expected_content_hash,
        )
        from urllib.parse import unquote, urlparse  # noqa: PLC0415
        from urllib.request import url2pathname  # noqa: PLC0415

        parsed = urlparse(uri)
        path = pathlib.Path(url2pathname(unquote(parsed.path))).resolve()
        hold_path = path.with_name(f"{path.name}.legal-hold.json")
        encoded = json.dumps(
            {"event_based_hold": bool(enabled)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = hold_path.with_name(f"{hold_path.name}.tmp")
        try:
            temporary.write_bytes(encoded)
            temporary.replace(hold_path)
        except OSError as exc:
            raise QuarantineError("local legal-hold synchronization failed") from exc


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
        metadata: dict[str, str] | None = None,
    ) -> QuarantineObject:
        if len(data) > self._max_size:
            raise QuarantineSizeError(
                f"payload {len(data)} bytes exceeds max_size {self._max_size}"
            )
        try:
            from google.api_core.exceptions import PreconditionFailed  # noqa: PLC0415
            from google.cloud import storage as gcs  # noqa: PLC0415
        except ImportError as exc:
            raise QuarantineError(
                "google-cloud-storage is not installed; "
                "set INBOUND_QUARANTINE_LOCAL_ROOT to use the local backend"
            ) from exc

        safe_metadata = _safe_metadata(metadata)
        key = _object_key(partition, message_id, filename)
        client = gcs.Client()
        bucket = client.bucket(self._bucket)
        blob = bucket.blob(key)
        if safe_metadata:
            blob.metadata = safe_metadata
            if "legal_hold" in safe_metadata:
                blob.event_based_hold = safe_metadata["legal_hold"] == "true"
        extra: dict = {}
        if content_type:
            extra["content_type"] = content_type
        # A generation precondition makes create immutable and repeatable. A
        # replay sees PreconditionFailed and returns the same deterministic URI;
        # no read permission is needed by the objectCreator-only receipt service.
        try:
            blob.upload_from_string(data, if_generation_match=0, **extra)
        except PreconditionFailed:
            pass
        uri = f"gs://{self._bucket}/{key}"
        return QuarantineObject(uri=uri, size=len(data))

    def get(
        self,
        uri: str,
        *,
        expected_org_id: str | None = None,
        expected_datastream_id: str | None = None,
        expected_content_hash: str | None = None,
    ) -> bytes:
        prefix = f"gs://{self._bucket}/"
        if not uri.startswith(prefix):
            raise QuarantineError(f"uri does not belong to bucket {self._bucket!r}: {uri!r}")
        try:
            from google.cloud import storage as gcs  # noqa: PLC0415
        except ImportError as exc:
            raise QuarantineError("google-cloud-storage is not installed") from exc

        key = uri[len(prefix) :]
        expected_values = (expected_org_id, expected_datastream_id, expected_content_hash)
        if any(value is not None for value in expected_values):
            if not all(isinstance(value, str) and value for value in expected_values):
                raise QuarantineError("complete expected object scope is required")
            expected_prefix = _expected_key_prefix(
                org_id=expected_org_id,
                datastream_id=expected_datastream_id,
                content_hash=expected_content_hash,
            )
            if not key.startswith(expected_prefix):
                raise QuarantineError("quarantine uri is outside expected object scope")
        client = gcs.Client()
        bucket = client.bucket(self._bucket)
        blob = bucket.blob(key)
        try:
            return blob.download_as_bytes()
        except Exception as exc:  # noqa: BLE001
            raise QuarantineError(f"GCS read error for {uri!r}: {exc}") from exc

    def get_bounded(
        self,
        uri: str,
        *,
        max_bytes: int,
        expected_org_id: str | None = None,
        expected_datastream_id: str | None = None,
        expected_content_hash: str | None = None,
    ) -> bytes:
        prefix = f"gs://{self._bucket}/"
        if not uri.startswith(prefix):
            raise QuarantineError("quarantine uri belongs to another bucket")
        key = uri[len(prefix) :]
        expected_values = (expected_org_id, expected_datastream_id, expected_content_hash)
        if any(value is not None for value in expected_values):
            if not all(isinstance(value, str) and value for value in expected_values):
                raise QuarantineError("complete expected object scope is required")
            expected_prefix = _expected_key_prefix(
                org_id=expected_org_id,
                datastream_id=expected_datastream_id,
                content_hash=expected_content_hash,
            )
            if not key.startswith(expected_prefix):
                raise QuarantineError("quarantine uri is outside expected object scope")
        try:
            from google.cloud import storage as gcs  # noqa: PLC0415

            blob = gcs.Client().bucket(self._bucket).blob(key)
            blob.reload()
            if blob.size is None or int(blob.size) > max_bytes:
                raise QuarantineSizeError(
                    "quarantine object exceeds read limit",
                    observed_size=int(blob.size) if blob.size is not None else None,
                    max_bytes=max_bytes,
                )
            data = blob.download_as_bytes(start=0, end=max_bytes)
        except QuarantineSizeError:
            raise
        except ImportError as exc:
            raise QuarantineError("google-cloud-storage is not installed") from exc
        except Exception as exc:
            raise QuarantineError("GCS bounded read failed") from exc
        if len(data) > max_bytes:
            raise QuarantineSizeError(
                "quarantine object exceeds read limit",
                observed_size=len(data),
                max_bytes=max_bytes,
            )
        return data

    def set_legal_hold(
        self,
        uri: str,
        *,
        enabled: bool,
        expected_org_id: str,
        expected_datastream_id: str,
        expected_content_hash: str,
    ) -> None:
        prefix = f"gs://{self._bucket}/"
        if not uri.startswith(prefix):
            raise QuarantineError("legal-hold URI belongs to another bucket")
        key = uri[len(prefix) :]
        expected_prefix = _expected_key_prefix(
            org_id=expected_org_id,
            datastream_id=expected_datastream_id,
            content_hash=expected_content_hash,
        )
        if not key.startswith(expected_prefix):
            raise QuarantineError("legal-hold URI is outside expected object scope")
        try:
            from google.cloud import storage as gcs  # noqa: PLC0415
        except ImportError as exc:
            raise QuarantineError("google-cloud-storage is not installed") from exc
        blob = gcs.Client().bucket(self._bucket).blob(key)
        try:
            blob.reload()
            generation = blob.generation
            blob.event_based_hold = bool(enabled)
            blob.patch(if_generation_match=generation)
        except Exception as exc:  # noqa: BLE001
            raise QuarantineError("GCS legal-hold synchronization failed") from exc


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
