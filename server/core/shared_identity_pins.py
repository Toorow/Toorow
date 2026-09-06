"""Pin a Datastream column to a canonical field, through the Mapping tab's own change engine.

WHY THIS MODULE EXISTS (governance.md, amendment of 2026-09-05). On the reference
project every pin of a column to a canonical field had been made by a harness
script through the console routes: an agent at the MCP door could publish a View
that crosses two flows and could not make them crossable. Jean, 2026-09-05: « tu
dois pouvoir avec des commandes MCP adapter les références… demain quand j'aurai
besoin de mapper des variables x et x' tu seras incapable de le faire ».

ONE PATH. A pin is a governed mapping change on the version in force: the same
`prepare_change` -> `confirm_change` pair the Mapping tab plays, so a change that
moves only bindings is published as an overlay and re-pulls nothing, and a
change that would touch a column stays a candidate. Nothing here writes a
mapping version, an execution or a pointer by itself. The confirmation secret
`prepare_change` mints is consumed here, inside the caller's transaction, and
never returned: the result carries what the confirmation answered, minus it.

The caller owns the transaction and the access decision (`governance_mcp`
resolves `edit` on the Project before calling; the console's equivalent is the
workbench route's own guard).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.canonical_field_registry import list_visible_canonical_fields
from core.datastream_change import DatastreamChangeError, confirm_change, prepare_change

__all__ = ["PIN_REASON", "SharedIdentityRefused", "pin_shared_identity"]

PIN_REASON = "names the canonical field the Project's flows share (shared identities)"
_MEASURE_ROLE_PREFIX = "measure"
_IMPLEMENTING = frozenset({"confirmed", "resolved"})


class SharedIdentityRefused(ValueError):
    """A refusal with a code a caller can act on, and a sentence a person reads."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_field(conn, *, project_id: str, canonical_field_id: str) -> dict[str, Any]:
    for field in list_visible_canonical_fields(conn, project_id=project_id):
        if str(field.get("id")) == canonical_field_id:
            return field
    raise SharedIdentityRefused(
        "unknown_canonical_field",
        "No active canonical field of this Project or of the platform has that id: "
        "read the proposal again, or declare the field first.",
    )


