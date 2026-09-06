"""Observations and physical Datastream bindings for tracked entities (Story 48.5).

This is the DATA half of Competitors. Governance
(:mod:`core.tracked_entities`) owns what an identity means and what a source
calls it. Everything here is physical: which exact Datastream can see it, which
report and fields it was seen through, and the exact request a future run would
make.

THE JOIN THAT WAS WRONG

``CompetitorsCompiler`` decided a Datastream was applicable by comparing
``datastream.module_name`` to a binding's opaque ``source`` string. Two strings
matching is not a capability. It meant every enabled Datastream of a connector
looked applicable whether or not its selected report could return an entity at
all, and one active binding could mark a whole Project Complete.

Applicability is now read from the Connector's own ``tracked_entity``
declaration (Story 48.5, AC3) intersected with the report this Datastream
actually selected. A connector with no declaration is ``not_applicable`` with a
reason. A connector that declares a report this Datastream did not select is
``not_applicable`` too -- with a different reason, because "this source cannot"
and "this Datastream does not" are different facts and an operator repairs them
in different places.

WHAT A CONFIRMATION MAY AND MAY NOT DO

A confirmed Project registry edit writes binding versions in
``application_state='candidate'``. It does not publish them, does not move a
plan, mapping or publication pointer, and does not dispatch a pull. Normal Data
review remains the only path to ``published``, and only a published binding
supplies values to :func:`resolve_query_drivers`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from core.master_data import MasterDataConflict, MasterDataNotFound, content_hash

logger = logging.getLogger(__name__)

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

DIRECTION_OBSERVE = "observe"
DIRECTION_COLLECT = "collect"

STATE_CANDIDATE = "candidate"
STATE_PUBLISHED = "published"
STATE_SUPERSEDED = "superseded"
STATE_EXCLUDED = "excluded"

VALUE_OBSERVED = "observed"
VALUE_PRIVACY_TRUNCATED = "privacy_truncated"
VALUE_WITHHELD = "withheld"
VALUE_UNRESOLVED = "unresolved"

#: Why a Datastream cannot participate. Each one points at a different repair,
#: which is the whole reason they are not one `unavailable`.
REASON_NO_DECLARATION = "connector_declares_no_tracked_entity_support"
REASON_REPORT_NOT_SELECTED = "selected_report_declares_no_tracked_entity_support"
REASON_NO_REPORT_SELECTED = "datastream_has_no_selected_report"
REASON_DECLARED = "declared"


def _mint(prefix: str) -> str:
    return f"{prefix}_" + "".join(secrets.choice(_ALPHABET) for _ in range(26))


def hash_value(value: str) -> str:
    """The match key for an observed value.

    Hashed rather than stored plain because a provider value is untrusted input
    that may carry personal data. The display projection is a separate column an
    authorized view may read; the hash is what a decision matches on.
    """

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Detection. What a Connector declared, intersected with what this Datastream
# selected. No provider name appears anywhere below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Support:
    """Whether one Datastream can participate, and the evidence either way."""

    applicable: bool
    reason_code: str
    connector_name: str | None = None
    report_id: str | None = None
    direction: str | None = None
    entity_kinds: tuple[str, ...] = ()
    candidate_field_ids: tuple[str, ...] = ()
    identity_field_id: str | None = None
    label_field_id: str | None = None
    own_marker_field_id: str | None = None
    population: Mapping[str, Any] | None = None
    query_driver: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": "detected" if self.applicable else "not_applicable",
            "reason_code": self.reason_code,
            "connector_name": self.connector_name,
            "report_id": self.report_id,
            "direction": self.direction,
            "entity_kinds": list(self.entity_kinds),
            "candidate_field_ids": list(self.candidate_field_ids),
            "identity_field_id": self.identity_field_id,
            "label_field_id": self.label_field_id,
            "own_marker_field_id": self.own_marker_field_id,
            "population": dict(self.population or {}),
            "query_driver": dict(self.query_driver) if self.query_driver else None,
        }


def tracked_entity_declaration(module_name: str | None) -> dict[str, Any] | None:
    """Read one Connector's declaration from its installed manifest.

    Fail-soft on the loader being unavailable (a unit-test context, a partially
    started server) because an unreadable loader must not be interpreted as
    "this connector declares support".
    """

    if not module_name:
        return None
    try:
        from core.main import _loaded_modules  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- the loader is not available in every context
        return None
    manifest = next(
        (
            module.manifest
            for module in _loaded_modules
            if getattr(module, "name", None) == module_name
        ),
        None,
    )
    if not isinstance(manifest, dict):
        return None
    capabilities = manifest.get("source_capabilities")
    if not isinstance(capabilities, dict):
        return None
    declaration = capabilities.get("tracked_entity")
    return dict(declaration) if isinstance(declaration, dict) else None


def selected_report_id(datastream: Mapping[str, Any]) -> str | None:
    """The report this Datastream actually selected, from its own config."""

    config = datastream.get("config") or {}
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    report = config.get("report") if isinstance(config.get("report"), dict) else {}
    value = source.get("report_id") or report.get("id")
    return str(value) if value else None


def detect_support(
    datastream: Mapping[str, Any], declaration: Mapping[str, Any] | None = None
) -> Support:
    """Can THIS Datastream see a tracked entity? Read, never inferred."""

    module_name = datastream.get("module_name")
    if declaration is None:
        declaration = tracked_entity_declaration(module_name)
    if not declaration:
        return Support(
            applicable=False,
            reason_code=REASON_NO_DECLARATION,
            connector_name=str(module_name) if module_name else None,
        )
    report_id = selected_report_id(datastream)
    if not report_id:
        return Support(
            applicable=False,
            reason_code=REASON_NO_REPORT_SELECTED,
            connector_name=str(module_name) if module_name else None,
        )
    entry = next(
        (
            item
            for item in declaration.get("reports", [])
            if str(item.get("report_id")) == report_id
        ),
        None,
    )
    if entry is None:
        return Support(
            applicable=False,
            reason_code=REASON_REPORT_NOT_SELECTED,
            connector_name=str(module_name) if module_name else None,
            report_id=report_id,
        )
    return Support(
        applicable=True,
        reason_code=REASON_DECLARED,
        connector_name=str(module_name),
        report_id=report_id,
        direction=str(entry["direction"]),
        entity_kinds=tuple(entry.get("entity_kinds") or ()),
        candidate_field_ids=tuple(entry.get("candidate_field_ids") or ()),
        identity_field_id=entry.get("identity_field_id"),
        label_field_id=entry.get("label_field_id"),
        own_marker_field_id=entry.get("own_marker_field_id"),
        population=entry.get("population") or {},
        query_driver=entry.get("query_driver"),
    )


def connector_fingerprint(datastream: Mapping[str, Any], support: Support) -> str:
    """Pin exactly what a binding was validated against.

    Includes the Datastream's own capability fingerprint AND the declaration
    shape: a connector that keeps its fingerprint but changes which field
    carries the identity has still invalidated every binding built on it.
    """

    return content_hash(
        {
            "capability_fingerprint": datastream.get("capability_fingerprint"),
            "connector_name": support.connector_name,
            "report_id": support.report_id,
            "direction": support.direction,
            "identity_field_id": support.identity_field_id,
            "candidate_field_ids": sorted(support.candidate_field_ids),
            "query_driver": dict(support.query_driver or {}),
        }
    )


# ---------------------------------------------------------------------------
# Observations. What was actually seen, with the evidence that makes it worth
# something.
# ---------------------------------------------------------------------------

_OBSERVATION_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "datastream_id",
    "connection_ref_id",
    "account_scope",
    "connector_name",
    "connector_fingerprint",
    "report_id",
    "field_id",
    "grain",
    "pull_id",
    "publication_version_id",
    "plan_version_id",
    "mapping_version_id",
    "raw_value_hash",
    "normalized_value",
    "display_value",
    "value_state",
    "population_completeness",
    "observed_from",
    "observed_to",
    "occurrence_count",
    "created_by",
    "created_at",
)


def record_observation(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    connector_name: str,
    connector_fingerprint_value: str,
    report_id: str,
    field_id: str,
    raw_value: str,
    normalized_value: str,
    observed_from: date | str,
    observed_to: date | str,
    actor: str,
    display_value: str | None = None,
    value_state: str = VALUE_OBSERVED,
    population_completeness: str = "declared_scope",
    grain: Sequence[str] = (),
    connection_ref_id: str | None = None,
    account_scope: str | None = None,
    pull_id: str | None = None,
    publication_version_id: str | None = None,
    plan_version_id: str | None = None,
    mapping_version_id: str | None = None,
    occurrence_count: int = 1,
) -> dict[str, Any]:
    """Append one observation. Replaying the same window updates nothing.

    The uniqueness key is (Datastream, report, field, value, window). A replayed
    publication therefore adds no rows and inflates no count -- an important
    property once coverage numbers are computed from these.
    """

    digest = hash_value(raw_value)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.entity_observation_versions
                (id, org_id, project_id, datastream_id, connection_ref_id, account_scope,
                 connector_name, connector_fingerprint, report_id, field_id, grain,
                 pull_id, publication_version_id, plan_version_id, mapping_version_id,
                 raw_value_hash, normalized_value, display_value, value_state,
                 population_completeness, observed_from, observed_to, occurrence_count,
                 created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {", ".join(_OBSERVATION_COLUMNS)}
            """,
            (
                _mint("eobs"),
                org_id,
                project_id,
                datastream_id,
                connection_ref_id,
                account_scope,
                connector_name,
                connector_fingerprint_value,
                report_id,
                field_id,
                list(grain),
                pull_id,
                publication_version_id,
                plan_version_id,
                mapping_version_id,
                digest,
                normalized_value,
                display_value,
                value_state,
                population_completeness,
                observed_from,
                observed_to,
                occurrence_count,
                actor,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return dict(zip(_OBSERVATION_COLUMNS, row, strict=False))
        cur.execute(
            f"""
            SELECT {", ".join(_OBSERVATION_COLUMNS)}
            FROM app.entity_observation_versions
            WHERE project_id = %s AND datastream_id = %s AND report_id = %s
              AND field_id = %s AND raw_value_hash = %s
              AND observed_from = %s AND observed_to = %s
            """,
            (project_id, datastream_id, report_id, field_id, digest, observed_from, observed_to),
        )
        existing = cur.fetchone()
    if existing is None:  # pragma: no cover - only under a concurrent delete
        raise MasterDataConflict("observation could not be recorded or read back")
    return dict(zip(_OBSERVATION_COLUMNS, existing, strict=False))


def record_observations_for_pull(
    conn,
    *,
    datastream: Mapping[str, Any],
    project_id: str,
    pull_id: str,
    date_from: date | str,
    date_to: date | str,
    actor: str,
    connection_ref_id: str | None = None,
    org_id: str | None = None,
) -> int:
    """Record what a landed pull actually OBSERVED in its candidate fields (48.5).

    WHY THIS EXISTS. Measured 2026-08-04: ``record_observation`` above had ZERO
    production callers and ``app.entity_observation_versions`` held zero rows --
    while ``observation_summary`` and ``capability_compilers`` READ that table to
    show source coverage. The competitors surface could therefore only ever say
    "no candidate observed", whatever a Datastream collected. That is the exact
    defect AI-161 recorded one story earlier for the day-boundary evidence, and
    the repair has the same shape at the same seam.

    Reads rather than guesses: the candidate fields come from the connector's
    tracked-entity DECLARATION (``detect_support``), and the values from the rows
    this pull actually landed (``verification.distinct_raw_values``). A Datastream
    whose connector declares nothing records nothing -- and says so by writing no
    row, never by writing an empty observation.

    Returns the number of observations appended.
    """
    support = detect_support(datastream)
    if not support.applicable or not support.candidate_field_ids:
        return 0

    from core.verification import distinct_raw_values  # noqa: PLC0415

    observed = distinct_raw_values(
        pull_id,
        support.candidate_field_ids,
        provider=datastream.get("module_name"),
        project_id=project_id,
        # AI-302: WHICH relation this Datastream's rows landed in. Without it the
        # read falls back to the module's single registered table, which is a guess
        # for the 9 connectors that land in several -- and a governance read that
        # looks in the wrong table reports "nothing observed" about a pull that
        # observed plenty.
        report_profile_id=datastream.get("report_profile_id"),
    )
    if not observed:
        return 0

    # `get_datastream` does not carry org_id (its SELECT is project-scoped), so it
    # is resolved HERE rather than threaded through the caller: an observation
    # written under the wrong org would be readable by the wrong tenant, and a
    # parameter every caller must remember is a parameter someone will forget.
    if not org_id:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
        if not row or not row[0]:
            logger.warning(
                "entity_bindings: no org for project=%s -- observations skipped", project_id
            )
            return 0
        org_id = str(row[0])

    fingerprint_value = connector_fingerprint(datastream, support)
    written = 0
    for field_id, values in observed.items():
        for raw_value, occurrences in values.items():
            row = record_observation(
                conn,
                org_id=org_id,
                project_id=project_id,
                datastream_id=str(datastream.get("id") or ""),
                connector_name=str(support.connector_name or ""),
                connector_fingerprint_value=fingerprint_value,
                report_id=str(support.report_id or ""),
                field_id=str(field_id),
                raw_value=raw_value,
                # Normalisation is casefold only, deliberately. Deciding that two
                # spellings are the SAME entity is the registry's job, with a
                # confidence and a confirmation -- doing it here would be a silent
                # identity decision, which is the fifth `Incomplete if` of this
                # surface ("source matches apply without confidence and
                # confirmation").
                normalized_value=raw_value.strip().casefold(),
                display_value=raw_value,
                observed_from=date_from,
                observed_to=date_to,
                actor=actor,
                connection_ref_id=connection_ref_id,
                pull_id=pull_id,
                occurrence_count=occurrences,
            )
            if row:
                written += 1
    return written


def list_observations(
    conn,
    *,
    project_id: str,
    datastream_id: str | None = None,
    value_states: Sequence[str] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses = ["project_id = %s"]
    params: list[Any] = [project_id]
    if datastream_id:
        clauses.append("datastream_id = %s")
        params.append(datastream_id)
    if value_states:
        clauses.append("value_state = ANY(%s)")
        params.append(list(value_states))
    params.append(max(1, min(int(limit), 5000)))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_OBSERVATION_COLUMNS)}
            FROM app.entity_observation_versions
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return [
            dict(zip(_OBSERVATION_COLUMNS, row, strict=False)) for row in cur.fetchall()
        ]


