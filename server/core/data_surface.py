"""Canonical, source-agnostic read model for the six Project Data lenses."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


class DataObjectNotFound(LookupError):
    """The requested object does not exist in the authorized Project snapshot."""


DATA_SURFACE_QUERIES: dict[str, str] = {
    "datastreams": """
        SELECT d.id, d.name, d.source_kind, d.module_name, d.enabled, d.schedule_mode,
               -- WHAT THIS DATASTREAM IS FOR. `app.datastreams.data_role` holds one
               -- of the seven values `core.datastreams.DATA_ROLES` declares, and it
               -- has had a write path since 2026-07-27. The fleet printed
               -- "the read model carries no data-type classification" for every row
               -- while the column sat one SELECT away: the sentence was true when it
               -- was written and false by the time it was read. A NULL here is
               -- "nobody has classified this stream", which the screen says in as
               -- many words -- it is not the same statement as "the product cannot
               -- know".
               d.data_role,
               -- WHICH SOURCE SCOPE IT PULLS FROM (migration 211). The Sources lens
               -- has answered "used by how many Datastreams" through this column
               -- since it existed; the fleet could not answer the reverse question
               -- -- which account a row reads -- because the column was never
               -- selected here. Two absences travel separately and must not be
               -- collapsed: a NULL `source_account_id` means this stream names no
               -- scope at all, while a scope with no `label` means the authorization
               -- exposed none. The second falls back to the account's own external
               -- id, exactly as the `sources` lens does below.
               d.source_account_id,
               ca.label AS source_account_label,
               ca.external_account_id AS source_account_external_id,
               d.current_plan_version_id AS active_plan_version_id,
               pv.version_number AS active_plan_version,
               pv.executable, pv.validation_issues, pv.writer_kind,
               pv.destination_policy, d.current_published_execution_id,
               -- WHAT THE CURRENT PLAN COLLECTS, and at which grain. The selection
               -- lives under `source.selection` of the plan's normalized payload --
               -- there is no `selection` key at the root, measured on every one of
               -- the plan versions in the disposable cluster. `jsonb_typeof` guards
               -- each read so a payload written before the selection existed yields
               -- NULL rather than raising 22023: NULL is "this plan says nothing
               -- about it", and a Datastream with no current plan at all gets NULL
               -- from the LEFT JOIN itself. Never 0 -- a zero here would read as a
               -- plan that selected nothing.
               CASE
                   WHEN jsonb_typeof(pv.normalized_payload #> '{source,selection,grain}') = 'array'
                   THEN pv.normalized_payload #> '{source,selection,grain}'
               END AS plan_grain,
               CASE
                   WHEN jsonb_typeof(
                       pv.normalized_payload #> '{source,selection,metrics}'
                   ) = 'array'
                   THEN jsonb_array_length(pv.normalized_payload #> '{source,selection,metrics}')
               END AS plan_metric_count,
               CASE
                   WHEN jsonb_typeof(
                       pv.normalized_payload #> '{source,selection,dimensions}'
                   ) = 'array'
                   THEN jsonb_array_length(pv.normalized_payload #> '{source,selection,dimensions}')
               END AS plan_dimension_count,
               pe.state AS publication_state, pe.state_changed_at AS published_at,
               pe.row_count AS published_row_count,
               ss.next_run_at,
               last_good.execution_id AS last_known_good_execution_id,
               latest_execution.id AS latest_execution_id,
               latest_execution.state AS latest_execution_state,
               -- WHEN THAT RUN LAST MOVED. The fleet carried the state of the latest
               -- execution and no instant at all, so "failed" could be five minutes
               -- or five months old and the row read the same. `published_at` is not
               -- the answer: it is the last PUBLICATION, so a run that failed has
               -- none, and the `Published` column already carries it.
               latest_execution.state_changed_at AS latest_execution_at,
               latest_execution.error_code AS latest_error_code,
               ch.status AS connection_health_state,
               ch.last_checked_at AS health_checked_at,
               -- The fleet's link to Governance and the MDM, and it is the only
               -- one that exists at this level: `mdm_business_links.target_type`
               -- is CHECK-constrained to topic/procedure/target_field/schema_doc/
               -- report_view (migration 130), so a Business Domain does NOT bind
               -- to a Datastream directly. The real chain is
               --   Datastream -> mapping version -> canonical target fields -> Domain
               -- so what the fleet can say truthfully is whether a stream reaches
               -- the semantic layer at all, and how far it is from doing so.
               -- `blocking_count` is the actionable half: it names the distance.
               d.current_mapping_version_id,
               mv.version_number AS active_mapping_version,
               mv.blocking_count AS mapping_blocking_count,
               mv.executable AS mapping_executable,
               d.created_at, d.updated_at
        FROM app.project_flux pf
        JOIN app.datastreams d
          ON d.id = pf.flux_id AND d.org_id = pf.org_id
        LEFT JOIN app.datastream_plan_versions pv
          ON pv.id = d.current_plan_version_id
         AND pv.datastream_id = d.id AND pv.project_id = d.project_id
        LEFT JOIN app.datastream_executions pe
          ON pe.id = d.current_published_execution_id
         AND pe.datastream_id = d.id AND pe.project_id = d.project_id
        LEFT JOIN app.datastream_mapping_versions mv
          ON mv.id = d.current_mapping_version_id
         AND mv.datastream_id = d.id AND mv.project_id = d.project_id
        LEFT JOIN app.datastream_schedule_state ss
          ON ss.plan_version_id = pv.id
        -- One row at most: `source_account_id` is UNIQUE on this table
        -- (`uq_credential_accounts_source_account_id`, migration 133), so this
        -- join cannot multiply the fleet.
        LEFT JOIN app.credential_accounts ca
          ON ca.source_account_id = d.source_account_id
        LEFT JOIN LATERAL (
            SELECT pl.execution_id
            FROM app.datastream_publication_log pl
            WHERE pl.datastream_id = d.id AND pl.project_id = d.project_id
            ORDER BY pl.published_at DESC
            LIMIT 1
        ) last_good ON TRUE
        LEFT JOIN LATERAL (
            SELECT candidate.id, candidate.state, candidate.state_changed_at,
                   candidate.error_code,
                   -- WHY that run exists, so the fleet can stop calling every
                   -- moving run a collection. `projection_plan_ref.origin` is the
                   -- one place story 63.7 stamps it, and `run_origins.origin_of`
                   -- is the one reader; four of the declared origins carry
                   -- `reads_provider_windows=False`, so a `mapping_change` was
                   -- printing `Collecting` on this list while the live band, over
                   -- the same run, said « This update reads no provider window ».
                   -- Two surfaces, two contradictory words, one execution.
                   candidate.projection_plan_ref AS latest_projection_plan_ref
            FROM app.datastream_executions candidate
            WHERE candidate.datastream_id = d.id
              AND candidate.project_id = d.project_id
            ORDER BY candidate.created_at DESC
            LIMIT 1
        ) latest_execution ON TRUE
        LEFT JOIN app.connection_health ch
          ON ch.connection_ref_id = d.connection_ref_id
        WHERE pf.project_id = %s AND d.archived_at IS NULL
        ORDER BY d.name, d.id
    """,
    "sources": """
        SELECT ca.source_account_id, ca.label,
               -- THE SCOPE'S OWN NAME. `ca.label` is whatever the provider's
               -- discovery call happened to return, and it is NULL for every
               -- account discovered before the label write landed -- so five
               -- Search Console properties all rendered as one repeated string
               -- ("Unlabelled source account") and nothing on the screen could
               -- tell them apart. The glossary defines a Source Account as "a
               -- provider account, property, dataset or equivalent source
               -- scope", and this column is that scope's name; it is the same
               -- value `app.connection_account_scope.account_label` already
               -- stores and shows. It is a scope name, never a credential.
               ca.external_account_id,
               -- WHICH Connector's discovery returned this scope (migration
               -- 210). NULL for rows discovered before the column existed.
               ca.discovered_for_connector,
               cr.provider AS connector_id,
               -- THE ADDRESS OF THE AUTHORIZATION ITSELF, never selected.
               -- This query has joined `app.connection_ref` since it was
               -- written and read four of its columns, never its id. So a
               -- Source Account could not name the connection it IS, and every
               -- action the server offers on a connection -- backfill, ad-hoc
               -- pull, account discovery -- was unreachable from the one screen
               -- that owns authorizations. No front-end change could have
               -- surfaced them: the field was never on the wire.
               cr.id AS connection_ref_id,
               CASE WHEN cr.owner_org_id = p.org_id THEN 'organization' ELSE 'delegated' END
                   AS authorization_scope,
               cr.auth_path AS authorization_kind,
               ca.available, ca.discovered_at, ca.last_seen_at,
               -- AUTHORIZATION HEALTH, one join away and never taken.
               -- `app.connection_health.status` is the `ok|stale|revoked` state
               -- machine (migrations 005 and 007) that the Datastreams query has
               -- joined since it was written. Sources reported `available` /
               -- `unavailable` instead -- a boolean where a revoked credential
               -- and a merely stale one look identical, on the one page whose
               -- function names "authorization health".
               ch.status AS connection_health_state,
               ch.last_checked_at AS health_checked_at,
               COUNT(DISTINCT pf.flux_id) AS used_by_count,
               owner_org.name AS owner_org_name
        FROM app.projects p
        JOIN app.connection_ref cr
          ON cr.owner_org_id = p.org_id
          OR EXISTS (
              SELECT 1
              FROM app.credential_account_grants cag
              WHERE cag.credential_id = cr.id
                AND cag.grantee_org_id = p.org_id
                AND cag.status = 'active'
          )
        JOIN app.credential_accounts ca ON ca.credential_id = cr.id
        LEFT JOIN app.organizations owner_org ON owner_org.id = cr.owner_org_id
        LEFT JOIN app.connection_health ch ON ch.connection_ref_id = cr.id
        -- USED BY: the Datastreams reading THIS account, not every Datastream
        -- of the authorization. One Google consent exposes five Search Console
        -- properties, so joining on the credential alone printed the same
        -- "used by 12" against all five. `source_account_id` (migration 211) is
        -- what makes the question answerable; a Datastream that predates it and
        -- names no account counts against none, which is honest -- it is not
        -- known which of the five it reads.
        LEFT JOIN app.datastreams d
          ON d.connection_ref_id = cr.id
         AND d.source_account_id = ca.source_account_id
        LEFT JOIN app.project_flux pf
          ON pf.flux_id = d.id AND pf.project_id = p.id AND pf.org_id = p.org_id
        WHERE p.id = %s
          AND (
              cr.owner_org_id = p.org_id
              OR EXISTS (
                  SELECT 1
                  FROM app.credential_account_grants cag
                  WHERE cag.credential_id = cr.id
                    AND cag.external_account_id = ca.external_account_id
                    AND cag.grantee_org_id = p.org_id
                    AND cag.status = 'active'
              )
          )
        -- `cr.id` belongs in the GROUP BY, not merely in the SELECT. This query
        -- aggregates (COUNT(DISTINCT pf.flux_id)) and groups by `cr.provider`, so
        -- adding a bare `cr.id` to the projection raises 42803 at runtime -- a
        -- component test cannot see it, and the whole Sources lens would have
        -- returned an error instead of a list.
        GROUP BY ca.source_account_id, ca.label, ca.external_account_id,
                 ca.discovered_for_connector, cr.id, cr.provider,
                 cr.owner_org_id, p.org_id, cr.auth_path, ca.available,
                 ca.discovered_at, ca.last_seen_at, ch.status, ch.last_checked_at, owner_org.name
        ORDER BY COALESCE(ca.label, ca.external_account_id), ca.source_account_id
    """,
    "imports": """
        SELECT l.id, l.datastream_id, d.name AS datastream_name,
               l.execution_id, l.plan_version_id, l.mapping_version_id,
               l.import_contract_id, l.feed_format, l.write_mode,
               l.source_metadata, l.landing_relation, l.content_hash,
               l.row_count, l.rejected_row_count, l.outcome,
               execution.state AS candidate_state,
               d.current_published_execution_id,
               receipt.id AS receipt_id, receipt.channel AS receipt_channel,
               receipt.state AS receipt_state,
               receipt.attachment_count, receipt.total_bytes,
               receipt.error_code AS receipt_error_code,
               l.snapshot_observed_at, l.created_at, l.updated_at
        FROM app.managed_feed_import_ledger l
        JOIN app.datastreams d
          ON d.id = l.datastream_id AND d.project_id = l.project_id
        JOIN app.project_flux pf
          ON pf.flux_id = d.id AND pf.project_id = l.project_id
        LEFT JOIN app.datastream_executions execution
          ON execution.id = l.execution_id
         AND execution.datastream_id = l.datastream_id
         AND execution.project_id = l.project_id
        LEFT JOIN LATERAL (
            SELECT inbound.id, inbound.channel, inbound.state,
                   inbound.attachment_count, inbound.total_bytes,
                   inbound.error_code
            FROM app.inbound_receipts inbound
            WHERE inbound.import_ledger_id = l.id
              AND inbound.datastream_id = l.datastream_id
            ORDER BY inbound.created_at DESC
            LIMIT 1
        ) receipt ON TRUE
        WHERE l.project_id = %s
        ORDER BY l.created_at DESC, l.id
    """,
    "events": """
        SELECT c.id, c.datastream_id, d.name AS datastream_name, c.name,
               c.lifecycle_state, c.active_version_id,
               v.version_number AS active_version,
               v.connector_contract_version_id, v.connector_fingerprint,
               v.source_mapping, v.collection_policy,
               v.normalized_payload_hash, v.review_state,
               v.created_at AS version_created_at,
               observations.observation_count,
               observations.latest_observation_at,
               c.created_at, c.updated_at
        FROM app.event_configurations c
        JOIN app.datastreams d
          ON d.id = c.datastream_id AND d.project_id = c.project_id
        LEFT JOIN app.event_configuration_versions v
          ON v.id = c.active_version_id
         AND v.datastream_id = c.datastream_id AND v.project_id = c.project_id
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS observation_count,
                   MAX(observation.event_date) AS latest_observation_at
            FROM app.context_events observation
            WHERE observation.event_configuration_version_id = v.id
              AND observation.datastream_id = c.datastream_id
              AND observation.project_id = c.project_id
        ) observations ON TRUE
        WHERE c.project_id = %s AND c.lifecycle_state <> 'archived'
        ORDER BY c.name, c.id
    """,
    "connectors": """
        SELECT i.connector_name AS connector_id, i.environment,
               i.state AS installation_state, i.blocking_cause,
               i.last_verified_at, a.state AS activation_state,
               v.id AS active_contract_version_id,
               v.version_number AS active_contract_version,
               v.connector_fingerprint, v.contract_schema_version,
               v.contract_snapshot, v.validation_evidence,
               v.created_at AS contract_created_at,
               coverage.datastream_count, i.updated_at
        FROM app.projects p
        JOIN app.connector_installations i ON TRUE
        LEFT JOIN app.connector_activations a
          ON a.org_id = p.org_id
         AND a.connector_name = i.connector_name
         AND a.environment = i.environment
        LEFT JOIN LATERAL (
            SELECT cv.*
            FROM app.connector_contract_versions cv
            -- BY MODULE, not by installation row (migration 248). Joined on
            -- `cv.installation_id = i.id` this lens could only ever show the
            -- contract of an INBOUND channel, so the contract columns were null
            -- for every pull Connector a Project actually uses.
            WHERE cv.connector_id = i.connector_name
              AND cv.environment = i.environment
            ORDER BY cv.version_number DESC
            LIMIT 1
        ) v ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(DISTINCT d.id) AS datastream_count
            FROM app.project_flux pf
            JOIN app.datastreams d ON d.id = pf.flux_id
            WHERE pf.project_id = p.id
              AND d.module_name = i.connector_name
              AND d.archived_at IS NULL
        ) coverage ON TRUE
        WHERE p.id = %s
          AND (a.id IS NOT NULL OR coverage.datastream_count > 0)
        ORDER BY i.connector_name, i.environment
    """,
}

_LENS_TYPES = {
    "datastreams": "datastream",
    "sources": "source-account",
    "imports": "import",
    "events": "event-configuration",
    "connectors": "connector",
}

_API_PATHS = {
    "datastreams": "datastreams",
    "sources": "source-accounts",
    "imports": "imports",
    "events": "event-configurations",
    "connectors": "connectors",
}

_CONSOLE_SECTIONS = {
    "datastreams": "datastreams",
    "sources": "sources",
    "imports": "imports",
    "events": "events",
    "connectors": "connectors",
}


def _run_origin(projection_plan: Any) -> str | None:
    """The origin key of the latest run, read by the ONE reader that owns it.

    `run_origins.origin_of` accepts the `jsonb` column in either shape psycopg
    can hand it over, and answers `None` for every execution minted before story
    63.7 wrote the column. That `None` travels: the fleet says the state without
    the reason rather than inventing one.
    """
    from core.run_origins import origin_of  # noqa: PLC0415

    return origin_of(projection_plan)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _object_ref(project_id: str, lens: str, object_id: str) -> dict[str, str]:
    object_type = _LENS_TYPES[lens]
    collection = _API_PATHS[lens]
    return {
        "object_type": object_type,
        "id": object_id,
        "href": f"/api/projects/{project_id}/{collection}/{object_id}",
    }


def _project_org(conn: Any, project_id: str) -> str | None:
    """The organization every console address is rooted on.

    `app.projects.org_id` was added NULLable and backfilled (migration 035), so
    a project without one is possible. That case emits NO console link at all
    rather than one that cannot resolve — see `_console_link`."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if not row:
        return None
    value = row[0] if not isinstance(row, Mapping) else row.get("org_id")
    return (value or "").strip() or None


