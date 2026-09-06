"""Signed, bounded authority for exact Analytics feedback.

The sidecar is an attestation, not authorization.  Adapters must still resolve
the current request identity and all pinned owners on one scoped connection
before writing anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ulid import ULID

SCHEMA_VERSION = "exact-feedback.v1"
RECEIPT_SCHEMA_VERSION = "exact-feedback-receipt.v1"
PURPOSE = "analyze_feedback"
SIDECAR_META_KEY = "toorow.feedback"
MAX_TOKEN_BYTES = 4096
MAX_COMMENT = 2000
MAX_RETRY_KEY = 200
MAX_DELIVERED_ROWS = 1000
MAX_DELIVERED_FIELDS = 150
MAX_PATH_STEP_ORDINALS = 200
DEFAULT_TTL = timedelta(minutes=15)
_LOCAL_SECRET = secrets.token_bytes(32)
logger = logging.getLogger(__name__)
_PIN_FIELDS = frozenset(
    {
        "render_id",
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
    }
)


class FeedbackContextError(ValueError):
    """Safe refusal raised by the canonical feedback contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        verified_claims: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.verified_claims = dict(verified_claims or {})


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise FeedbackContextError("invalid_context", "The feedback context is not valid.")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def feedback_context_secret() -> bytes:
    """Return the purpose-scoped secret, failing closed for OAuth deployments."""
    configured = os.getenv("TOOROW_FEEDBACK_CONTEXT_SECRET", "").encode()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("TOOROW_FEEDBACK_CONTEXT_SECRET must contain at least 32 bytes")
        return configured
    if os.getenv("TOOROW_AUTH_MODE", "disabled").strip().lower() != "disabled":
        raise RuntimeError(
            "TOOROW_FEEDBACK_CONTEXT_SECRET is required unless authentication is disabled"
        )
    return _LOCAL_SECRET


def _text(value: Any, field: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise FeedbackContextError("invalid_context", f"The feedback {field} is not valid.")
    return value.strip()


def _delivered_rows(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FeedbackContextError("invalid_context", "The delivered Result window is not valid.")
    start, count = value.get("start"), value.get("count")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or start < 0
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or count > MAX_DELIVERED_ROWS
    ):
        raise FeedbackContextError("invalid_context", "The delivered Result window is not valid.")
    if set(value) == {"start", "count", "fields"}:
        fields = value["fields"]
        if not isinstance(fields, list) or len(fields) > MAX_DELIVERED_FIELDS:
            raise FeedbackContextError(
                "invalid_context", "The delivered Result window is not valid."
            )
        cleaned = [_text(field, "field") for field in fields]
        if len(set(cleaned)) != len(cleaned):
            raise FeedbackContextError(
                "invalid_context", "The delivered Result window is not valid."
            )
        return {
            "start": start,
            "count": count,
            "field_count": len(cleaned),
            "fields_hash": hashlib.sha256(_canonical_json(cleaned)).hexdigest(),
        }
    if set(value) != {"start", "count", "field_count", "fields_hash"}:
        raise FeedbackContextError("invalid_context", "The delivered Result window is not valid.")
    field_count, fields_hash = value["field_count"], value["fields_hash"]
    if (
        not isinstance(field_count, int)
        or isinstance(field_count, bool)
        or field_count < 0
        or field_count > MAX_DELIVERED_FIELDS
        or not isinstance(fields_hash, str)
        or len(fields_hash) != 64
        or any(char not in "0123456789abcdef" for char in fields_hash)
    ):
        raise FeedbackContextError("invalid_context", "The delivered Result window is not valid.")
    return dict(value)


def _pin_claims(pins: Mapping[str, Any] | None) -> dict[str, Any]:
    if pins is None:
        return {}
    if not isinstance(pins, Mapping) or not set(pins).issubset(_PIN_FIELDS):
        raise FeedbackContextError("invalid_context", "The feedback Render pins are not valid.")
    return {field: pins[field] for field in _PIN_FIELDS if pins.get(field) is not None}


def _claims(value: Mapping[str, Any], *, now: datetime, expires_at: datetime) -> dict[str, Any]:
    result_hash = _text(value.get("result_content_hash"), "Result hash", maximum=64)
    if len(result_hash) != 64 or any(char not in "0123456789abcdef" for char in result_hash):
        raise FeedbackContextError("invalid_context", "The feedback Result is not valid.")
    claims: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "org_id": _text(value.get("org_id"), "organization"),
        "project_id": _text(value.get("project_id"), "Project"),
        "surface": _text(value.get("surface"), "surface", maximum=32),
        "interaction_ref": _text(value.get("interaction_ref"), "interaction"),
        "result_id": _text(value.get("result_id"), "Result"),
        "result_content_hash": result_hash,
        "delivered_rows": _delivered_rows(value.get("delivered_rows")),
        "issued_at": int(_utc(now).timestamp()),
        "expires_at": int(_utc(expires_at).timestamp()),
    }
    optional_text = (
        "render_id",
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
        "ai_path_id",
    )
    for field in optional_text:
        if value.get(field) is not None:
            claims[field] = _text(value[field], field)
    trace_id = value.get("w3c_trace_id")
    if trace_id is not None:
        trace_id = _text(trace_id, "trace", maximum=32)
        if (
            len(trace_id) != 32
            or any(char not in "0123456789abcdef" for char in trace_id)
            or trace_id == "0" * 32
        ):
            raise FeedbackContextError("invalid_context", "The feedback trace is not valid.")
        claims["w3c_trace_id"] = trace_id
    ordinals = value.get("path_step_ordinals")
    if ordinals is not None:
        if not isinstance(ordinals, list) or len(ordinals) > MAX_PATH_STEP_ORDINALS:
            raise FeedbackContextError("invalid_context", "The feedback AI Path is not valid.")
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in ordinals
        ):
            raise FeedbackContextError("invalid_context", "The feedback AI Path is not valid.")
        claims["path_step_ordinals"] = sorted(set(ordinals))
    pins = [claims.get(field) for field in optional_text[1:6]]
    if any(pin is not None for pin in pins) and not all(pin is not None for pin in pins):
        raise FeedbackContextError("invalid_context", "The feedback Render pins are incomplete.")
    if claims.get("render_id") is not None and not all(pin is not None for pin in pins):
        raise FeedbackContextError("invalid_context", "The feedback Render pins are incomplete.")
    return claims


