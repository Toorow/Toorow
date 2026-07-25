"""toorow -- Shared declarative flow interface (Story 8.7, Epic 8 Part D).

The COMMON interface through which BOTH the admin UI (REST, via flows_api.py) and
the LLM agent (MCP tools, via main.py) read and edit data flows. Two flow kinds:

  * ``datastream`` -- built ON TOP of core.datastreams (Story 8.2) + core.datamodel
    (Story 8.5). Single write path: create/update the datastream row and upsert its
    mappings via those modules -- never inline SQL, never bypassing their audit.
  * ``report`` -- a per-project override that extends a module report pack
    (base_report_id). Stored additively in app.report_overrides (migration 025).
    The READ side merges base pack + override AT THIS LAYER ONLY (reports.py is not
    touched). Story 8.9 wires the merged doc into rendering.

Public API (all take an open psycopg ``conn`` for the DB-bound ops):
    validate_flow(doc)                                   -> (ok: bool, errors: list[dict])
    list_flows(project_id, identity, conn, kind=None)    -> list[dict] (summaries)
    get_flow(project_id, kind, flow_id, identity, conn, loaded_modules=None) -> dict | None
    upsert_flow(project_id, doc, identity, conn, loaded_modules=None)        -> dict

GUARDRAILS (hard):
  - AD-3 secrets: the JSON schemas set additionalProperties:false everywhere, so a
    token/access_token/secret field is REJECTED by validate_flow BEFORE any DB touch.
    connection_ref_id is validated as an OPAQUE FK reference only (exists + belongs
    to the project). This module NEVER reads a token.
  - AD-5 scoping: identity_has_project_access on every op. A cross-project access
    returns None/raises FlowScopeError AND writes an 'access_denied' audit row.
  - AD-7 audit: every effective upsert writes a 'flow_updated' audit row with a
    minimal before/after diff. A no-op upsert (idempotent re-apply) writes nothing.
  - Validate-before-write: upsert_flow calls validate_flow at the top and refuses on
    any error.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Literal audit actions (mirror datamodel.py which uses literal 'mapping.updated'
# rather than adding an ACTION_ constant; audit.py is not owned by this story).
_ACTION_FLOW_UPDATED = "flow_updated"
_ACTION_ACCESS_DENIED = "access_denied"

_SCHEMA_DIR = Path(__file__).parent / "schemas"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FlowValidationError(ValueError):
    """Raised by upsert_flow when the document fails schema validation.

    Carries the structured ``errors`` list (json-path + actionable message).
    """

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__("; ".join(e.get("message", "") for e in errors) or "invalid flow")


class FlowScopeError(PermissionError):
    """Raised when the caller may not touch a project (AD-5). Surfaces as 404."""


class FlowConflictError(ValueError):
    """Raised on a name/uniqueness conflict (datastream (project, name))."""


class FlowUnavailableError(RuntimeError):
    """Raised when strict scope or capability evidence is unavailable."""


# ---------------------------------------------------------------------------
# Schema loading + validation
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _load_schema(kind: str) -> dict:
    """Load and cache the JSON schema for *kind* ('datastream' | 'report')."""
    path = _SCHEMA_DIR / f"flow.{kind}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


# Human-friendly French guidance keyed by (json-path suffix, validator) so raw
# jsonschema messages become actionable. Falls back to the raw message otherwise.
def _french_message(err) -> str:
    """Translate a jsonschema ValidationError into an actionable French message."""
    validator = getattr(err, "validator", "")
    path = "/" + "/".join(str(p) for p in err.absolute_path)
    if validator == "additionalProperties":
        return (
            f"{path}: champ non autorise detecte. Les documents de flux interdisent "
            "tout champ inconnu (garde-fou anti-secret : aucun jeton/token/secret "
            "ne peut etre transmis). Retirez le champ signale."
        )
    if validator == "required":
        return f"{path}: champ obligatoire manquant ({err.message})."
    if validator == "const":
        return (
            f"{path}: valeur imposee. {err.message}. "
            "Verifiez schema_version ('1') et kind ('datastream' ou 'report')."
        )
    if validator == "enum":
        return f"{path}: valeur invalide. {err.message}."
    if validator in ("minimum", "maximum"):
        return f"{path}: valeur hors bornes. {err.message}."
    if validator == "type":
        return f"{path}: type invalide. {err.message}."
    if validator == "pattern":
        return f"{path}: format invalide. {err.message}."
    if validator == "minLength":
        return f"{path}: la valeur ne peut pas etre vide."
    return f"{path}: {err.message}"


def validate_flow(doc: dict) -> tuple[bool, list[dict]]:
    """Validate a flow document against its schema. Returns (ok, errors).

    Each error is a dict: {"path": "<json-path>", "message": "<actionable FR>"}.
    The ``kind`` field selects the schema; an unknown/missing kind is itself an
    error (before any schema lookup). Never touches the DB (dry-run safe).
    """
    from jsonschema import Draft202012Validator  # noqa: PLC0415

    if not isinstance(doc, dict):
        return False, [{"path": "/", "message": "Le document doit etre un objet JSON."}]

    kind = doc.get("kind")
    if kind not in ("datastream", "report"):
        return False, [
            {
                "path": "/kind",
                "message": (
                    f"kind est requis et doit valoir 'datastream' ou 'report' (recu: {kind!r})."
                ),
            }
        ]

    schema = _load_schema(kind)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    rendered_errors = [
        {"path": "/" + "/".join(str(p) for p in e.absolute_path), "message": _french_message(e)}
        for e in errors
    ]
    if kind == "datastream" and doc.get("schema_version") == "2" and not errors:
        from core.datastream_intents import (  # noqa: PLC0415
            DatastreamIntentStructuralError,
            normalize_intent,
        )

        try:
            normalize_intent(doc.get("intent"))
        except DatastreamIntentStructuralError as exc:
            rendered_errors.extend(
                {
                    "path": "/intent" + ("" if issue.path == "$" else issue.path[1:]),
                    "message": issue.message,
                }
                for issue in exc.issues
            )
    if not rendered_errors:
        return True, []
    return False, rendered_errors


# ---------------------------------------------------------------------------
# Scope helper (AD-5)
# ---------------------------------------------------------------------------


def _assert_access(
    project_id: str,
    identity: str,
    conn,
    *,
    minimum_role: str | None = None,
) -> None:
    """Raise FlowScopeError (=> 404) + audit 'access_denied' if scope is violated."""
    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_has_project_access,
        identity_has_project_role,
    )

    try:
        allowed = (
            identity_has_project_role(project_id, identity, minimum_role, conn)
            if minimum_role is not None
            else identity_has_project_access(project_id, identity, conn)
        )
    except ProjectAccessUnavailable as exc:
        raise FlowUnavailableError("project access could not be verified") from exc
    if allowed:
        return
    _audit(
        identity,
        _ACTION_ACCESS_DENIED,
        {"resource": "flow", "project_id": project_id},
    )
    raise FlowScopeError(f"Acces refuse au projet {project_id}")


def _audit(identity: str, action: str, metadata: dict) -> None:
    """Write an audit row (best-effort; never raises)."""
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=identity or "anonymous",
            action=action,
            provider_account="",
            connection_ref="",
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - audit never blocks the caller
        logger.warning("flows: audit_write_failed action=%s: %s", action, exc)


# ---------------------------------------------------------------------------
# Datastream <-> flow doc serialisation
# ---------------------------------------------------------------------------


def _fetch_mappings(datastream_id: str, conn) -> list[dict]:
    """Return the mappings array for a datastream (source_field ASC)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_field, target_field, is_key_column
            FROM app.datastream_mappings
            WHERE datastream_id = %s
            ORDER BY source_field ASC
            """,
            (datastream_id,),
        )
        return [
            {"source_field": r[0], "target_field": r[1], "is_key_column": bool(r[2])}
            for r in cur.fetchall()
        ]


def _datastream_row_to_flow(row: dict, mappings: list[dict]) -> dict:
    """Convert a datastreams.py row dict into a flow.datastream document."""
    if row.get("versioned") or row.get("current_plan_version_id"):
        return {
            "schema_version": "2",
            "kind": "datastream",
            "id": row.get("id"),
            "project_id": row.get("project_id"),
            "name": row.get("name"),
            "intent": row.get("intent_payload"),
            "current_plan_version_id": row.get("current_plan_version_id"),
            "current_plan_version": row.get("current_plan_version"),
            "source_kind": row.get("source_kind"),
            "writer_kind": row.get("writer_kind"),
            "destination_policy": row.get("destination_policy"),
            "cadence_mode": row.get("cadence_mode"),
            "validation_state": row.get("validation_state"),
            "next_run_at": row.get("next_run_at"),
            "enabled": bool(row.get("enabled")),
        }
    return {
        "schema_version": "1",
        "kind": "datastream",
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "name": row.get("name"),
        "module_name": row.get("module_name"),
        "connection_ref_id": row.get("connection_ref_id"),
        "report_profile_id": row.get("report_profile_id"),
        "enabled": bool(row.get("enabled")),
        "schedule_mode": row.get("schedule_mode"),
        "refetch_days": row.get("refetch_days"),
        "date_window_days": row.get("date_window_days"),
        "config": row.get("config") or {},
        "mappings": mappings,
    }


def _connection_ref_belongs(connection_ref_id: str, project_id: str, conn) -> bool:
    """Opaque-FK guard: the connection_ref exists AND belongs to the project (AD-3).

    NEVER reads a token -- only the id/project relationship.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.connection_ref WHERE id = %s AND project_id = %s",
            (connection_ref_id, project_id),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Report override <-> flow doc