def _console_link(
    org_id: str | None,
    project_id: str,
    lens: str,
    object_id: str,
    tab: str = "overview",
) -> str | None:
    """One console address, in the grammar the client router actually parses.

    That grammar is `/org/{org}/project/{project}/{workspace}/{section}/object/
    {type}/{id}/tab/{tab}` — `ui/admin/src/shell/router.tsx:129,138,158,186,191`.
    This function used to emit `/project/{p}/data/{section}/{type}/{id}/{tab}`,
    which is wrong three times over: no `/org/` root, no `object/` literal, no
    `tab/` literal. `router.tsx:129` refuses anything whose first segment is not
    `org`, so EVERY link this function has ever produced — 17 call sites across
    five lenses — landed on the unknown-route screen. Measured 2026-08-03: the
    Datastream fleet's Mapping badge was the visible symptom, the defect was
    class-wide.

    Returns `None` when the organization is unknown: a link that cannot resolve
    is worse than no link, because the screen reports it as a way forward."""
    if not org_id:
        return None
    return (
        f"/org/{org_id}/project/{project_id}/data/{_CONSOLE_SECTIONS[lens]}"
        f"/object/{_LENS_TYPES[lens]}/{object_id}/tab/{tab}"
    )


def _links(**pairs: str | None) -> dict[str, str]:
    """Only the addresses that resolve. A `None` is dropped, never rendered."""
    return {key: value for key, value in pairs.items() if value}