def mint_feedback_context(
    claims: Mapping[str, Any],
    *,
    secret: bytes | None = None,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> dict[str, str]:
    """Mint the only client-visible exact feedback authority sidecar."""
    issued = _utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    if ttl <= timedelta(0) or ttl > timedelta(hours=1):
        raise FeedbackContextError("invalid_context", "The feedback context lifetime is not valid.")
    expires = issued + ttl
    document = _claims(claims, now=issued, expires_at=expires)
    body = _b64encode(_canonical_json(document))
    signature = _b64encode(
        hmac.digest(secret or feedback_context_secret(), body.encode(), "sha256")
    )
    token = f"{body}.{signature}"
    if len(token.encode()) > MAX_TOKEN_BYTES:
        raise FeedbackContextError("invalid_context", "The feedback context is too large.")
    return {
        "schema_version": SCHEMA_VERSION,
        "token": token,
        "interaction_ref": document["interaction_ref"],
        "expires_at": _iso(expires),
    }


def verify_feedback_context(
    sidecar: Mapping[str, Any],
    *,
    secret: bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify signature and shape; expiry keeps verified claims for safe reissue."""
    generic = "The feedback context is not valid."
    if not isinstance(sidecar, Mapping) or set(sidecar) != {
        "schema_version",
        "token",
        "interaction_ref",
        "expires_at",
    }:
        raise FeedbackContextError("invalid_context", generic)
    token = sidecar.get("token")
    if sidecar.get("schema_version") != SCHEMA_VERSION or not isinstance(token, str):
        raise FeedbackContextError("invalid_context", generic)
    if len(token.encode()) > MAX_TOKEN_BYTES or token.count(".") != 1:
        raise FeedbackContextError("invalid_context", generic)
    body, signature = token.split(".")
    expected = hmac.digest(secret or feedback_context_secret(), body.encode(), "sha256")
    try:
        signature_bytes = _b64decode(signature)
    except (ValueError, TypeError) as exc:
        raise FeedbackContextError("invalid_context", generic) from exc
    if not hmac.compare_digest(signature_bytes, expected):
        raise FeedbackContextError("invalid_context", generic)
    try:
        document = json.loads(_b64decode(body))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FeedbackContextError("invalid_context", generic) from exc
    if not isinstance(document, Mapping):
        raise FeedbackContextError("invalid_context", generic)
    try:
        issued_raw = document["issued_at"]
        expires_raw = document["expires_at"]
        if type(issued_raw) is not int or type(expires_raw) is not int:
            raise TypeError("timestamps must be integers")
        issued = datetime.fromtimestamp(issued_raw, timezone.utc)
        expires = datetime.fromtimestamp(expires_raw, timezone.utc)
        checked = _claims(document, now=issued, expires_at=expires)
    except (KeyError, TypeError, ValueError, OverflowError, FeedbackContextError) as exc:
        raise FeedbackContextError("invalid_context", generic) from exc
    if dict(document) != checked:
        raise FeedbackContextError("invalid_context", generic)
    if sidecar.get("interaction_ref") != checked["interaction_ref"] or sidecar.get(
        "expires_at"
    ) != _iso(expires):
        raise FeedbackContextError("invalid_context", generic)
    current = _utc(now or datetime.now(timezone.utc))
    if issued > current or expires <= issued or expires - issued > timedelta(hours=1):
        raise FeedbackContextError("invalid_context", generic)
    if current >= expires:
        raise FeedbackContextError(
            "feedback_context_expired",
            "The feedback context expired. Retry with the refreshed context.",
            verified_claims=checked,
        )
    return checked


def normalize_feedback_request(
    *,
    claims: Mapping[str, Any],
    target: Any,
    polarity: Any,
    comment: Any,
    retry_key: Any,
) -> dict[str, Any]:
    """Normalize the closed command without accepting caller-owned authority."""
    if polarity not in {"positive", "negative"}:
        raise FeedbackContextError("invalid_request", "Feedback polarity is not valid.")
    if comment is not None and (not isinstance(comment, str) or len(comment) > MAX_COMMENT):
        raise FeedbackContextError("invalid_request", "Feedback comment is not valid.")
    retry = _text(retry_key, "retry key", maximum=MAX_RETRY_KEY)
    if not isinstance(target, Mapping) or not isinstance(target.get("kind"), str):
        raise FeedbackContextError("invalid_target", "The feedback target is not available.")
    kind = target["kind"]
    if kind == "answer":
        if set(target) != {"kind"}:
            raise FeedbackContextError("invalid_target", "The feedback target is not available.")
        normalized_target = {"kind": "answer"}
    elif kind == "datum":
        if set(target) != {"kind", "row_index", "field"}:
            raise FeedbackContextError("invalid_target", "The feedback target is not available.")
        row_index, field = target["row_index"], target["field"]
        window = claims.get("delivered_rows") or {}
        start, count = window.get("start"), window.get("count")
        if (
            not isinstance(row_index, int)
            or isinstance(row_index, bool)
            or not isinstance(start, int)
            or not isinstance(count, int)
            or row_index < start
            or row_index >= start + count
            or not isinstance(field, str)
            or not field.strip()
            or len(field) > 200
        ):
            raise FeedbackContextError("invalid_target", "The feedback target is not available.")
        normalized_target = {"kind": "datum", "row_index": row_index, "field": field}
    elif kind == "path_step":
        if set(target) != {"kind", "ordinal"}:
            raise FeedbackContextError("invalid_target", "The feedback target is not available.")
        ordinal = target["ordinal"]
        if (
            not claims.get("ai_path_id")
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal not in claims.get("path_step_ordinals", [])
        ):
            raise FeedbackContextError("invalid_target", "The feedback target is not available.")
        normalized_target = {"kind": "path_step", "ordinal": ordinal}
    else:
        raise FeedbackContextError("invalid_target", "The feedback target is not available.")
    return {
        "target": normalized_target,
        "polarity": polarity,
        "comment": comment,
        "retry_key": retry,
    }


def request_fingerprint(
    request: Mapping[str, Any], *, claims: Mapping[str, Any], secret: bytes | None = None
) -> tuple[str, str]:
    """Return (retry-key hash, semantic request hash), excluding token/iat/exp."""
    key = secret or feedback_context_secret()
    retry_hash = hmac.new(key, str(request["retry_key"]).encode(), hashlib.sha256).hexdigest()
    pins = {
        name: claims.get(name)
        for name in (
            "org_id",
            "project_id",
            "surface",
            "interaction_ref",
            "result_id",
            "result_content_hash",
            "render_id",
            "visualization_spec_version_id",
            "renderer_build_id",
            "runtime_build_id",
            "theme_version",
            "formatter_version",
            "ai_path_id",
        )
        if claims.get(name) is not None
    }
    semantic = {
        "target": request["target"],
        "polarity": request["polarity"],
        "comment": request["comment"],
        "pins": pins,
    }
    return retry_hash, hashlib.sha256(_canonical_json(semantic)).hexdigest()


def mint_result_feedback_context(
    conn,
    *,
    org_id: str,
    project_id: str,
    result_id: str,
    surface: str,
    delivered_rows: list[Mapping[str, Any]],
    delivered_fields: list[str],
    row_start: int = 0,
    delivered_columns: list[str] | None = None,
    pins: Mapping[str, Any] | None = None,
    w3c_trace_id: str | None = None,
) -> dict[str, str] | None:
    """Mint a sidecar only after matching the bytes delivered from the Result."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT qr.content_hash, qr.ai_path_id, qr.ai_path_absent_literal,
                   p.rows_chunk, ap.lifecycle, p.result_schema
            FROM app.query_results qr
            JOIN app.query_result_payloads p
              ON p.result_id = qr.id AND p.org_id = qr.org_id AND p.project_id = qr.project_id
             AND p.content_hash = qr.content_hash
            LEFT JOIN app.ai_paths ap
              ON ap.id = qr.ai_path_id AND ap.org_id = qr.org_id AND ap.project_id = qr.project_id
            WHERE qr.id = %s AND qr.org_id = %s AND qr.project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise FeedbackContextError("invalid_context", "The feedback Result is not available.")
    stored_rows = row[3] if isinstance(row[3], list) else []
    delivered = [dict(item) for item in delivered_rows if isinstance(item, Mapping)]
    expected = stored_rows[row_start : row_start + len(delivered)]
    if delivered_columns is not None:
        schema_fields = row[5].get("fields", []) if isinstance(row[5], Mapping) else []
        source_columns = [
            item["name"]
            for item in schema_fields
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        ]
        indexes = {name: index for index, name in enumerate(source_columns)}
        projected: list[dict[str, Any]] = []
        for item in expected:
            if isinstance(item, Mapping):
                projected.append({name: item.get(name) for name in delivered_columns})
            elif isinstance(item, (list, tuple)) and all(
                name in indexes and indexes[name] < len(item) for name in delivered_columns
            ):
                projected.append({name: item[indexes[name]] for name in delivered_columns})
            else:
                raise FeedbackContextError(
                    "invalid_context", "The delivered Result window is not valid."
                )
        expected = projected
    if len(delivered) != len(delivered_rows) or expected != delivered:
        raise FeedbackContextError("invalid_context", "The delivered Result window is not valid.")
    if row[1] is not None and row[4] != "finalized":
        raise FeedbackContextError("invalid_context", "The feedback AI Path is not available.")
    path_ordinals: list[int] | None = None
    if row[1] is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ordinal FROM app.ai_path_steps WHERE path_id = %s ORDER BY ordinal",
                (row[1],),
            )
            path_ordinals = [int(item[0]) for item in cur.fetchall()]
    return mint_eligible_delivery_feedback_context(
        conn,
        org_id=org_id,
        project_id=project_id,
        result_id=result_id,
        result_content_hash=row[0],
        surface=surface,
        stored_rows=stored_rows,
        delivered_rows=delivered,
        delivered_fields=delivered_fields,
        row_start=row_start,
        ai_path_id=row[1],
        path_step_ordinals=path_ordinals,
        pins=pins,
        w3c_trace_id=w3c_trace_id,
    )


def remint_result_slice_feedback_context(
    conn,
    *,
    claims: Mapping[str, Any],
    page: Mapping[str, Any],
) -> dict[str, str] | None:
    """Reissue one signed authority for an exact frozen Result page."""
    if (
        page.get("result_id") != claims.get("result_id")
        or page.get("content_hash") != claims.get("result_content_hash")
        or not isinstance(page.get("columns"), list)
        or not isinstance(page.get("rows"), list)
        or page.get("offset") is None
    ):
        raise FeedbackContextError("invalid_context", "The feedback context is not valid.")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.result_schema, p.manifest, qr.query_spec_version_id
            FROM app.query_results qr
            JOIN app.query_result_payloads p
              ON p.result_id = qr.id AND p.org_id = qr.org_id AND p.project_id = qr.project_id
             AND p.content_hash = qr.content_hash
            WHERE qr.id = %s AND qr.org_id = %s AND qr.project_id = %s
              AND qr.content_hash = %s
            """,
            (
                claims.get("result_id"),
                claims.get("org_id"),
                claims.get("project_id"),
                claims.get("result_content_hash"),
            ),
        )
        result = cur.fetchone()
    if result is None:
        raise FeedbackContextError("invalid_context", "The feedback context is not valid.")

    schema, manifest, query_spec_version_id = result
    fields = feedback_fields_from_schema(schema)
    spec_id = claims.get("visualization_spec_version_id")
    if spec_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT spec FROM app.visualization_spec_versions
                WHERE id = %s AND org_id = %s AND project_id = %s
                  AND query_spec_version_id = %s
                """,
                (spec_id, claims.get("org_id"), claims.get("project_id"), query_spec_version_id),
            )
            spec = cur.fetchone()
        if spec is None:
            raise FeedbackContextError("invalid_context", "The feedback context is not valid.")
        fields = feedback_fields_from_visualization_spec(schema, spec[0], manifest)

    render_id = claims.get("render_id")
    if render_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT result_id, result_content_hash, visualization_spec_version_id,
                       renderer_build_id, runtime_build_id, theme_version, formatter_version
                FROM app.renders
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (render_id, claims.get("org_id"), claims.get("project_id")),
            )
            render = cur.fetchone()
        expected = tuple(
            claims.get(name)
            for name in (
                "result_id",
                "result_content_hash",
                "visualization_spec_version_id",
                "renderer_build_id",
                "runtime_build_id",
                "theme_version",
                "formatter_version",
            )
        )
        if render != expected:
            raise FeedbackContextError("invalid_context", "The feedback context is not valid.")

    prior_window = claims.get("delivered_rows") or {}
    attested = _delivered_rows({"start": 0, "count": 0, "fields": fields})
    if (
        prior_window.get("field_count") != attested["field_count"]
        or prior_window.get("fields_hash") != attested["fields_hash"]
        or not set(fields).issubset(page["columns"])
    ):
        raise FeedbackContextError("invalid_context", "The feedback context is not valid.")

    pins = {name: claims[name] for name in _PIN_FIELDS if claims.get(name) is not None}
    return mint_result_feedback_context(
        conn,
        org_id=str(claims["org_id"]),
        project_id=str(claims["project_id"]),
        result_id=str(claims["result_id"]),
        surface=str(claims["surface"]),
        delivered_rows=page["rows"],
        delivered_fields=fields,
        row_start=int(page["offset"]),
        delivered_columns=page["columns"],
        pins=pins,
        w3c_trace_id=claims.get("w3c_trace_id"),
    )


