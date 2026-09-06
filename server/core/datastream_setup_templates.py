"""Story 57.7 -- a validated setup, saved once and reopened for another account.

Two halves, and only the first one is new:

  * SAVING derives a reusable payload from the revision that was ACTUALLY
    materialized, seals it with the same `canonical_hash` the revisions use, and
    refuses the eleventh template of an organization by name.
  * APPLYING adds nothing. `PATCH /api/projects/{id}/datastream-setup-drafts/{id}`
    already accepts a whole `operator_input` (`datastream_preconfiguration.update_draft`),
    which is exactly what a template holds -- so a template is a PATCH, never a
    translation. A translator between two shapes would be a second home for a
    server rule, and 57.9 already refused one.

WHAT A TEMPLATE IS NOT: a semantic registry. It carries references and
selections, no definition and no hand-written label. See migration 216 for the
measurement that decided the table.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.audit import declare_action
from core.datastream_preconfiguration import (
    _OPERATOR_SOURCE_KEYS,
    _validate_operator_union,
    canonical_hash,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Elles passaient par le wrapper local `_audit`, donc
# le releve des appelants directs de `write_audit_row` ne les voyait pas :
# un ecrivain INDIRECT est un ecrivain. Le garde de conformance suit
# maintenant les wrappers qui transmettent `action`.
ACTION_DATASTREAM_SETUP_TEMPLATE_RETIRED = declare_action("datastream.setup_template.retired")
ACTION_DATASTREAM_SETUP_TEMPLATE_SAVED = declare_action("datastream.setup_template.saved")


# QUELQUES TEMPLATES, PAS UNE BIBLIOTHEQUE. The plan's words, and the refusal is
# real: an organization at the limit is told which ones to retire rather than
# quietly growing a catalogue. Read by the server (the refusal) and projected on
# screen (`{n} of 10 templates`), so the limit is legible before the click.
MAX_ORG_SETUP_TEMPLATES = 10

# THE MEASURED SCOPE OF EVERY SOURCE REFERENCE. The open variables are DERIVED
# from this table, never hand-listed: a reference whose scope does not reach
# across the organization cannot travel with the template, so it is removed and
# reopened. A hand-written list would be stale at the first schema change.
#
#   organization  `cr.owner_org_id = p.org_id`, or an active
#                 `credential_account_grants` row
#                 (datastream_setup_observations.py:1425-1432, data_surface.py:120-130)
#   deployment    connector installation is per ENVIRONMENT, not per project
#                 (082_connector_installation_state.sql:61-73)
#   declaration   not a reference at all -- a value the operator typed or ticked
#   project       `app.file_source_templates.project_id NOT NULL`
#                 (097_file_source_templates.sql:46,:77-78)
#   draft         addressed `(id, draft_id, project_id)`; meaningless elsewhere
#                 (datastream_setup_observations.py:515-520, :1465-1469)
SOURCE_REFERENCE_SCOPE: dict[str, str] = {
    "source_account_ref": "organization",
    "connector_ref": "deployment",
    "connector_contract_version_ref": "deployment",
    "report_ref": "deployment",
    "access_ref": "organization",
    "object_ref": "declaration",
    "declared_writer": "declaration",
    "readonly_acknowledged": "declaration",
    "channel": "declaration",
    "channel_contract": "declaration",
    "sheet_ref": "declaration",
    "template_ref": "project",
    "staged_asset_ref": "draft",
    "observation_ref": "draft",
}
_TRAVELS = {"organization", "deployment", "declaration"}

# THE ONE VARIABLE THE GESTURE ITSELF OPENS. Its scope travels -- a Source
# Account is reachable organization-wide -- so it is reopened by PURPOSE, not by
# scope: duplicating a Datastream for another account is the whole point.
#
# The country is NOT a second one, and that is measured: 0 of 133 report
# profiles declares a country/geo/market/region filter, 5 of 39 connectors carry
# a `country` dimension at all, and the geographic posture is read on the
# PROJECT (`compile_geographic_intent`, `datastream_intents.py:229-320`;
# `fetch_country_registry(conn, *, project_id)`, `country_registry.py:571-573`).
# No operator-input key names a country in any mode, so there is none to reopen.
DUPLICATED_VARIABLE = "source_account_ref"

# Keys of the common union that describe the DRAFT, not the configuration.
# `wizard_state` is a position in one session; `observation_ref` addresses
# evidence of one draft. Both are removed and neither is asked again: discovery
# regathers the evidence, and the wizard opens where a new draft opens.
_DRAFT_ONLY_COMMON_KEYS = ("observation_ref", "wizard_state")


class TemplateValidationError(ValueError):
    code = "invalid_setup_template"


class TemplateNotFound(LookupError):
    code = "not_found"


class TemplateConflict(RuntimeError):
    code = "setup_template_conflict"


class TemplateLimitReached(TemplateConflict):
    code = "setup_template_limit_reached"


class TemplateConflictLabel(TemplateConflict):
    code = "duplicate_label"


class TemplateConflictDuplicate(TemplateConflict):
    code = "duplicate_configuration"


class NoRecordedOperatorInput(TemplateValidationError):
    code = "no_recorded_operator_input"


def derive_template_payload(
    operator_input: dict[str, Any], *, mode: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Return the reusable payload and the variables it deliberately leaves open.

    Pure: no database, no clock, no network. What comes out is a
    `normalized_operator_input` minus what cannot travel, so re-injecting the
    account produces something `update_draft` accepts unchanged.
    """
    if not isinstance(operator_input, dict):
        raise TemplateValidationError("operator_input must be an object")
    resolved = str(mode or operator_input.get("mode") or "")
    if resolved not in _OPERATOR_SOURCE_KEYS:
        raise TemplateValidationError("operator_input mode is unsupported")

    payload = deepcopy(operator_input)
    payload["mode"] = resolved
    for key in _DRAFT_ONLY_COMMON_KEYS:
        payload.pop(key, None)

    source = payload.get("source")
    if source is not None and not isinstance(source, dict):
        raise TemplateValidationError("source must be an object")
    open_variables: list[str] = []
    if isinstance(source, dict):
        for key in sorted(set(source) & _OPERATOR_SOURCE_KEYS[resolved]):
            scope = SOURCE_REFERENCE_SCOPE.get(key, "declaration")
            if scope in _TRAVELS and key != DUPLICATED_VARIABLE:
                continue
            source.pop(key, None)
            # A draft-scoped reference is EVIDENCE, regathered by discovery --
            # it is not a question anyone is asked, so it is not declared open.
            if scope != "draft":
                open_variables.append(key)
        payload["source"] = source
    # The union check is the same one the applying route runs, so a payload that
    # cannot be re-applied never becomes a template.
    _validate_operator_union(payload)
    return payload, sorted(open_variables)


