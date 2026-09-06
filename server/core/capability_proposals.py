"""The common, source-agnostic per-Datastream capability proposal contract (Story 48.1).

One governed capability change compiles into one exact proposal per applicable
Datastream. This module owns that contract and nothing else:

* the coverage vocabulary and the denominator invariant
  ``applicable = complete + partial + unavailable + excluded + pending``;
* one deterministic serializer, shared by an existing Datastream and by the
  preconfiguration of a new one, so both produce the same content hash;
* the generic half of the impact shape -- grain, cardinality/scan, quota/cost,
  recent history, historical coverage, backfill and fan-out -- computed from
  evidence every Datastream already has;
* a registry through which a capability plugs its own semantics in.

What it deliberately does not own: the capability semantics themselves. Country,
Currency & FX, Reporting Timezone, Tax & Fees and Competitors are compiled by
adapters in ``core.capability_compilers`` that consume their owners' versioned
contracts. The common core never branches on a provider or connector name, and a
capability that cannot prove its governed evidence yields an explicit
``unavailable`` with a canonical repair route -- never an optimistic default.

Coverage is a count of persisted rows, not an assertion. Before this contract,
Project Settings reported every active Datastream as ``pending`` because nothing
per-Datastream was ever written down.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Protocol

from ulid import ULID

# The five states that partition the applicable denominator, plus the one that
# stays out of it. Order is the reporting order; it is not alphabetical.
COVERAGE_STATES: tuple[str, ...] = ("complete", "partial", "unavailable", "excluded", "pending")
NOT_APPLICABLE = "not_applicable"
ALL_COVERAGE_STATES: tuple[str, ...] = (*COVERAGE_STATES, NOT_APPLICABLE)

#: Every block the ratified Data contract requires a capability to expose for one
#: Datastream. Migration 139 enforces their presence; this tuple is the source.
IMPACT_BLOCKS: tuple[str, ...] = (
    "detected_support_selection",
    "grain_before_after",
    "cardinality_scan",
    "quota_cost",
    "recent_history",
    "historical_coverage",
    "backfill",
    "fan_out",
)

PROPOSAL_SCHEMA = "datastream_capability_proposal.v1"


class CapabilityProposalError(ValueError):
    """The compiled proposal or aggregate violates the common contract."""

    code = "invalid_capability_proposal"


class CapabilityGovernanceUnavailable(RuntimeError):
    """Required governed owner evidence is missing; the caller must fail closed."""

    code = "capability_governance_unavailable"


# ---------------------------------------------------------------------------
# Canonical hashing -- one implementation, shared with the Change Set payload.
# ---------------------------------------------------------------------------


def _by_value(value: Any) -> Any:
    """Render a governed policy object by its VALUES, or refuse.

    `project_evidence` carries the resolved policy objects a capability's `assess`
    reads -- `MoneyPolicy`, `TimezonePolicy` -- and `_dependency_snapshot`
    fingerprints that whole dict. `json.dumps` cannot serialize a frozen dataclass,
    so the fingerprint raised, `prepare_change_set` mapped it to
    `project_settings_unavailable`, and PREPARING ANY CHANGE SET ON A PROJECT WITH A
    CONFIRMED MONEY OR TIMEZONE POLICY ANSWERED 503.

    Measured 2026-08-23, and the reason nobody met it: all 32 production Projects
    carry `currency_fx` and `reporting_timezone` at `draft`, so no Project has ever
    had a policy to serialize. The two requirements were mutually exclusive --
    without a confirmed policy `confirm` refuses `change_set_blocked`, with one
    `prepare` answered 503 -- which closed the activation path of EVERY optional
    capability, Country included.

    `asdict` and not `repr`: a fingerprint over a repr would change the day someone
    adds a field to the dataclass, silently invalidating every prepared Change Set.
    Anything that is neither a dataclass nor JSON still raises, so this widens what
    can be hashed without widening what can be hashed WRONGLY -- and because
    `json.dumps` only consults this hook for values it could not serialize itself,
    no hash that exists today can change.
    """

    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"{type(value).__name__} has no canonical value form")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_by_value,
        )
    except (TypeError, ValueError) as exc:
        raise CapabilityProposalError("proposal content must be JSON serializable") from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The owner reference. The semantic contract already exists (Story 46.1 route
# registry + the server-side factory); this only names capability owners with it.
# A browser URL is never stored: the client builds hrefs from the canonical
# registry, so a route Epic 49 renames cannot be frozen into a pinned version.
# ---------------------------------------------------------------------------

#: Canonical Governance section owning each capability's definitions, taken from
#: the ratified boundary table in ``docs/product-architecture/project-settings.md``
#: and expressed with the section slugs declared in the 46.1 navigation registry.
GOVERNANCE_SECTION_BY_CAPABILITY: dict[str, str] = {
    "country": "master-data",
    "currency_fx": "semantic-model",
    "reporting_timezone": "controls-quality",
    "tax_fees": "controls-quality",
    "competitors": "master-data",
    # Story 61.5. THIS ENTRY IS NOT OPTIONAL BOOKKEEPING: the function below
    # RAISES on a key it does not know, and `read_datastream_capabilities` calls
    # it for every key of `CAPABILITY_SPECS` on every Datastream. A sixth key
    # declared there and missing here takes down the Workbench projection of
    # every Datastream on every Project.
    #
    # `master-data`, from the ratified boundary: "Governance owns media plans,
    # their versions and the ordered matching rules" (`project-settings.md`). A
    # media plan is a governed business object with its own identity and
    # versions, which is what Master Data holds ("enabled registries",
    # `governance.md`); the matching rules are the mechanism that binds observed
    # placements to it, not the definition of the object.
    "placement_mapping": "master-data",
    # Story 70.3, and `master-data` for a reason this table has already had to
    # state once: the objects the capability depends on are Governance-owned
    # business identities. The MDM common key IS a Master Data object
    # (`app.mdm_common_keys`), and the Semantic View relationship that approves
    # crossing on it pins that key's exact version. The method cascade is the
    # mechanism that binds observed rows to the identity, not the definition of
    # the identity -- the same split that put Placement Mapping here.
    #
    # NOT OPTIONAL BOOKKEEPING, for the reason written six lines up: the function
    # below RAISES on a key it does not know, and it is called for every key of
    # `CAPABILITY_SPECS` on every Datastream.
    "analytics_alignment": "master-data",
}


def governance_owner_reference(
    capability_key: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """Return the semantic Governance owner reference for *capability_key*."""
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    section = GOVERNANCE_SECTION_BY_CAPABILITY.get(capability_key)
    if section is None:
        raise CapabilityProposalError(f"unknown capability: {capability_key!r}")
    return owner_reference(
        "governance",
        section,
        object_type=object_type,
        object_id=object_id,
        version_id=version_id,
        # A version is only addressable inside an object's own workbench tab, and
        # the router refuses `version_id` without one. `versions` is the tab every
        # Epic 49 object contract carries for its history.
        tab="versions" if version_id else None,
    )


def datastream_owner_reference(
    datastream_id: str, *, tab: str = "overview", action: str | None = None
) -> dict[str, Any]:
    """Return the semantic Data owner reference for one Datastream Workbench tab."""
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    return owner_reference(
        "data",
        "datastreams",
        object_type="datastream",
        object_id=datastream_id,
        tab=tab,
        action=action,
    )


# ---------------------------------------------------------------------------
# What a capability adapter returns.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GovernanceOwner:
    """One versioned Governance object a proposal depends on."""

    object_type: str
    object_id: str
    version_id: str
    evidence_hash: str

    def as_reference(self, capability_key: str) -> dict[str, Any]:
        return {
            "owner_kind": "governance",
            "object_type": self.object_type,
            "object_id": self.object_id,
            "version_id": self.version_id,
            "evidence_hash": self.evidence_hash,
            "capability_key": capability_key,
            "owner_reference": governance_owner_reference(
                capability_key,
                object_type=self.object_type,
                object_id=self.object_id,
                version_id=self.version_id,
            ),
        }


@dataclass(frozen=True, slots=True)
class CapabilityException:
    """A scoped, reasoned deviation with an accountable owner."""

    kind: str  # "exception" | "exclusion"
    severity: str  # "blocking" | "degrading" | "informational"
    reason_code: str
    reason: str
    owner_kind: str = "governance"
    evidence_hash: str | None = None
    evidence_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityBlocker:
    """A condition that must be repaired before activation may proceed."""

    code: str
    message: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class CapabilityAssessment:
    """One capability's verdict on one Datastream."""

    applicability: str  # "applicable" | "not_applicable"
    coverage_state: str
    reason: str
    detected_support_selection: dict[str, Any] = field(default_factory=dict)
    grain_after: list[str] | None = None
    governance_owners: tuple[GovernanceOwner, ...] = ()
    blockers: tuple[CapabilityBlocker, ...] = ()
    exceptions: tuple[CapabilityException, ...] = ()
    repair: dict[str, Any] | None = None
    #: Adapter-supplied overrides for generic blocks it can compute better.
    impact_overrides: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.applicability not in {"applicable", NOT_APPLICABLE}:
            raise CapabilityProposalError("applicability is invalid")
        if self.coverage_state not in ALL_COVERAGE_STATES:
            raise CapabilityProposalError("coverage state is invalid")
        if (self.applicability == NOT_APPLICABLE) != (self.coverage_state == NOT_APPLICABLE):
            raise CapabilityProposalError("applicability and coverage state disagree")
        if self.coverage_state == "excluded" and not any(
            exc.kind == "exclusion" for exc in self.exceptions
        ):
            # An exclusion is a governed decision. Absence of support is
            # `unavailable`; it may never be laundered into `excluded`.
            raise CapabilityProposalError("excluded coverage requires an exclusion decision")
        if not self.reason.strip():
            raise CapabilityProposalError("a coverage state must carry its reason")


