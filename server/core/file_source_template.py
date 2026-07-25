"""toorow -- File-source template artifact: create + version (Story 22.11, AD-2).

The ONE immutable, content-hashed, versioned template artifact for the
file-source ingestion framework. A file-source template declares the canonical
TARGET a later import maps onto -- required canonical fields (ids from
``app.mdm_canonical_fields``), grain, matrix class (planned/actual/extrapolated),
and placement coordinates -- independent of any file's shifting layout.

Two kinds share ONE artifact + ONE producer interface (AD-2):
  * ``catalog``    -- a declarative JSONB contract (this story's focus);
  * ``adaptation`` -- a locked ``.py`` + its content-hash (Phase B-3).

Every create is written through ``operations.execute_operation`` (audit + outbox
+ idempotent replay -- no parallel audit path). Idempotency is two-layered and
consistent:
  * identical contract for the same (project, template_code) -> the SAME version
    is returned (operation replay + the DB UNIQUE(content_hash) dedup);
  * a different contract -> a NEW version row is appended (version = MAX+1);
  * UPDATE/DELETE of identity columns is rejected by the 097 immutability trigger.

Layering (mirrors csv_excel_import): a PURE validation/hash layer (no I/O,
offline-testable) + a DB layer (registry check + create/read, pg-gated). The
API layer maps the typed errors to 4xx.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ulid import ULID

# Closed vocabularies (mirror the 097 CHECK constraints).
TEMPLATE_KINDS = frozenset({"catalog", "adaptation"})
PLACEMENT_CLASSES = frozenset({"planned", "actual", "extrapolated"})

ACTION_FILE_SOURCE_TEMPLATE_CREATED = "file_source.template.created"

_TEMPLATE_COLS = [
    "id",
    "project_id",
    "template_code",
    "version",
    "kind",
    "content_hash",
    "idempotency_key_hash",
    "contract",
    "placement_class",
    "grain",
    "created_by",
    "created_at",
    "label",
    "is_active",
]


# ---------------------------------------------------------------------------
# Typed errors (stable code -> HTTP status at the API layer).
# ---------------------------------------------------------------------------


class FileSourceTemplateError(ValueError):
    """Base for file-source template errors (carries a stable code)."""

    code = "file_source_template_error"


class FileSourceTemplateValidationError(FileSourceTemplateError):
    """A caller-supplied value is invalid (maps to 422)."""

    code = "invalid_param"


class UnknownCanonicalField(FileSourceTemplateValidationError):
    """A declared required/optional field id is not an active mdm_canonical_field."""

    code = "unknown_canonical_field"


# ---------------------------------------------------------------------------
# Pure layer: contract validation + content hash (no I/O).
# ---------------------------------------------------------------------------


def _require_str_list(value: Any, *, name: str, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise FileSourceTemplateValidationError(f"'{name}' must be a list of field ids.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise FileSourceTemplateValidationError(
                f"'{name}' must contain non-empty field id strings."
            )
        out.append(item.strip())
    if not allow_empty and not out:
        raise FileSourceTemplateValidationError(f"'{name}' must not be empty.")
    if len(set(out)) != len(out):
        raise FileSourceTemplateValidationError(f"'{name}' contains duplicate ids.")
    return out


def validate_template_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a file-source template contract (pure, no DB).

    Required shape:
      kind            : 'catalog' | 'adaptation'
      required_fields : non-empty list of canonical field ids
      optional_fields : list of canonical field ids (default [])
      grain           : non-empty string (e.g. 'daily', 'broadcast_spot')
      class            : 'planned' | 'actual' | 'extrapolated'  (AD-6: mandatory)
      placement       : { metric: str, period: str, dimension?: [str...] }
    Optional:
      aliases, field_types, reshape (an Excel ReshapeSpec-like dict), and for an
      'adaptation' kind, py_content_hash (the locked .py's SHA-256).

    Returns a normalised copy (sorted lists, canonical placement) so the content
    hash is stable. Raises FileSourceTemplateValidationError (422) on any issue.
    """
    if not isinstance(contract, dict):
        raise FileSourceTemplateValidationError("Contract must be an object.")

    kind = contract.get("kind")
    if kind not in TEMPLATE_KINDS:
        raise FileSourceTemplateValidationError(
            f"'kind' must be one of {sorted(TEMPLATE_KINDS)}; got {kind!r}."
        )

    required_fields = _require_str_list(
        contract.get("required_fields"), name="required_fields", allow_empty=False
    )
    optional_fields = _require_str_list(
        contract.get("optional_fields", []), name="optional_fields", allow_empty=True
    )
    overlap = set(required_fields) & set(optional_fields)
    if overlap:
        raise FileSourceTemplateValidationError(
            f"fields cannot be both required and optional: {sorted(overlap)}."
        )

    grain = contract.get("grain")
    if not isinstance(grain, str) or not grain.strip():
        raise FileSourceTemplateValidationError("'grain' must be a non-empty string.")

    placement_class = contract.get("class")
    if placement_class not in PLACEMENT_CLASSES:
        raise FileSourceTemplateValidationError(
            f"'class' must be one of {sorted(PLACEMENT_CLASSES)} (a template with no "
            f"declared class is refused, AD-6); got {placement_class!r}."
        )

    placement = contract.get("placement")
    if not isinstance(placement, dict):
        raise FileSourceTemplateValidationError(
            "'placement' must be an object with metric + period coordinates."
        )
    metric = placement.get("metric")
    period = placement.get("period")
    if not isinstance(metric, str) or not metric.strip():
        raise FileSourceTemplateValidationError("'placement.metric' must be a non-empty string.")
    if not isinstance(period, str) or not period.strip():
        raise FileSourceTemplateValidationError("'placement.period' must be a non-empty string.")
    dimension = placement.get("dimension", [])
    dimension = _require_str_list(dimension, name="placement.dimension", allow_empty=True)

    normalised: dict[str, Any] = {
        "kind": kind,
        "required_fields": sorted(required_fields),
        "optional_fields": sorted(optional_fields),
        "grain": grain.strip(),
        "class": placement_class,
        "placement": {
            "metric": metric.strip(),
            "period": period.strip(),
            "dimension": sorted(dimension),
        },
    }

    # Optional passthrough (kept in the hash so a change versions a new row).
    for opt_key in ("aliases", "field_types", "reshape"):
        if contract.get(opt_key) is not None:
            if not isinstance(contract[opt_key], dict):
                raise FileSourceTemplateValidationError(f"'{opt_key}' must be an object.")
            normalised[opt_key] = contract[opt_key]

    if kind == "adaptation":
        py_hash = contract.get("py_content_hash")
        if not isinstance(py_hash, str) or not _is_sha256(py_hash):
            raise FileSourceTemplateValidationError(
                "an 'adaptation' template requires 'py_content_hash' "
                "(the locked .py SHA-256)."
            )
        normalised["py_content_hash"] = py_hash
        # The locked .py source is stored so the adaptation replays deterministically
        # (Story 22.24). When present, its SHA-256 MUST equal py_content_hash.
        py_source = contract.get("py_source")
        if py_source is not None:
            if not isinstance(py_source, str) or not py_source.strip():
                raise FileSourceTemplateValidationError("'py_source' must be a non-empty string.")
            if hashlib.sha256(py_source.encode("utf-8")).hexdigest() != py_hash:
                raise FileSourceTemplateValidationError(
                    "'py_content_hash' does not match the SHA-256 of 'py_source'."
                )
            normalised["py_source"] = py_source

    return normalised


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def compute_content_hash(normalised_contract: dict[str, Any]) -> str:
    """SHA-256 of the canonical (sorted-key) normalised contract JSON (64 hex)."""
    canonical = json.dumps(normalised_contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_template_code(template_code: str) -> str:
    code = (template_code or "").strip()
    if not code or len(code) > 128 or not code[0].isalnum():
        raise FileSourceTemplateValidationError(
            "'template_code' must be 1..128 chars, start alphanumeric "
            "(letters/digits/_/- allowed)."
        )
    if not all(c.isalnum() or c in "_-" for c in code):
        raise FileSourceTemplateValidationError(
            "'template_code' may only contain letters, digits, '_' and '-'."
        )
    return code


def default_idempotency_key(project_id: str, template_code: str, content_hash: str) -> str:
    """Deterministic idempotency key: identical (scope + content) replays cleanly."""
    return f"file_source_template:{project_id}:{template_code}:{content_hash}"


# ---------------------------------------------------------------------------
# DB layer: registry check + read helpers.
# ---------------------------------------------------------------------------


def _assert_required_fields_registered(
    conn, *, project_id: str, field_ids: list[str]
) -> None:
    """Every declared field id must be an ACTIVE mdm_canonical_field in scope.

    Scope = the project's own canonical fields OR the platform-canonical rows
    (project_id IS NULL), per AD-5. Fails closed with the offending ids listed.
    """
    if not field_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.mdm_canonical_fields
            WHERE id = ANY(%s) AND status = 'active'
              AND (project_id = %s OR project_id IS NULL)
            """,
            (list(field_ids), project_id),
        )
        found = {row[0] for row in cur.fetchall()}
    unknown = [fid for fid in field_ids if fid not in found]
    if unknown:
        raise UnknownCanonicalField(
            "these field ids are not active canonical fields in scope: "
            + ", ".join(sorted(unknown))
        )


def _row_to_template(row: tuple[Any, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, val in zip(_TEMPLATE_COLS, row):
        if col == "created_at" and val is not None:
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


def _select_by_content(conn, *, project_id: str, template_code: str, content_hash: str):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TEMPLATE_COLS)}
            FROM app.file_source_templates
            WHERE project_id = %s AND template_code = %s AND content_hash = %s
            """,
            (project_id, template_code, content_hash),
        )
        row = cur.fetchone()
    return _row_to_template(row) if row else None