def _version_in_force(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.name, d.current_mapping_version_id, m.mapping_payload,
                   m.source_schema_hash, m.plan_version_id
              FROM app.datastreams d
              LEFT JOIN app.datastream_mapping_versions m
                     ON m.id = d.current_mapping_version_id
             WHERE d.id = %s AND d.project_id = %s AND d.archived_at IS NULL
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if not row:
        raise SharedIdentityRefused(
            "unknown_datastream", "That Datastream is not one of this Project's: pick it from the proposal."
        )
    name, version_id, payload, schema_hash, plan_version_id = row
    if not version_id or not isinstance(payload, dict):
        raise SharedIdentityRefused(
            "no_mapping_in_force",
            f"`{name}` publishes no mapping version yet: publish its mapping first, then pin.",
        )
    return {
        "name": str(name),
        "mapping_version_id": str(version_id),
        "payload": payload,
        "source_schema_hash": schema_hash or payload.get("source_schema_hash"),
        "plan_version_id": plan_version_id,
    }


def _carrier(entry: Any) -> tuple[str, str]:
    if not isinstance(entry, Mapping):
        raise SharedIdentityRefused("malformed_carrier", "Each carrier is {datastream_id, column}.")
    datastream_id = str(entry.get("datastream_id") or "").strip()
    column = str(entry.get("column") or "").strip()
    if not datastream_id or not column:
        raise SharedIdentityRefused("malformed_carrier", "Each carrier names a datastream_id and a column.")
    return datastream_id, column


def pin_shared_identity(
    conn,
    *,
    project_id: str,
    canonical_field_id: str,
    carriers: Sequence[Any],
    actor: str,
    idempotency_key: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Pin `column` of each carrier Datastream to *canonical_field_id*, one governed change per flow.

    Returns, per flow, what the change engine answered: `pinned` (an overlay or a
    candidate was minted -- `overlay_execution_id` / `candidate_execution_id` and
    the engine's `no_candidate_reason` say which), `already_pinned` (the version in
    force already names that field), or `refused` with the engine's code and
    sentence. A refusal on one flow does not stop the others: the caller asked
    for N pins and reads N answers. Nothing about a confirmation secret is
    returned.
    """
    canonical_field_id = str(canonical_field_id or "").strip()
    if not canonical_field_id:
        raise SharedIdentityRefused("missing_canonical_field", "canonical_field_id is required.")
    if not isinstance(carriers, (list, tuple)) or not carriers:
        raise SharedIdentityRefused("missing_carriers", "At least one carrier {datastream_id, column} is required.")
    field = _canonical_field(conn, project_id=project_id, canonical_field_id=canonical_field_id)
    field_kind = str(field.get("concept_kind") or "")
    flows: list[dict[str, Any]] = []
    for entry in carriers:
        datastream_id, column = _carrier(entry)
        answer: dict[str, Any] = {"datastream_id": datastream_id, "column": column}
        try:
            version = _version_in_force(conn, project_id=project_id, datastream_id=datastream_id)
            answer["datastream_name"] = version["name"]
            payload = version["payload"]
            fields, target, changed = [], None, False
            for raw in payload.get("fields") or []:
                if not isinstance(raw, dict):
                    continue
                current = dict(raw)
                if str(current.get("field_id") or "") == column:
                    target = current
                    suggestion = current.get("suggestion") if isinstance(current.get("suggestion"), dict) else {}
                    is_measure = str(suggestion.get("semantic_role") or "").startswith(_MEASURE_ROLE_PREFIX)
                    if (field_kind == "metric") != is_measure:
                        raise SharedIdentityRefused(
                            "role_mismatch",
                            f"`{column}` is a {'measure' if is_measure else 'dimension'} of `{version['name']}` and "
                            f"`{field.get('canonical_name')}` is a canonical {field_kind}: pick a field of the same role.",
                        )
                    binding = dict(current.get("binding") or {})
                    if binding.get("mdm_target") == canonical_field_id and binding.get("status") in _IMPLEMENTING:
                        answer.update(outcome="already_pinned", mapping_version_id=version["mapping_version_id"])
                    else:
                        binding.update(
                            {
                                "mdm_target": canonical_field_id,
                                "status": "confirmed",
                                "confirmed_by": actor,
                                "confirmed_reason": reason or PIN_REASON,
                                "blocking_reason": None,
                            }
                        )
                        current["binding"] = binding
                        changed = True
                fields.append(current)
            if target is None:
                raise SharedIdentityRefused(
                    "unknown_column",
                    f"`{version['name']}` carries no column `{column}` in its mapping in force: read the proposal again.",
                )
            if not changed:
                flows.append(answer)
                continue
            proposed = {
                "mapping_contract_version": payload.get("mapping_contract_version") or "1",
                "source_schema_hash": version["source_schema_hash"],
                "plan_version_id": version["plan_version_id"],
                "grain": payload.get("grain") or [],
                "fields": fields,
                "ambiguities": payload.get("ambiguities") or [],
            }
            prepared = prepare_change(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                kind="mapping",
                proposed_payload=proposed,
                actor=actor,
                idempotency_key=f"{idempotency_key}:{datastream_id}",
            )
            confirmed = confirm_change(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                preparation_id=prepared["preparation_id"],
                confirmation_secret=prepared["confirmation_secret"],
                actor=actor,
            )
            answer["outcome"] = "pinned"
            for key in (
                "mapping_version_id",
                "overlay_execution_id",
                "overlay_of",
                "candidate_execution_id",
                "no_candidate_reason",
                "publication_log_id",
            ):
                if key in (confirmed or {}):
                    answer[key] = confirmed[key]
        except SharedIdentityRefused as exc:
            answer.update(outcome="refused", code=exc.code, message=exc.message)
        except DatastreamChangeError as exc:
            answer.update(outcome="refused", code=str(getattr(exc, "code", "refused")), message=str(exc))
        flows.append(answer)
    return {
        "canonical_field_id": canonical_field_id,
        "canonical_name": field.get("canonical_name"),
        "concept_kind": field_kind,
        "flows": flows,
        "pinned": sum(1 for f in flows if f.get("outcome") == "pinned"),
        "already_pinned": sum(1 for f in flows if f.get("outcome") == "already_pinned"),
        "refused": sum(1 for f in flows if f.get("outcome") == "refused"),
    }