@dataclass(frozen=True, slots=True)
class CompileContext:
    """Everything a compiler may read, resolved once per prepare."""

    org_id: str
    project_id: str
    capability_key: str
    change_set_id: str
    intent: dict[str, Any]
    requested_state: str  # "enabled" | "disabled"
    project_evidence: dict[str, Any]
    actor: str
    loaded_modules: list[Any] | None = None


class CapabilityCompiler(Protocol):
    """The plug point. A capability answers for one Datastream at a time."""

    capability_key: str

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """Resolve Project-wide governed evidence once, before the fan-out."""

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        """Return this capability's verdict for one Datastream."""


_COMPILERS: dict[str, CapabilityCompiler] = {}


def register_compiler(compiler: CapabilityCompiler) -> CapabilityCompiler:
    """Register *compiler* for its capability key (idempotent, last wins)."""
    from core.project_settings import CAPABILITY_SPECS  # noqa: PLC0415

    if compiler.capability_key not in CAPABILITY_SPECS:
        raise CapabilityProposalError(f"unknown capability: {compiler.capability_key!r}")
    _COMPILERS[compiler.capability_key] = compiler
    return compiler


def compiler_for(capability_key: str) -> CapabilityCompiler:
    """Return the registered compiler, importing the adapters on first use."""
    if capability_key not in _COMPILERS:
        import core.capability_compilers  # noqa: F401, PLC0415 -- registers on import
    compiler = _COMPILERS.get(capability_key)
    if compiler is None:
        raise CapabilityProposalError(f"no compiler registered for {capability_key!r}")
    return compiler


# ---------------------------------------------------------------------------
# Coverage aggregation. The denominator is the whole point.
# ---------------------------------------------------------------------------