def mint_delivery_feedback_context(
    *,
    org_id: str,
    project_id: str,
    result_id: str,
    result_content_hash: str,
    surface: str,
    stored_rows: list[Any],
    delivered_rows: list[Mapping[str, Any]],
    delivered_fields: list[str],
    row_start: int = 0,
    ai_path_id: str | None = None,
    path_step_ordinals: list[int] | None = None,
    pins: Mapping[str, Any] | None = None,
    w3c_trace_id: str | None = None,
) -> dict[str, str]:
    """Mint from an already scoped immutable Result loaded by its producer."""
    delivered = [dict(item) for item in delivered_rows if isinstance(item, Mapping)]
    expected = stored_rows[row_start : row_start + len(delivered)]
    if len(delivered) != len(delivered_rows) or expected != delivered:
        raise FeedbackContextError("invalid_context", "The delivered Result window is not valid.")
    return mint_feedback_context(
        {
            "org_id": org_id,
            "project_id": project_id,
            "surface": surface,
            "interaction_ref": f"afi_{ULID()}",
            "result_id": result_id,
            "result_content_hash": result_content_hash,
            "delivered_rows": {
                "start": row_start,
                "count": len(delivered),
                "fields": delivered_fields,
            },
            "ai_path_id": ai_path_id,
            "path_step_ordinals": path_step_ordinals,
            "w3c_trace_id": w3c_trace_id,
            **_pin_claims(pins),
        }
    )