def _origin_revision(conn, *, project_id: str, datastream_id: str) -> tuple[str, dict[str, Any]]:
    """The revision that was MATERIALIZED, and its operator input.

    NOT `app.datastream_setup_drafts.current_revision_id`. Revisions are
    append-only, so a draft's current revision can be later than the one that
    was validated and created -- a template derived from it would carry a
    configuration nobody ever confirmed. `app.datastream_setup_materializations`
    holds no revision column (measured 2026-08-05), so the exact revision is
    named through the evidence chain it does hold:
    materialization -> final review -> preview -> draft revision.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id, r.normalized_operator_input "
            "FROM app.datastream_setup_materializations m "
            "JOIN app.datastream_setup_final_reviews f "
            "  ON f.id = m.final_review_id AND f.draft_id = m.draft_id "
            " AND f.project_id = m.project_id "
            "JOIN app.datastream_setup_previews p "
            "  ON p.id = f.preview_id AND p.draft_id = f.draft_id "
            " AND p.project_id = f.project_id "
            "JOIN app.datastream_setup_draft_revisions r "
            "  ON r.id = p.draft_revision_id AND r.draft_id = p.draft_id "
            " AND r.project_id = p.project_id "
            "WHERE m.project_id = %s AND m.datastream_id = %s",
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    if row is None:
        raise NoRecordedOperatorInput(
            "This Datastream was not created by the setup wizard, so no reusable "
            "operator input was ever recorded."
        )
    value = row[1]
    if isinstance(value, str):
        import json  # noqa: PLC0415

        value = json.loads(value)
    if not isinstance(value, dict) or not value.get("mode"):
        raise NoRecordedOperatorInput(
            "The recorded operator input of this Datastream names no source mode, "
            "so nothing reusable can be derived from it."
        )
    return str(row[0]), value


def _org_of_project(conn, *, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None or not row[0]:
        raise TemplateNotFound("Project not found")
    return str(row[0])


_TEMPLATE_COLUMNS = (
    "id, label, org_id, origin_project_id, origin_datastream_id, mode, connector_ref, "
    "report_ref, origin_contract_version_ref, origin_source_account_ref, template_payload, "
    "open_variables, content_hash, created_by, created_at"
)


def _template_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    import json  # noqa: PLC0415

    def _json(value: Any, fallback: Any) -> Any:
        if isinstance(value, str):
            return json.loads(value)
        return fallback if value is None else value

    return {
        "template_ref": row[0],
        "label": row[1],
        "org_ref": row[2],
        "origin_project_ref": row[3],
        "origin_datastream_ref": row[4],
        "mode": row[5],
        "connector_ref": row[6],
        "report_ref": row[7],
        "origin_contract_version_ref": row[8],
        "origin_source_account_ref": row[9],
        "operator_input": _json(row[10], {}),
        "open_variables": _json(row[11], []),
        "content_hash": row[12],
        "created_by": row[13],
        "created_at": row[14].isoformat() if hasattr(row[14], "isoformat") else str(row[14]),
    }


def _active_labels(conn, *, org_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label FROM app.datastream_setup_templates "
            "WHERE org_id = %s AND is_active ORDER BY created_at",
            (org_id,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def _audit(conn, *, actor: str, action: str, org_id: str, template_id: str, content_hash: str):
    from core.audit import insert_audit_row  # noqa: PLC0415

    insert_audit_row(
        conn,
        identity=actor,
        action=action,
        provider_account="datastream_setup_templates",
        connection_ref="",
        metadata={"org_id": org_id, "template_id": template_id, "content_hash": content_hash},
    )


def save_template(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    label: str,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Save the configuration a Datastream was materialized from, under a name."""
    from core.data_identities import mint_data_id  # noqa: PLC0415
    from core.datastream_preconfiguration import _canonical  # noqa: PLC0415

    clean = (label or "").strip()
    if not 1 <= len(clean) <= 80:
        raise TemplateValidationError("A template name is 1 to 80 characters")
    org_id = _org_of_project(conn, project_id=project_id)
    key_hash = canonical_hash(idempotency_key)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM app.datastream_setup_templates "
            "WHERE org_id = %s AND idempotency_key_hash = %s",
            (org_id, key_hash),
        )
        replay = cur.fetchone()
    if replay:
        result = _template_payload(replay)
        result["idempotent_replay"] = True
        return result

    # THE LIMIT IS CHECKED BEFORE THE ORIGIN IS READ. An organization at ten is
    # refused whatever Datastream is offered, so making the refusal depend on
    # the evidence chain resolving would answer the wrong question first.
    labels = _active_labels(conn, org_id=org_id)
    if len(labels) >= MAX_ORG_SETUP_TEMPLATES:
        raise TemplateLimitReached(
            f"This organization already holds {len(labels)} saved configurations, "
            f"which is the limit of {MAX_ORG_SETUP_TEMPLATES}. Retire one of "
            f"{', '.join(labels)} before saving another."
        )
    if clean in labels:
        raise TemplateConflictLabel(f"A template named {clean} already exists in this organization")

    revision_id, operator_input = _origin_revision(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    payload, open_variables = derive_template_payload(operator_input)
    source = operator_input.get("source") or {}
    content_hash = canonical_hash(payload)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT label FROM app.datastream_setup_templates "
            "WHERE org_id = %s AND content_hash = %s AND is_active",
            (org_id, content_hash),
        )
        same = cur.fetchone()
        if same:
            raise TemplateConflictDuplicate(
                f"This exact configuration is already saved as {same[0]}"
            )
        template_id = mint_data_id("dst")
        cur.execute(
            "INSERT INTO app.datastream_setup_templates (id,org_id,origin_project_id,"
            "origin_datastream_id,origin_draft_revision_id,label,mode,connector_ref,report_ref,"
            "origin_contract_version_ref,origin_source_account_ref,template_payload,"
            "open_variables,content_hash,idempotency_key_hash,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
            (
                template_id,
                org_id,
                project_id,
                datastream_id,
                revision_id,
                clean,
                payload["mode"],
                source.get("connector_ref") or None,
                source.get("report_ref") or None,
                source.get("connector_contract_version_ref") or None,
                source.get(DUPLICATED_VARIABLE) or None,
                _canonical(payload),
                _canonical(open_variables),
                content_hash,
                key_hash,
                actor,
            ),
        )
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_SETUP_TEMPLATE_SAVED,
            org_id=org_id,
            template_id=template_id,
            content_hash=content_hash,
        )
        cur.execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM app.datastream_setup_templates WHERE id = %s",
            (template_id,),
        )
        result = _template_payload(cur.fetchone())
    result["idempotent_replay"] = False
    return result