def aggregate_coverage(states: list[str]) -> dict[str, Any]:
    """Aggregate exact per-Datastream states into the truthful coverage counts.

    ``excluded`` stays inside the applicable denominator -- a Datastream a human
    decided to leave out is still in scope, and hiding it would inflate the
    percentage. ``not_applicable`` is counted separately and never enters it.

    Zero applicable Datastreams renders ``Not applicable``. It is never 100%:
    a percentage over an empty denominator is the exact failure the ratified
    Project Settings contract names.
    """
    counts = {state: 0 for state in ALL_COVERAGE_STATES}
    for state in states:
        if state not in counts:
            raise CapabilityProposalError(f"unknown coverage state: {state!r}")
        counts[state] += 1
    applicable = sum(counts[state] for state in COVERAGE_STATES)
    result: dict[str, Any] = {
        "applicable": applicable,
        **{state: counts[state] for state in COVERAGE_STATES},
        "not_applicable": counts[NOT_APPLICABLE],
    }
    if applicable != sum(counts[state] for state in COVERAGE_STATES):  # pragma: no cover
        raise CapabilityProposalError("coverage denominator invariant violated")
    if applicable == 0:
        return {**result, "label": "Not applicable", "percentage": None}
    percentage = round((counts["complete"] / applicable) * 100, 1)
    return {**result, "label": f"{percentage:g}% complete", "percentage": percentage}


def settings_coverage(coverage: dict[str, Any]) -> dict[str, int]:
    """Project the aggregate onto the six counts the Change Set payload carries."""
    return {
        "applicable": int(coverage["applicable"]),
        **{state: int(coverage[state]) for state in COVERAGE_STATES},
    }


# ---------------------------------------------------------------------------
# The generic impact blocks. Computed from evidence every Datastream has.
# ---------------------------------------------------------------------------


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _grain_before_after(
    mapping_payload: dict[str, Any], grain_after: list[str] | None
) -> dict[str, Any]:
    before = [str(item) for item in (mapping_payload.get("grain") or [])]
    after = before if grain_after is None else [str(item) for item in grain_after]
    added = [item for item in after if item not in before]
    removed = [item for item in before if item not in after]
    return {
        "before": before,
        "after": after,
        "added": added,
        "removed": removed,
        # A removed grain dimension is a loss of resolution, never a silent drop.
        "changes_grain": bool(added or removed),
    }


def _cardinality_scan(
    mapping_payload: dict[str, Any], grain_after: list[str] | None, preferences: dict[str, Any]
) -> dict[str, Any]:
    from core.datastream_projection import (  # noqa: PLC0415
        estimate_cardinality_and_scan,
        resolve_thresholds,
    )

    if not mapping_payload.get("fields"):
        return {"state": "unavailable", "reason": "No mapping version pins a field universe."}
    max_card, max_scan, source = resolve_thresholds(preferences)
    payload = dict(mapping_payload)
    if grain_after is not None:
        payload["grain"] = list(grain_after)
    estimate = estimate_cardinality_and_scan(
        payload,
        max_grain_cardinality=max_card,
        max_scan_bytes=max_scan,
        threshold_source=source,
    )
    return {"state": "estimated", **estimate}


def _quota_cost(connector_contract: dict[str, Any]) -> dict[str, Any]:
    quota = connector_contract.get("quota_cost")
    if not isinstance(quota, dict) or not quota:
        return {
            "state": "unavailable",
            "reason": "The Connector contract declares no quota or cost bound.",
        }
    return {"state": "declared", **quota}