def build_collection_envelope(
    *,
    project_id: str,
    lens: str,
    items: list[dict[str, Any]],
    evidence_as_of: Any,
    unavailable_reasons: list[dict[str, str]],
    allowed_actions: list[str],
    generated_at: str | None = None,
    collection_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if lens not in {*_LENS_TYPES, "overview"}:
        raise ValueError(f"unsupported Data lens: {lens}")
    schema_lens = "overview" if lens == "overview" else lens
    envelope = {
        "schema_version": f"data-{schema_lens}.v1",
        "project_ref": {
            "object_type": "project",
            "id": project_id,
            "href": f"/api/projects/{project_id}/data-overview",
        },
        "generated_at": generated_at or _now(),
        "evidence_as_of": _iso(evidence_as_of),
        "items": items,
        "unavailable_reasons": unavailable_reasons,
        "allowed_actions": allowed_actions,
    }
    if collection_meta:
        allowed_collection_meta = {
            "total", "bound", "next_cursor", "applied_filters", "filter_options"
        }
        if set(collection_meta) - allowed_collection_meta:
            raise ValueError("collection metadata contains a canonical envelope key")
        envelope.update(collection_meta)
    return envelope


def project_datastream(
    row: Mapping[str, Any], *, project_id: str, org_id: str | None = None
) -> dict[str, Any]:
    object_id = str(row["id"])
    published_at = row.get("published_at")
    health_checked_at = row.get("health_checked_at")
    evidence_at = row.get("updated_at") or published_at or health_checked_at
    executable = row.get("executable")
    # STORY 59.2 -- the open issues of this Datastream, and the key is ABSENT
    # when the aggregate could not be read. Three states travel inside it
    # (`monitored`, `count`, `highest_severity`); the missing key is a FOURTH and
    # the screen says so rather than printing a zero nobody measured, exactly as
    # `WorkbenchOverviewPage` already does for `dq_monitors`.
    open_issues = row.get("open_issues") if "open_issues" in row else None
    return {
        "object_ref": _object_ref(project_id, "datastreams", object_id),
        "project_ref": {"object_type": "project", "id": project_id},
        "name": row.get("name") or object_id,
        "source_kind": row.get("source_kind") or "unavailable",
        "connector_ref": (
            {"object_type": "connector", "id": str(row["module_name"])}
            if row.get("module_name")
            else None
        ),
        "active_version_ref": (
            {
                "object_type": "datastream-plan-version",
                "id": str(row["active_plan_version_id"]),
                "version": row.get("active_plan_version"),
            }
            if row.get("active_plan_version_id")
            else None
        ),
        "published_execution_ref": (
            {
                "object_type": "publication",
                "id": str(row["current_published_execution_id"]),
            }
            if row.get("current_published_execution_id")
            else None
        ),
        "last_known_good_publication_ref": (
            {
                "object_type": "publication",
                "id": str(row["last_known_good_execution_id"]),
            }
            if row.get("last_known_good_execution_id")
            else None
        ),
        "candidate_ref": (
            {
                "object_type": "datastream-execution",
                "id": str(row["latest_execution_id"]),
            }
            if row.get("latest_execution_id")
            else None
        ),
        "states": {
            "lifecycle": "active" if row.get("enabled") else "paused",
            "validation": (
                "executable"
                if executable is True
                else "blocked"
                if executable is False
                else "unavailable"
            ),
            "publication": row.get("publication_state") or "unavailable",
            "health": row.get("connection_health_state") or "unavailable",
            "freshness": "observed" if published_at or health_checked_at else "unavailable",
        },
        "evidence": {
            "validation_issues": row.get("validation_issues") or [],
            "published_at": _iso(published_at),
            "published_row_count": row.get("published_row_count"),
            "health_checked_at": _iso(health_checked_at),
            "next_run_at": _iso(row.get("next_run_at")),
            "latest_candidate_state": row.get("latest_execution_state"),
            # The origin KEY, never a sentence: the console owns the words
            # (`runOrigins.ts`, mirrored from `run_origins.py`), and an origin
            # this build does not know must reach the screen unchanged rather
            # than be folded onto a neighbouring one. `None` on every execution
            # minted before story 63.7 — measured 100 of 428 on the disposable
            # base — and `None` is a real answer, not a missing key.
            "latest_run_origin": _run_origin(row.get("latest_projection_plan_ref")),
            "latest_exception": row.get("latest_error_code"),
            "destination_policy": row.get("destination_policy"),
            # `data.md:50` requires the fleet list to show cadence, and the SELECT
            # has carried `d.schedule_mode` since this query was written -- it was
            # read from the database and dropped between the SQL and the JSON, so
            # the screen could not have shown it however it was written. Same for
            # the plan version and the last-known-good pointer, both already
            # joined. Emitting them costs one row each and closes three of the
            # nine contents the ratified list asks for.
            "cadence": row.get("schedule_mode"),
            "active_plan_version": row.get("active_plan_version"),
            "last_known_good_execution_id": row.get("last_known_good_execution_id"),
            # What a person opens this fleet to know, and none of it was on the
            # wire before story 58.8. Every one of these five is `None` rather
            # than a zero or an empty string when the fact does not exist: the
            # screen owes a sentence for an absence, and it cannot write one for
            # a `0` it cannot tell from a measurement.
            "data_role": row.get("data_role"),
            "source_account_id": row.get("source_account_id"),
            # The provider's label when it gave one, the scope's own name
            # otherwise -- the same fallback the `sources` lens applies, so one
            # account is not called two things on two screens. Both NULL means
            # the authorization exposes no name at all, which the screen says.
            "source_account_label": (
                row.get("source_account_label") or row.get("source_account_external_id")
            ),
            "latest_run_at": _iso(row.get("latest_execution_at")),
            # The plan's own selection. `plan_grain` is the list, not a count:
            # the fleet renders the grain itself, and a count of grain columns
            # would say how many without saying which.
            "plan_grain": row.get("plan_grain"),
            "plan_metric_count": row.get("plan_metric_count"),
            "plan_dimension_count": row.get("plan_dimension_count"),
            # The Governance/MDM reach. `mapped` is deliberately a THREE-valued
            # answer, not a boolean: a Datastream with no mapping version is not
            # the same statement as one whose mapping is blocked, and collapsing
            # them is how "nothing arrives" becomes indistinguishable from
            # "nothing was configured".
            "mapping_version_id": row.get("current_mapping_version_id"),
            "active_mapping_version": row.get("active_mapping_version"),
            "mapping_blocking_count": row.get("mapping_blocking_count"),
            "mapping_executable": row.get("mapping_executable"),
            **({"open_issues": dict(open_issues)} if open_issues is not None else {}),
        },
        "evidence_as_of": _iso(evidence_at),
        "links": _links(
            overview=_console_link(org_id, project_id, "datastreams", object_id),
            runs=_console_link(org_id, project_id, "datastreams", object_id, "runs"),
            outputs=_console_link(org_id, project_id, "datastreams", object_id, "outputs"),
            # The Mapping tab is where a person acts on the Governance gap the
            # fleet reports. Without it the screen could name the problem and
            # send nobody anywhere, which is the complaint this whole surface is
            # being rebuilt against.
            mapping=_console_link(org_id, project_id, "datastreams", object_id, "mapping"),
        ),
    }


def project_source_account(
    row: Mapping[str, Any], *, project_id: str, org_id: str | None = None
) -> dict[str, Any]:
    object_id = str(row["source_account_id"])
    last_seen = row.get("last_seen_at")
    used_by_count = int(row.get("used_by_count") or 0)
    connector_id = str(row.get("connector_id") or "unavailable")
    return {
        "object_ref": _object_ref(project_id, "sources", object_id),
        "project_ref": {"object_type": "project", "id": project_id},
        "connector_ref": {"object_type": "connector", "id": connector_id},
        # The Connector this scope actually belongs to, when discovery recorded
        # it. `connector_ref` above carries the AUTHORIZATION's provider, which
        # for a Google direct grant is the string "google" and names no tool.
        "discovered_for_connector": row.get("discovered_for_connector"),
        # Named `connection_ref` because that is the table and the route
        # segment: `POST /api/connections/{id}/backfill` takes exactly this id.
        "connection_ref": {
            "object_type": "connection",
            "id": str(row.get("connection_ref_id") or ""),
        },
        # The provider's label when it gave one, the scope's own name otherwise.
        # A constant fallback is not a name: it makes every account of one
        # authorization identical, and choosing between five identical options
        # is not a choice.
        "label": row.get("label") or row.get("external_account_id") or "Unlabelled source account",
        "authorization_ref": {
            "object_type": "source-authorization",
            "owner_scope": row.get("authorization_scope") or "unavailable",
            "kind": row.get("authorization_kind") or "unavailable",
            "owner_org_name": row.get("owner_org_name"),
        },
        "states": {
            "availability": "available" if row.get("available") else "unavailable",
            # The real state machine, not a second boolean: `revoked` and `stale`
            # are different problems with different repairs, and this page's
            # function names "authorization health" as one of its four jobs.
            "authorization": str(row.get("connection_health_state") or "unavailable"),
            "freshness": "observed" if last_seen else "unavailable",
            "usage": "used" if used_by_count else "unused",
        },
        "evidence": {
            "discovered_at": _iso(row.get("discovered_at")),
            "last_seen_at": _iso(last_seen),
            "used_by_count": used_by_count,
            "authorization_scope": row.get("authorization_scope"),
            "authorization_kind": row.get("authorization_kind"),
            "health_checked_at": _iso(row.get("health_checked_at")),
        },
        "evidence_as_of": _iso(last_seen or row.get("discovered_at")),
        "links": _links(
            overview=_console_link(org_id, project_id, "sources", object_id),
            used_by=_console_link(org_id, project_id, "sources", object_id, "used-by"),
        ),
    }


def project_import(
    row: Mapping[str, Any], *, project_id: str, org_id: str | None = None
) -> dict[str, Any]:
    object_id = str(row["id"])
    outcome = str(row.get("outcome") or "unavailable")
    rejected = int(row.get("rejected_row_count") or 0)
    datastream_id = str(row["datastream_id"])
    return {
        "object_ref": _object_ref(project_id, "imports", object_id),
        "project_ref": {"object_type": "project", "id": project_id},
        "datastream_ref": {"object_type": "datastream", "id": datastream_id},
        "name": f"{row.get('datastream_name') or datastream_id} import",
        "source_kind": "managed_feed",
        "active_version_refs": {
            "plan": row.get("plan_version_id"),
            "mapping": row.get("mapping_version_id"),
            "contract": row.get("import_contract_id"),
        },
        "candidate_ref": (
            {"object_type": "datastream-execution", "id": str(row["execution_id"])}
            if row.get("execution_id")
            else None
        ),
        "publication_ref": (
            {"object_type": "publication", "id": str(row["execution_id"])}
            if row.get("execution_id")
            and row.get("current_published_execution_id") == row.get("execution_id")
            else None
        ),
        "receipt_ref": (
            {"object_type": "inbound-receipt", "id": str(row["receipt_id"])}
            if row.get("receipt_id")
            else None
        ),
        "states": {
            "lifecycle": outcome,
            "validation": "rejected_rows" if rejected else "no_rejections",
            "candidate": row.get("candidate_state") or "unavailable",
            "publication": (
                "current"
                if row.get("execution_id")
                and row.get("current_published_execution_id") == row.get("execution_id")
                else "not_current"
                if outcome == "published"
                else "unavailable"
            ),
            "receipt": row.get("receipt_state") or "unavailable",
            "freshness": "observed" if row.get("snapshot_observed_at") else "unavailable",
        },
        "evidence": {
            "feed_format": row.get("feed_format"),
            "write_mode": row.get("write_mode"),
            "source_metadata": row.get("source_metadata") or {},
            "landing_relation": row.get("landing_relation"),
            "content_hash": row.get("content_hash"),
            "row_count": row.get("row_count"),
            "rejected_row_count": rejected,
            "candidate_outcome": row.get("candidate_state"),
            "receipt": {
                "channel": row.get("receipt_channel"),
                "state": row.get("receipt_state"),
                "attachment_count": row.get("attachment_count"),
                "total_bytes": row.get("total_bytes"),
                "error_code": row.get("receipt_error_code"),
            },
            "snapshot_observed_at": _iso(row.get("snapshot_observed_at")),
            "imported_at": _iso(row.get("created_at")),
        },
        "evidence_as_of": _iso(row.get("updated_at") or row.get("created_at")),
        "links": _links(
            overview=_console_link(org_id, project_id, "imports", object_id),
            raw_evidence=_console_link(org_id, project_id, "imports", object_id, "raw-evidence"),
            validation=_console_link(org_id, project_id, "imports", object_id, "validation"),
            publication=_console_link(org_id, project_id, "imports", object_id, "publication"),
        ),
    }


def project_event_configuration(
    row: Mapping[str, Any], *, project_id: str, org_id: str | None = None
) -> dict[str, Any]:
    object_id = str(row["id"])
    datastream_id = str(row["datastream_id"])
    return {
        "object_ref": _object_ref(project_id, "events", object_id),
        "project_ref": {"object_type": "project", "id": project_id},
        "datastream_ref": {"object_type": "datastream", "id": datastream_id},
        "name": row.get("name") or object_id,
        "active_version_ref": (
            {
                "object_type": "event-configuration-version",
                "id": str(row["active_version_id"]),
                "version": row.get("active_version"),
            }
            if row.get("active_version_id")
            else None
        ),
        "connector_contract_ref": (
            {
                "object_type": "connector-contract-version",
                "id": str(row["connector_contract_version_id"]),
                "fingerprint": row.get("connector_fingerprint"),
            }
            if row.get("connector_contract_version_id")
            else None
        ),
        "states": {
            "lifecycle": row.get("lifecycle_state") or "unavailable",
            "review": row.get("review_state") or "unavailable",
            "binding": "pinned" if row.get("active_version_id") else "unavailable",
            "collection": "observed" if row.get("latest_observation_at") else "unavailable",
            "usage": "linked" if int(row.get("observation_count") or 0) else "unavailable",
        },
        "evidence": {
            "source_mapping": row.get("source_mapping") or {},
            "collection_policy": row.get("collection_policy") or {},
            "normalized_payload_hash": row.get("normalized_payload_hash"),
            "version_created_at": _iso(row.get("version_created_at")),
            "latest_observation_at": _iso(row.get("latest_observation_at")),
            "observation_count": int(row.get("observation_count") or 0),
        },
        "evidence_as_of": _iso(row.get("updated_at") or row.get("created_at")),
        "links": _links(
            overview=_console_link(org_id, project_id, "events", object_id),
            source_mapping=_console_link(org_id, project_id, "events", object_id, "source-mapping"),
            collection=_console_link(org_id, project_id, "events", object_id, "collection"),
            usage=_console_link(org_id, project_id, "events", object_id, "usage"),
        ),
    }


def project_connector(
    row: Mapping[str, Any], *, project_id: str, org_id: str | None = None
) -> dict[str, Any]:
    from core.connector_installation import sanitize_blocking_cause

    object_id = str(row["connector_id"])
    environment = str(row.get("environment") or "default")
    route_id = object_id if environment == "default" else f"{object_id}@{environment}"
    datastream_count = int(row.get("datastream_count") or 0)
    raw_blocking_cause = row.get("blocking_cause")
    blocking_cause = (
        sanitize_blocking_cause(raw_blocking_cause) if raw_blocking_cause is not None else None
    )
    return {
        "object_ref": _object_ref(project_id, "connectors", route_id),
        "project_ref": {"object_type": "project", "id": project_id},
        "connector_id": object_id,
        "environment": environment,
        "active_version_ref": (
            {
                "object_type": "connector-contract-version",
                "id": str(row["active_contract_version_id"]),
                "version": row.get("active_contract_version"),
                "fingerprint": row.get("connector_fingerprint"),
            }
            if row.get("active_contract_version_id")
            else None
        ),
        "states": {
            "installation": row.get("installation_state") or "unavailable",
            "activation": row.get("activation_state") or "unavailable",
            "contract": "versioned" if row.get("active_contract_version_id") else "unavailable",
            "coverage": "used" if datastream_count else "unused",
        },
        "evidence": {
            "blocking_cause": blocking_cause,
            "last_verified_at": _iso(row.get("last_verified_at")),
            "contract_schema_version": row.get("contract_schema_version"),
            "contract": row.get("contract_snapshot") or {},
            "validation": row.get("validation_evidence") or {},
            "datastream_count": datastream_count,
        },
        "evidence_as_of": _iso(row.get("updated_at") or row.get("contract_created_at")),
        "links": _links(
            overview=_console_link(org_id, project_id, "connectors", route_id),
            versions=_console_link(org_id, project_id, "connectors", route_id, "versions"),
        ),
    }


_PROJECTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "datastreams": project_datastream,
    "sources": project_source_account,
    "imports": project_import,
    "events": project_event_configuration,
    "connectors": project_connector,
}