def mint_eligible_delivery_feedback_context(
    conn,
    *,
    org_id: str,
    project_id: str,
    result_id: str,
    result_content_hash: str,
    surface: str,
    stored_rows: list[Any],
    delivered_rows: list[Mapping[str, Any]],
    delivered_fields: list[str],
    row_start: int = 0,
    ai_path_id: str | None = None,
    path_step_ordinals: list[int] | None = None,
    pins: Mapping[str, Any] | None = None,
    w3c_trace_id: str | None = None,
    source: str = "authenticated",
) -> dict[str, str] | None:
    """Mint only when the same transaction recorded this interaction as eligible."""
    try:
        sidecar = mint_delivery_feedback_context(
            org_id=org_id,
            project_id=project_id,
            result_id=result_id,
            result_content_hash=result_content_hash,
            surface=surface,
            stored_rows=stored_rows,
            delivered_rows=delivered_rows,
            delivered_fields=delivered_fields,
            row_start=row_start,
            ai_path_id=ai_path_id,
            path_step_ordinals=path_step_ordinals,
            pins=pins,
            w3c_trace_id=w3c_trace_id,
        )
        claims = verify_feedback_context(sidecar)
    except FeedbackContextError as exc:
        logger.info("analyze_feedback: feedback capability omitted (%s)", exc.code)
        return None

    from core.feedback_review import record_feedback_eligibility  # noqa: PLC0415

    recorded = record_feedback_eligibility(conn, claims=claims, source=source)
    return sidecar if recorded is not None else None