# ---------------------------------------------------------------------------

# Keys a report override may contribute on top of the base pack (the merge whitelist).
_REPORT_OVERRIDE_KEYS = (
    "display_name",
    "metrics",
    "dimensions",
    "layout",
    "date_window",
    "narrative_prompt",
    "metric_definitions",
    "llm_commentary_guidelines",
    # Story 9.8: optional preferred card template id (e.g. "kpi", "conversions").
    # get_card / get_report honor it; an explicit LLM template arg still overrides.
    # Validated in the flow.report schema; get_card ignores an unknown id with a warning.
    "card_template",
    # Story 10.5: per-project/per-report cannibalisation thresholds (keywords card).
    # Read by cards._fetch_card_config -> _BlockContext.config; defaults 0.20 / 2.0 apply
    # when absent. No platform-wide constants -- always project/report-scoped.
    "cannib_min_share_pct",
    "cannib_min_position_gap",
)


def _fetch_report_override(project_id: str, base_report_id: str, conn) -> dict | None:
    """Return the stored override doc for (project, base_report_id) or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT doc FROM app.report_overrides
            WHERE project_id = %s AND base_report_id = %s
            """,
            (project_id, base_report_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    doc = row[0]
    return doc if isinstance(doc, dict) else json.loads(doc)


def _base_report_doc(base_report_id: str, loaded_modules) -> dict | None:
    """Load the base module report pack as a report flow doc (read-only).

    Uses core.reports.find_report (read-only resolution). reports.py is not
    modified by this story.
    """
    if "/" not in (base_report_id or ""):
        return None
    module_name, _, report_def_id = base_report_id.partition("/")
    from core import reports as reports_module  # noqa: PLC0415

    report = reports_module.find_report(loaded_modules or [], module_name, report_def_id)
    if report is None:
        return None
    base: dict = {
        "schema_version": "1",
        "kind": "report",
        "id": base_report_id,
        "base_report_id": base_report_id,
        "display_name": report.get("display_name"),
        "metrics": report.get("metrics", []),
        "dimensions": report.get("dimensions", []),
        "layout": report.get("layout", {}),
        "date_window": report.get("date_window", {}),
        "narrative_prompt": report.get("narrative_prompt"),
    }
    return base


def _merge_report(
    base: dict | None, override: dict | None, base_report_id: str, project_id: str | None
) -> dict:
    """Merge a base report pack doc with a stored override (override wins per key).

    The merged doc is what Story 8.9 will render. R6 fields (metric_definitions,
    llm_commentary_guidelines) come from the override only (packs do not define them
    yet). dict-valued keys (layout, date_window, metric_definitions) shallow-merge;
    everything else is replaced when present in the override.
    """
    merged: dict = (
        dict(base)
        if base
        else {
            "schema_version": "1",
            "kind": "report",
            "id": base_report_id,
            "base_report_id": base_report_id,
        }
    )
    merged["project_id"] = project_id
    if override:
        for key in _REPORT_OVERRIDE_KEYS:
            if key not in override:
                continue
            val = override[key]
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                combined = dict(merged[key])
                combined.update(val)
                merged[key] = combined
            else:
                merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# Public API (AI-02): cross-module callers must not reach into the underscore-
# prefixed report-read helpers above. These public names are the stable surface;
# the underscore functions remain as the in-module implementation (and as
# backward-compat aliases for existing tests that import them directly).
# ---------------------------------------------------------------------------
base_report_doc = _base_report_doc
fetch_report_override = _fetch_report_override
merge_report = _merge_report


def fetch_report_override_merged(
    project_id: str, base_report_id: str, loaded_modules, conn
) -> dict:
    """Return the base report pack merged with its stored override (one public step).

    Wraps the three-step read (base doc -> override -> merge) so callers
    (get_report in main.py, get_card in cards.py) share ONE public entry point
    instead of chaining three private functions (AI-02). *conn* is a live DB
    connection supplied by the caller so scoping/lifetime stays at the call site.
    """
    base = _base_report_doc(base_report_id, loaded_modules)
    override = _fetch_report_override(project_id, base_report_id, conn)
    return _merge_report(base, override, base_report_id, project_id)


# ---------------------------------------------------------------------------
# Diff (minimal before/after for audit + idempotency)
# ---------------------------------------------------------------------------

# Fields that are server-managed / non-comparable and excluded from the diff.
_DIFF_IGNORE = {"created_at", "updated_at", "created_by", "schema_version", "kind"}


def compute_diff(before: dict | None, after: dict) -> dict:
    """Return {field: {"before": x, "after": y}} for keys that changed.

    Used both for the audit metadata AND for idempotency (empty diff => no-op).
    Comparison is value-equality on the comparable keys (ignores server-managed
    timestamps). ``before`` None means a create (every 'after' key is a change).
    """
    diff: dict = {}
    before = before or {}
    keys = (set(before) | set(after)) - _DIFF_IGNORE
    for key in sorted(keys):
        b = before.get(key)
        a = after.get(key)
        if b != a:
            diff[key] = {"before": b, "after": a}
    return diff


# ---------------------------------------------------------------------------
# Public: list_flows
# ---------------------------------------------------------------------------


def list_flows(project_id: str, identity: str, conn, kind: str | None = None) -> list[dict]:
    """Return flow SUMMARIES for a project (datastreams + report overrides).

    kind filters to one kind when given. AD-5: scope-checked; a violation raises
    FlowScopeError (=> 404). Summaries are lean (no full mappings / merged docs).
    """
    _assert_access(project_id, identity, conn)
    from core import datastreams as ds_module  # noqa: PLC0415

    out: list[dict] = []

    if kind in (None, "datastream"):
        for row in ds_module.list_datastreams(project_id, conn):
            out.append(
                {
                    "kind": "datastream",
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "module_name": row.get("module_name"),
                    "enabled": bool(row.get("enabled")),
                    "connection_ref_id": row.get("connection_ref_id"),
                    "report_profile_id": row.get("report_profile_id"),
                    "source_kind": row.get("source_kind"),
                    "destination_policy": row.get("destination_policy"),
                    "cadence_mode": row.get("cadence_mode"),
                    "validation_state": row.get("validation_state"),
                    "current_plan_version_id": row.get("current_plan_version_id"),
                    "current_plan_version": row.get("current_plan_version"),
                    "next_run_at": row.get("next_run_at"),
                }
            )

    if kind in (None, "report"):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, base_report_id, doc
                FROM app.report_overrides
                WHERE project_id = %s
                ORDER BY base_report_id ASC
                """,
                (project_id,),
            )
            for row in cur.fetchall():
                doc = row[2] if isinstance(row[2], dict) else json.loads(row[2])
                out.append(
                    {
                        "kind": "report",
                        "id": row[1],  # flow id == base_report_id
                        "override_id": row[0],
                        "base_report_id": row[1],
                        "display_name": doc.get("display_name"),
                    }
                )

    return out


# ---------------------------------------------------------------------------
# Public: get_flow
# ---------------------------------------------------------------------------


def get_flow(
    project_id: str,
    kind: str,
    flow_id: str,
    identity: str,
    conn,
    loaded_modules=None,
) -> dict | None:
    """Return the FULL declarative flow document, or None if not found.

    datastream: the datastreams row + its mappings, serialised as a flow doc.
    report:     the base pack MERGED with the stored override (merge at this layer;
                reports.py untouched). Returns None only when neither a base pack
                NOR an override exists for base_report_id=flow_id.

    AD-5: scope-checked (a cross-project id returns None; the datastreams getter is
    itself project-scoped).
    """
    _assert_access(project_id, identity, conn)

    if kind == "datastream":
        from core import datastreams as ds_module  # noqa: PLC0415

        row = ds_module.get_datastream(flow_id, project_id, conn)
        if row is None:
            return None
        mappings = _fetch_mappings(flow_id, conn)
        return _datastream_row_to_flow(row, mappings)

    if kind == "report":
        base = _base_report_doc(flow_id, loaded_modules)
        override = _fetch_report_override(project_id, flow_id, conn)
        if base is None and override is None:
            return None
        return _merge_report(base, override, flow_id, project_id)

    return None


# ---------------------------------------------------------------------------
# Public: upsert_flow
# ---------------------------------------------------------------------------


def upsert_flow(
    project_id: str,
    doc: dict,
    identity: str,
    conn,
    loaded_modules=None,
) -> dict:
    """Validate + upsert a flow document. Returns a result envelope.

    Result shape:
        {"kind", "id", "changed": bool, "flow": <full merged/serialised doc>,
         "diff": {field: {before, after}}}

    Order of operations (guardrails):
      1. validate_flow(doc) -- refuse BEFORE any DB touch (raises FlowValidationError).
      2. AD-5 scope check (raises FlowScopeError => 404 + audit access_denied).
      3. connection_ref_id opaque-FK guard for datastream flows (AD-3).
      4. Read current state -> compute minimal diff. Empty diff => no-op
         (changed:false, no write, no audit).
      5. Apply via the OWNING module functions (single write path), then audit
         'flow_updated' with the diff.
    """
    # 1. Validate-before-write.
    ok, errors = validate_flow(doc)
    if not ok:
        raise FlowValidationError(errors)

    # The doc's own project_id must match the scope it is being written under.
    kind = doc["kind"]
    doc_project = doc.get("project_id")
    if kind == "datastream" and doc_project and doc_project != project_id:
        raise FlowValidationError(
            [
                {
                    "path": "/project_id",
                    "message": (
                        "project_id du document differe du projet cible "
                        f"({doc_project!r} != {project_id!r})."
                    ),
                }
            ]
        )

    # 2. Scope. Versioned intent writes always use the strict role resolver.
    minimum_role = None
    if kind == "datastream" and doc.get("schema_version") == "2":
        destination = doc["intent"]["destination"]
        minimum_role = "owner" if destination["policy"] == "external_read_only" else "member"
    _assert_access(project_id, identity, conn, minimum_role=minimum_role)

    if kind == "datastream":
        return _upsert_datastream(project_id, doc, identity, conn, loaded_modules)
    return _upsert_report(project_id, doc, identity, conn, loaded_modules)


def _upsert_datastream(
    project_id: str, doc: dict, identity: str, conn, loaded_modules=None
) -> dict:
    """Upsert a datastream flow via core.datastreams + core.datamodel (single write path)."""
    from core import datastreams as ds_module  # noqa: PLC0415

    if doc.get("schema_version") == "2":
        return _upsert_versioned_datastream(
            project_id, doc, identity, conn, loaded_modules, ds_module
        )
    # 3. Opaque-FK guard (AD-3): connection_ref must exist + belong to the project.
    connection_ref_id = doc.get("connection_ref_id")
    if connection_ref_id and not _connection_ref_belongs(connection_ref_id, project_id, conn):
        raise FlowValidationError(
            [
                {
                    "path": "/connection_ref_id",
                    "message": (
                        f"connection_ref_id {connection_ref_id!r} est introuvable ou "
                        "n'appartient pas a ce projet."
                    ),
                }
            ]
        )

    flow_id = doc.get("id")
    before_flow: dict | None = None
    if flow_id:
        existing = ds_module.get_datastream(flow_id, project_id, conn)
        if existing is None:
            # An id was supplied but does not resolve in this project -> not found (AD-5).
            raise FlowScopeError(f"Datastream {flow_id} introuvable dans le projet {project_id}")
        before_flow = _datastream_row_to_flow(existing, _fetch_mappings(flow_id, conn))

    # Desired scalar fields (map flow doc -> datastreams row fields).
    scalar = {
        "name": doc.get("name"),
        "module_name": doc.get("module_name"),
        "connection_ref_id": connection_ref_id,
        "report_profile_id": doc.get("report_profile_id"),
        "enabled": doc.get("enabled", True),
        "schedule_mode": doc.get("schedule_mode", "nightly"),
        "refetch_days": doc.get("refetch_days", 3),
        "date_window_days": doc.get("date_window_days", 30),
        "config": doc.get("config") or None,
    }
    desired_mappings = doc.get("mappings", [])

    if flow_id is None:
        # CREATE.
        # Fix [MEDIUM #5]: previously committed the datastream row BEFORE applying
        # mappings, which would orphan an enabled datastream with no mappings on
        # mapping failure.  Now apply mappings BEFORE commit so the whole create
        # is atomic: a mapping error triggers a rollback leaving no orphan.
        try:
            created = ds_module.create_datastream(scalar, project_id, identity, conn)
        except Exception as exc:  # UniqueViolation on (project, name)
            if "unique" in str(exc).lower() or "UniqueViolation" in type(exc).__name__:
                conn.rollback()
                raise FlowConflictError(
                    f"Un flux nomme {scalar['name']!r} existe deja dans ce projet."
                )
            raise
        flow_id = created["id"]
        # Apply mappings BEFORE commit -- if this raises the whole transaction rolls back.
        try:
            _apply_mappings(flow_id, desired_mappings, [], identity, conn)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        after_flow = _datastream_row_to_flow(
            ds_module.get_datastream(flow_id, project_id, conn),
            _fetch_mappings(flow_id, conn),
        )
        diff = compute_diff(None, after_flow)
        _audit(
            identity,
            _ACTION_FLOW_UPDATED,
            {
                "kind": "datastream",
                "id": flow_id,
                "project_id": project_id,
                "op": "create",
                "diff": diff,
            },
        )
        return {
            "kind": "datastream",
            "id": flow_id,
            "changed": True,
            "flow": after_flow,
            "diff": diff,
        }

    # UPDATE. Compute the diff against current state for idempotency.
    desired_after = _datastream_row_to_flow(
        {
            **{
                k: doc.get(k)
                for k in (
                    "id",
                    "project_id",
                    "name",
                    "module_name",
                    "connection_ref_id",
                    "report_profile_id",
                    "enabled",
                    "schedule_mode",
                    "refetch_days",
                    "date_window_days",
                )
            },
            "config": scalar["config"] or {},
        },
        _normalise_mappings(desired_mappings),
    )
    diff = compute_diff(before_flow, desired_after)
    if not diff:
        # Idempotent no-op.
        return {
            "kind": "datastream",
            "id": flow_id,
            "changed": False,
            "flow": before_flow,
            "diff": {},
        }

    ds_module.update_datastream(flow_id, project_id, scalar, conn)
    conn.commit()
    _apply_mappings(flow_id, desired_mappings, before_flow.get("mappings", []), identity, conn)

    after_flow = _datastream_row_to_flow(
        ds_module.get_datastream(flow_id, project_id, conn),
        _fetch_mappings(flow_id, conn),
    )
    final_diff = compute_diff(before_flow, after_flow)
    if not final_diff:
        return {
            "kind": "datastream",
            "id": flow_id,
            "changed": False,
            "flow": after_flow,
            "diff": {},
        }
    _audit(
        identity,
        _ACTION_FLOW_UPDATED,
        {
            "kind": "datastream",
            "id": flow_id,
            "project_id": project_id,
            "op": "update",
            "diff": final_diff,
        },
    )
    return {
        "kind": "datastream",
        "id": flow_id,
        "changed": True,
        "flow": after_flow,
        "diff": final_diff,
    }


def _upsert_versioned_datastream(
    project_id: str,
    doc: dict,
    identity: str,
    conn,
    loaded_modules,
    ds_module,
) -> dict:
    """Route schema-version-2 writes through the immutable intent service."""

    from core.datastream_intents import save_datastream_intent  # noqa: PLC0415

    intent = doc["intent"]
    source = intent["source"]
    capabilities = None
    connection_ref_id = source.get("connection_ref_id")
    if connection_ref_id:
        if not _connection_ref_belongs(connection_ref_id, project_id, conn):
            raise FlowValidationError(
                [
                    {
                        "path": "/intent/source/connection_ref_id",
                        "message": "connection_ref_id est introuvable dans ce projet.",
                    }
                ]
            )
        from core.source_capabilities import (  # noqa: PLC0415
            SourceCapabilitiesNotFound,
            SourceCapabilitiesUnavailable,
            get_scoped_source_capabilities,
        )

        try:
            capabilities = get_scoped_source_capabilities(
                project_id=project_id,
                connection_ref_id=connection_ref_id,
                identity=identity,
                loaded_modules=loaded_modules or [],
                conn=conn,
            )
        except SourceCapabilitiesNotFound as exc:
            raise FlowScopeError("Source introuvable dans ce projet") from exc
        except SourceCapabilitiesUnavailable as exc:
            raise FlowUnavailableError("Catalogue source indisponible") from exc

    flow_id = doc.get("id")
    if flow_id is None:
        shell = ds_module.create_datastream(
            {
                "name": doc["name"],
                "source_kind": source["kind"],
                "module_name": capabilities["module"]["name"] if capabilities else None,
                "connection_ref_id": connection_ref_id,
                "report_profile_id": source.get("report_id"),
                "enabled": False,
            },
            project_id,
            identity,
            conn,
        )
        flow_id = shell["id"]
    elif ds_module.get_datastream(flow_id, project_id, conn) is None:
        raise FlowScopeError(f"Datastream {flow_id} introuvable dans le projet {project_id}")

    from core.datastream_intents import (  # noqa: PLC0415
        DatastreamIntentConflict,
        DatastreamIntentNotFound,
        DatastreamIntentUnavailable,
    )
    from core.geographic_reporting import fetch_project_geographic_posture

    geographic_posture = fetch_project_geographic_posture(project_id, conn)

    try:
        version = save_datastream_intent(
            datastream_id=flow_id,
            project_id=project_id,
            intent=intent,
            identity=identity,
            idempotency_key=doc["idempotency_key"],
            conn=conn,
            loaded_modules=loaded_modules or [],
            capabilities=capabilities,
            geographic_posture=geographic_posture,
            reason=doc.get("reason", "flow_draft_saved"),
            trace_id=doc.get("trace_id"),
        )
    except DatastreamIntentConflict as exc:
        raise FlowConflictError("idempotency_conflict") from exc
    except DatastreamIntentNotFound as exc:
        raise FlowScopeError("Datastream introuvable") from exc
    except DatastreamIntentUnavailable as exc:
        raise FlowUnavailableError("Intention Datastream indisponible") from exc
    row = ds_module.get_datastream(flow_id, project_id, conn)
    after_flow = _datastream_row_to_flow(row, [])
    return {
        "kind": "datastream",
        "id": flow_id,
        "changed": not version.get("idempotent_replay", False),
        "flow": after_flow,
        "diff": {"current_plan_version_id": {"after": version["id"]}},
        "plan_version": version,
    }


def _normalise_mappings(mappings: list[dict]) -> list[dict]:
    """Canonicalise + sort a mappings array for stable diff comparison."""
    norm = [
        {
            "source_field": m.get("source_field"),
            "target_field": m.get("target_field"),
            "is_key_column": bool(m.get("is_key_column", False)),
        }
        for m in (mappings or [])
    ]
    return sorted(norm, key=lambda m: m["source_field"] or "")


def _apply_mappings(
    datastream_id: str,
    desired: list[dict],
    current: list[dict],
    identity: str,
    conn,
) -> None:
    """Apply the desired mappings array via datamodel.upsert_mapping (single write path).

    Fix [HIGH #4]: previously only upserted -- dropping a mapping from the desired list
    was a silent no-op.  Now performs a DELETE pass first for source_fields that exist in
    current but not in desired, then upserts changed/new mappings.  This makes PUT
    behaviour correct and ensures the diff reflects removals.
    """
    from core import datamodel as dm_module  # noqa: PLC0415

    desired_srcs: set[str] = {m["source_field"] for m in (desired or []) if m.get("source_field")}
    current_by_src = {m["source_field"]: m.get("target_field") for m in current}

    # --- Delete pass: remove mappings that are no longer desired ---
    srcs_to_delete = [src for src in current_by_src if src not in desired_srcs]
    if srcs_to_delete:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM app.datastream_mappings
                WHERE datastream_id = %s
                  AND source_field = ANY(%s)
                """,
                (datastream_id, srcs_to_delete),
            )
        logger.debug("flows: mappings_deleted ds=%s removed=%s", datastream_id, srcs_to_delete)

    # --- Upsert pass: add/update mappings whose target_field changed ---
    for m in desired or []:
        src = m.get("source_field")
        if not src:
            continue
        tgt = m.get("target_field")
        if src in current_by_src and current_by_src[src] == tgt:
            continue  # unchanged
        dm_module.upsert_mapping(
            datastream_id=datastream_id,
            source_field=src,
            target_field_name=tgt,
            identity=identity,
            conn=conn,
        )


def _upsert_report(
    project_id: str,
    doc: dict,
    identity: str,
    conn,
    loaded_modules,
) -> dict:
    """Upsert a report-override flow into app.report_overrides (additive table).

    The stored doc holds only the override contribution; get_flow merges it over
    the base pack. Idempotent: an unchanged override writes nothing.
    """
    from ulid import ULID  # noqa: PLC0415

    base_report_id = doc["base_report_id"]
    before_override = _fetch_report_override(project_id, base_report_id, conn)

    # The override contribution = the doc's override keys (skip identity/base fields).
    override_doc = {
        "schema_version": "1",
        "kind": "report",
        "id": doc.get("id") or base_report_id,
        "base_report_id": base_report_id,
        "project_id": project_id,
    }
    for key in _REPORT_OVERRIDE_KEYS:
        if key in doc:
            override_doc[key] = doc[key]

    diff = compute_diff(before_override, override_doc)
    if not diff:
        merged = _merge_report(
            _base_report_doc(base_report_id, loaded_modules),
            before_override,
            base_report_id,
            project_id,
        )
        return {
            "kind": "report",
            "id": base_report_id,
            "changed": False,
            "flow": merged,
            "diff": {},
        }

    doc_json = json.dumps(override_doc)
    with conn.cursor() as cur:
        if before_override is None:
            override_id = f"rovr_{ULID()}"
            cur.execute(
                """
                INSERT INTO app.report_overrides
                    (id, project_id, base_report_id, doc, created_by)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (override_id, project_id, base_report_id, doc_json, identity or "system"),
            )
        else:
            cur.execute(
                """
                UPDATE app.report_overrides
                SET doc = %s::jsonb
                WHERE project_id = %s AND base_report_id = %s
                """,
                (doc_json, project_id, base_report_id),
            )
    conn.commit()

    _audit(
        identity,
        _ACTION_FLOW_UPDATED,
        {
            "kind": "report",
            "id": base_report_id,
            "project_id": project_id,
            "op": "create" if before_override is None else "update",
            "diff": diff,
        },
    )

    merged = _merge_report(
        _base_report_doc(base_report_id, loaded_modules),
        override_doc,
        base_report_id,
        project_id,
    )
    return {"kind": "report", "id": base_report_id, "changed": True, "flow": merged, "diff": diff}
