"""toorow -- Import template catalog and inbound Datastream creation (Story 38.6).

Provides:
  get_template(conn, code, version)        -> dict | None
  latest_version(conn, code)               -> int | None
  list_templates(conn)                     -> list[dict]
  create_inbound_datastream(conn, *, ...)  -> dict

Design invariants:
  - AC3: template catalog rows are IMMUTABLE (enforced by the DB trigger
    protect_import_template; this module never issues UPDATE or DELETE on
    app.import_templates). A new version is a new row.
  - AC5 (no second ledger): Datastream binding lives EXCLUSIVELY in
    app.datastreams.config via the Epic 12 create_datastream seam. This
    module calls datastreams.create_datastream(source_kind='managed_feed')
    and pins {template_code, template_version, channels, publishable} in
    config. No second datastream table is created here.
  - AC4 (generic draft): a GENERIC_TABULAR_V1 Datastream is created with
    enabled=False and config.publishable=False. The publish gate is lifted
    only after required canonical semantics are confirmed (later mapping story).
  - AD-2: source-agnostic throughout. No provider/vendor vocabulary in code,
    comments, or docstrings. The template_code is opaque data from the catalog.
  - Operations are run through execute_operation (audit + outbox, idempotent
    replay). No parallel audit path.

ASCII-only stdout (AI-03). No private framework attributes (AI-02). Lazy imports
inside function bodies (no import cycle with core.main).
"""

from __future__ import annotations

import logging
from typing import Any

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_INBOUND_DATASTREAM_CREATED = declare_action("inbound.datastream.created")


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid inbound channels (source-agnostic vocabulary).
# ---------------------------------------------------------------------------

VALID_CHANNELS: frozenset[str] = frozenset({"email", "webhook", "upload"})

# Code reserved for the discover-first generic fallback.
_GENERIC_CODE = "GENERIC_TABULAR_V1"

# Audit constant (mirrors core.audit convention: "<domain>.<noun>.<verb>").
ACTION_INBOUND_DATASTREAM_CREATED = "inbound.datastream.created"


# ---------------------------------------------------------------------------
# Custom exceptions.
# ---------------------------------------------------------------------------


class TemplateNotFound(ValueError):
    """Template code/version does not exist in the catalog (fail closed)."""


class TemplateChannelError(ValueError):
    """channels argument is invalid (empty, non-subset, or wrong type)."""


class ConnectorActivationRequired(RuntimeError):
    """The organization has no ACTIVE activation for this connector."""


# ---------------------------------------------------------------------------
# Read helpers (catalog -- reference data, never mutated here).
# ---------------------------------------------------------------------------


def _row_to_template(cols: list[str], row: tuple) -> dict[str, Any]:
    """Serialize one import_templates row to a dict.

    Safe read-model: template_code, version, title, contract, is_generic,
    created_at (ISO string). No secret or cross-tenant detail.
    """
    out: dict[str, Any] = {}
    for col, val in zip(cols, row):
        if col == "created_at" and val is not None:
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


def get_template(conn, code: str, version: int) -> dict[str, Any] | None:
    """Return one template row by (code, version), or None.

    Reads app.import_templates. Never raises on a missing row -- callers
    that need a hard failure should check the return value.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT template_code, version, title, contract, is_generic, created_at
            FROM app.import_templates
            WHERE template_code = %s AND version = %s
            """,
            (code, version),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cols = ["template_code", "version", "title", "contract", "is_generic", "created_at"]
    return _row_to_template(cols, row)