def feedback_fields_from_schema(schema: Any) -> list[str]:
    """Project only targetable displayed fields from a Result schema."""
    if not isinstance(schema, Mapping) or not isinstance(schema.get("fields"), list):
        return []
    projected: list[str] = []
    for field in schema["fields"]:
        if not isinstance(field, Mapping) or field.get("hidden") is True:
            continue
        name = field.get("name")
        role = field.get("role")
        if (
            isinstance(name, str)
            and name
            and not name.startswith("_")
            and role not in {"internal", "provenance"}
        ):
            projected.append(name)
    return projected


def feedback_fields_from_visualization_spec(
    schema: Any, spec: Any, manifest: Any
) -> list[str]:
    """Map the pinned visual's bound field ids to safe Result field names."""
    safe = feedback_fields_from_schema(schema)
    if not isinstance(schema, Mapping) or not isinstance(spec, Mapping):
        return []
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return []
    identifier_to_name: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, Mapping) or field.get("name") not in safe:
            continue
        name = str(field["name"])
        identifier_to_name[name] = name
        if isinstance(field.get("id"), str):
            identifier_to_name[field["id"]] = name

    declared: list[str] = []
    evidence = spec.get("evidence")
    datum_fields = evidence.get("datum_fields") if isinstance(evidence, Mapping) else None
    if isinstance(datum_fields, list):
        declared.extend(item for item in datum_fields if isinstance(item, str))
    bindings = spec.get("bindings")
    if isinstance(bindings, Mapping):
        for value in bindings.values():
            if isinstance(value, str):
                declared.append(value)
            elif isinstance(value, list):
                declared.extend(item for item in value if isinstance(item, str))

    provenance = manifest.get("provenance") if isinstance(manifest, Mapping) else None
    values = provenance.get("values") if isinstance(provenance, Mapping) else None
    member_to_source = {
        str(item["member_id"]): str(item["source_field"])
        for item in (values if isinstance(values, list) else [])
        if isinstance(item, Mapping) and item.get("member_id") and item.get("source_field")
    }
    names = {
        identifier_to_name[source]
        for item in declared
        if (source := member_to_source.get(item, item)) in identifier_to_name
    }
    return [field for field in safe if field in names]


def score_feedback_after_commit(
    *, status: str, trace_id: str | None, polarity: str, comment: str | None
) -> None:
    """Best-effort Langfuse score; adapters call this only after commit."""
    if status != "recorded" or not trace_id:
        return
    try:
        from core import tracing  # noqa: PLC0415

        if not tracing.is_enabled():
            return
        from langfuse import Langfuse  # noqa: PLC0415

        client = Langfuse()
        client.score(
            trace_id=trace_id,
            name="user_feedback",
            value=1.0 if polarity == "positive" else -1.0,
            comment=comment or None,
        )
        client.flush()
    except Exception as exc:  # noqa: BLE001 -- telemetry cannot fail a receipt
        logger.warning("analyze_feedback: langfuse_score_failed: %s", exc)