def observation_summary(conn, *, project_id: str) -> dict[str, dict[str, Any]]:
    """Per-Datastream counts, including the ones that resolved to nothing.

    ``unresolved``, ``withheld`` and ``privacy_truncated`` are counted
    separately and deliberately. Folding them into "no competitor found" is the
    exact inference the ratified contract forbids: a value the provider withheld
    is not evidence that nothing was there.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT datastream_id, value_state, COUNT(*),
                   BOOL_OR(population_completeness = 'reportable_subset')
              FROM app.entity_observation_versions
             WHERE project_id = %s
             GROUP BY datastream_id, value_state
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    summary: dict[str, dict[str, Any]] = {}
    for datastream_id, value_state, count, partial_population in rows:
        entry = summary.setdefault(
            str(datastream_id),
            {"total": 0, "by_state": {}, "population_is_subset": False},
        )
        entry["total"] += int(count)
        entry["by_state"][str(value_state)] = int(count)
        entry["population_is_subset"] = entry["population_is_subset"] or bool(partial_population)
    return summary


# ---------------------------------------------------------------------------
# Physical bindings.
# ---------------------------------------------------------------------------

_BINDING_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "datastream_id",
    "source_identity_version_id",
    "node_id",
    "project_role",
    "project_association_id",
    "version_number",
    "connector_name",
    "connection_ref_id",
    "account_scope",
    "report_id",
    "field_ids",
    "direction",
    "query_driver",
    "connector_fingerprint",
    "plan_version_id",
    "mapping_version_id",
    "publication_version_id",
    "application_state",
    "exception_reason_code",
    "exception_reason",
    "content_hash",
    "created_by",
    "created_at",
    "published_at",
    "published_by",
)


def _binding(row: Any) -> dict[str, Any]:
    return dict(zip(_BINDING_COLUMNS, row, strict=False))


def list_bindings(
    conn,
    *,
    project_id: str,
    datastream_id: str | None = None,
    node_ids: Sequence[str] | None = None,
    states: Sequence[str] = (STATE_CANDIDATE, STATE_PUBLISHED, STATE_EXCLUDED),
) -> list[dict[str, Any]]:
    clauses = ["project_id = %s", "application_state = ANY(%s)"]
    params: list[Any] = [project_id, list(states)]
    if datastream_id:
        clauses.append("datastream_id = %s")
        params.append(datastream_id)
    if node_ids is not None:
        if not node_ids:
            return []
        clauses.append("node_id = ANY(%s)")
        params.append(list(node_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_BINDING_COLUMNS)}
            FROM app.datastream_entity_binding_versions
            WHERE {" AND ".join(clauses)}
            ORDER BY datastream_id, node_id, version_number DESC
            """,
            tuple(params),
        )
        return [_binding(row) for row in cur.fetchall()]