def _fan_out(conn, project_id: str, datastream_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.consumer_kind, COUNT(DISTINCT u.consumer_ref) AS consumers
                 FROM app.datastream_output_used_by u
                 JOIN app.datastream_outputs o ON o.id = u.output_id
                WHERE o.project_id = %s AND o.datastream_id = %s
                GROUP BY u.consumer_kind ORDER BY u.consumer_kind""",
            (project_id, datastream_id),
        )
        by_kind = {str(row[0]): int(row[1]) for row in cur.fetchall()}
    return {
        "state": "observed",
        "downstream_consumers": sum(by_kind.values()),
        "by_consumer_kind": by_kind,
    }


def _history(conn, project_id: str, datastream_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(recent_history, historical_coverage)`` -- recent-first, then depth."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, state, state_changed_at, row_count
                 FROM app.datastream_executions
                WHERE project_id = %s AND datastream_id = %s
                ORDER BY created_at DESC LIMIT 5""",
            (project_id, datastream_id),
        )
        recent = [
            {
                "execution_id": row[0],
                "state": row[1],
                "at": row[2].isoformat() if row[2] else None,
                "row_count": row[3],
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            """SELECT MIN(date_from)::text, MAX(date_to)::text, COUNT(*)
                 FROM app.pull_jobs
                WHERE datastream_id = %s AND state = 'done'""",
            (datastream_id,),
        )
        row = cur.fetchone()
    earliest, latest, completed = (row or (None, None, 0))
    recent_history = {
        "state": "observed" if recent else "unavailable",
        "executions": recent,
        "reason": None if recent else "No execution has run for this Datastream yet.",
    }
    historical_coverage = {
        "state": "observed" if earliest else "unavailable",
        "earliest_completed_date": earliest,
        "latest_completed_date": latest,
        "completed_pulls": int(completed or 0),
        "reason": None if earliest else "No completed pull evidences retained history.",
    }
    return recent_history, historical_coverage


def _backfill(
    connector_contract: dict[str, Any], historical_coverage: dict[str, Any], required: bool
) -> dict[str, Any]:
    bound = connector_contract.get("max_provider_backfill_days")
    bounded = isinstance(bound, int) and bound > 0
    return {
        "required": bool(required),
        "feasible": bool(bounded) if required else None,
        "max_provider_backfill_days": bound if bounded else None,
        "bound_evidence": "provider_capability" if bounded else "unavailable",
        "earliest_retained_date": historical_coverage.get("earliest_completed_date"),
    }


# ---------------------------------------------------------------------------
# The deterministic serializer. One shape for an existing Datastream and for the
# preconfiguration of a new one -- the setup Draft store is not repurposed.
# ---------------------------------------------------------------------------


def serialize_proposal(
    *,
    project_id: str,
    org_id: str,
    datastream_id: str,
    capability_key: str,
    change_set_id: str,
    base_configuration_version_id: str | None,
    intended_configuration_content_hash: str,
    connector_contract: dict[str, Any],
    source_schema: dict[str, Any],
    current_plan_version_id: str | None,
    current_mapping_version_id: str | None,
    current_published_execution_id: str | None,
    assessment: CapabilityAssessment,
    impact: dict[str, Any],
    dependency_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Serialize one proposal deterministically, then hash exactly what was said.

    The hash covers the compiled verdict and every dependency pin, and excludes
    the minted id and the clock -- so the same inputs always produce the same
    content hash whether the caller arrived over REST, over MCP or through
    Datastream preconfiguration.
    """
    if set(impact) != set(IMPACT_BLOCKS):
        missing = sorted(set(IMPACT_BLOCKS) - set(impact))
        extra = sorted(set(impact) - set(IMPACT_BLOCKS))
        raise CapabilityProposalError(
            f"impact blocks are inexact (missing={missing}, unexpected={extra})"
        )
    governance_owner_references = [
        owner.as_reference(capability_key) for owner in assessment.governance_owners
    ]
    body = {
        "schema": PROPOSAL_SCHEMA,
        "project_id": project_id,
        "org_id": org_id,
        "datastream_id": datastream_id,
        "capability_key": capability_key,
        "change_set_id": change_set_id,
        "base_configuration_version_id": base_configuration_version_id,
        "intended_configuration_content_hash": intended_configuration_content_hash,
        "connector_contract": connector_contract,
        "source_schema": source_schema,
        "current_plan_version_id": current_plan_version_id,
        "current_mapping_version_id": current_mapping_version_id,
        "current_published_execution_id": current_published_execution_id,
        "governance_owner_references": sorted(
            governance_owner_references,
            key=lambda ref: (ref["object_type"], ref["object_id"], ref["version_id"]),
        ),
        "applicability": assessment.applicability,
        "coverage_state": assessment.coverage_state,
        "coverage_reason": assessment.reason,
        "impact": impact,
        "blocker_references": [
            {"code": item.code, "message": item.message, "required": item.required}
            for item in assessment.blockers
        ],
        "exception_references": [
            {
                "kind": item.kind,
                "severity": item.severity,
                "reason_code": item.reason_code,
                "reason": item.reason,
                "owner_kind": item.owner_kind,
            }
            for item in assessment.exceptions
        ],
        "repair_reference": assessment.repair,
        "dependency_snapshot": dependency_snapshot,
    }
    body["dependency_fingerprint"] = canonical_hash(dependency_snapshot)
    body["content_hash"] = canonical_hash(body)
    return body


# ---------------------------------------------------------------------------
# Compilation. Every active, non-archived Datastream receives exactly one row.
# ---------------------------------------------------------------------------

_DATASTREAM_QUERY = """
SELECT d.id, d.name, d.org_id, d.module_name, d.source_kind, d.data_role,
       d.lifecycle_state, d.config, d.connection_ref_id,
       d.current_plan_version_id, d.current_mapping_version_id,
       d.current_published_execution_id,
       plan.normalized_payload AS plan_payload, plan.content_hash AS plan_content_hash,
       plan.capability_fingerprint,
       mapping.mapping_payload, mapping.source_schema_hash,
       mapping.content_hash AS mapping_content_hash
  FROM app.datastreams d
  LEFT JOIN app.datastream_plan_versions plan
         ON plan.id = d.current_plan_version_id
        AND plan.datastream_id = d.id AND plan.project_id = d.project_id
  LEFT JOIN app.datastream_mapping_versions mapping
         ON mapping.id = d.current_mapping_version_id
        AND mapping.datastream_id = d.id AND mapping.project_id = d.project_id
 WHERE d.project_id = %s
   AND d.archived_at IS NULL
   AND d.enabled = TRUE
   AND d.lifecycle_state = 'active'
 ORDER BY d.id
"""


def fetch_applicable_datastreams(conn, project_id: str) -> list[dict[str, Any]]:
    """Read every active, non-archived Datastream with its exact current pins."""
    with conn.cursor() as cur:
        cur.execute(_DATASTREAM_QUERY, (project_id,))
        columns = [description[0] for description in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        row["plan_payload"] = _json(row.get("plan_payload"), {})
        row["mapping_payload"] = _json(row.get("mapping_payload"), {})
        row["config"] = _json(row.get("config"), {})
    return rows


def _project_preferences(conn, project_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT max_projection_grain_cardinality, max_projection_scan_bytes
                 FROM app.project_preferences WHERE project_id = %s""",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return {
        "max_projection_grain_cardinality": row[0],
        "max_projection_scan_bytes": row[1],
    }


def _connector_contract(datastream: dict[str, Any]) -> dict[str, Any]:
    """Pin the installed adapter contract without naming any provider here."""
    config = datastream.get("config") or {}
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    report = config.get("report") if isinstance(config.get("report"), dict) else {}
    quota = report.get("quota_cost") if isinstance(report.get("quota_cost"), dict) else {}
    return {
        "module_name": datastream.get("module_name"),
        "source_kind": datastream.get("source_kind"),
        "connection_ref_id": datastream.get("connection_ref_id"),
        "report_id": source.get("report_id") or report.get("id"),
        "capability_fingerprint": datastream.get("capability_fingerprint"),
        "quota_cost": quota,
        "max_provider_backfill_days": report.get("max_provider_backfill_days"),
    }


def _source_schema(datastream: dict[str, Any]) -> dict[str, Any]:
    mapping_payload = datastream.get("mapping_payload") or {}
    fields = mapping_payload.get("fields") or []
    return {
        "source_schema_hash": datastream.get("source_schema_hash"),
        "mapping_content_hash": datastream.get("mapping_content_hash"),
        "field_count": len(fields) if isinstance(fields, list) else 0,
        "plan_content_hash": datastream.get("plan_content_hash"),
    }


def _dependency_snapshot(
    datastream: dict[str, Any], project_evidence: dict[str, Any]
) -> dict[str, Any]:
    """Pin every dimension a confirmation must recheck for this Datastream."""
    return {
        "datastream_id": datastream.get("id"),
        "lifecycle_state": datastream.get("lifecycle_state"),
        "connector_contract": _connector_contract(datastream),
        "source_schema": _source_schema(datastream),
        "current_plan_version_id": datastream.get("current_plan_version_id"),
        "current_mapping_version_id": datastream.get("current_mapping_version_id"),
        "current_published_execution_id": datastream.get("current_published_execution_id"),
        "project_evidence_fingerprint": canonical_hash(project_evidence),
    }


def compile_capability_proposals(
    conn,
    *,
    org_id: str,
    project_id: str,
    change_set_id: str,
    capability_key: str,
    requested_state: str,
    intent: dict[str, Any],
    base_configuration_version_id: str | None,
    intended_configuration_content_hash: str,
    actor: str,
    loaded_modules: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Compile one exact proposal for every active, non-archived Datastream."""
    compiler = compiler_for(capability_key)
    project_evidence = compiler.project_evidence(conn, project_id=project_id, org_id=org_id)
    context = CompileContext(
        org_id=org_id,
        project_id=project_id,
        capability_key=capability_key,
        change_set_id=change_set_id,
        intent=intent,
        requested_state=requested_state,
        project_evidence=project_evidence,
        actor=actor,
        loaded_modules=loaded_modules,
    )
    preferences = _project_preferences(conn, project_id)
    proposals: list[dict[str, Any]] = []
    for datastream in fetch_applicable_datastreams(conn, project_id):
        assessment = compiler.assess(conn, context=context, datastream=datastream)
        connector_contract = _connector_contract(datastream)
        mapping_payload = datastream.get("mapping_payload") or {}
        recent_history, historical_coverage = _history(
            conn, project_id, str(datastream["id"])
        )
        grain = _grain_before_after(mapping_payload, assessment.grain_after)
        impact = {
            "detected_support_selection": assessment.detected_support_selection,
            "grain_before_after": grain,
            "cardinality_scan": _cardinality_scan(
                mapping_payload, assessment.grain_after, preferences
            ),
            "quota_cost": _quota_cost(connector_contract),
            "recent_history": recent_history,
            "historical_coverage": historical_coverage,
            "backfill": _backfill(
                connector_contract,
                historical_coverage,
                required=bool(grain["added"]) and assessment.applicability == "applicable",
            ),
            "fan_out": _fan_out(conn, project_id, str(datastream["id"])),
        }
        impact.update(
            {
                key: value
                for key, value in assessment.impact_overrides.items()
                if key in IMPACT_BLOCKS
            }
        )
        proposals.append(
            serialize_proposal(
                project_id=project_id,
                org_id=str(datastream.get("org_id") or org_id),
                datastream_id=str(datastream["id"]),
                capability_key=capability_key,
                change_set_id=change_set_id,
                base_configuration_version_id=base_configuration_version_id,
                intended_configuration_content_hash=intended_configuration_content_hash,
                connector_contract=connector_contract,
                source_schema=_source_schema(datastream),
                current_plan_version_id=datastream.get("current_plan_version_id"),
                current_mapping_version_id=datastream.get("current_mapping_version_id"),
                current_published_execution_id=datastream.get("current_published_execution_id"),
                assessment=assessment,
                impact=impact,
                dependency_snapshot=_dependency_snapshot(datastream, project_evidence),
            )
        )
    return proposals


# ---------------------------------------------------------------------------
# Persistence. Proposals are immutable; a replay reuses the stored row.
# ---------------------------------------------------------------------------


def persist_proposals(
    conn, *, project_id: str, change_set_id: str, proposals: list[dict[str, Any]], actor: str
) -> list[dict[str, Any]]:
    """Persist compiled proposals, returning each with its stable identity."""
    stored: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for proposal in proposals:
            cur.execute(
                """SELECT id FROM app.datastream_capability_proposals
                    WHERE project_id = %s AND content_hash = %s""",
                (project_id, proposal["content_hash"]),
            )
            existing = cur.fetchone()
            if existing is not None:
                stored.append({**proposal, "id": existing[0], "replayed": True})
                continue
            proposal_id = f"dscp_{ULID()}"
            cur.execute(
                """INSERT INTO app.datastream_capability_proposals
                   (id, org_id, project_id, datastream_id, capability_key, change_set_id,
                    base_configuration_version_id, intended_configuration_content_hash,
                    connector_contract, source_schema, current_plan_version_id,
                    current_mapping_version_id, current_published_execution_id,
                    governance_owner_references, applicability, coverage_state, impact,
                    blocker_references, exception_references, repair_reference,
                    dependency_snapshot, dependency_fingerprint, content_hash, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,
                           %s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s)""",
                (
                    proposal_id,
                    proposal["org_id"],
                    project_id,
                    proposal["datastream_id"],
                    proposal["capability_key"],
                    change_set_id,
                    proposal["base_configuration_version_id"],
                    proposal["intended_configuration_content_hash"],
                    _canonical_json(proposal["connector_contract"]),
                    _canonical_json(proposal["source_schema"]),
                    proposal["current_plan_version_id"],
                    proposal["current_mapping_version_id"],
                    proposal["current_published_execution_id"],
                    _canonical_json(proposal["governance_owner_references"]),
                    proposal["applicability"],
                    proposal["coverage_state"],
                    _canonical_json(proposal["impact"]),
                    _canonical_json(proposal["blocker_references"]),
                    _canonical_json(proposal["exception_references"]),
                    _canonical_json(proposal["repair_reference"])
                    if proposal["repair_reference"] is not None
                    else None,
                    _canonical_json(proposal["dependency_snapshot"]),
                    proposal["dependency_fingerprint"],
                    proposal["content_hash"],
                    actor,
                ),
            )
            stored.append({**proposal, "id": proposal_id, "replayed": False})
    return stored


def persist_exceptions(
    conn,
    *,
    org_id: str,
    project_id: str,
    change_set_id: str,
    proposals: list[dict[str, Any]],
    actor: str,
) -> list[dict[str, Any]]:
    """Persist each proposal's exceptions with a stable identity and owner."""
    references: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for proposal in proposals:
            capability_key = proposal["capability_key"]
            for item in proposal["exception_references"]:
                owner_reference = (
                    governance_owner_reference(capability_key)
                    if item["owner_kind"] == "governance"
                    else datastream_owner_reference(proposal["datastream_id"])
                )
                repair = proposal.get("repair_reference") or owner_reference
                evidence_hash = canonical_hash(
                    {
                        "proposal_content_hash": proposal["content_hash"],
                        "reason_code": item["reason_code"],
                    }
                )
                exception_id = f"capx_{ULID()}"
                cur.execute(
                    """INSERT INTO app.capability_exceptions
                       (id, org_id, project_id, capability_key, change_set_id, datastream_id,
                        proposal_id, kind, severity, reason_code, reason, owner_kind,
                        owner_reference, repair_reference, evidence_hash, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                       ON CONFLICT (project_id, change_set_id, capability_key, reason_code,
                                    COALESCE(datastream_id, ''))
                       DO UPDATE SET updated_at = NOW()
                       RETURNING id, lifecycle_state""",
                    (
                        exception_id,
                        org_id,
                        project_id,
                        capability_key,
                        change_set_id,
                        proposal["datastream_id"],
                        proposal.get("id"),
                        item["kind"],
                        item["severity"],
                        item["reason_code"],
                        item["reason"],
                        item["owner_kind"],
                        _canonical_json(owner_reference),
                        _canonical_json(repair),
                        evidence_hash,
                        actor,
                    ),
                )
                row = cur.fetchone()
                references.append(
                    {
                        "exception_id": row[0],
                        "lifecycle_state": row[1],
                        "capability_key": capability_key,
                        "datastream_id": proposal["datastream_id"],
                        "kind": item["kind"],
                        "severity": item["severity"],
                        "reason_code": item["reason_code"],
                        "reason": item["reason"],
                        "owner_kind": item["owner_kind"],
                        "owner_reference": owner_reference,
                        "repair_reference": repair,
                        "evidence_hash": evidence_hash,
                    }
                )
    return references


def persist_configuration_owner_references(
    conn,
    *,
    project_id: str,
    configuration_version_id: str,
    prior_configuration_version_id: str | None,
    proposals: list[dict[str, Any]],
) -> int:
    """Freeze the activated version's complete owner set.

    Every reference the new version depends on is written, and every reference
    the prior version carried that this change did not supersede is carried
    forward and marked as such -- so an active version always names its whole
    Data and Governance surface, not only the part that moved.
    """
    written: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for proposal in proposals:
        key = ("data", "datastream", proposal["datastream_id"], proposal["id"])
        written[key] = {
            "owner_kind": "data",
            "object_type": "datastream",
            "object_id": proposal["datastream_id"],
            "version_id": proposal["id"],
            "capability_key": proposal["capability_key"],
            "owner_reference": datastream_owner_reference(proposal["datastream_id"]),
            "evidence_hash": proposal["content_hash"],
            "proposal_id": proposal["id"],
        }
        for ref in proposal["governance_owner_references"]:
            gov_key = ("governance", ref["object_type"], ref["object_id"], ref["version_id"])
            written[gov_key] = {
                "owner_kind": "governance",
                "object_type": ref["object_type"],
                "object_id": ref["object_id"],
                "version_id": ref["version_id"],
                "capability_key": ref.get("capability_key"),
                "owner_reference": ref["owner_reference"],
                "evidence_hash": ref["evidence_hash"],
                "proposal_id": proposal["id"],
            }
    with conn.cursor() as cur:
        carried: list[dict[str, Any]] = []
        if prior_configuration_version_id:
            cur.execute(
                """SELECT owner_kind, object_type, object_id, version_id, capability_key,
                          owner_reference, evidence_hash
                     FROM app.project_configuration_owner_references
                    WHERE project_id = %s AND configuration_version_id = %s""",
                (project_id, prior_configuration_version_id),
            )
            for row in cur.fetchall():
                key = (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
                if key in written:
                    continue
                carried.append(
                    {
                        "owner_kind": row[0],
                        "object_type": row[1],
                        "object_id": row[2],
                        "version_id": row[3],
                        "capability_key": row[4],
                        "owner_reference": _json(row[5], {}),
                        "evidence_hash": row[6],
                        "proposal_id": None,
                    }
                )
        for entry in [*written.values(), *carried]:
            cur.execute(
                """INSERT INTO app.project_configuration_owner_references
                   (project_id, configuration_version_id, owner_kind, object_type, object_id,
                    version_id, capability_key, owner_reference, evidence_hash, proposal_id,
                    carried_forward)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (
                    project_id,
                    configuration_version_id,
                    entry["owner_kind"],
                    entry["object_type"],
                    entry["object_id"],
                    entry["version_id"],
                    entry["capability_key"],
                    _canonical_json(entry["owner_reference"]),
                    entry["evidence_hash"],
                    entry["proposal_id"],
                    entry["proposal_id"] is None,
                ),
            )
    return len(written) + len(carried)


# ---------------------------------------------------------------------------
# Read models. Settings, the Workbench, REST and MCP all read these two.
# ---------------------------------------------------------------------------

_PROPOSAL_COLUMNS = (
    "id",
    "datastream_id",
    "capability_key",
    "change_set_id",
    "applicability",
    "coverage_state",
    "impact",
    "blocker_references",
    "exception_references",
    "repair_reference",
    "governance_owner_references",
    "connector_contract",
    "source_schema",
    "current_plan_version_id",
    "current_mapping_version_id",
    "dependency_fingerprint",
    "content_hash",
    "created_at",
)


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    for key, default in (
        ("impact", {}),
        ("blocker_references", []),
        ("exception_references", []),
        ("repair_reference", None),
        ("governance_owner_references", []),
        ("connector_contract", {}),
        ("source_schema", {}),
    ):
        row[key] = _json(row.get(key), default)
    created = row.get("created_at")
    row["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    return row


def _latest_proposals(
    conn, *, project_id: str, capability_key: str | None = None, datastream_id: str | None = None
) -> list[dict[str, Any]]:
    """Read the newest proposal per (Datastream, capability) in Project scope."""
    columns = ", ".join(f"p.{name}" for name in _PROPOSAL_COLUMNS)
    filters = ["p.project_id = %s"]
    params: list[Any] = [project_id]
    if capability_key is not None:
        filters.append("p.capability_key = %s")
        params.append(capability_key)
    if datastream_id is not None:
        filters.append("p.datastream_id = %s")
        params.append(datastream_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {columns}, d.name AS datastream_name, d.module_name,
                       d.lifecycle_state
                  FROM app.datastream_capability_proposals p
                  JOIN app.datastreams d
                    ON d.id = p.datastream_id AND d.project_id = p.project_id
                 WHERE {" AND ".join(filters)} AND d.archived_at IS NULL
                   AND p.created_at = (
                       SELECT MAX(inner_p.created_at)
                         FROM app.datastream_capability_proposals inner_p
                        WHERE inner_p.project_id = p.project_id
                          AND inner_p.datastream_id = p.datastream_id
                          AND inner_p.capability_key = p.capability_key
                   )
                 ORDER BY p.capability_key, d.name, p.datastream_id""",
            params,
        )
        names = [description[0] for description in cur.description]
        return [_hydrate(dict(zip(names, row, strict=True))) for row in cur.fetchall()]


def read_project_capability(conn, *, project_id: str, capability_key: str) -> dict[str, Any]:
    """The Project/capability projection: exact rows plus derived coverage."""
    from core.project_settings import CAPABILITY_SPECS  # noqa: PLC0415

    if capability_key not in CAPABILITY_SPECS:
        raise CapabilityProposalError(f"unknown capability: {capability_key!r}")
    proposals = _latest_proposals(conn, project_id=project_id, capability_key=capability_key)
    coverage = aggregate_coverage([row["coverage_state"] for row in proposals])
    exceptions = read_exceptions(conn, project_id=project_id, capability_key=capability_key)
    return {
        "schema": "project_capability.v1",
        "project_id": project_id,
        "capability_key": capability_key,
        "availability": CAPABILITY_SPECS[capability_key]["availability"],
        "dependencies": CAPABILITY_SPECS[capability_key]["dependencies"],
        "coverage": coverage,
        "datastreams": [
            {
                "proposal_id": row["id"],
                "datastream_id": row["datastream_id"],
                "datastream_name": row["datastream_name"],
                "module_name": row["module_name"],
                "applicability": row["applicability"],
                "coverage_state": row["coverage_state"],
                "impact": row["impact"],
                "blockers": row["blocker_references"],
                "exceptions": row["exception_references"],
                "repair": row["repair_reference"],
                "governance_owners": row["governance_owner_references"],
                "owner_reference": datastream_owner_reference(row["datastream_id"]),
                "content_hash": row["content_hash"],
            }
            for row in proposals
        ],
        "exceptions": exceptions,
        "governance_owner_reference": governance_owner_reference(capability_key),
        "governance_owner": read_master_data_owner(
            conn, project_id=project_id, capability_key=capability_key
        ),
    }


#: The Master Data object kind each capability's definitions live in, for the
#: capabilities whose owner is a registry. Absent means the capability is owned
#: by another Governance section and has no registry to summarise -- not that
#: the lookup failed.
MASTER_DATA_KIND_BY_CAPABILITY: dict[str, str] = {
    "country": "country",
    "competitors": "competitor",
}


def read_master_data_owner(
    conn, *, project_id: str, capability_key: str
) -> dict[str, Any] | None:
    """A bounded summary of the registry that owns this capability's definitions.

    Generic on purpose: Story 48.2 forbids a Country-specific MCP tool family,
    so Country reaches a host through *this* payload. The summary is bounded and
    carries the canonical owner link for full editing -- an MCP host gets enough
    to reason and to point somewhere, never the whole hierarchy.
    """

    kind = MASTER_DATA_KIND_BY_CAPABILITY.get(capability_key)
    if kind is None:
        return None
    from core.master_data import registry_summary  # noqa: PLC0415

    return registry_summary(conn, project_id=project_id, object_kind=kind)


def read_datastream_capabilities(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """The Datastream/capability projection consumed by the Workbench.

    A CAPABILITY THAT IS OFF IS NOT IN THIS ANSWER -- amendment 2 of the
    2026-08-05 review, held open by this function until the 2026-08-11 one
    measured it. « Éteinte, la capacité n'apparaît nulle part -- ni onglet, ni
    panneau, ni colonne. » This loop iterated `CAPABILITY_SPECS`, the six DECLARED
    keys, and never opened `app.project_capabilities` at all, so every Datastream
    of every Project listed all six whatever the Project had decided. Measured on
    the review's Project: four `disabled`, two `draft`, none `ready` -- six of six
    should have been absent, and six of six were drawn. A reader was shown a
    `Country` module on a flux whose mapping carries no country field.

    THE SAME THRESHOLD AS THE TAB. `ready` and `degraded`, from
    `CAPABILITY_ACTIVE_STATES` -- the states `capabilityTabs.ts` already uses to
    open `Cost` and `Placements`. Two thresholds would let the tab and the panel
    disagree about one capability, which is the shape of the defect this fixes.

    An `unset` key -- no row at all, meaning `app.seed_project_capabilities` never
    ran for this Project -- is off here too. It is a fact about the control plane,
    not a default this read may paper over.
    """
    from core.project_capability_states import (  # noqa: PLC0415
        CAPABILITY_ACTIVE_STATES,
        read_capability_states,
    )
    from core.project_settings import CAPABILITY_SPECS  # noqa: PLC0415

    states = read_capability_states(
        conn, project_id=project_id, capability_keys=tuple(CAPABILITY_SPECS)
    )
    proposals = {
        row["capability_key"]: row
        for row in _latest_proposals(
            conn, project_id=project_id, datastream_id=datastream_id
        )
    }
    capabilities = []
    for capability_key, spec in CAPABILITY_SPECS.items():
        if str(states.get(capability_key, {}).get("state")) not in CAPABILITY_ACTIVE_STATES:
            continue
        row = proposals.get(capability_key)
        if row is None:
            capabilities.append(
                {
                    "capability_key": capability_key,
                    "availability": spec["availability"],
                    "applicability": "pending",
                    "coverage_state": "pending",
                    "reason": "No Change Set has compiled this capability for this Datastream.",
                    "impact": {},
                    "blockers": [],
                    "exceptions": [],
                    "repair": None,
                    "governance_owner_reference": governance_owner_reference(capability_key),
                }
            )
            continue
        capabilities.append(
            {
                "capability_key": capability_key,
                "availability": spec["availability"],
                "proposal_id": row["id"],
                "applicability": row["applicability"],
                "coverage_state": row["coverage_state"],
                "impact": row["impact"],
                "blockers": row["blocker_references"],
                "exceptions": row["exception_references"],
                "repair": row["repair_reference"],
                "governance_owners": row["governance_owner_references"],
                "governance_owner_reference": governance_owner_reference(capability_key),
                "content_hash": row["content_hash"],
            }
        )
    return {
        "schema": "datastream_capabilities.v1",
        "project_id": project_id,
        "datastream_id": datastream_id,
        "capabilities": capabilities,
        "primary_action": _primary_capability_action(capabilities),
    }


_ACTION_PRIORITY = {"unavailable": 0, "excluded": 1, "partial": 2, "pending": 3}


def _primary_capability_action(capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Name the single highest-priority repair or review, never a list of six."""
    blocked = [item for item in capabilities if item.get("blockers")]
    pool = blocked or [
        item
        for item in capabilities
        if item.get("coverage_state") in _ACTION_PRIORITY
        and item.get("applicability") == "applicable"
    ]
    if not pool:
        return None
    chosen = min(
        pool,
        key=lambda item: (
            0 if item.get("blockers") else 1,
            _ACTION_PRIORITY.get(str(item.get("coverage_state")), 9),
            str(item.get("capability_key")),
        ),
    )
    return {
        "capability_key": chosen["capability_key"],
        "kind": "repair" if chosen.get("blockers") else "review",
        "label": "Repair capability" if chosen.get("blockers") else "Review capability coverage",
        "reason": (
            chosen["blockers"][0]["message"]
            if chosen.get("blockers")
            else f"Coverage is {chosen.get('coverage_state')}."
        ),
        "owner_reference": chosen.get("repair") or chosen["governance_owner_reference"],
    }


def read_exceptions(
    conn, *, project_id: str, capability_key: str | None = None
) -> list[dict[str, Any]]:
    """Read exception references without copying the underlying rule or evidence."""
    filters = ["project_id = %s", "lifecycle_state <> 'superseded'"]
    params: list[Any] = [project_id]
    if capability_key is not None:
        filters.append("capability_key = %s")
        params.append(capability_key)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, capability_key, datastream_id, proposal_id, kind, severity,
                       reason_code, reason, owner_kind, owner_reference, repair_reference,
                       evidence_hash, evidence_version_id, lifecycle_state
                  FROM app.capability_exceptions
                 WHERE {" AND ".join(filters)}
                 ORDER BY severity, capability_key, reason_code, datastream_id""",
            params,
        )
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        row["owner_reference"] = _json(row.get("owner_reference"), {})
        row["repair_reference"] = _json(row.get("repair_reference"), {})
    return rows


def read_configuration_owner_references(
    conn, *, project_id: str, configuration_version_id: str
) -> list[dict[str, Any]]:
    """Read the complete immutable owner set of one activated version."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT owner_kind, object_type, object_id, version_id, capability_key,
                      owner_reference, evidence_hash, proposal_id, carried_forward
                 FROM app.project_configuration_owner_references
                WHERE project_id = %s AND configuration_version_id = %s
                ORDER BY owner_kind, object_type, object_id, version_id""",
            (project_id, configuration_version_id),
        )
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        row["owner_reference"] = _json(row.get("owner_reference"), {})
    return rows


# ---------------------------------------------------------------------------
# Callable helpers the Change Set orchestration needs.
# ---------------------------------------------------------------------------


def required_blockers(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the blockers that must stop activation, with their exact owner."""
    return [
        {
            **blocker,
            "capability_key": proposal["capability_key"],
            "datastream_id": proposal["datastream_id"],
            "owner_reference": proposal.get("repair_reference")
            or datastream_owner_reference(proposal["datastream_id"]),
        }
        for proposal in proposals
        for blocker in proposal["blocker_references"]
        if blocker.get("required", True)
    ]


def proposal_fingerprints(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Freeze the exact proposal identities a review is bound to."""
    return sorted(
        (
            {
                "proposal_id": proposal.get("id"),
                "datastream_id": proposal["datastream_id"],
                "capability_key": proposal["capability_key"],
                "content_hash": proposal["content_hash"],
                "dependency_fingerprint": proposal["dependency_fingerprint"],
            }
            for proposal in proposals
        ),
        key=lambda item: (item["capability_key"], item["datastream_id"]),
    )


def registered_compilers() -> dict[str, CapabilityCompiler]:
    """Expose the registry so a test can assert WHICH capabilities plug in.

    Not how many: the count is `len(CAPABILITY_SPECS)` and the interesting
    failure is a key declared in one registry and missing from the other, which
    only a comparison of names can catch.
    """
    return dict(_COMPILERS)
