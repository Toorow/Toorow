"""Draft-scoped quarantine lifecycle for pre-Datastream file assets."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from core.csv_excel_import import detect_format
from core.data_identities import mint_data_id
from core.inbound_quarantine import DEFAULT_MAX_SIZE, open_quarantine_store


class SetupAssetValidationError(ValueError):
    code = "invalid_setup_asset"


class SetupAssetNotFound(LookupError):
    code = "not_found"


def _payload(row: tuple[Any, ...], *, replay: bool = False) -> dict[str, Any]:
    return {
        "asset_ref": row[0],
        "draft_ref": row[1],
        "content_hash": row[2],
        "detected_format": row[3],
        "byte_count": int(row[4]),
        "state": row[5],
        "expires_at": row[6].isoformat(),
        "cleanup_owner": row[7],
        "idempotent_replay": replay,
    }


def stage_setup_asset(
    conn,
    *,
    project_id: str,
    draft_id: str,
    actor: str,
    filename: str,
    content_type: str | None,
    data: bytes,
) -> dict[str, Any]:
    """Write bytes outside Postgres and persist only bounded lifecycle evidence."""
    if not filename.strip() or len(filename) > 255:
        raise SetupAssetValidationError("A bounded file name is required")
    if not data:
        raise SetupAssetValidationError("The staged file is empty")
    if len(data) > DEFAULT_MAX_SIZE:
        raise SetupAssetValidationError("The staged file exceeds the 25 MiB limit")
    try:
        detected = detect_format(filename, data)
    except ValueError as exc:
        raise SetupAssetValidationError(str(exc)) from exc
    normalized_format = "xlsx" if detected == "excel" else detected
    if normalized_format not in {"csv", "tsv", "xlsx", "sav", "unknown"}:
        normalized_format = "unknown"
    content_hash = hashlib.sha256(data).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastream_setup_drafts WHERE id=%s AND project_id=%s FOR UPDATE",
            (draft_id, project_id),
        )
        if cur.fetchone() is None:
            raise SetupAssetNotFound("Draft not found")
        cur.execute(
            "SELECT id,draft_id,content_hash,detected_format,byte_count,state,expires_at,"
            "cleanup_owner "
            "FROM app.datastream_setup_assets WHERE draft_id=%s AND content_hash=%s",
            (draft_id, content_hash),
        )
        existing = cur.fetchone()
        if existing:
            return _payload(existing, replay=True)
        asset_id = mint_data_id("dsa")
        store = open_quarantine_store(max_size=DEFAULT_MAX_SIZE)
        stored = store.put(
            partition=hashlib.sha256(f"{project_id}:{draft_id}".encode()).hexdigest(),
            message_id=asset_id,
            filename=filename,
            data=data,
            content_type=content_type,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        cur.execute(
            "INSERT INTO app.datastream_setup_assets "
            "(id,project_id,draft_id,storage_ref,content_hash,detected_format,byte_count,state,"
            "expires_at,created_by,cleanup_owner) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,'available',%s,%s,'datastream_setup_asset_retention')",
            (
                asset_id,
                project_id,
                draft_id,
                stored.uri,
                content_hash,
                normalized_format,
                stored.size,
                expires_at,
                actor,
            ),
        )
        cur.execute(
            "SELECT id,draft_id,content_hash,detected_format,byte_count,state,expires_at,"
            "cleanup_owner "
            "FROM app.datastream_setup_assets WHERE id=%s",
            (asset_id,),
        )
        return _payload(cur.fetchone())


def load_setup_asset(storage_ref: str, detected_format: str) -> tuple[str, bytes]:
    """Read one exact quarantine object for bounded schema discovery."""
    suffix = "xlsx" if detected_format == "xlsx" else detected_format
    return f"staged.{suffix}", open_quarantine_store(max_size=DEFAULT_MAX_SIZE).get(storage_ref)