def propose_binding(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    node_id: str,
    project_role: str,
    support: Support,
    fingerprint: str,
    actor: str,
    source_identity_version_id: str | None = None,
    project_association_id: str | None = None,
    connection_ref_id: str | None = None,
    account_scope: str | None = None,
    driver_values: Sequence[str] = (),
    own_values: Sequence[str] = (),
    plan_version_id: str | None = None,
    mapping_version_id: str | None = None,
) -> dict[str, Any]:
    """Record a NON-LIVE binding candidate for one identity on one Datastream.

    Returns the existing row untouched when a live binding already carries the
    same content, and PRESERVES a local exclusion rather than overwriting it --
    a Project-wide edit must never silently undo a Datastream-level decision
    someone took deliberately.
    """

    if not support.applicable:
        raise MasterDataConflict(
            f"this Datastream cannot bind a tracked entity: {support.reason_code}"
        )
    driver = {}
    if support.query_driver:
        driver = dict(support.query_driver) | {
            "values": sorted({str(item) for item in driver_values}),
            "own_values": sorted({str(item) for item in own_values}),
        }
    digest = content_hash(
        {
            "datastream_id": datastream_id,
            "node_id": node_id,
            "project_role": project_role,
            "report_id": support.report_id,
            "direction": support.direction,
            "account_scope": account_scope,
            "source_identity_version_id": source_identity_version_id,
            "query_driver": driver,
            "connector_fingerprint": fingerprint,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_BINDING_COLUMNS)}
            FROM app.datastream_entity_binding_versions
            WHERE project_id = %s AND datastream_id = %s AND node_id = %s
              AND application_state = ANY(%s)
            """,
            (
                project_id,
                datastream_id,
                node_id,
                [STATE_CANDIDATE, STATE_PUBLISHED, STATE_EXCLUDED],
            ),
        )
        row = cur.fetchone()
        if row is not None:
            existing = _binding(row)
            if existing["application_state"] == STATE_EXCLUDED:
                # A scoped exception outranks the Project intent. Confirming a
                # global edit reports it as preserved; it does not resolve it.
                return existing
            if existing["content_hash"] == digest:
                return existing
            cur.execute(
                """
                UPDATE app.datastream_entity_binding_versions
                   SET application_state = 'superseded'
                 WHERE id = %s
                """,
                (existing["id"],),
            )
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.datastream_entity_binding_versions
            WHERE project_id = %s AND datastream_id = %s AND node_id = %s
            """,
            (project_id, datastream_id, node_id),
        )
        (next_number,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO app.datastream_entity_binding_versions
                (id, org_id, project_id, datastream_id, source_identity_version_id,
                 node_id, project_role, project_association_id, version_number,
                 connector_name, connection_ref_id, account_scope, report_id, field_ids,
                 direction, query_driver, connector_fingerprint, plan_version_id,
                 mapping_version_id, application_state, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s, %s, %s, 'candidate', %s, %s)
            RETURNING {", ".join(_BINDING_COLUMNS)}
            """,
            (
                _mint("deb"),
                org_id,
                project_id,
                datastream_id,
                source_identity_version_id,
                node_id,
                project_role,
                project_association_id,
                next_number,
                support.connector_name,
                connection_ref_id,
                account_scope,
                support.report_id,
                sorted(
                    {
                        value
                        for value in (
                            *support.candidate_field_ids,
                            support.identity_field_id,
                            support.label_field_id,
                            support.own_marker_field_id,
                        )
                        if value
                    }
                ),
                support.direction,
                json.dumps(driver),
                fingerprint,
                plan_version_id,
                mapping_version_id,
                digest,
                actor,
            ),
        )
        return _binding(cur.fetchone())


def publish_binding(
    conn, *, project_id: str, binding_id: str, actor: str, publication_version_id: str | None = None
) -> dict[str, Any]:
    """Data's own decision. Nothing in the capability lifecycle calls this."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.datastream_entity_binding_versions
               SET application_state = 'published', published_at = NOW(), published_by = %s,
                   publication_version_id = COALESCE(%s, publication_version_id)
             WHERE project_id = %s AND id = %s AND application_state = 'candidate'
            RETURNING {", ".join(_BINDING_COLUMNS)}
            """,
            (actor, publication_version_id, project_id, binding_id),
        )
        row = cur.fetchone()
    if row is None:
        raise MasterDataNotFound("no candidate binding with that id in this Project")
    return _binding(row)


def exclude_binding(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    node_id: str,
    reason_code: str,
    reason: str,
    actor: str,
    support: Support | None = None,
    fingerprint: str = "",
) -> dict[str, Any]:
    """Record a reasoned, Datastream-scoped refusal to bind this identity."""

    if not reason_code or not reason:
        raise MasterDataConflict("an exclusion states its reason")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastream_entity_binding_versions
               SET application_state = 'superseded'
             WHERE project_id = %s AND datastream_id = %s AND node_id = %s
               AND application_state IN ('candidate', 'published')
            """,
            (project_id, datastream_id, node_id),
        )
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.datastream_entity_binding_versions
            WHERE project_id = %s AND datastream_id = %s AND node_id = %s
            """,
            (project_id, datastream_id, node_id),
        )
        (next_number,) = cur.fetchone()
        digest = content_hash(
            {
                "datastream_id": datastream_id,
                "node_id": node_id,
                "excluded": True,
                "reason_code": reason_code,
                "version_number": next_number,
            }
        )
        cur.execute(
            f"""
            INSERT INTO app.datastream_entity_binding_versions
                (id, org_id, project_id, datastream_id, node_id, project_role,
                 version_number, connector_name, report_id, direction, query_driver,
                 connector_fingerprint, application_state, exception_reason_code,
                 exception_reason, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{{}}'::jsonb, %s,
                    'excluded', %s, %s, %s, %s)
            RETURNING {", ".join(_BINDING_COLUMNS)}
            """,
            (
                _mint("deb"),
                org_id,
                project_id,
                datastream_id,
                node_id,
                "reference",
                next_number,
                (support.connector_name if support else None) or "unknown",
                (support.report_id if support else None) or "unknown",
                (support.direction if support else None) or DIRECTION_OBSERVE,
                fingerprint or content_hash({"excluded": datastream_id, "node": node_id}),
                reason_code,
                reason,
                digest,
                actor,
            ),
        )
        return _binding(cur.fetchone())


def apply_confirmed_fan_out(
    conn,
    *,
    org_id: str,
    project_id: str,
    proposals: Sequence[Mapping[str, Any]],
    actor: str,
) -> dict[str, Any]:
    """Turn one confirmed Project registry edit into per-Datastream candidates.

    This is the fan-out, and the four things it does NOT do are the contract:

    * it never publishes -- every binding lands in ``candidate``, so normal Data
      review stays the only route to a run consuming it;
    * it never moves a plan, mapping or publication pointer;
    * it never dispatches a pull;
    * it never overwrites a Datastream-scoped exclusion. An excluded pair is
      counted as PRESERVED and reported, because silently undoing a local
      decision is how a global edit destroys work someone did deliberately.

    It also cannot reach another Project: `proposals` are this Change Set's, and
    every write below is keyed on the `project_id` the caller was authorized for.
    Organization identity reuse fans nothing sideways.
    """

    from core.tracked_entities import (  # noqa: PLC0415
        list_source_identities,
        project_roles,
    )

    applicable = [
        proposal
        for proposal in proposals
        if str(proposal.get("capability_key")) == "competitors"
        and str(proposal.get("applicability")) == "applicable"
    ]
    summary = {
        "datastreams_considered": len(applicable),
        "bindings_created": 0,
        "bindings_unchanged": 0,
        "exceptions_preserved": 0,
        "skipped_without_source_identity": 0,
        "published": 0,  # Always zero. Stated so the absence is verifiable.
    }
    if not applicable:
        return summary

    roles = project_roles(conn, project_id=project_id)
    if not roles:
        return summary
    node_ids = [str(role["node_id"]) for role in roles]
    identities: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for identity in list_source_identities(conn, org_id=org_id, node_ids=node_ids):
        key = (str(identity["node_id"]), str(identity["connector_name"]))
        identities.setdefault(key, []).append(identity)

    datastreams = _datastreams_by_id(
        conn, project_id=project_id, ids=[str(item["datastream_id"]) for item in applicable]
    )

    for proposal in applicable:
        datastream = datastreams.get(str(proposal["datastream_id"]))
        if datastream is None:
            continue
        support = detect_support(datastream)
        if not support.applicable:
            # The proposal was compiled against a contract that has since changed.
            # Refusing here rather than binding is the whole point of the pin.
            continue
        fingerprint = connector_fingerprint(datastream, support)
        for role in roles:
            node_id = str(role["node_id"])
            matched = identities.get((node_id, str(support.connector_name)), [])
            if support.direction == DIRECTION_COLLECT and not matched:
                summary["skipped_without_source_identity"] += 1
                continue
            identity = matched[0] if matched else None
            before = list_bindings(
                conn,
                project_id=project_id,
                datastream_id=str(datastream["id"]),
                node_ids=[node_id],
            )
            result = propose_binding(
                conn,
                org_id=org_id,
                project_id=project_id,
                datastream_id=str(datastream["id"]),
                node_id=node_id,
                project_role=str(role["project_role"]),
                support=support,
                fingerprint=fingerprint,
                actor=actor,
                source_identity_version_id=(identity or {}).get("id"),
                project_association_id=str(role["id"]),
                connection_ref_id=datastream.get("connection_ref_id"),
                account_scope=(identity or {}).get("account_scope"),
                driver_values=[str((identity or {}).get("external_id"))] if identity else [],
                own_values=(
                    [str((identity or {}).get("external_id"))]
                    if identity and str(role["project_role"]) == "own"
                    else []
                ),
            )
            if result["application_state"] == STATE_EXCLUDED:
                summary["exceptions_preserved"] += 1
            elif before and before[0]["id"] == result["id"]:
                summary["bindings_unchanged"] += 1
            else:
                summary["bindings_created"] += 1
    return summary


def _datastreams_by_id(
    conn, *, project_id: str, ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """The exact rows the compiler read, re-read at confirmation time."""

    if not ids:
        return {}
    with conn.cursor() as cur:
        # `capability_fingerprint` IS NOT A COLUMN OF `app.datastreams` -- it lives
        # on the plan version (and on the mapping version), and this SELECT named
        # it on the wrong relation. Postgres raised `UndefinedColumn` on the FIRST
        # row it was asked for, so binding confirmation failed for every project
        # with an applicable proposal, and passed only on the empty set the guard
        # above returns early for. Found 2026-08-23 by preparing every literal SQL
        # string of `server/core` against a migrated schema.
        #
        # JOINED, not dropped. `connector_fingerprint` reads this value with
        # `.get()`, so deleting the column from the SELECT would have "fixed" the
        # crash by hashing `None` -- every binding pinned to a fingerprint that
        # can never change, which is the opposite of what the pin exists for. The
        # join is the one `capability_proposals._DATASTREAM_QUERY` already makes,
        # column for column.
        cur.execute(
            """
            SELECT d.id, d.module_name, d.config, d.connection_ref_id,
                   plan.capability_fingerprint
              FROM app.datastreams d
              LEFT JOIN app.datastream_plan_versions plan
                     ON plan.id = d.current_plan_version_id
                    AND plan.datastream_id = d.id
                    AND plan.project_id = d.project_id
             WHERE d.project_id = %s AND d.id = ANY(%s) AND d.archived_at IS NULL
            """,
            (project_id, list(ids)),
        )
        columns = [description[0] for description in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        config = row.get("config")
        if isinstance(config, str):
            row["config"] = json.loads(config)
        elif config is None:
            row["config"] = {}
    return {str(row["id"]): row for row in rows}


def resolve_query_drivers(
    conn, *, project_id: str, datastream_id: str
) -> dict[str, list[str]]:
    """The exact keyword arguments a run may pass, from PUBLISHED bindings only.

    A candidate binding contributes nothing. That is the line between "the
    operator approved the registry" and "the data pipeline was changed", and it
    is enforced here rather than trusted to a caller.
    """

    drivers: dict[str, list[str]] = {}
    for binding in list_bindings(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        states=(STATE_PUBLISHED,),
    ):
        driver = binding.get("query_driver") or {}
        parameter = driver.get("parameter")
        if not parameter:
            continue
        values = drivers.setdefault(str(parameter), [])
        for value in driver.get("values") or []:
            if value not in values:
                values.append(str(value))
        own_parameter = driver.get("own_marker_parameter")
        if own_parameter:
            own_values = drivers.setdefault(str(own_parameter), [])
            for value in driver.get("own_values") or []:
                if value not in own_values:
                    own_values.append(str(value))
    return {key: sorted(value) for key, value in drivers.items()}


__all__ = [
    "DIRECTION_COLLECT",
    "DIRECTION_OBSERVE",
    "REASON_DECLARED",
    "REASON_NO_DECLARATION",
    "REASON_NO_REPORT_SELECTED",
    "REASON_REPORT_NOT_SELECTED",
    "STATE_CANDIDATE",
    "STATE_EXCLUDED",
    "STATE_PUBLISHED",
    "Support",
    "apply_confirmed_fan_out",
    "connector_fingerprint",
    "detect_support",
    "exclude_binding",
    "hash_value",
    "list_bindings",
    "list_observations",
    "observation_summary",
    "propose_binding",
    "publish_binding",
    "record_observation",
    "record_observations_for_pull",
    "resolve_query_drivers",
    "selected_report_id",
    "tracked_entity_declaration",
]
