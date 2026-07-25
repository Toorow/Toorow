"""toorow -- Airbyte REST API client (Story 3.1, AC7).

Minimal Airbyte API surface for triggering syncs and polling job status.

Environment variables
---------------------
AIRBYTE_BASE_URL         default "http://localhost:8000" (abctl/docker-compose local).
                         Phase B: set to the GKE internal service URL
                         (e.g. "http://airbyte-server.airbyte.svc.cluster.local:8001")
                         -- no code changes required, env-var only config switch.
AIRBYTE_API_KEY          optional; bearer token. Airbyte OSS local does not require
                         auth. Airbyte Cloud/Enterprise does.
AIRBYTE_TIMEOUT_SECONDS  default 30. Seconds before httpx raises TimeoutException.

Skip-if-unreachable pattern for integration tests
-------------------------------------------------
    import os, httpx
    def _airbyte_reachable(url: str) -> bool:
        try:
            httpx.get(url + "/api/v1/health", timeout=3).raise_for_status()
            return True
        except Exception:
            return False

    AIRBYTE_BASE_URL = os.environ.get("AIRBYTE_BASE_URL", "http://localhost:8000")
    airbyte_available = pytest.mark.skipif(
        not _airbyte_reachable(AIRBYTE_BASE_URL),
        reason="Local Airbyte not running -- set AIRBYTE_BASE_URL or run abctl",
    )

Phase B note (TODO)
-------------------
When Airbyte runs on GKE behind Cloud Tasks, the core-minted pull_id should be
threaded through Airbyte job metadata so raw staging rows carry a canonical pull_
ULID rather than the airbyte_<job_id> surrogate.
# TODO(Phase-B): thread pull_id through Airbyte job metadata (AD-7).

AD-3 compliance
---------------
This module never stores, logs, or caches OAuth tokens. Airbyte obtains
credentials from Nango at configuration time (one-time admin step documented in
infra/airbyte/SETUP.md). The token is stored inside Airbyte's own config store --
never in our Postgres or module code. Airbyte is the extraction-only exception
layer acknowledged in AD-3.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable helpers (read at call time -- same pattern as nango_client)
# ---------------------------------------------------------------------------


def _airbyte_base_url() -> str:
    """Read AIRBYTE_BASE_URL (default 'http://localhost:8000')."""
    return os.environ.get("AIRBYTE_BASE_URL", "http://localhost:8000").rstrip("/")


def _airbyte_timeout() -> float:
    """Read AIRBYTE_TIMEOUT_SECONDS (default 30)."""
    try:
        return float(os.environ.get("AIRBYTE_TIMEOUT_SECONDS", "30"))
    except (ValueError, TypeError):
        return 30.0


def _auth_headers() -> dict[str, str]:
    """Return Authorization header if AIRBYTE_API_KEY is set, else empty dict.

    Airbyte OSS local does not require auth. Airbyte Cloud/Enterprise does.
    """
    api_key = os.environ.get("AIRBYTE_API_KEY", "").strip()
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AirbyteSyncError(RuntimeError):
    """Raised when the Airbyte API returns a non-200 response or is unreachable.

    Callers should catch this to distinguish Airbyte failures from other errors.
    The message includes the HTTP status and response body when available.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def trigger_sync(connection_id: str, *, pull_id: str | None = None) -> dict:
    """Trigger an Airbyte sync for the given connection.

    POST /api/v1/connections/sync

    Args:
        connection_id: Airbyte connection UUID to sync.
        pull_id: Core-minted ULID (AD-7) surfaced in the job payload under
            "connectorMetadata.pull_id" when provided.  Airbyte ignores unknown
            metadata keys; the field is preserved in the returned job dict so
            callers can correlate Airbyte jobs with toorow pull rows.
            No-op when None (backward-compatible default).

    Returns:
        dict with at minimum {"job_id": str, "status": "pending",
        "pull_id": str | None}.
        job_id is the Airbyte job ID (integer or string depending on version).
        pull_id echoes the caller-supplied value (None if not provided).

    Raises:
        AirbyteSyncError: on non-200 response or connection error.

    AD-2: no provider-specific strings -- pull_id is sourced from the caller.
    """
    base_url = _airbyte_base_url()
    url = f"{base_url}/api/v1/connections/sync"
    # Include pull_id in connectorMetadata when provided so Airbyte job rows
    # carry the canonical toorow ULID (AD-7).  Airbyte ignores
    # unknown top-level fields; connectorMetadata is the conventional slot.
    payload: dict = {"connectionId": connection_id}
    if pull_id is not None:
        payload["connectorMetadata"] = {"pull_id": pull_id}
    headers = {"Content-Type": "application/json", **_auth_headers()}
    timeout = _airbyte_timeout()

    logger.info(
        "airbyte_client: trigger_sync connection_id=%s pull_id=%s url=%s",
        connection_id,
        pull_id,
        url,
    )

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise AirbyteSyncError(
            f"Airbyte trigger_sync timed out after {timeout}s: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise AirbyteSyncError(
            f"Airbyte trigger_sync request error: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise AirbyteSyncError(
            f"Airbyte trigger_sync failed: HTTP {resp.status_code} -- {resp.text[:500]}"
        )

    body = resp.json()
    # Airbyte API v1: { "job": { "id": ..., "status": "pending", ... } }
    job_block = body.get("job", body)
    job_id = str(job_block.get("id", ""))
    status = job_block.get("status", "pending")

    logger.info(
        "airbyte_client: trigger_sync dispatched job_id=%s pull_id=%s status=%s",
        job_id,
        pull_id,
        status,
    )

    return {"job_id": job_id, "status": status, "pull_id": pull_id}


def get_sync_status(job_id: str) -> dict:
    """Poll the status of an Airbyte sync job.

    GET /api/v1/jobs/{job_id}

    Args:
        job_id: Airbyte job ID (as string; cast to appropriate type if needed).

    Returns:
        dict with at minimum {"job_id": str, "status": str, "created_at": str,
        "ended_at": str | None}.

    Raises:
        AirbyteSyncError: on non-200 response or connection error.
    """
    base_url = _airbyte_base_url()
    url = f"{base_url}/api/v1/jobs/{job_id}"
    headers = _auth_headers()
    timeout = _airbyte_timeout()

    logger.info("airbyte_client: get_sync_status job_id=%s url=%s", job_id, url)

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise AirbyteSyncError(
            f"Airbyte get_sync_status timed out after {timeout}s: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise AirbyteSyncError(
            f"Airbyte get_sync_status request error: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise AirbyteSyncError(
            f"Airbyte get_sync_status failed: HTTP {resp.status_code} -- {resp.text[:500]}"
        )

    body = resp.json()
    # Airbyte API v1: { "job": { "id": ..., "status": ..., "createdAt": ..., "endedAt": ... } }
    job_block = body.get("job", body)

    return {
        "job_id": str(job_block.get("id", job_id)),
        "status": job_block.get("status", ""),
        "created_at": job_block.get("createdAt") or job_block.get("created_at") or "",
        "ended_at": job_block.get("endedAt") or job_block.get("ended_at") or None,
    }