def list_templates(conn, *, project_id: str) -> dict[str, Any]:
    """The ACTIVE templates of this project's organization, with count and limit.

    Scoped by organization and not by project, which is the whole gesture: the
    twin of a Datastream is often built in another project of the same client.
    """
    org_id = _org_of_project(conn, project_id=project_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM app.datastream_setup_templates "
            "WHERE org_id = %s AND is_active ORDER BY created_at DESC",
            (org_id,),
        )
        templates = [_template_payload(row) for row in cur.fetchall()]
    return {
        "templates": templates,
        "count": len(templates),
        "limit": MAX_ORG_SETUP_TEMPLATES,
    }


def retire_template(conn, *, project_id: str, template_ref: str, actor: str) -> dict[str, Any]:
    """Logical retirement. The row keeps its provenance and its content hash."""
    org_id = _org_of_project(conn, project_id=project_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_setup_templates "
            "SET is_active = FALSE, retired_at = NOW(), retired_by = %s "
            "WHERE id = %s AND org_id = %s AND is_active",
            (actor, template_ref, org_id),
        )
        if cur.rowcount == 0:
            raise TemplateNotFound("Template not found")
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_SETUP_TEMPLATE_RETIRED,
            org_id=org_id,
            template_id=template_ref,
            content_hash="",
        )
    return {"template_ref": template_ref, "is_active": False}