_ACTIONS = {
    "datastreams": "datastream.create",
    "sources": "source-account.connect",
    "imports": "import.create",
    "events": "event-configuration.create",
    "connectors": "connector.inspect",
}


def _merge_open_issues(conn: Any, project_id: str, rows: list[dict[str, Any]]) -> None:
    """Story 59.2 -- the Datastream's open issues, merged into the row it belongs to.

    ONE GROUPED AGGREGATE FOR THE WHOLE LENS, and it lands here rather than in
    the projector because `_PROJECTORS[lens](row, project_id=…, org_id=…)` is a
    signature the six lenses share: widening it would touch all six for a fact
    that belongs to one. The projector then receives a row that already carries
    its count, and holds no opinion about where it came from.

    A `LEFT JOIN LATERAL` in the lens query was the other candidate and it is
    refused on shape, not on today's row count: no index on `app.dq_issues`
    covers `(project_id, datastream_id)` for an issue with `execution_id` NULL
    (`idx_dq_issues_execution` is partial on `IS NOT NULL`), so the per-row form
    would have needed a migration to stay bounded. The grouped read is served by
    an index that already exists.

    FAIL-SOFT, AND THE ABSENCE IS THE MESSAGE. An unreadable issue store leaves
    the key OFF the row: the screen then says the count could not be read, which
    is not the same statement as "no issue is open" and must never be rendered as
    a `0`. Taking the whole fleet down for it would hide the twelve other columns
    that were read perfectly well.

    THE SWALLOW LIVES IN `dq_governance.read_open_issue_counts`, NOT HERE, AND IT
    IS A SAVEPOINT RATHER THAN A `try/except`. A `try/except` around the aggregate
    caught the Python exception and left the TRANSACTION aborted, so this lens
    degraded correctly while `_compose_overview` -- which reads the six lenses on
    the same connection -- raised `InFailedSqlTransaction` on the next one. The
    fail-soft protected the lens that would have survived anyway and broke the one
    that composes all six.
    """

    from core.dq_governance import (  # noqa: PLC0415 -- read model imports its reader lazily
        NO_ISSUE_SUMMARY,
        read_open_issue_counts,
    )

    summaries = read_open_issue_counts(
        conn,
        project_id=project_id,
        # Arbitrage 1: acknowledged is REVIEWED for this badge. The predicate is
        # not copied -- the question is declared, and `dq_governance` owns the
        # answer.
        include_acknowledged=False,
    )
    # `None` is "the read failed"; `{}` is "it succeeded and found nothing". Only
    # the first leaves the key off the row.
    if summaries is None:
        return
    for row in rows:
        row["open_issues"] = summaries.get(str(row.get("id") or ""), NO_ISSUE_SUMMARY)