def latest_version(conn, code: str) -> int | None:
    """Return the highest version number for template_code, or None.

    Returns None when the code does not exist in the catalog at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(version)
            FROM app.import_templates
            WHERE template_code = %s
            """,
            (code,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def list_templates(conn) -> list[dict[str, Any]]:
    """Return all templates, ordered by template_code ASC, version ASC.

    Safe read-model: each entry exposes template_code, version, title,
    required_fields (from contract), optional_fields (from contract),
    is_generic, created_at. No raw contract dump -- only the safe catalog
    projection suitable for the REST read-model (no secrets).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT template_code, version, title, contract, is_generic, created_at
            FROM app.import_templates
            ORDER BY template_code ASC, version ASC
            """,
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    result = []
    for row in rows:
        raw = _row_to_template(cols, row)
        contract = raw.get("contract") or {}
        result.append(
            {
                "template_code": raw["template_code"],
                "version": raw["version"],
                "title": raw["title"],
                "required_fields": contract.get("required_fields", []),
                "optional_fields": contract.get("optional_fields", []),
                "identity_keys": contract.get("identity_keys", []),
                "grain": contract.get("grain"),
                "is_generic": raw["is_generic"],
                "created_at": raw["created_at"],
            }
        )
    return result


# ---------------------------------------------------------------------------
# create_inbound_datastream -- the single write path (AC5).
# ---------------------------------------------------------------------------


def create_inbound_datastream(
    conn,
    *,
    project_id: str,
    name: str,
    connector_name: str,
    template_code: str,
    template_version: int,
    channels: list[str],
    created_by: str,
    org_id: str,
    idempotency_key: str,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Create one inbound managed-feed Datastream bound to a template + channels.

    Validates the template (AC3: pinned version; AC4: generic -> draft),
    validates channels (subset of {email, webhook, upload}, non-empty), then
    calls the Epic 12 ``datastreams.create_datastream`` seam with
    source_kind='managed_feed' and a config that pins:
      {template_code, template_version, channels, publishable}.

    AC5 (no second ledger): this function NEVER inserts into a second table.
    The ONLY write path is ``datastreams.create_datastream``.

    AC4 (generic fallback): when template_code == GENERIC_TABULAR_V1 (or
    is_generic is TRUE), the Datastream is created with enabled=False and
    config.publishable=False. The publish gate is lifted only when required
    canonical semantics are confirmed in the later mapping story.

    Returns the created datastream dict (from Epic 12 read-model).

    Raises:
      TemplateNotFound: the (template_code, template_version) pair does not
        exist in the catalog. Fails closed -- no Datastream is created.
      TemplateChannelError: channels is empty, contains unknown values, or
        is not a list/set.
    """
    # ------------------------------------------------------------------
    # Input validation: normalize every operation identity before hashing.
    # ------------------------------------------------------------------
    for field, value in (
        ("project_id", project_id),
        ("name", name),
        ("connector_name", connector_name),
        ("template_code", template_code),
        ("created_by", created_by),
        ("org_id", org_id),
        ("idempotency_key", idempotency_key),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
    if len(idempotency_key.strip()) > 255:
        raise ValueError("idempotency_key is too long")
    if (
        isinstance(template_version, bool)
        or not isinstance(template_version, int)
        or template_version < 1
    ):
        raise ValueError("template_version must be a positive integer")
    project_id = project_id.strip()
    name = name.strip()
    connector_name = connector_name.strip()
    template_code = template_code.strip()
    created_by = created_by.strip()
    org_id = org_id.strip()
    idempotency_key = idempotency_key.strip()

    tpl = get_template(conn, template_code, template_version)
    if tpl is None:
        raise TemplateNotFound(
            f"template '{template_code}' version {template_version} not found in catalog"
        )

    # ------------------------------------------------------------------
    # Input validation: channels must be non-empty subset of VALID_CHANNELS.
    # ------------------------------------------------------------------
    if not channels or not isinstance(channels, (list, set, tuple)):
        raise TemplateChannelError("channels must be a non-empty list")
    if any(not isinstance(ch, str) or not ch.strip() for ch in channels):
        raise TemplateChannelError("channels must contain non-empty strings")
    channel_set = {ch.strip().lower() for ch in channels}
    unknown = channel_set - VALID_CHANNELS
    if unknown:
        raise TemplateChannelError(
            f"unknown channels {sorted(unknown)}; valid values are {sorted(VALID_CHANNELS)}"
        )

    # ------------------------------------------------------------------
    # AC4: generic template -> draft (publishable=False, enabled=False).
    # ------------------------------------------------------------------
    is_generic = bool(tpl.get("is_generic"))
    publishable = not is_generic
    enabled = not is_generic

    # ------------------------------------------------------------------
    # Build the config dict that pins the template binding on the Datastream.
    # This is the SOLE binding record (AC5: no second ledger).
    # ------------------------------------------------------------------
    ds_config: dict[str, Any] = {
        "connector_name": connector_name,
        "template_code": template_code,
        "template_version": template_version,
        "channels": sorted(channel_set),
        "publishable": publishable,
    }
    if is_generic:
        ds_config["publish_gate"] = "canonical_semantics_required"

    # ------------------------------------------------------------------
    # Write: route through execute_operation for audit + outbox + idempotency.
    # ------------------------------------------------------------------
    import core.datastreams as _datastreams_mod  # noqa: PLC0415
    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    spec = OperationSpec(
        command_type=ACTION_INBOUND_DATASTREAM_CREATED,
        actor=created_by,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"connector:{connector_name}",
            f"template:{template_code}:{template_version}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "inbound-datastream-v1",
            "catalog": "import-template-registry-v1",
            "tool": "rest-v1",
        },
        # Deterministic over business inputs only (no row id in payload).
        # Row id is minted inside _epic12_create at write time.
        request_payload={
            "connector_name": connector_name,
            "template_code": template_code,
            "template_version": template_version,
            "channels": sorted(channel_set),
            "publishable": publishable,
            "project_id": project_id,
            "name": name,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"inbound-datastream:{org_id}:{project_id}:{connector_name}:"
            f"{template_code}:{template_version}"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        import os  # noqa: PLC0415

        from core.connector_activation import get_activation  # noqa: PLC0415

        environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
        activation = get_activation(
            operation_conn,
            org_id=org_id,
            connector_name=connector_name,
            environment=environment,
        )
        if activation is None or activation.get("state") != "ACTIVE":
            raise ConnectorActivationRequired("connector activation is unavailable")

        # AC5 proof: this is the ONLY write path. We call create_datastream
        # (the Epic 12 seam) -- no direct INSERT into app.datastreams here.
        ds = _datastreams_mod.create_datastream(
            {
                "name": name,
                "source_kind": "managed_feed",
                "enabled": enabled,
                "schedule_mode": "manual",
                "config": ds_config,
            },
            project_id,
            created_by,
            operation_conn,
        )
        result = dict(ds)
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "datastream_id": ds["id"],
                "template_code": template_code,
                "template_version": template_version,
                "channels": sorted(channel_set),
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)

    data = op_result.result
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("datastream operation returned an incomplete result")
    config = data.get("config")
    if not isinstance(config, dict) or config.get("connector_name") != connector_name:
        raise RuntimeError("datastream operation returned an inconsistent result")
    return dict(data)
