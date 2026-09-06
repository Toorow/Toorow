"""Datastream-owned Event Configuration commands on the shared operation spine."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

from ulid import ULID

from core.audit import declare_action
from core.data_identities import build_event_configuration_version
from core.evidence_index import owner_route
from core.operations import (
    MutationResult,
    OperationResult,
    OperationSpec,
    _canonical_hash,
    execute_operation,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_DATA_EVENT_CONFIGURATION_CREATE = declare_action("data.event-configuration.create")
ACTION_DATA_EVENT_CONFIGURATION_VERSION_ACTIVATE = declare_action(
    "data.event-configuration.version.activate"
)
ACTION_DATA_EVENT_CONFIGURATION_VERSION_CONFIRM = declare_action(
    "data.event-configuration.version.confirm"
)
ACTION_DATA_EVENT_CONFIGURATION_VERSION_CREATE = declare_action(
    "data.event-configuration.version.create"
)



class EventConfigurationNotFound(LookupError):
    pass


class EventConfigurationInvalid(ValueError):
    pass


class EventConfigurationStale(RuntimeError):
    pass


def _mint_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _operation_payload(result: OperationResult) -> dict[str, Any]:
    return {
        **result.result,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "outbox_event_id": result.outbox_event_id,
        "replayed": result.replayed,
    }


def _spec(
    *,
    command: str,
    actor: str,
    org_id: str,
    project_id: str,
    datastream_id: str,
    event_configuration_id: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    confirmation_mode: str = "none",
    confirmation_reference: str | None = None,
    trace_id: str | None = None,
) -> OperationSpec:
    return OperationSpec(
        command_type=command,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"flux:{datastream_id}",
            f"event-configuration:{event_configuration_id}",
        ),
        idempotency_key=idempotency_key,
        host_context={},
        versions={"policy": "data-event-configuration.v1"},
        request_payload=request_payload,
        provider_references={},
        confirmation_mode=confirmation_mode,
        confirmation_reference=confirmation_reference,
        trace_id=trace_id,
    )


def create_event_configuration(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    org_id: str,
    name: str,
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name or len(name) > 160:
        raise EventConfigurationInvalid("name is required and must not exceed 160 characters")
    configuration_id = _mint_id("ecfg")
    spec = _spec(
        command=ACTION_DATA_EVENT_CONFIGURATION_CREATE,
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        datastream_id=datastream_id,
        event_configuration_id=configuration_id,
        idempotency_key=idempotency_key,
        request_payload={
            "project_id": project_id,
            "datastream_id": datastream_id,
            "name": name,
        },
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        del operation_id
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project_id, org_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s AND org_id = %s
                  AND archived_at IS NULL
                FOR SHARE
                """,
                (datastream_id, project_id, org_id),
            )
            row = cur.fetchone()
            if not row:
                raise EventConfigurationNotFound("Datastream not found")
            cur.execute(
                """
                INSERT INTO app.event_configurations
                    (id, datastream_id, project_id, org_id, name, lifecycle_state, created_by)
                VALUES (%s, %s, %s, %s, %s, 'draft', %s)
                """,
                (configuration_id, datastream_id, project_id, org_id, name, actor),
            )
        result = {
            "event_configuration_id": configuration_id,
            "datastream_id": datastream_id,
            "project_id": project_id,
            "lifecycle_state": "draft",
        }
        return MutationResult("succeeded", None, _canonical_hash(result), result, result)

    return _operation_payload(execute_operation(conn, spec, mutation=mutation))