#: What a lens merges into its rows AFTER the query and BEFORE the projection.
#: One entry today; the seam exists so a second lens cannot be tempted into a
#: per-row subquery to get the same shape.
_ROW_MERGERS: dict[str, Callable[[Any, str, list[dict[str, Any]]], None]] = {
    "datastreams": _merge_open_issues,
}


def _fetch_rows(conn: Any, lens: str, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(DATA_SURFACE_QUERIES[lens], (project_id,))
        columns = [description[0] for description in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    merge = _ROW_MERGERS.get(lens)
    if merge is not None and rows:
        merge(conn, project_id, rows)
    return rows


def _latest_evidence(items: list[Mapping[str, Any]]) -> str | None:
    values = [str(item["evidence_as_of"]) for item in items if item.get("evidence_as_of")]
    return max(values) if values else None


def _empty_reason(lens: str) -> dict[str, str]:
    return {
        "code": f"{lens}_evidence_empty",
        "message": f"No owned {lens} evidence is available for this Project.",
    }


#: The collections whose envelope carries the page facts -- `total`, `bound`,
#: `next_cursor`, `applied_filters`, `filter_options`.
#:
#: FOUR OF THE FIVE, AND THE FIFTH IS NOT AN OVERSIGHT. `sources` and `events`
#: joined `imports` and `connectors` on 2026-08-17: both screens already send a
#: cursor and already draw the footer (`Sources.tsx:163`,
#: `BusinessContextPanel.tsx:92`), and `DataCollectionLayout` renders those
#: controls only when the envelope carries the facts -- so the two pagers were
#: mounted against an envelope that could never light them. `datastreams` stays
#: out because its rows carry a merged aggregate (`_merge_open_issues`) whose
#: fail-soft is per-fleet, and paging it is a story of its own rather than a
#: line in this set.
#:
#: NOTHING HERE IS ESTIMATED. Every lens fetches its whole authorized snapshot
#: before this function runs, so `total` is the number of rows that matched the
#: filters -- counted, never inferred from a page size.
PAGED_LENSES = {"sources", "imports", "events", "connectors"}

#: The lenses whose rows name a Datastream, and therefore the only ones whose
#: `datastreams` filter can narrow anything. `sources` names an authorization
#: scope instead, so offering the filter there would be a control that empties
#: the table whatever is picked.
DATASTREAM_FILTERED_LENSES = {"imports", "events"}


def _compose_collection(
    project_id: str,
    lens: str,
    conn: Any,
    *,
    object_id: str | None,
    can_edit: bool,
    filters: Mapping[str, str] | None = None,
    limit: int | None = None,
    cursor: int = 0,
) -> dict[str, Any]:
    org_id = _project_org(conn, project_id)
    rows = _fetch_rows(conn, lens, project_id)
    projector = _PROJECTORS[lens]
    items = [projector(row, project_id=project_id, org_id=org_id) for row in rows]
    if object_id is not None:
        items = [item for item in items if item["object_ref"]["id"] == object_id]
        if not items:
            raise DataObjectNotFound(object_id)
    collection_meta: dict[str, Any] | None = None
    if object_id is None and lens in PAGED_LENSES:
        applied = {key: value for key, value in (filters or {}).items() if value}
        query = applied.get("q", "").casefold()
        state = applied.get("state", "")
        date_from = applied.get("from", "")
        date_to = applied.get("to", "")
        datastream = applied.get("datastream", "")
        all_state_options = sorted(
            {str(value) for item in items for value in item.get("states", {}).values() if value}
        )
        all_datastream_options = sorted(
            {
                str(item.get("datastream_ref", {}).get("id"))
                for item in items
                if item.get("datastream_ref", {}).get("id")
            }
        )
        if query:
            items = [
                item
                for item in items
                if query
                in " ".join(
                    str(value)
                    for value in (
                        item.get("name"),
                        item.get("label"),
                        item.get("connector_id"),
                        # The Connector a row BELONGS to, which is a column on
                        # the Sources table and was not searchable: only the
                        # `connectors` lens carries `connector_id` at the root,
                        # every other lens names its Connector through a ref.
                        (item.get("connector_ref") or {}).get("id"),
                        item.get("object_ref", {}).get("id"),
                        (item.get("datastream_ref") or {}).get("id"),
                    )
                    if value
                ).casefold()
            ]
        if state:
            items = [item for item in items if state in item.get("states", {}).values()]
        if datastream:
            items = [
                item
                for item in items
                if item.get("datastream_ref", {}).get("id") == datastream
            ]
        if lens == "imports" and date_from:
            items = [
                item
                for item in items
                if str(
                    item.get("evidence", {}).get("imported_at")
                    or item.get("evidence_as_of")
                    or ""
                )[:10] >= date_from
            ]
        if lens == "imports" and date_to:
            items = [
                item
                for item in items
                if (
                    observed := str(
                        item.get("evidence", {}).get("imported_at")
                        or item.get("evidence_as_of")
                        or ""
                    )[:10]
                )
                and observed <= date_to
            ]
        total = len(items)
        if cursor and cursor >= total:
            raise ValueError("cursor does not reference an available collection page")
        page_limit = limit or 25
        items = items[cursor : cursor + page_limit]
        next_cursor = cursor + page_limit if cursor + page_limit < total else None
        collection_meta = {
            "total": total,
            "bound": page_limit,
            "next_cursor": str(next_cursor) if next_cursor is not None else None,
            "applied_filters": applied,
            "filter_options": {
                "states": all_state_options,
                "datastreams": all_datastream_options,
            },
        }
    unavailable = [] if items else [_empty_reason(lens)]
    actions = [_ACTIONS[lens]] if can_edit or lens == "connectors" else []
    return build_collection_envelope(
        project_id=project_id,
        lens=lens,
        items=items,
        evidence_as_of=_latest_evidence(items),
        unavailable_reasons=unavailable,
        allowed_actions=actions,
        collection_meta=collection_meta,
    )


def _compose_overview(project_id: str, conn: Any, *, can_edit: bool) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    org_id = _project_org(conn, project_id)
    for lens in _LENS_TYPES:
        rows = _fetch_rows(conn, lens, project_id)
        projector = _PROJECTORS[lens]
        items = [projector(row, project_id=project_id, org_id=org_id) for row in rows]
        if not items:
            unavailable.append(_empty_reason(lens))
        state_counts: dict[str, dict[str, int]] = {}
        for item in items:
            for axis, value in item.get("states", {}).items():
                axis_counts = state_counts.setdefault(axis, {})
                axis_counts[value] = axis_counts.get(value, 0) + 1
        summaries.append(
            {
                "object_ref": {
                    "object_type": "data-lens",
                    "id": lens,
                    "href": f"/api/projects/{project_id}/{_API_PATHS[lens]}",
                },
                "lens": lens,
                "object_count": len(items),
                "states": {"evidence": "available" if items else "unavailable"},
                "evidence": {"state_counts": state_counts},
                "evidence_as_of": _latest_evidence(items),
                "links": {"collection": f"/api/projects/{project_id}/{_API_PATHS[lens]}"},
            }
        )
    allowed = [value for key, value in _ACTIONS.items() if can_edit and key != "connectors"]
    return build_collection_envelope(
        project_id=project_id,
        lens="overview",
        items=summaries,
        evidence_as_of=_latest_evidence(summaries),
        unavailable_reasons=unavailable,
        allowed_actions=allowed,
    )


def compose_data_surface(
    project_id: str,
    lens: str,
    conn: Any,
    *,
    object_id: str | None = None,
    can_edit: bool = False,
    filters: Mapping[str, str] | None = None,
    limit: int | None = None,
    cursor: int = 0,
) -> dict[str, Any]:
    """Compose one canonical Data lens from a single authorized DB snapshot."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")
    if lens == "overview":
        if object_id is not None:
            raise ValueError("Data Overview has no object detail")
        return _compose_overview(project_id, conn, can_edit=can_edit)
    if lens not in _LENS_TYPES:
        raise ValueError(f"unsupported Data lens: {lens}")
    return _compose_collection(
        project_id,
        lens,
        conn,
        object_id=object_id,
        can_edit=can_edit,
        filters=filters,
        limit=limit,
        cursor=cursor,
    )