def get_file_source_template(
    conn, *, project_id: str, template_code: str, version: int
) -> dict[str, Any] | None:
    """Return one template version by (project, code, version), or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TEMPLATE_COLS)}
            FROM app.file_source_templates
            WHERE project_id = %s AND template_code = %s AND version = %s
            """,
            (project_id, template_code, version),
        )
        row = cur.fetchone()
    return _row_to_template(row) if row else None


def list_file_source_template_versions(
    conn, *, project_id: str, template_code: str
) -> list[dict[str, Any]]:
    """List every version of a template, newest version first."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TEMPLATE_COLS)}
            FROM app.file_source_templates
            WHERE project_id = %s AND template_code = %s
            ORDER BY version DESC
            """,
            (project_id, template_code),
        )
        rows = cur.fetchall()
    return [_row_to_template(r) for r in rows]


def _mint_template_id() -> str:
    return f"fst_{ULID()}"


# ---------------------------------------------------------------------------
# Write path: create a versioned template through execute_operation (AD-2).
# ---------------------------------------------------------------------------


def create_file_source_template(
    conn,
    *,
    project_id: str,
    org_id: str,
    template_code: str,
    contract: dict[str, Any],
    created_by: str,
    label: str | None = None,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Create one immutable, content-hashed template VERSION (idempotent).

    Validates the contract (pure) + that every required/optional field id is an
    active canonical field in scope, then writes ONE version row through
    ``operations.execute_operation``:
      * identical contract for (project, template_code) -> the existing version
        (no new row);
      * a different contract -> version = MAX(version)+1;
      * the caller owns the transaction (this never commits).

    Returns the created (or replayed) template row dict.
    """
    code = _validate_template_code(template_code)
    normalised = validate_template_contract(contract)
    _assert_required_fields_registered(
        conn,
        project_id=project_id,
        field_ids=normalised["required_fields"] + normalised["optional_fields"],
    )

    content_hash = compute_content_hash(normalised)
    idem = idempotency_key or default_idempotency_key(project_id, code, content_hash)
    placement_class = normalised["class"]
    grain = normalised["grain"]
    kind = normalised["kind"]

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    spec = OperationSpec(
        command_type=ACTION_FILE_SOURCE_TEMPLATE_CREATED,
        actor=created_by,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"file_source_template:{code}",
        ),
        idempotency_key=idem,
        host_context=host_context or {},
        versions={"policy": "file-source-template-v1"},
        request_payload={
            "project_id": project_id,
            "template_code": code,
            "content_hash": content_hash,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"file-source-template:{project_id}:{code}:{content_hash}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        # Defensive dedup (the operation replay usually short-circuits identical
        # re-creates before the mutation even runs; this guards a concurrent race).
        existing = _select_by_content(
            operation_conn, project_id=project_id, template_code=code,
            content_hash=content_hash,
        )
        if existing is not None:
            row = existing
        else:
            idem_hash = hashlib.sha256(idem.encode("utf-8")).hexdigest()
            with operation_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM app.file_source_templates
                    WHERE project_id = %s AND template_code = %s
                    """,
                    (project_id, code),
                )
                next_version = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO app.file_source_templates
                        (id, project_id, template_code, version, kind, content_hash,
                         idempotency_key_hash, contract, placement_class, grain,
                         created_by, label)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (project_id, template_code, content_hash) DO NOTHING
                    """,
                    (
                        _mint_template_id(), project_id, code, next_version, kind,
                        content_hash, idem_hash, json.dumps(normalised),
                        placement_class, grain, created_by, label,
                    ),
                )
            # Re-read: our insert may have been a no-op if a concurrent writer won
            # the content race -> return whichever row is now persisted.
            row = _select_by_content(
                operation_conn, project_id=project_id, template_code=code,
                content_hash=content_hash,
            )
            if row is None:  # pragma: no cover - the insert above just ran
                raise FileSourceTemplateError("template row not found after insert")

        result = {
            "template_id": row["id"],
            "template_code": row["template_code"],
            "version": row["version"],
            "kind": row["kind"],
            "content_hash": row["content_hash"],
            "class": row["placement_class"],
            "grain": row["grain"],
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "template_id": row["id"],
                "template_code": row["template_code"],
                "version": row["version"],
                "kind": row["kind"],
            },
        )

    execute_operation(conn, spec, mutation=mutation)
    # Return the authoritative persisted row (create OR idempotent replay).
    row = _select_by_content(
        conn, project_id=project_id, template_code=code, content_hash=content_hash
    )
    if row is None:  # pragma: no cover
        raise FileSourceTemplateError("template row not found after operation")
    return row