def create_event_configuration_version(
    conn,
    *,
    project_id: str,
    event_configuration_id: str,
    org_id: str,
    source_mapping: dict[str, Any],
    collection_policy: dict[str, Any],
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(source_mapping, dict) or not isinstance(collection_policy, dict):
        raise EventConfigurationInvalid("source_mapping and collection_policy must be objects")
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    version_id = _mint_id("ecv")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.datastream_id, d.module_name
            FROM app.event_configurations c
            JOIN app.datastreams d
              ON d.id = c.datastream_id AND d.project_id = c.project_id
            WHERE c.id = %s AND c.project_id = %s AND c.org_id = %s
              AND c.lifecycle_state <> 'archived'
            FOR SHARE OF c, d
            """,
            (event_configuration_id, project_id, org_id),
        )
        owner = cur.fetchone()
        if not owner:
            raise EventConfigurationNotFound("Event Configuration not found")
        datastream_id, connector_id = owner
        if connector_id:
            cur.execute(
                """
                SELECT v.id, v.connector_fingerprint
                FROM app.connector_contract_versions v
                -- A contract hangs from its MODULE (migration 248). It used to be
                -- filtered on an inbound installation being READY, a state a pull
                -- Connector can never reach because it has no domain to verify.
                WHERE v.connector_id = %s AND v.environment = %s
                ORDER BY v.version_number DESC
                LIMIT 1
                """,
                (connector_id, environment),
            )
            contract = cur.fetchone()
            if not contract:
                raise EventConfigurationStale("current connector contract is unavailable")
        else:
            # A managed feed has NO Connector (`module_name IS NULL`), so no
            # contract row can ever exist for it -- demanding one refused every
            # imported event stream (Story 68.4). It anchors on its pinned
            # MAPPING version instead, which the store refuses to update just as
            # firmly. The anchor is checked, not skipped:
            # `build_event_configuration_version` refuses a version carrying
            # neither.
            if not str((source_mapping or {}).get("mapping_version_id") or "").strip():
                raise EventConfigurationInvalid(
                    "a managed feed's event configuration must name the pinned "
                    "mapping version it collects under"
                )
            contract = (None, None)
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.event_configuration_versions
            WHERE event_configuration_id = %s
            """,
            (event_configuration_id,),
        )
        version_number = int(cur.fetchone()[0])

    # NULL for a managed feed, and NULL is the honest value: it anchors on its
    # pinned mapping version instead, and the column has always been nullable.
    contract_version_id = str(contract[0]) if contract[0] else None
    contract_fingerprint = str(contract[1]) if contract[1] else None

    version = build_event_configuration_version(
        event_configuration_id=event_configuration_id,
        datastream_id=str(datastream_id),
        version_number=version_number,
        connector_contract_version_id=contract_version_id,
        connector_fingerprint=contract_fingerprint,
        source_mapping=source_mapping,
        collection_policy=collection_policy,
        actor=actor,
    )
    version["id"] = version_id
    spec = _spec(
        command=ACTION_DATA_EVENT_CONFIGURATION_VERSION_CREATE,
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        datastream_id=str(datastream_id),
        event_configuration_id=event_configuration_id,
        idempotency_key=idempotency_key,
        request_payload={
            "project_id": project_id,
            "event_configuration_id": event_configuration_id,
            "source_mapping": source_mapping,
            "collection_policy": collection_policy,
            "connector_contract_version_id": contract_version_id,
            "connector_fingerprint": contract_fingerprint,
        },
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.event_configuration_versions
                    (id, event_configuration_id, datastream_id, project_id,
                     version_number, connector_contract_version_id, connector_fingerprint,
                     source_mapping, collection_policy, normalized_payload_hash,
                     review_state, operation_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'draft', %s, %s)
                """,
                (
                    version_id,
                    event_configuration_id,
                    datastream_id,
                    project_id,
                    version_number,
                    contract[0],
                    contract[1],
                    json.dumps(source_mapping),
                    json.dumps(collection_policy),
                    version["normalized_payload_hash"],
                    operation_id,
                    actor,
                ),
            )
        result = {
            "event_configuration_id": event_configuration_id,
            "version_id": version_id,
            "version_number": version_number,
            "review_state": "draft",
            "connector_contract_version_id": contract_version_id,
            "connector_fingerprint": contract_fingerprint,
        }
        return MutationResult("succeeded", None, _canonical_hash(result), result, result)

    return _operation_payload(execute_operation(conn, spec, mutation=mutation))


def confirm_event_configuration_version(
    conn,
    *,
    project_id: str,
    event_configuration_id: str,
    version_id: str,
    org_id: str,
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.datastream_id
            FROM app.event_configuration_versions v
            JOIN app.event_configurations c ON c.id = v.event_configuration_id
            WHERE v.id = %s AND v.event_configuration_id = %s
              AND v.project_id = %s AND c.org_id = %s
            """,
            (version_id, event_configuration_id, project_id, org_id),
        )
        owner = cur.fetchone()
    if not owner:
        raise EventConfigurationNotFound("Event Configuration version not found")
    datastream_id = str(owner[0])
    spec = _spec(
        command=ACTION_DATA_EVENT_CONFIGURATION_VERSION_CONFIRM,
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        datastream_id=datastream_id,
        event_configuration_id=event_configuration_id,
        idempotency_key=idempotency_key,
        request_payload={"version_id": version_id},
        confirmation_mode="human",
        confirmation_reference=f"{event_configuration_id}:{version_id}:{actor}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.event_configuration_versions
                SET review_state = 'confirmed', confirmation_id = %s
                WHERE id = %s AND event_configuration_id = %s AND project_id = %s
                  AND datastream_id = %s AND review_state = 'draft'
                RETURNING id
                """,
                (operation_id, version_id, event_configuration_id, project_id, datastream_id),
            )
            if not cur.fetchone():
                raise EventConfigurationStale("version is not awaiting confirmation")
        result = {
            "event_configuration_id": event_configuration_id,
            "version_id": version_id,
            "review_state": "confirmed",
        }
        return MutationResult("succeeded", None, _canonical_hash(result), result, result)

    return _operation_payload(execute_operation(conn, spec, mutation=mutation))


def activate_event_configuration_version(
    conn,
    *,
    project_id: str,
    event_configuration_id: str,
    version_id: str,
    org_id: str,
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.datastream_id
            FROM app.event_configurations c
            JOIN app.event_configuration_versions v
              ON v.event_configuration_id = c.id
             AND v.datastream_id = c.datastream_id AND v.project_id = c.project_id
            WHERE c.id = %s AND c.project_id = %s AND c.org_id = %s AND v.id = %s
            """,
            (event_configuration_id, project_id, org_id, version_id),
        )
        owner = cur.fetchone()
    if not owner:
        raise EventConfigurationNotFound("Event Configuration version not found")
    owning_datastream_id = str(owner[0])
    spec = _spec(
        command=ACTION_DATA_EVENT_CONFIGURATION_VERSION_ACTIVATE,
        actor=actor,
        org_id=org_id,
        project_id=project_id,
        datastream_id=owning_datastream_id,
        event_configuration_id=event_configuration_id,
        idempotency_key=idempotency_key,
        request_payload={"version_id": version_id},
        confirmation_mode="human",
        confirmation_reference=f"activate:{event_configuration_id}:{version_id}:{actor}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.datastream_id, c.project_id, c.org_id,
                       v.id, v.review_state, v.connector_contract_version_id,
                       v.connector_fingerprint, d.module_name
                FROM app.event_configurations c
                JOIN app.event_configuration_versions v
                  ON v.event_configuration_id = c.id
                 AND v.datastream_id = c.datastream_id AND v.project_id = c.project_id
                JOIN app.datastreams d
                  ON d.id = c.datastream_id AND d.project_id = c.project_id
                WHERE c.id = %s AND c.project_id = %s AND c.org_id = %s AND v.id = %s
                FOR UPDATE OF c, v
                """,
                (event_configuration_id, project_id, org_id, version_id),
            )
            row = cur.fetchone()
            if not row:
                raise EventConfigurationNotFound("Event Configuration version not found")
            datastream_id = str(row[1])
            if row[5] != "confirmed":
                raise EventConfigurationStale("version is not confirmed")
            # A managed feed has no Connector, so there is no contract to have
            # CHANGED: it anchors on its pinned mapping version, which the store
            # refuses to update at all (Story 68.4). Re-reading a contract for
            # `connector_id IS NULL` returned no row and refused every
            # activation -- staleness reported about a thing that does not
            # exist.
            if row[8]:
                cur.execute(
                    """
                    SELECT v.id, v.connector_fingerprint
                    FROM app.connector_contract_versions v
                    -- Same reading as the preparation query above (migration 248).
                    WHERE v.connector_id = %s AND v.environment = %s
                    ORDER BY v.version_number DESC
                    LIMIT 1
                    """,
                    (row[8], environment),
                )
                current_contract = cur.fetchone()
                if not current_contract or (row[6], row[7]) != tuple(current_contract):
                    raise EventConfigurationStale(
                        "connector contract changed after preparation"
                    )
            cur.execute(
                """
                UPDATE app.event_configuration_versions
                SET review_state = 'superseded'
                WHERE event_configuration_id = %s AND project_id = %s
                  AND review_state = 'active' AND id <> %s
                """,
                (event_configuration_id, project_id, version_id),
            )
            cur.execute(
                """
                UPDATE app.event_configuration_versions
                SET review_state = 'active', operation_id = %s
                WHERE id = %s AND event_configuration_id = %s AND project_id = %s
                """,
                (operation_id, version_id, event_configuration_id, project_id),
            )
            cur.execute(
                """
                UPDATE app.event_configurations
                SET active_version_id = %s, lifecycle_state = 'active', updated_at = NOW()
                WHERE id = %s AND datastream_id = %s AND project_id = %s AND org_id = %s
                """,
                (version_id, event_configuration_id, datastream_id, project_id, org_id),
            )
        result = {
            "event_configuration_id": event_configuration_id,
            "version_id": version_id,
            "review_state": "active",
            "datastream_id": datastream_id,
        }
        return MutationResult("succeeded", None, _canonical_hash(result), result, result)

    return _operation_payload(execute_operation(conn, spec, mutation=mutation))


# ---------------------------------------------------------------------------
# The read Context Hub is allowed to make (Story 49.6 AC9).
# ---------------------------------------------------------------------------

#: What an observation resolves to when it cannot be PROVEN Data-owned.
#: Migration 133 put this literal on the column itself
#: (`binding_state CHECK (binding_state IN ('linked', 'unavailable'))`), and AC9
#: asks for the same word: "Missing or denied Data evidence is non-disclosing
#: and shown as unavailable."
EVENT_BINDING_LINKED = "linked"
EVENT_BINDING_UNAVAILABLE = "unavailable"

_SELECT_EVENT_OBSERVATION_REFERENCES = """
    SELECT o.id,
           o.type,
           o.event_date,
           o.binding_state,
           o.datastream_id,
           o.execution_id,
           o.event_configuration_version_id,
           v.event_configuration_id,
           v.version_number
      FROM app.context_events o
      LEFT JOIN app.event_configuration_versions v
             ON v.id = o.event_configuration_version_id
            AND v.datastream_id = o.datastream_id
            AND v.project_id = o.project_id
     WHERE o.project_id = %(project_id)s
       AND o.id = ANY(%(event_ids)s)
"""


def resolve_event_observation_references(
    conn, *, project_id: str, event_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Resolve observed Event ids to their DATA-OWNED reference, or to nothing.

    This is the read Story 49.6 AC9 authorises, and the shape of the answer is
    the whole point. It returns an IDENTITY and a POINTER -- the observation id,
    its type and date, the owning Datastream, the Event Configuration version
    and the execution that produced it. It never returns the source mapping, the
    collection policy, the payload, a source sample, timezone evidence or run
    evidence: AC9 says Context Hub "stores no Event definition, mapping,
    payload, source sample, timezone, run evidence or editable copy", and a
    reader that returned those would have made a second copy of the Event with
    an extra step, not with a table.

    Two failures answer identically, on purpose. An observation of another
    Project and an observation that does not exist are both simply absent from
    the result -- the scope is part of the WHERE, not a filter applied after,
    so neither confirms the other. An observation that exists but whose binding
    cannot be proven comes back as ``unavailable`` rather than as a link to a
    Datastream nobody demonstrated owns it.
    """
    wanted = [str(e) for e in dict.fromkeys(event_ids) if e]
    if not wanted:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            _SELECT_EVENT_OBSERVATION_REFERENCES,
            {"project_id": project_id, "event_ids": wanted},
        )
        rows = cur.fetchall()

    resolved: dict[str, dict[str, Any]] = {}
    for row in rows:
        (
            event_id, event_type, event_date, binding_state,
            datastream_id, execution_id, version_id, configuration_id, version_number,
        ) = row

        # `binding_state` alone is not enough to publish a link: migration 133
        # bound legacy rows only where exactly ONE owning Datastream could be
        # proven, and a row could carry `linked` with its version row since
        # deleted. Every part of the pointer must be present, or none is.
        proven = (
            binding_state == EVENT_BINDING_LINKED
            and bool(datastream_id)
            and bool(version_id)
            and bool(configuration_id)
        )
        reference: dict[str, Any] = {
            "event_id": event_id,
            "binding_state": EVENT_BINDING_LINKED if proven else EVENT_BINDING_UNAVAILABLE,
        }
        if not proven:
            # Non-disclosing: the caller learns that the evidence cannot be
            # reached, and nothing about why.
            resolved[event_id] = reference
            continue

        reference.update(
            {
                "event_type": event_type,
                "event_date": event_date.isoformat() if event_date else None,
                "datastream_id": datastream_id,
                "event_configuration_id": configuration_id,
                "event_configuration_version_id": version_id,
                "version_number": version_number,
                "execution_id": execution_id,
                "owner_route": owner_route(
                    "event-configuration", configuration_id, version_id
                ),
            }
        )
        resolved[event_id] = reference
    return resolved
