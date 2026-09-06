"""Evidence-backed, non-authorizing Datastream preconfiguration compiler.

The pure compiler accepts only normalized, persisted evidence snapshots. It does
no provider I/O and owns no active Datastream, mapping, plan or publication
pointer. Persistence helpers for the stable draft lifecycle live below it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from core.audit import declare_action
from core.datastream_field_mapping import profile_fields
from core.datastream_projection import ProjectionCompileError, compile_projection
from core.first_report_draft import recommend_first_report

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_DATASTREAM_PRECONFIGURATION_COMPILED = declare_action("datastream.preconfiguration.compiled")
ACTION_DATASTREAM_SETUP_DRAFT_CREATED = declare_action("datastream.setup_draft.created")
ACTION_DATASTREAM_SETUP_DRAFT_REVISED = declare_action("datastream.setup_draft.revised")
ACTION_DATASTREAM_SETUP_DRAFT_DISCARDED = declare_action("datastream.setup_draft.discarded")



logger = logging.getLogger(__name__)

EvidenceKind = Literal[
    "connector_contract",
    "observed_metadata",
    "project_setting",
    "governance_preset",
    "operator_input",
]
Requirement = Literal["required", "recommended", "optional", "automatic"]
ItemStatus = Literal["complete", "missing", "blocked", "warning", "not_applicable", "needs_review"]

#: Channels whose file does not exist until something is delivered. An address
#: cannot be issued before the Datastream is materialized, so their schema
#: CANNOT be observed at setup. Stated here rather than imported: core never
#: reaches into the delivery seam (AD-2), so the set is declared on each side of
#: it and neither names the other.
_CHANNELS_WITH_NO_FILE_BEFORE_DELIVERY = frozenset({"inbound_email", "webhook"})

#: What such a channel says instead of a schema, in the vocabulary
#: `core.inbound_discovery.NO_DELIVERY_YET` already uses.
_AWAITING_FIRST_DELIVERY = "no_delivery_received_yet"


class EvidenceRef(TypedDict):
    kind: EvidenceKind
    object_type: str
    object_id: str
    version_id: str
    fingerprint: str
    observed_at: str


class PreconfigurationValidationError(ValueError):
    code = "invalid_preconfiguration_input"


SCHEMA_VERSION = "1"
SECTION_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "source": ("operator_input", "connector_contract"),
    "mode": ("operator_input",),
    "fields": ("operator_input", "connector_contract", "observed_metadata"),
    "grain": ("operator_input", "connector_contract", "observed_metadata"),
    "physical_mapping": (
        "operator_input",
        "connector_contract",
        "observed_metadata",
        "project_configuration",
        "governance_presets",
    ),
    "classification": (
        "connector_contract",
        "observed_metadata",
        "project_configuration",
        "governance_presets",
    ),
    "schedule": ("operator_input", "connector_contract", "observed_metadata"),
    "history": ("operator_input", "connector_contract", "observed_metadata"),
    "cost_quota": ("operator_input", "connector_contract", "observed_metadata"),
    "capabilities": (
        "connector_contract",
        "observed_metadata",
        "project_configuration",
        "capabilities",
        "governance_presets",
    ),
    "processing": (
        "connector_contract",
        "observed_metadata",
        "project_configuration",
        "capabilities",
        "governance_presets",
    ),
    "outputs": (
        "connector_contract",
        "observed_metadata",
        "project_configuration",
        "capabilities",
        "governance_presets",
    ),
    "downstream_candidates": (
        "connector_contract",
        "project_configuration",
        "capabilities",
        "governance_presets",
    ),
}
_SENSITIVE_KEYS = {
    "credential",
    "credentials",
    "secret",
    "access_token",
    "refresh_token",
    "provider_account_id",
    "raw_sample",
    "raw_payload",
    "authorization",
}
_AUTHORITY_KEYS = {
    "datastream_id",
    "plan_version_id",
    "mapping_version_id",
    "publication_id",
    "current_plan_version_id",
    "enabled",
}
_EPOCH = "1970-01-01T00:00:00Z"

# THE SIX PROJECT CAPABILITIES, NAMED ONCE, IN ENGLISH (story 57.4, sixth by 61.5).
#
# The screen used to render `humanKey("capability.country")` -> `Capability.country`,
# which is a machine key wearing a capital letter. The name is composed here and not
# in the `.tsx` for the same reason the effect sentence below is: a label written in
# the front is a second semantic registry, and it can carry no `evidence_ref`.
# `capabilityLabel` in the Workbench is the same table for a different screen and is
# deliberately NOT imported -- that file belongs to another surface.
CAPABILITY_LABELS: dict[str, str] = {
    "country": "Country",
    "currency_fx": "Currency & FX",
    # La casse est celle des documents ratifies -- `project-settings.md`,
    # `governance.md` et `page-structure.md` ecrivent `Tax & Fees` et
    # `Reporting Timezone`. Elle doit rester identique a
    # `ui/admin/src/ui/CapabilityCoverage.tsx`, et
    # `test_capability_labels_have_one_spelling.py` refuse qu'elles divergent
    # ou qu'une troisieme table apparaisse.
    #
    # A NE PAS CONFONDRE avec le libelle du CHAMP `reporting_timezone` des
    # defauts de projet (`Reporting timezone`, minuscule), qui est une question
    # de formulaire et non cette capacite-ci.
    "reporting_timezone": "Reporting Timezone",
    "tax_fees": "Tax & Fees",
    "competitors": "Competitors",
    "placement_mapping": "Placement Mapping",
    "analytics_alignment": "Analytics Alignment",
}

# The one sentence that stands for an effect NOBODY can compute before a Datastream
# exists. It is a stated absence, never a placeholder: several of the capabilities
# reach it on most connectors, and that is an answer.
EFFECT_UNKNOWN = "Effect unknown before the first run"


def _owner_reference(
    *,
    workspace: str | None = None,
    section: str | None = None,
    action: str | None = None,
    global_surface: str | None = None,
    global_section: str | None = None,
) -> dict[str, Any]:
    """A SEMANTIC destination -- never a composed URL. The eleven keys the shell reads.

    THE WHOLE CLASS, not one link. This compiler composed ten project-rooted paths
    (`/projects/{project_id}/…`) and not one of them resolves: `parsePath`
    (`ui/admin/src/shell/router.tsx`) refuses every address whose first segment is not
    `account`, `platform` or `org`, and the real shape is
    `/org/{orgSegment}/project/{projectSegment}/…`. The server cannot build it -- it
    does not know the Organization, and the address carries a SLUG the shell resolves
    at runtime -- so it must not try. It names the destination; the router builds the
    address. Same eleven keys as `project_readiness._global`, same resolver as
    `ContextHubRoute` and `ContentRouter.openOwner`.
    """
    return {
        "surface": "global" if global_surface else "project",
        "workspace": workspace,
        "section": section,
        "global_surface": global_surface,
        "global_section": global_section,
        "object_type": None,
        "object_id": None,
        "tab": None,
        "action": action,
        "version_id": None,
        "evidence_id": None,
    }


def _capability_owner_reference() -> dict[str, Any]:
    """`Project Settings › Capabilities`, the owner of every Project capability."""
    return _owner_reference(global_surface="project-settings", global_section="capabilities")


def _country_effect(
    contract: dict[str, Any],
    report: dict[str, Any] | None,
    grain: list[str] | None,
    field_universe: list[dict[str, Any]],
) -> tuple[str, str]:
    """What Country would add to THIS Datastream, from the selected report alone.

    The grain choice is the one `compile_geographic_intent` makes -- the smallest
    `supported_grains` entry that contains the current grain and a country field --
    and `_country_fields` is imported rather than re-implemented. What is NOT read is
    the Project's country registry: that is a database read this proposal does not
    make, so the sentence states its own basis instead of implying markets it never
    consulted. Measured 2026-08-05: 5 connectors of 39 carry a country field at all,
    so "this source declares none" is the ordinary answer, not a failure.
    """
    from core.datastream_intents import _country_fields  # noqa: PLC0415

    grain_before = sorted(grain or [])
    before = ", ".join(grain_before) or "no declared grain"
    if report and contract:
        candidates = _country_fields(report, contract)
    else:
        candidates = sorted(
            {
                str(field.get("field_id"))
                for field in field_universe
                if field.get("canonical_target") == "country"
                or str(field.get("field_id") or "").lower() == "country"
            }
        )
    if not candidates:
        return (
            "This source declares no country dimension, so Country would add nothing "
            "to this Datastream.",
            "not_applicable",
        )
    carried = [field for field in candidates if field in set(grain_before)]
    if carried:
        return (
            f"This Datastream already carries {carried[0]} in its grain ({before}); "
            "Country would add no dimension here.",
            "covered",
        )
    supported = [
        sorted(set(item))
        for item in (report or {}).get("supported_grains", [])
        if isinstance(item, list)
    ]
    if supported:
        compatible = [
            (candidate, field)
            for field in candidates
            for candidate in supported
            if set(grain_before) <= set(candidate) and field in candidate
        ]
        if not compatible:
            return (
                f"This report declares {candidates[0]} but no supported grain that adds "
                f"it to {before}, so Country would add nothing to this Datastream.",
                "not_applicable",
            )
        grain_after, field = min(compatible, key=lambda item: (len(item[0]), item[0], item[1]))
        return (
            f"Adds {field} to the grain: {before} -> {', '.join(grain_after)}. Read from "
            "the connector contract only; the governed markets are settled in Project "
            "Settings.",
            "covered",
        )
    after = ", ".join(sorted(set(grain_before) | {candidates[0]}))
    return (
        f"Adds {candidates[0]} to the grain: {before} -> {after}. Read from the observed "
        "schema only; the governed markets are settled in Project Settings.",
        "covered",
    )


def _currency_fx_effect(mapping_value: dict[str, Any] | None) -> tuple[str, str]:
    """What Currency & FX would do here: carry provenance, never change the grain.

    The monetary roles are the ones THIS proposal already assigned
    (`profile_fields` -> `suggestion.semantic_role`), shown in the mapping table on the
    same screen. The authority on what is monetary is `app.semantic_concepts`
    (`value_type = 'money'`), which this compiler does not read -- so the sentence names
    its own basis rather than presenting a count as a verdict.
    """
    fields = (mapping_value or {}).get("fields") or []
    if not fields:
        return (
            f"{EFFECT_UNKNOWN}: no field mapping is compiled yet, so no monetary field "
            "can be named.",
            "unavailable",
        )
    monetary = sorted(
        {
            str(field.get("field_id"))
            for field in fields
            if str(field.get("role") or "") in {"measure_spend", "measure_revenue"}
        }
    )
    if not monetary:
        return (
            "Not applicable: no mapped field carries a monetary role, so this Datastream "
            "publishes no source currency.",
            "not_applicable",
        )
    return (
        f"{len(monetary)} mapped field(s) carry a monetary role ({', '.join(monetary)}): "
        "each publishes its native currency, unit and adapter, and conversion is applied "
        "at read with as-of provenance, never in staging. Which canonical targets are "
        "monetary is settled by the Project's semantic Concepts, which this proposal does "
        "not read.",
        "partial",
    )


def _reporting_timezone_effect(contract: dict[str, Any]) -> tuple[str, str]:
    """Reporting timezone SIGNALS a day-boundary difference. It never re-aligns one.

    Three sources of thirty-nine declare `source_capabilities.time_context` (measured
    2026-08-05), and none of them declares a zone -- only the LOCUS the boundary is set
    at. The offset itself is observed at the first publication
    (`ReportingTimezoneCompiler`: no publication -> `time_boundary_unobserved`), so no
    number is ever produced here. The verb is the one gravel-set in the compiler's own
    exception: "signalled and never corrected: at DATE grain there is no hour to
    re-slice."
    """
    time_context = contract.get("time_context")
    locus = str((time_context or {}).get("locus") or "") if isinstance(time_context, dict) else ""
    if not locus:
        return (
            f"{EFFECT_UNKNOWN}: this connector declares no time context, so no day "
            "boundary can be read here. A difference with the Project reporting timezone "
            "is signalled, never corrected.",
            "unavailable",
        )
    if locus == "none":
        return (
            "This connector declares that it sets no day boundary of its own. A "
            "difference with the Project reporting timezone is signalled and never "
            "corrected: at DATE grain there is no hour to re-slice.",
            "partial",
        )
    return (
        f"This connector declares its day boundary at the {locus} level; the zone itself "
        "is observed at the first publication. A difference with the Project reporting "
        "timezone is signalled and never corrected: at DATE grain there is no hour to "
        "re-slice.",
        "partial",
    )


def _tax_fees_effect(source_category: Any, data_role: Any) -> tuple[str, str]:
    """The C.3 agreement table, called as-is. No cascade phase is ever enumerated.

    `derive_source_type` is pure and both its signals exist before creation: the
    connector manifest's `public_catalog.category` and the data role story 57.10 asks
    for at `Source`. What does NOT exist is the matched ladder: `_match_ladder` reads
    inputs an executed publication observed, so the reachable phases cannot be listed
    and this sentence does not list them.
    """
    from core.fee_tax_source_types import (  # noqa: PLC0415
        derive_source_type,
        exclusion_reason_for,
        is_in_cost_cascade,
    )

    source_type = derive_source_type(source_category, data_role)
    signals = (
        f"source category {source_category or 'undeclared'}, "
        f"data role {data_role or 'undeclared'}"
    )
    if is_in_cost_cascade(source_type):
        return (
            f"This Datastream resolves to {source_type} ({signals}) and enters the cost "
            "cascade. Which rules match is decided by evidence a publication observes, so "
            "none is named before the first run.",
            "partial",
        )
    return (
        f"This Datastream resolves to {source_type} ({signals}): "
        f"{exclusion_reason_for(source_type)}.",
        "not_applicable",
    )


def _competitors_effect(contract: dict[str, Any], report_id: str | None) -> tuple[str, str]:
    """Whether the SELECTED report can carry a tracked entity, read from the contract.

    `detect_support` needs an `app.datastreams` row, which does not exist yet; the
    declaration it reads does, normalized into the contract snapshot as
    `tracked_entity` (`source_capabilities.normalize_tracked_entity`). Two Datastreams
    of the same connector differ here, because support is declared PER REPORT.
    """
    declaration = contract.get("tracked_entity")
    if not isinstance(declaration, dict) or declaration.get("support") != "declared":
        return (
            "Not applicable: this connector declares no tracked-entity contract, so "
            "Competitors would add nothing to this Datastream.",
            "not_applicable",
        )
    reports = [item for item in declaration.get("reports", []) if isinstance(item, dict)]
    if not report_id:
        return (
            f"{EFFECT_UNKNOWN}: tracked-entity support is declared per report, and no "
            "report is selected yet.",
            "unavailable",
        )
    entry = next(
        (item for item in reports if str(item.get("report_id")) == str(report_id)), None
    )
    if entry is None:
        return (
            f"This connector declares tracked-entity support for {len(reports)} report(s), "
            f"and the selected report {report_id} is not one of them; Competitors would "
            "add nothing to this Datastream.",
            "not_applicable",
        )
    kinds = ", ".join(str(kind) for kind in entry.get("entity_kinds") or []) or "unspecified kinds"
    return (
        f"The selected report {report_id} declares a tracked-entity contract "
        f"({entry.get('direction') or 'undeclared direction'}: {kinds}), so this "
        "Datastream can carry tracked competitor rows.",
        "covered",
    )


def _capability_effect(
    capability_key: str,
    *,
    contract: dict[str, Any],
    report: dict[str, Any] | None,
    report_id: str | None,
    grain: list[str] | None,
    field_universe: list[dict[str, Any]],
    mapping_value: dict[str, Any] | None,
    source_category: Any,
    data_role: Any,
) -> tuple[str, str]:
    """One sentence per capability, about THIS Datastream, or a stated absence.

    Pure on its arguments: no compiler is imported (every one of them requires an
    `app.datastreams` row that does not exist during setup), and no new evidence is
    read. A capability whose effect cannot be derived from the draft says so; it never
    borrows the sentence of another. The constant this replaced --
    "May change compatible Datastream fields, grain, processing and Outputs." on every
    row -- was a placeholder wearing the shape of an answer.
    """
    if capability_key == "country":
        return _country_effect(contract, report, grain, field_universe)
    if capability_key == "currency_fx":
        return _currency_fx_effect(mapping_value)
    if capability_key == "reporting_timezone":
        return _reporting_timezone_effect(contract)
    if capability_key == "tax_fees":
        return _tax_fees_effect(source_category, data_role)
    if capability_key == "competitors":
        return _competitors_effect(contract, report_id)
    return (
        f"{EFFECT_UNKNOWN}: this proposal derives no effect for the {capability_key} "
        "capability.",
        "unavailable",
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


_OPERATOR_COMMON = {
    "mode",
    "source",
    "configure",
    "name",
    "scope",
    "observation_ref",
    "data_role",
    "domain_ids",
    "schedule",
    "wizard_state",
}
_OPERATOR_SOURCE_KEYS = {
    "connector_pull": {
        "source_account_ref",
        "connector_ref",
        "connector_contract_version_ref",
        "report_ref",
        "observation_ref",
    },
    "external_bq": {
        "access_ref",
        "object_ref",
        "declared_writer",
        "readonly_acknowledged",
        "observation_ref",
    },
    "managed_feed": {
        "channel",
        "source_account_ref",
        "template_ref",
        "staged_asset_ref",
        "sheet_ref",
        "observation_ref",
        # Story 57.3. The DECLARED contract of an inbound channel: how often a
        # delivery is expected, and which senders the operator promises. It is
        # operator input, not evidence -- nothing observes it, because an email
        # address and a webhook token have no provider to interrogate. It stays
        # out of `MODE_KEYS` in `datastream_setup_observations` on purpose: a key
        # added there would also be accepted on `file_schema` and `sheet_schema`,
        # which declare no channel contract at all.
        "channel_contract",
    },
}
_OPERATOR_CONFIGURE_KEYS = {
    "connector_pull": {
        "date_field",
        "date_window",
        "metrics",
        "dimensions",
        "filters",
        "history_intent",
        "cadence_intent",
        "grain",
    },
    "external_bq": {
        "watermark_semantics",
        "logical_dataset_name",
        "expected_freshness",
        "verification_window",
        "expected_history",
        "row_filters",
    },
    "managed_feed": {
        "input_ref",
        "parsing_contract",
        "logical_dataset_name",
        "date_semantics",
        "grain",
        "write_mode",
    },
}


#: The two keys `_load_compiler_inputs` puts INSIDE operator_input because
#: `compile_preconfiguration` reads them back: `project_id` scopes the proposal
#: (:819) and `revision_id` is popped as the version of the operator_input
#: evidence ref (:608). They are not operator fields, so they are named once here
#: and shared by the injector and the check -- the two used to disagree, and any
#: draft that declared its mode died at compile on
#: "Mode-irrelevant operator fields are not accepted: project_id, revision_id".
#: An operator may still never send them: `project_id` would let a client choose
#: the evidence scope of its own proposal, so `update_draft` keeps the strict
#: form and only the compiler's own call passes `allow_injected`.
_COMPILER_INJECTED = frozenset({"project_id", "revision_id"})


def _validate_operator_union(operator: dict[str, Any], *, allow_injected: bool = False) -> None:
    mode = operator.get("mode")
    if mode is None:
        return
    if mode not in _OPERATOR_SOURCE_KEYS:
        raise PreconfigurationValidationError("operator_input mode is unsupported")
    accepted = _OPERATOR_COMMON | _COMPILER_INJECTED if allow_injected else _OPERATOR_COMMON
    irrelevant = sorted(set(operator) - accepted)
    if irrelevant:
        raise PreconfigurationValidationError(
            f"Mode-irrelevant operator fields are not accepted: {', '.join(irrelevant)}"
        )
    source = operator.get("source", {})
    configure = operator.get("configure", {})
    if not isinstance(source, dict) or not isinstance(configure, dict):
        raise PreconfigurationValidationError("source and configure must be objects")
    wrong_source = sorted(set(source) - _OPERATOR_SOURCE_KEYS[mode])
    wrong_configure = sorted(set(configure) - _OPERATOR_CONFIGURE_KEYS[mode])
    if wrong_source or wrong_configure:
        wrong = wrong_source + wrong_configure
        raise PreconfigurationValidationError(
            f"Mode-irrelevant operator fields are not accepted: {', '.join(wrong)}"
        )
    if mode == "managed_feed" and configure.get("write_mode") not in (None, "replace"):
        raise PreconfigurationValidationError(
            "Append is unavailable without a durable-key execution contract"
        )


def _validate_inputs(inputs: dict[str, Any], *, allow_injected: bool = False) -> None:
    if not isinstance(inputs, dict) or not isinstance(inputs.get("operator_input"), dict):
        raise PreconfigurationValidationError("operator_input must be an object")
    if "observed_metadata" in inputs["operator_input"]:
        raise PreconfigurationValidationError(
            "Client-authored observed_metadata is forbidden; use an owned observation_ref"
        )
    _validate_operator_union(inputs["operator_input"], allow_injected=allow_injected)
    keys = set(_walk_keys(inputs))
    sensitive = sorted(keys & _SENSITIVE_KEYS)
    if sensitive:
        raise PreconfigurationValidationError(
            f"Sensitive evidence keys are forbidden: {', '.join(sensitive)}"
        )
    observed = (
        inputs.get("observed_metadata") or inputs["operator_input"].get("observed_metadata") or {}
    )
    safe = observed.get("safe_metadata") or {}
    if len(_canonical(safe).encode("utf-8")) > 8192:
        raise PreconfigurationValidationError(
            "Observed metadata exceeds the bounded evidence limit"
        )


def _source_ref(
    source: dict[str, Any] | None, kind: EvidenceKind, object_type: str
) -> EvidenceRef | None:
    if not source:
        return None
    fingerprint = str(source.get("fingerprint") or "")
    if len(fingerprint) != 64:
        fingerprint = canonical_hash(source)
    return {
        "kind": kind,
        "object_type": object_type,
        "object_id": str(source.get("object_id") or object_type),
        "version_id": str(source.get("version_id") or "unavailable"),
        "fingerprint": fingerprint,
        "observed_at": str(source.get("observed_at") or _EPOCH),
    }


def _dependencies(inputs: dict[str, Any]) -> dict[str, Any]:
    operator = deepcopy(inputs["operator_input"])
    return {
        "operator_input": {
            "version_id": str(operator.pop("revision_id", "unpersisted")),
            "fingerprint": canonical_hash(operator),
        },
        "connector_contract": _source_ref(
            inputs.get("connector_contract"), "connector_contract", "connector_contract"
        ),
        "observed_metadata": _source_ref(
            inputs.get("observed_metadata"), "observed_metadata", "observed_metadata"
        ),
        "project_configuration": _source_ref(
            inputs.get("project_configuration"), "project_setting", "project_configuration"
        ),
        "capabilities": sorted(
            [
                {
                    "key": str(item.get("key")),
                    "state": str(item.get("state")),
                    "active_version_id": item.get("active_version_id"),
                    "pending_version_id": item.get("pending_version_id"),
                    "fingerprint": str(item.get("fingerprint") or canonical_hash(item)),
                }
                for item in inputs.get("capabilities", [])
            ],
            key=lambda item: item["key"],
        ),
        "governance_presets": sorted(
            [
                {
                    "key": str(item.get("key")),
                    "version_id": str(item.get("version_id") or "unavailable"),
                    "fingerprint": str(item.get("fingerprint") or canonical_hash(item)),
                }
                for item in inputs.get("governance_presets", [])
            ],
            key=lambda item: item["key"],
        ),
    }


class _NoopConnection:
    def cursor(self):
        raise RuntimeError("pure compiler has no database cursor")


def _compiler_now(inputs: dict[str, Any]) -> datetime:
    """Anchor time-derived recommendations to persisted evidence, never wall time."""
    candidates: list[datetime] = []
    for source in (
        inputs.get("connector_contract"),
        inputs.get("observed_metadata"),
        inputs.get("project_configuration"),
        inputs.get("operator_input"),
    ):
        observed_at = (source or {}).get("observed_at")
        if not observed_at:
            continue
        try:
            parsed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
            candidates.append(
                parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
            )
        except ValueError:
            continue
    return max(candidates) if candidates else datetime(1970, 1, 1, tzinfo=timezone.utc)


def _evidence(inputs: dict[str, Any], dependencies: dict[str, Any]) -> dict[str, EvidenceRef]:
    operator = dependencies["operator_input"]
    result: dict[str, EvidenceRef] = {
        "operator_input": {
            "kind": "operator_input",
            "object_type": "setup_draft_revision",
            "object_id": "pending_setup_draft",
            "version_id": operator["version_id"],
            "fingerprint": operator["fingerprint"],
            "observed_at": str(inputs["operator_input"].get("observed_at") or _EPOCH),
        }
    }
    for key, kind, object_type in (
        ("connector_contract", "connector_contract", "connector_contract"),
        ("observed_metadata", "observed_metadata", "observed_metadata"),
        ("project_configuration", "project_setting", "project_configuration"),
    ):
        ref = _source_ref(inputs.get(key), kind, object_type)
        if ref:
            result[key] = ref
    for preset in inputs.get("governance_presets", []):
        key = str(preset.get("key") or "preset")
        result[f"governance_preset:{key}"] = {
            "kind": "governance_preset",
            "object_type": "governance_preset",
            "object_id": key,
            "version_id": str(preset.get("version_id") or "unavailable"),
            "fingerprint": str(preset.get("fingerprint") or canonical_hash(preset)),
            "observed_at": str(preset.get("observed_at") or _EPOCH),
        }
    return result


def _item(
    *,
    key: str,
    section: str,
    requirement: Requirement,
    status: ItemStatus,
    value: Any,
    evidence: list[EvidenceRef],
    section_fingerprint: str,
    confidence: str = "medium",
    rationale: str = "Supported by the referenced normalized evidence.",
    coverage: str = "covered",
    blockers: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    owners: list[dict[str, str]] | None = None,
    impact: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "section": section,
        "requirement": requirement,
        "status": status,
        "proposed_value": value,
        "evidence_refs": evidence,
        "confidence": {"level": confidence, "rationale": rationale},
        "coverage": {"state": coverage},
        "exceptions": [],
        "blockers": blockers or [],
        "warnings": warnings or [],
        "owner_links": owners or [],
        "downstream_impact": impact or [],
        "dependency_fingerprint": section_fingerprint,
    }


def _grain_list(value: Any) -> list[str]:
    """The grain as a list of field ids, whatever shape it arrives in.

    THE OPERATOR ANSWERS A STRING. `configure.grain` is typed `string` in the
    wizard -- "date,video,channel_id" -- while a report profile answers a list.
    Everything downstream did `sorted(grain)`, and sorting a string sorts its
    CHARACTERS: the joint grain became [',', ',', '_', 'a', 'a', 'c', ...] and
    the datastream config was refused for non-unique elements. Measured
    2026-08-11 at the last call of the funnel walk.

    Order is not preserved on purpose -- the callers already sort -- but each
    field appears once, which is what a grain is.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        parts = [str(value).strip()]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return seen


def _report_field_ids(contract: dict[str, Any], report_ref: str) -> set[str] | None:
    """The fields THIS report declares, or None when the contract does not say.

    A Connector's contract lists every field of every report it serves. Offering
    all of them for ONE chosen report is how `video_upload` -- an event of
    youtube's `video_upload` profile -- ended up beside `date` in a `video_daily`
    mapping and blocked it with `mixed_grain`: two date candidates at different
    granularities that never belonged to the same report. Measured 2026-08-11 on
    the funnel walk; it blocks every Connector that declares both metrics and
    events, which is most of them.
    """
    if not report_ref:
        return None
    for report in contract.get("reports") or []:
        if not isinstance(report, dict) or str(report.get("id")) != report_ref:
            continue
        declared = {
            str(field)
            for key in ("metrics", "dimensions", "events")
            for field in (report.get(key) or [])
            if isinstance(field, str)
        }
        return declared or None
    return None


def _normalized_field_universe(
    inputs: dict[str, Any], report_ref: str = ""
) -> list[dict[str, Any]]:
    """Merge Connector-contract and observed-schema fields into one deterministic shape."""
    contract = (inputs.get("connector_contract") or {}).get("contract") or {}
    declared_for_report = _report_field_ids(contract, report_ref)
    observed = (inputs.get("observed_metadata") or {}).get("safe_metadata") or {}
    records: dict[str, dict[str, Any]] = {}
    for item in contract.get("fields") or []:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("field_id") or item.get("name") or "").strip()
        if not field_id:
            continue
        if declared_for_report is not None and field_id not in declared_for_report:
            continue
        records[field_id] = deepcopy(item) | {"field_id": field_id}

    observed_fields = list(observed.get("fields") or [])
    observed_fields.extend(
        {"field_id": field_id, "type": "unknown"}
        for field_id in observed.get("field_ids") or []
        if isinstance(field_id, str)
    )
    numeric_types = {"integer", "decimal", "float", "number", "bigint", "numeric", "float64"}
    date_types = {"date", "timestamp", "datetime", "timestamptz"}
    from core.datastream_setup_observations import is_container_field  # noqa: PLC0415

    for item in observed_fields:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("field_id") or item.get("name") or "").strip()
        if not field_id or field_id in records:
            continue
        # The observation of a Connector contract lists the same whole-contract
        # field set, so the report filter applies to it too. A FILE's observed
        # schema declares no report and is never filtered: `declared_for_report`
        # is None there.
        if declared_for_report is not None and field_id not in declared_for_report:
            continue
        # A FOLDER IS NOT A COLUMN, and this is the only place that can say so.
        # The classification below reads a physical type: `record` is neither a
        # date nor a numeric, so a STRUCT emitted by discovery would land here as
        # a selectable `dimension` -- a grouping offered as a column, which can
        # only fail later. Discovery DESCRIBES nested fields (an operator whose
        # columns are nested must see them); this refuses to OFFER them. The rule
        # is shared so every adapter inherits it, not just the BigQuery one.
        if is_container_field(item):
            continue
        physical_type = str(item.get("physical_type") or item.get("type") or "unknown").lower()
        lowered_id = field_id.lower()
        kind = (
            "date"
            if physical_type in date_types
            else ("metric" if physical_type in numeric_types else "dimension")
        )
        semantic_hints: list[str] = []
        if kind == "date" or lowered_id in {"date", "day", "event_date", "timestamp"}:
            kind = "date"
            semantic_hints.append("primary_date")
        if "spend" in lowered_id or "cost" in lowered_id:
            semantic_hints.append("spend")
        if "revenue" in lowered_id or "sales" in lowered_id:
            semantic_hints.append("revenue")
        records[field_id] = {
            "field_id": field_id,
            "kind": kind,
            "physical_type": physical_type,
            "semantic_hints": semantic_hints,
            "canonical_target": None,
            "aggregation": "sum" if kind == "metric" else "none",
            "non_additive": False,
            "sensitivity": "unknown",
        }
    return [records[field_id] for field_id in sorted(records)]


def compile_preconfiguration(
    inputs: dict[str, Any], *, previous_proposal: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compile one deterministic proposal from exact normalized evidence."""
    # The compiler is the ONE caller allowed the keys its loader injects.
    _validate_inputs(inputs, allow_injected=True)
    dependencies = _dependencies(inputs)
    dependency_fingerprint = canonical_hash(dependencies)
    refs = _evidence(inputs, dependencies)
    operator = deepcopy(inputs["operator_input"])
    contract_source = inputs.get("connector_contract") or {}
    contract = deepcopy(contract_source.get("contract") or {})
    # TWO ABSENCES THAT ARE NOT WORK ITEMS, and `freeze_final_review` counted
    # both as blockers -- so no managed feed could ever be materialised.
    #
    #  * `_feed_has_no_report`: `schedule.policy` and `cost_quota.estimate` read a
    #    connector report, and `history.window` reads two `configure` keys
    #    `_OPERATOR_CONFIGURE_KEYS['managed_feed']` does not even accept. A managed
    #    feed has no report and no way to supply those, EVER. `not_applicable`
    #    already exists in `ItemStatus` and already means exactly this.
    #  * `_feed_awaits_first_delivery`: an inbound channel has no file until a
    #    delivery arrives, an address is only issued against a MATERIALIZED
    #    Datastream, and `observe_first_delivery` needs one that already received.
    #    Its schema-dependent sections become WARNINGS -- `freeze_final_review`
    #    demands an explicit acknowledgement for each, so creating a Datastream
    #    whose first delivery will be RETAINED for review stays a conscious act.
    #    A staged upload keeps blocking: the deferral belongs to the CHANNEL.
    _feed_has_no_report = operator.get("mode") == "managed_feed"
    _feed_awaits_first_delivery = _feed_has_no_report and (operator.get("source") or {}).get(
        "channel"
    ) in _CHANNELS_WITH_NO_FILE_BEFORE_DELIVERY
    project = inputs.get("project_configuration") or {}
    project_id = str(operator.get("project_id") or project.get("project_id") or "project")
    section_fingerprints = {
        key: canonical_hash({dep: dependencies.get(dep) for dep in deps})
        for key, deps in SECTION_DEPENDENCIES.items()
    }

    field_universe = _normalized_field_universe(
        inputs, str((operator.get("source") or {}).get("report_ref") or "")
    )
    profile = None
    if field_universe:
        profile = profile_fields(field_records=field_universe, sample_data=None)
    recommendation = None
    if contract.get("reports"):
        recommendation = recommend_first_report(
            _NoopConnection(),
            project_id=project_id,
            capabilities=contract,
            account={"eligible": True},
            actor="preconfiguration-compiler",
            now=_compiler_now(inputs),
        )
    recommended = recommendation.as_dict() if recommendation else None

    def section(key: str, status: ItemStatus, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "key": key,
            "status": status,
            "dependency_fingerprint": section_fingerprints[key],
            "items": items,
        }

    sections: list[dict[str, Any]] = []
    source_value = operator.get("source")
    source_status: ItemStatus = "complete" if source_value else "missing"
    sections.append(
        section(
            "source",
            source_status,
            [
                _item(
                    key="source.scope",
                    section="source",
                    requirement="required",
                    status=source_status,
                    value=source_value,
                    evidence=[refs["operator_input"]]
                    + ([refs["connector_contract"]] if "connector_contract" in refs else []),
                    section_fingerprint=section_fingerprints["source"],
                    confidence="high" if source_value else "none",
                    coverage="covered" if source_value else "unavailable",
                    blockers=[]
                    if source_value
                    else [
                        {
                            "cause": "missing_source",
                            # WHERE the repair is made, named rather than addressed.
                            # `repair_route` was rendered as TEXT inside the blocker
                            # summary (`summarizeObject`), so an operator read an
                            # address nothing could open and no click ever revealed it.
                            "repair_owner": _owner_reference(
                                workspace="data", section="sources"
                            ),
                        }
                    ],
                )
            ],
        )
    )
    mode = operator.get("mode")
    mode_status: ItemStatus = (
        "complete" if mode in {"connector_pull", "external_bq", "managed_feed"} else "blocked"
    )
    sections.append(
        section(
            "mode",
            mode_status,
            [
                _item(
                    key="mode.selection",
                    section="mode",
                    requirement="required",
                    status=mode_status,
                    value=mode,
                    evidence=[refs["operator_input"]],
                    section_fingerprint=section_fingerprints["mode"],
                    confidence="high",
                    blockers=[]
                    if mode_status == "complete"
                    # RETIRED, not converted: the repair named the wizard the operator
                    # is already standing in (`data/datastreams`, action `create`).
                    # A destination that is the current screen is not a repair, and it
                    # was never rendered as one -- only printed inside the blocker text.
                    else [{"cause": "unsupported_mode"}],
                )
            ],
        )
    )

    field_status: ItemStatus = (
        "complete" if profile else "warning" if _feed_awaits_first_delivery else "missing"
    )
    fields_value = [
        {
            "field_id": item["field_id"],
            "physical_type": item["physical_type"],
            "profile": item["profile"],
        }
        for item in (profile or {}).get("fields", [])
    ]
    field_evidence = [
        refs[key] for key in ("connector_contract", "observed_metadata") if key in refs
    ]
    sections.append(
        section(
            "fields",
            field_status,
            [
                _item(
                    key="fields.selection",
                    section="fields",
                    requirement="required",
                    status=field_status,
                    value=fields_value or None,
                    evidence=field_evidence,
                    section_fingerprint=section_fingerprints["fields"],
                    confidence="high" if profile else "none",
                    coverage="covered" if profile else "unavailable",
                    warnings=[{"cause": _AWAITING_FIRST_DELIVERY}]
                    if not profile and _feed_awaits_first_delivery
                    else [],
                    blockers=[]
                    if profile or _feed_awaits_first_delivery
                    else [
                        {
                            "cause": "missing_connector_contract_or_observation",
                            "repair_owner": _owner_reference(
                                workspace="data", section="connectors"
                            ),
                        }
                    ],
                )
            ],
        )
    )
    grain = _grain_list(
        (operator.get("configure") or {}).get("grain")
        or (recommended or {}).get("grain")
        or ((profile or {}).get("grain") if profile else None)
    )
    grain_status: ItemStatus = (
        "complete" if grain else "warning" if _feed_awaits_first_delivery else "missing"
    )
    sections.append(
        section(
            "grain",
            grain_status,
            [
                _item(
                    key="grain.joint",
                    section="grain",
                    requirement="required",
                    status=grain_status,
                    value=grain,
                    evidence=field_evidence,
                    section_fingerprint=section_fingerprints["grain"],
                    confidence="high" if grain else "none",
                    coverage="covered" if grain else "unavailable",
                )
            ],
        )
    )

    preset_evidence = [ref for key, ref in refs.items() if key.startswith("governance_preset:")]
    mapping_blocked = not profile or any(
        field["binding"]["status"] == "blocking" for field in (profile or {}).get("fields", [])
    )
    mapping_status: ItemStatus = "blocked" if mapping_blocked else "warning"
    # A channel with no file yet cannot evidence a mapping, and blocking on it
    # would make the channel unreachable. It defers, ACKNOWLEDGED, never silently.
    if not profile and _feed_awaits_first_delivery:
        mapping_status = "warning"
    mapping_value = (
        {
            "grain": (profile or {}).get("grain", []),
            "joint_grain": sorted(grain or (profile or {}).get("grain", [])),
            "fields": [
                {
                    "field_id": field["field_id"],
                    "source_identity": field["field_id"],
                    "role": field["suggestion"]["semantic_role"],
                    "semantic_type": field["physical_type"],
                    "canonical_target": field["binding"]["canonical_target"],
                    "mdm_target": field["binding"]["mdm_target"],
                    "aggregation": field["suggestion"]["aggregation"],
                    "sensitivity": field["suggestion"]["sensitivity"],
                    # Story 60.6, arbitrage 2. DERIVED, never decided here.
                    # This key used to be `True` in hard for every field, which
                    # is why it could never say anything and why nothing but the
                    # activation bug of AI-249 ever depended on it. The authority
                    # is `binding.status == "excluded"` -- the key the compiler,
                    # the file import and the fail-closed MDM already read. It is
                    # kept as a mirror because the review bundle is read by name
                    # elsewhere; a second writer is what is gone.
                    "included": field["binding"]["status"] != "excluded",
                    "profile": field["profile"],
                    "suggestion": field["suggestion"],
                    "binding": field["binding"],
                }
                for field in (profile or {}).get("fields", [])
            ],
        }
        if profile
        else None
    )
    sections.append(
        section(
            "physical_mapping",
            mapping_status,
            [
                _item(
                    key="mapping.proposal",
                    section="physical_mapping",
                    requirement="required",
                    status=mapping_status,
                    value=mapping_value,
                    evidence=field_evidence
                    + ([refs["project_configuration"]] if "project_configuration" in refs else [])
                    + preset_evidence,
                    section_fingerprint=section_fingerprints["physical_mapping"],
                    coverage="partial" if profile else "unavailable",
                    warnings=[{"cause": _AWAITING_FIRST_DELIVERY}]
                    if not profile and _feed_awaits_first_delivery
                    else [],
                    # RETIRED for the same reason as `unsupported_mode`: the repair
                    # addressed this very wizard.
                    blockers=[]
                    if profile or _feed_awaits_first_delivery
                    else [{"cause": "mapping_evidence_unavailable"}],
                )
            ],
        )
    )
    classification_value = [
        {
            "field_id": field["field_id"],
            "role": field["suggestion"]["semantic_role"],
            "status": "suggested",
        }
        for field in (profile or {}).get("fields", [])
    ]
    class_status: ItemStatus = (
        "warning"
        if classification_value or _feed_awaits_first_delivery
        else "missing"
    )
    sections.append(
        section(
            "classification",
            class_status,
            [
                _item(
                    key="classification.fields",
                    section="classification",
                    requirement="recommended",
                    status=class_status,
                    value=classification_value or None,
                    evidence=field_evidence + preset_evidence,
                    section_fingerprint=section_fingerprints["classification"],
                    coverage="partial" if classification_value else "unavailable",
                    warnings=[{"cause": "operator_confirmation_required"}]
                    if classification_value
                    else [],
                )
            ],
        )
    )

    report = next(
        (
            item
            for item in contract.get("reports", [])
            if item.get("id")
            == (
                (operator.get("source") or {}).get("report_ref")
                or (recommended or {}).get("report_id")
            )
        ),
        None,
    )
    cadence = (report or {}).get("cadence")
    schedule_status: ItemStatus = (
        "warning" if cadence else "not_applicable" if _feed_has_no_report else "missing"
    )
    sections.append(
        section(
            "schedule",
            schedule_status,
            [
                _item(
                    key="schedule.policy",
                    section="schedule",
                    requirement="recommended",
                    status=schedule_status,
                    value=cadence,
                    evidence=([refs["connector_contract"]] if "connector_contract" in refs else []),
                    section_fingerprint=section_fingerprints["schedule"],
                    coverage="partial" if cadence else "unavailable",
                )
            ],
        )
    )
    history_value = (
        (operator.get("configure") or {}).get("history_intent")
        or (operator.get("configure") or {}).get("date_window")
        or ((recommended or {}).get("interval"))
    )
    history_status: ItemStatus = (
        "warning" if history_value else "not_applicable" if _feed_has_no_report else "missing"
    )
    sections.append(
        section(
            "history",
            history_status,
            [
                _item(
                    key="history.window",
                    section="history",
                    requirement="recommended",
                    status=history_status,
                    value=history_value,
                    evidence=[refs["operator_input"]]
                    + ([refs["connector_contract"]] if "connector_contract" in refs else []),
                    section_fingerprint=section_fingerprints["history"],
                    coverage="partial" if history_value else "unavailable",
                )
            ],
        )
    )
    # Recommendation quota state is mutable; the contract declaration is exact evidence.
    cost = (report or {}).get("quota_cost")
    cost_status: ItemStatus = (
        "warning" if cost else "not_applicable" if _feed_has_no_report else "missing"
    )
    sections.append(
        section(
            "cost_quota",
            cost_status,
            [
                _item(
                    key="cost_quota.estimate",
                    section="cost_quota",
                    requirement="automatic",
                    status=cost_status,
                    value=cost,
                    evidence=([refs["connector_contract"]] if "connector_contract" in refs else []),
                    section_fingerprint=section_fingerprints["cost_quota"],
                    coverage="partial" if cost else "unavailable",
                )
            ],
        )
    )

    # The two signals the fee/tax agreement table needs, both known before creation:
    # the connector manifest's category (read once by `_load_compiler_inputs`, the same
    # value story 57.10 shows read-only at `Source`) and the operator's data role.
    source_category = contract_source.get("source_category")
    report_id = str((report or {}).get("id") or "") or None
    capability_items = []
    for capability in sorted(inputs.get("capabilities", []), key=lambda item: str(item.get("key"))):
        active = capability.get("active_version_id") and capability.get("state") in {
            "ready",
            "degraded",
        }
        status: ItemStatus = "complete" if active else "warning"
        fingerprint = str(capability.get("fingerprint") or canonical_hash(capability))
        evidence: EvidenceRef = {
            "kind": "project_setting",
            "object_type": "project_capability",
            "object_id": str(capability.get("key")),
            "version_id": str(
                capability.get("active_version_id")
                or capability.get("pending_version_id")
                or "unavailable"
            ),
            "fingerprint": fingerprint,
            "observed_at": str(capability.get("observed_at") or _EPOCH),
        }
        capability_key = str(capability.get("key"))
        effect, effect_coverage = _capability_effect(
            capability_key,
            contract=contract,
            report=report,
            report_id=report_id,
            grain=grain,
            field_universe=field_universe,
            mapping_value=mapping_value,
            source_category=source_category,
            data_role=operator.get("data_role"),
        )
        value = {
            "key": capability.get("key"),
            "name": CAPABILITY_LABELS.get(
                capability_key, capability_key.replace("_", " ").capitalize()
            ),
            "label": "Existing" if active else "Proposed in Project Settings",
            "state": capability.get("state"),
            "effect": effect,
            "effect_coverage": effect_coverage,
        }
        capability_items.append(
            _item(
                key=f"capability.{capability.get('key')}",
                section="capabilities",
                requirement="optional",
                status=status,
                value=value,
                evidence=[evidence],
                section_fingerprint=section_fingerprints["capabilities"],
                coverage="covered" if active else "pending",
                warnings=[] if active else [{"cause": "capability_not_active"}],
                owners=[
                    {
                        "object": "Project Settings",
                        # `Propose` IS the link to the owner, and it is the only gesture
                        # this screen offers on a capability. A capability is never
                        # enabled from the wizard: `Enable` and `Activate` belong to
                        # `project_settings_api` change sets, behind their own
                        # confirmation.
                        "label": "Propose" if not active else "Open Project Settings",
                        "owner_reference": _capability_owner_reference(),
                    }
                ],
            )
        )
    cap_status: ItemStatus = "warning" if capability_items else "not_applicable"
    sections.append(
        section(
            "capabilities",
            cap_status,
            capability_items
            or [
                _item(
                    key="capabilities.none",
                    section="capabilities",
                    requirement="automatic",
                    status="not_applicable",
                    # AN EMPTY LIST IS NOT A FAILURE, and it names who writes the rows:
                    # the `seed_project_capabilities` trigger fires on the Project
                    # INSERT (migration 131), so no row at all means the Project itself
                    # predates it -- a different fact from "the section is missing",
                    # which the screen states in its own words.
                    value={
                        "key": "capabilities.none",
                        "name": "Project capabilities",
                        "label": "This Project has no capability row yet",
                        "state": "absent",
                        "effect": (
                            "Project capabilities are written by the "
                            "seed_project_capabilities trigger when the Project is "
                            "created; this Project carries none, so no capability can "
                            "act on this Datastream."
                        ),
                        "effect_coverage": "unavailable",
                    },
                    evidence=(
                        [refs["project_configuration"]] if "project_configuration" in refs else []
                    ),
                    section_fingerprint=section_fingerprints["capabilities"],
                    coverage="unavailable",
                )
            ],
        )
    )

    projection = None
    if profile and not mapping_blocked:
        try:
            projection = compile_projection(
                {
                    "id": "proposal",
                    "plan_version_id": "proposal",
                    "source_schema_hash": profile["source_schema_hash"],
                    "capability_fingerprint": contract_source.get("fingerprint"),
                    "executable": True,
                    "mapping_payload": profile,
                }
            )
        except (ProjectionCompileError, ValueError, KeyError):
            projection = None
    processing_status: ItemStatus = (
        "warning" if profile or _feed_awaits_first_delivery else "blocked"
    )
    processing_value = {
        "label": "Will be created after confirmation",
        "full_grain_preserved": bool(profile),
        "projection_executable": bool((projection or {}).get("executable")),
        "issues": [issue.get("code") for issue in (projection or {}).get("issues", [])],
    }
    sections.append(
        section(
            "processing",
            processing_status,
            [
                _item(
                    key="processing.proposal",
                    section="processing",
                    requirement="automatic",
                    status=processing_status,
                    value=processing_value,
                    evidence=field_evidence,
                    section_fingerprint=section_fingerprints["processing"],
                    coverage="partial" if profile else "unavailable",
                )
            ],
        )
    )
    output_status: ItemStatus = (
        "warning" if profile or _feed_awaits_first_delivery else "blocked"
    )
    sections.append(
        section(
            "outputs",
            output_status,
            [
                _item(
                    key="outputs.full_grain",
                    section="outputs",
                    requirement="automatic",
                    status=output_status,
                    value={"label": "Will be created", "kind": "full_grain_output"}
                    if profile
                    else None,
                    evidence=field_evidence,
                    section_fingerprint=section_fingerprints["outputs"],
                    coverage="pending" if profile else "unavailable",
                    impact=["No Output exists until a reviewed candidate is published."],
                )
            ],
        )
    )
    sections.append(
        section(
            "downstream_candidates",
            "warning",
            [
                _item(
                    key="downstream.candidates",
                    section="downstream_candidates",
                    requirement="optional",
                    status="warning",
                    value={
                        "label": "Will remain a proposal",
                        "objects": ["Semantic View", "Report"],
                    },
                    evidence=[
                        refs[key]
                        for key in ("connector_contract", "project_configuration")
                        if key in refs
                    ],
                    section_fingerprint=section_fingerprints["downstream_candidates"],
                    coverage="pending",
                    # RENDERED, and dead: `ProposalItemCard` drew both as `<a href>`.
                    # Same repair as the capability rows -- the reference, resolved by
                    # the shell.
                    owners=[
                        {
                            "object": "Semantic View",
                            "owner_reference": _owner_reference(
                                workspace="governance", section="semantic-model"
                            ),
                        },
                        {
                            "object": "Report",
                            "owner_reference": _owner_reference(
                                workspace="analyze", section="reports"
                            ),
                        },
                    ],
                    impact=["Downstream owners must review and publish their own versions."],
                )
            ],
        )
    )

    invalidation_causes = []
    if previous_proposal:
        previous_dependencies = previous_proposal.get("dependency_snapshot") or {}
        changed = {
            key for key in dependencies if previous_dependencies.get(key) != dependencies.get(key)
        }
        for dependency in sorted(changed):
            affected = sorted(
                key for key, deps in SECTION_DEPENDENCIES.items() if dependency in deps
            )
            invalidation_causes.append(
                {
                    "dependency": dependency,
                    "cause": "dependency_fingerprint_changed",
                    "affected_sections": affected,
                }
            )
            for proposal_section in sections:
                if proposal_section["key"] in affected:
                    proposal_section["status"] = "needs_review"
                    for proposal_item in proposal_section["items"]:
                        proposal_item["status"] = "needs_review"

    summary = {
        "existing": [{"object": "Project configuration", "version_id": project.get("version_id")}]
        if project
        else [],
        "will_be_created": [
            {"object": "Datastream setup artifacts", "authority": "none until later confirmation"}
        ],
        "will_remain_a_proposal": [
            {"object": "Project capability", "key": item.get("key")}
            for item in inputs.get("capabilities", [])
            if not item.get("active_version_id")
        ]
        + [{"object": "Semantic View"}, {"object": "Report"}],
        "downstream_impact": [
            "A later confirmation may create immutable Datastream plan and mapping versions; "
            "this proposal cannot."
        ],
    }
    capability_effects = [
        {
            "key": item.get("key"),
            "version_id": item.get("active_version_id") or item.get("pending_version_id"),
            "state": "existing" if item.get("active_version_id") else "proposal",
        }
        for item in sorted(inputs.get("capabilities", []), key=lambda value: str(value.get("key")))
    ]
    # `owner_route` RETIRED, not converted. Measured 2026-08-05: nothing reads it --
    # `owner_proposals` appears in `ui/admin/src/datastreams/wizard/wizardApi.ts` as a
    # type and nowhere else, no screen renders it, and no server module consumes it.
    # A path nobody resolves is not a dead link, it is noise; the two owners a screen
    # DOES draw carry their reference in the `downstream_candidates` section above.
    owner_proposals = [
        {"object": "Business Domain", "object_id": domain_id, "state": "existing"}
        for domain_id in sorted(set(operator.get("domain_ids") or []))
    ] + [
        {"object": "Output", "state": "will_be_created"},
        {"object": "Semantic View", "state": "proposal"},
        {"object": "Report", "state": "proposal"},
    ]
    confirmed_intent_bundle = {
        "datastream_name": operator.get("name"),
        "data_role": operator.get("data_role"),
        "joint_grain": sorted(grain or (profile or {}).get("grain", [])),
        "field_mappings": (mapping_value or {}).get("fields", []),
        "business_domain_ids": sorted(set(operator.get("domain_ids") or [])),
        "capability_effects": capability_effects,
        "dq_gates": [
            {"key": "mapping_complete", "state": "blocked" if mapping_blocked else "existing"},
            {"key": "schema_drift", "state": "existing"},
        ],
        "processing": processing_value,
        "outputs": [{"kind": "full_grain_output", "state": "will_be_created"}],
        "exceptions": list((profile or {}).get("ambiguities", [])),
        "owner_proposals": owner_proposals,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dependency_snapshot": dependencies,
        "dependency_fingerprint": dependency_fingerprint,
        "section_fingerprints": section_fingerprints,
        "invalidation_causes": invalidation_causes,
        "sections": sections,
        "configuration_summary": summary,
        "confirmed_intent_bundle": confirmed_intent_bundle,
    }
    payload["content_hash"] = canonical_hash(payload)
    payload["proposal_token"] = canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "dependency_fingerprint": dependency_fingerprint,
            "content_hash": payload["content_hash"],
        }
    )
    if set(_walk_keys(payload)) & (_SENSITIVE_KEYS | _AUTHORITY_KEYS):
        raise PreconfigurationValidationError("Compiler output crossed the non-authority boundary")
    return payload


class PreconfigurationNotFound(LookupError):
    code = "not_found"


class PreconfigurationConflict(RuntimeError):
    code = "conflict"


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    if isinstance(value, str):
        return json.loads(value)
    return deepcopy(value)


def _resume_reference() -> dict[str, Any]:
    """WHERE a draft is resumed, named. `_resume_href` composed an address instead.

    THE WORST OF THE TEN, and it was on nobody's list: this is the way back to a
    draft. It composed `/projects/{id}/data/datastreams/add?draft=…&section=…`, and
    three things are wrong with that string at once -- `/projects/…` is refused on its
    first segment; `add` is not the declared shape (the collection declares the action
    `create`, so the address is `/data/datastreams/action/create`); and `data ›
    datastreams` declares NO query contract, so `draft` and `section` are dropped by
    `validateSectionQuery` even on a well-formed address.

    So no address can carry a draft id today, and composing one that pretends to is
    the fabrication this repo forbids. What resumes a draft is what already resumes it
    (`ContentRouter` -> `DatastreamCreate`, `sessionStorage`
    `datastream-setup-return:{projectId}`) plus the two exact facts this payload
    already carries: `draft_ref` and `first_incomplete_section`. This names the
    destination and nothing more.
    """
    return _owner_reference(workspace="data", section="datastreams", action="create")


def _draft_payload(row: tuple[Any, ...], *, replay: bool = False) -> dict[str, Any]:
    return {
        "draft_ref": row[0],
        "project_ref": row[1],
        "state": row[2],
        "current_revision_ref": row[3],
        "current_revision": int(row[4] or 0),
        "current_proposal_ref": row[5],
        "first_incomplete_section": row[6] or "source",
        "invalidation_causes": _json_value(row[7], []),
        # Kept as a key because story 47.2 contracted it, emptied because no honest
        # value exists. An empty string is the stated absence of an address; the
        # fabricated one it replaces resolved nowhere.
        "resume_href": "",
        "resume_reference": _resume_reference(),
        "idempotent_replay": replay,
    }


_DRAFT_SELECT = """
SELECT d.id, d.project_id, d.state, d.current_revision_id,
       r.revision_number, d.current_proposal_id, r.first_incomplete_section,
       r.invalidation_causes, r.normalized_operator_input
FROM app.datastream_setup_drafts d
LEFT JOIN app.datastream_setup_draft_revisions r ON r.id = d.current_revision_id
WHERE d.id = %s AND d.project_id = %s
"""

# `_DRAFT_SELECT` LEFT JOINs the revision, and Postgres refuses `FOR UPDATE` on
# the nullable side of an outer join (42P16, "FOR UPDATE cannot be applied to
# the nullable side of an outer join"). Both writers appended a bare
# `FOR UPDATE`, so EVERY autosave and EVERY compile raised before touching a
# row: the API mapped the psycopg error to its catch-all 503 ("Datastream setup
# is unavailable"), the wizard printed that banner, and no `operator_input` was
# ever persisted -- a draft could be created and never filled in.
# Only the draft row needs the lock; naming it is what the revisions table
# already does one module over (`setup_responsibilities.py:449`).
_LOCK_DRAFT_ROW = " FOR UPDATE OF d"


def _audit(
    conn, *, actor: str, action: str, project_id: str, draft_id: str, evidence_hash: str
) -> str:
    from core.audit import insert_audit_row

    return insert_audit_row(
        conn,
        identity=actor,
        action=action,
        provider_account="datastream_preconfiguration",
        connection_ref="",
        metadata={"project_id": project_id, "draft_id": draft_id, "evidence_hash": evidence_hash},
    )


def create_or_resume_draft(
    conn, *, project_id: str, actor: str, idempotency_key: str
) -> dict[str, Any]:
    """Create the empty server draft before discovery, or resume the Project draft."""
    from core.data_identities import mint_data_id

    key_hash = canonical_hash(idempotency_key)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastream_setup_drafts WHERE project_id=%s "
            # Migration 284 retired `exited`: it had no writer in the whole
            # repository, and this disjunct was always false.
            "AND (idempotency_key_hash=%s OR state = 'draft') "
            "ORDER BY (idempotency_key_hash=%s) DESC, updated_at DESC LIMIT 1 FOR UPDATE",
            (project_id, key_hash, key_hash),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(_DRAFT_SELECT, (existing[0], project_id))
            row = cur.fetchone()
            payload = _draft_payload(row, replay=True)
            # Same shape as `read_draft`: the create is idempotent per Project,
            # so this return IS how a configured draft reopens. Without the
            # saved input the wizard rendered EMPTY on a filled draft, and the
            # first edit overwrote it at the same accepted revision.
            payload["operator_input"] = _json_value(row[8], {})
            return payload
        draft_id, revision_id = mint_data_id("dsd"), mint_data_id("dsdr")
        empty: dict[str, Any] = {}
        content_hash = canonical_hash(empty)
        cur.execute(
            "INSERT INTO app.datastream_setup_drafts (id,project_id,created_by,idempotency_key_hash) VALUES (%s,%s,%s,%s)",  # noqa: E501
            (draft_id, project_id, actor, key_hash),
        )
        cur.execute(
            "INSERT INTO app.datastream_setup_draft_revisions (id,draft_id,project_id,revision_number,normalized_operator_input,first_incomplete_section,content_hash,idempotency_key_hash,change_reason,created_by) VALUES (%s,%s,%s,1,%s::jsonb,'source',%s,%s,'created',%s)",  # noqa: E501
            (revision_id, draft_id, project_id, _canonical(empty), content_hash, key_hash, actor),
        )
        cur.execute(
            "UPDATE app.datastream_setup_drafts SET current_revision_id=%s,updated_at=NOW() WHERE id=%s AND project_id=%s",  # noqa: E501
            (revision_id, draft_id, project_id),
        )
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_SETUP_DRAFT_CREATED,
            project_id=project_id,
            draft_id=draft_id,
            evidence_hash=content_hash,
        )
        cur.execute(_DRAFT_SELECT, (draft_id, project_id))
        row = cur.fetchone()
        payload = _draft_payload(row)
        # A fresh draft carries the empty input its first revision recorded --
        # stated, so the wizard never has to guess the key exists.
        payload["operator_input"] = _json_value(row[8], {})
        return payload


def list_drafts(conn, *, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.id,d.project_id,d.state,d.current_revision_id,r.revision_number,d.current_proposal_id,r.first_incomplete_section,r.invalidation_causes,r.normalized_operator_input FROM app.datastream_setup_drafts d LEFT JOIN app.datastream_setup_draft_revisions r ON r.id=d.current_revision_id WHERE d.project_id=%s ORDER BY d.updated_at DESC""",  # noqa: E501
            (project_id,),
        )
        return [_draft_payload(row) for row in cur.fetchall()]


def read_draft(conn, *, project_id: str, draft_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_DRAFT_SELECT, (draft_id, project_id))
        row = cur.fetchone()
    if row is None:
        raise PreconfigurationNotFound("Draft not found")
    payload = _draft_payload(row)
    payload["operator_input"] = _json_value(row[8], {})
    return payload


#: The one sentence a discarded draft answers with, wherever it is met. It names
#: the gesture that repairs -- start a new setup -- and never the state word.
DISCARDED_DRAFT_REFUSAL = (
    "This setup was discarded. Start a new Datastream setup to configure this source again."
)

#: The terminal draft state. Declared by migration 134, kept by 284 as the room a
#: terminal path would need, and given its writer on 2026-08-31 (AI-336).
DRAFT_STATE_DISCARDED = "archived"


def require_live_draft(conn, *, project_id: str, draft_id: str) -> str:
    """Refuse every WRITE on a discarded draft, in one sentence, for the class.

    `Discard this draft` is terminal (amendment 2026-08-31). Terminal has to mean
    terminal on every write and not only on the resume, so the four writers that
    can still be reached with a draft id -- autosave, discovery, the final review
    and the materialization -- ask this one question rather than each inventing
    its own.

    Returns the state it read, so a caller that already needs it does not read the
    row twice. Raises `PreconfigurationNotFound` when the draft is not this
    Project's, which is the same non-disclosure the API layer already applies.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.datastream_setup_drafts WHERE id=%s AND project_id=%s",
            (draft_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise PreconfigurationNotFound("Draft not found")
    if row[0] == DRAFT_STATE_DISCARDED:
        raise PreconfigurationConflict(DISCARDED_DRAFT_REFUSAL)
    return str(row[0])


def discard_draft(conn, *, project_id: str, draft_id: str, actor: str) -> dict[str, Any]:
    """End a setup draft: the terminal path AI-304 left open, ratified 2026-08-31.

    A SOFT ARCHIVE, never a DELETE -- and the state exists precisely so the row can
    stay. The draft's revisions are append-only, and its observations, previews and
    proposals reference it under `ON DELETE RESTRICT`: deleting the row would mean
    deleting the evidence of what was tried, which is not what abandoning a
    configuration means. What ends is the RESUME, not the record.

    `archived` sits outside `uq_datastream_setup_draft_resumable`, so the Project's
    single resumable slot is free the moment this returns and the next
    `Add Datastream` starts clean. That is the half migration 284 protected when it
    refused to reuse `exited`, whose predicate counted it as live.

    Idempotent: discarding an already-discarded draft is the same discard, answered
    as a replay rather than as an error about a state nobody typed.

    A MATERIALIZED draft is refused, and the refusal names the right gesture. That
    draft created a Datastream, and `CHECK ((state='materialized') =
    (materialized_datastream_id IS NOT NULL))` means `archived` and a live pointer
    cannot coexist -- so the database would refuse it too, in a sentence nobody
    could act on.
    """
    with conn.cursor() as cur:
        cur.execute(_DRAFT_SELECT + _LOCK_DRAFT_ROW, (draft_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise PreconfigurationNotFound("Draft not found")
        state = str(row[2])
        if state == DRAFT_STATE_DISCARDED:
            return _draft_payload(row, replay=True)
        if state == "materialized":
            raise PreconfigurationConflict(
                "This setup already created a Datastream. Archive the Datastream from its "
                "own screen; the draft that created it is kept as its record."
            )
        cur.execute(
            "UPDATE app.datastream_setup_drafts SET state='archived',updated_at=NOW() "
            "WHERE id=%s AND project_id=%s AND state='draft'",
            (draft_id, project_id),
        )
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_SETUP_DRAFT_DISCARDED,
            project_id=project_id,
            draft_id=draft_id,
            evidence_hash=canonical_hash({"discarded_revision": int(row[4] or 0)}),
        )
        cur.execute(_DRAFT_SELECT, (draft_id, project_id))
        # No `operator_input` on this answer, unlike `read_draft`: the answers are
        # what the person just chose to leave behind, and handing them back would
        # be offering the door this gesture closed.
        return _draft_payload(cur.fetchone())


def _first_incomplete(value: dict[str, Any]) -> str:
    mode = value.get("mode")
    source = value.get("source") or {}
    configure = value.get("configure") or {}
    if mode not in {"connector_pull", "external_bq", "managed_feed"} or not source.get(
        "observation_ref"
    ):
        return "source"
    # The name and the data role are ASKED AT `source` since story 57.10, and this
    # branch is what a resume link obeys. It used to return `classify_and_map` --
    # three sections after the one the fields now live in, so resuming a draft with
    # no role landed the operator past the field that was missing.
    if not value.get("name") or not value.get("data_role"):
        return "source"
    complete = (
        bool(
            configure.get("date_field") and configure.get("metrics") and configure.get("dimensions")
        )
        if mode == "connector_pull"
        else bool(configure.get("watermark_semantics") and configure.get("logical_dataset_name"))
        if mode == "external_bq"
        else bool(
            configure.get("input_ref")
            and configure.get("parsing_contract")
            and configure.get("logical_dataset_name")
        )
    )
    if not complete:
        return "configure"
    return str((value.get("wizard_state") or {}).get("first_incomplete") or "preview_validate")


def _invalidation(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    changed = []
    for key in ("mode", "source", "configure", "name", "data_role", "domain_ids", "schedule"):
        if previous.get(key) != current.get(key):
            changed.append(
                {
                    "dependency": key,
                    "cause": f"{key}_changed",
                    "affected_sections": [
                        section
                        for section, deps in SECTION_DEPENDENCIES.items()
                        if "operator_input" in deps
                    ],
                }
            )
    return changed


def update_draft(
    conn,
    *,
    project_id: str,
    draft_id: str,
    actor: str,
    idempotency_key: str,
    expected_revision: int,
    operator_input: dict[str, Any],
    change_reason: str = "autosave",
) -> dict[str, Any]:
    """Append one immutable autosave revision with optimistic concurrency."""
    from core.data_identities import mint_data_id

    _validate_inputs({"operator_input": operator_input})
    key_hash, content_hash = canonical_hash(idempotency_key), canonical_hash(operator_input)
    with conn.cursor() as cur:
        cur.execute(_DRAFT_SELECT + _LOCK_DRAFT_ROW, (draft_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise PreconfigurationNotFound("Draft not found")
        # A DISCARDED DRAFT DOES NOT AUTOSAVE (amendment 2026-08-31). Without this
        # line the wizard's own autosave would append a revision to a draft the
        # person abandoned -- and the statement below used to write `state='draft'`
        # on every autosave, which would have brought it back to life.
        if str(row[2]) == DRAFT_STATE_DISCARDED:
            raise PreconfigurationConflict(DISCARDED_DRAFT_REFUSAL)
        cur.execute(
            "SELECT id,revision_number FROM app.datastream_setup_draft_revisions WHERE draft_id=%s AND idempotency_key_hash=%s",  # noqa: E501
            (draft_id, key_hash),
        )
        replay = cur.fetchone()
        if replay:
            cur.execute(_DRAFT_SELECT, (draft_id, project_id))
            return _draft_payload(cur.fetchone(), replay=True)
        if int(row[4] or 0) != expected_revision:
            raise PreconfigurationConflict(
                "Expected draft revision does not match current revision"
            )
        cur.execute(
            "SELECT id,revision_number FROM app.datastream_setup_draft_revisions WHERE draft_id=%s AND content_hash=%s",  # noqa: E501
            (draft_id, content_hash),
        )
        duplicate = cur.fetchone()
        if duplicate:
            cur.execute(_DRAFT_SELECT, (draft_id, project_id))
            return _draft_payload(cur.fetchone(), replay=True)
        previous = _json_value(row[8], {})
        invalidation = _invalidation(previous, operator_input)
        revision_id, number = mint_data_id("dsdr"), expected_revision + 1
        first = _first_incomplete(operator_input)
        cur.execute(
            "INSERT INTO app.datastream_setup_draft_revisions (id,draft_id,project_id,revision_number,normalized_operator_input,first_incomplete_section,invalidation_causes,content_hash,idempotency_key_hash,change_reason,created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s)",  # noqa: E501
            (
                revision_id,
                draft_id,
                project_id,
                number,
                _canonical(operator_input),
                first,
                _canonical(invalidation),
                content_hash,
                key_hash,
                change_reason,
                actor,
            ),
        )
        cur.execute(
            # UNE REVISION NE TOUCHE PLUS L'ETAT DU TOUT (AI-336, 2026-08-31).
            # Elle ecrivait `state='draft'` sous condition, pour ne pas violer
            # `datastream_setup_drafts_check` sur un brouillon materialise. Mais
            # un brouillon ne quitte 'draft' que par la materialisation ou par
            # l'abandon : la seule chose que cette clause pouvait encore faire
            # etait RESSUSCITER un brouillon abandonne au premier autosave.
            # Seuls les pointeurs de revision et de proposition bougent.
            "UPDATE app.datastream_setup_drafts "
            "SET current_revision_id=%s,current_proposal_id=NULL,"
            "updated_at=NOW() WHERE id=%s AND project_id=%s",
            (revision_id, draft_id, project_id),
        )
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_SETUP_DRAFT_REVISED,
            project_id=project_id,
            draft_id=draft_id,
            evidence_hash=content_hash,
        )
        cur.execute(_DRAFT_SELECT, (draft_id, project_id))
        return _draft_payload(cur.fetchone())


def _contract_for_configuration(
    snapshot: dict[str, Any],
    *,
    operator_input: dict[str, Any],
    project_id: str,
    connector_id: str,
) -> dict[str, Any]:
    """The contract a CONFIGURATION is judged against: the pin, unless it is behind.

    THE CATALOGUE AND THE COMPILER READ TWO DIFFERENT THINGS, and the product
    offered what it then refused. `source-options` reads the module registry --
    AI-279 settled that: "the catalogue is every module the registry holds" --
    while this read the newest `connector_contract_versions` row, written when
    something was first bound. A module that gains a report therefore OFFERS it
    in the wizard and REFUSES it at compile: "Selected Connector report is
    unavailable". Measured 2026-08-11, eight reports out of nine.

    The pin's job is `contract_state` -- verified, stale, unverified -- not to
    decide what may be configured. So when the operator names a report the pin
    does not carry and the module declares TODAY, the module wins: it is the
    thing that will actually be called. The pin is rewritten at binding, which
    is where a commitment is made.

    Everything else is unchanged: a report the module does not declare either is
    still refused, by the same check, one line down.
    """
    normalized = _normalize_contract_snapshot(
        snapshot, project_id=project_id, connector_id=connector_id
    )
    report_ref = str((operator_input.get("source") or {}).get("report_ref") or "")
    if not report_ref:
        return normalized
    if any(item.get("id") == report_ref for item in normalized.get("reports", [])):
        return normalized

    try:
        from core.context_seed import load_registry_entry  # noqa: PLC0415

        # `load_registry_entry` rend un DICT {manifest, catalog, relations} --
        # pas un objet. Un getattr y trouvait toujours None, donc ce repli ne
        # se declenchait jamais et le refus restait entier.
        entry = load_registry_entry(connector_id) or {}
        manifest = entry.get("manifest") if isinstance(entry, dict) else None
        if not isinstance(manifest, dict):
            return normalized
        live = _normalize_contract_snapshot(
            manifest, project_id=project_id, connector_id=connector_id
        )
    except Exception:  # noqa: BLE001 -- an unreadable module leaves the pin in force
        return normalized

    if any(item.get("id") == report_ref for item in live.get("reports", [])):
        logger.info(
            "preconfiguration: contract_ahead_of_pin connector=%s report=%s "
            "-- the module declares it, the pinned snapshot does not",
            connector_id,
            report_ref,
        )
        return live
    return normalized


def _normalize_contract_snapshot(
    snapshot: dict[str, Any], *, project_id: str, connector_id: str
) -> dict[str, Any]:
    manifest = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else snapshot
    if "source_capabilities" not in manifest:
        return snapshot
    from core.source_capabilities import CapabilityValidationError, normalize_capabilities

    try:
        return normalize_capabilities(
            manifest, project_id=project_id, connection_ref_id=f"contract:{connector_id}"
        )
    except CapabilityValidationError as exc:
        raise PreconfigurationValidationError("Persisted Connector contract is invalid") from exc


def _csv_operator_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _validate_connector_configuration(
    operator_input: dict[str, Any], capabilities: dict[str, Any]
) -> None:
    source = operator_input.get("source") or {}
    configure = operator_input.get("configure") or {}
    report_ref = str(source.get("report_ref") or "")
    metrics = _csv_operator_values(configure.get("metrics"))
    dimensions = _csv_operator_values(configure.get("dimensions"))
    if not report_ref or not (metrics or dimensions):
        return
    report = next(
        (item for item in capabilities.get("reports", []) if item.get("id") == report_ref),
        None,
    )
    if report is None:
        raise PreconfigurationValidationError("Selected Connector report is unavailable")
    raw_filters = configure.get("filters")
    try:
        filters = (
            json.loads(raw_filters) if isinstance(raw_filters, str) and raw_filters.strip() else []
        )
    except json.JSONDecodeError as exc:
        raise PreconfigurationValidationError("Connector filters must be a JSON array") from exc
    # 57.12 (T3): the cadence is asked ONCE, at the Schedule step, and it lands
    # in `operator_input.schedule.mode` — the same key activation reads first
    # (`datastream_activation.py`, review build). `cadence_intent` was the
    # Configure step's second asking of the same question; the select is gone
    # from the screen, and the key stays as a fallback so a draft written before
    # its removal still compiles to what its operator chose.
    cadence = str(
        (operator_input.get("schedule") or {}).get("mode")
        or configure.get("cadence_intent")
        or "manual"
    )
    # AI-217: `weekly` is one of the four cadences the plan intent declares, and
    # this gate knew three. A draft naming it was refused here before the plan it
    # would have compiled to could be validated at all.
    if cadence not in {"manual", "daily", "weekly", "hourly"}:
        raise PreconfigurationValidationError("Connector cadence is unsupported")
    interval = (
        None if cadence == "manual"
        else 10080 if cadence == "weekly"
        else 1440 if cadence == "daily"
        else 60
    )
    from core.datastream_intents import DatastreamIntentStructuralError, validate_intent

    intent = {
        "contract_version": "1",
        "source": {
            "kind": "connector_pull",
            # The scope the pinned contract was normalized with
            # (`_normalize_contract_snapshot`): at draft time the contract is
            # CONNECTOR-scoped, and `source_account_ref` is a Source Account id,
            # not a connection ref. Writing the account id here mismatched the
            # catalog scope by construction -- every configured connector_pull
            # compile answered 422 `capability_scope_mismatch`.
            "connection_ref_id": "contract:"
            + str(source.get("connector_ref") or source.get("connector_id") or "unavailable"),
            "report_id": report_ref,
            "selection": {
                "selection_mode": report.get("selection_mode", "subset"),
                "metrics": metrics,
                "dimensions": dimensions,
                "grain": _csv_operator_values(configure.get("grain")) or dimensions,
                "filters": filters,
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {"start": None, "end_exclusive": None},
        "schedule": {
            "mode": cadence,
            "interval_minutes": interval,
            # The clock the JOB runs on, not this Project's reporting day boundary.
            # `timezone_origin` says so explicitly (Story 48.3): a preview built before
            # any Project configuration exists has no confirmed boundary to use, and
            # labelling this UTC as one would be the silent assumption AC7 forbids.
            "timezone": "UTC",
            "timezone_origin": "scheduling_default_unconfirmed",
            "watermark": {"kind": "date_window", "delay_minutes": 0},
            "late_arrival": {"lookback_minutes": 0},
            "retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 1},
        },
    }
    try:
        validation = validate_intent(intent, capabilities=capabilities)
    except DatastreamIntentStructuralError as exc:
        raise PreconfigurationValidationError("Connector intent is structurally invalid") from exc
    if not validation.executable:
        codes = ", ".join(sorted({issue.code for issue in validation.issues}))
        raise PreconfigurationValidationError(f"Connector intent is incompatible: {codes}")


def _load_compiler_inputs(
    conn,
    *,
    project_id: str,
    draft_id: str,
    revision_id: str,
    operator_input: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        # These two keys are `_COMPILER_INJECTED`; adding a third here without
        # naming it there brings back the 422 this pair used to raise.
        "operator_input": {**operator_input, "project_id": project_id, "revision_id": revision_id},
        "connector_contract": None,
        "observed_metadata": None,
        "project_configuration": None,
        "capabilities": [],
        "governance_presets": [],
    }
    source = operator_input.get("source") or {}
    connector_id = str(source.get("connector_ref") or source.get("connector_id") or "")
    contract_version_id = str(source.get("connector_contract_version_ref") or "")
    observation_id = str(
        operator_input.get("observation_ref") or source.get("observation_ref") or ""
    )
    if observation_id:
        from core.datastream_setup_observations import load_compiler_observation

        result["observed_metadata"] = load_compiler_observation(
            conn,
            project_id=project_id,
            draft_id=draft_id,
            revision_id=revision_id,
            observation_id=observation_id,
        )
    with conn.cursor() as cur:
        if connector_id:
            if contract_version_id:
                cur.execute(
                    "SELECT id,contract_snapshot,connector_fingerprint,created_at FROM app.connector_contract_versions WHERE id=%s AND connector_id=%s",  # noqa: E501
                    (contract_version_id, connector_id),
                )
            else:
                cur.execute(
                    "SELECT id,contract_snapshot,connector_fingerprint,created_at FROM app.connector_contract_versions WHERE connector_id=%s ORDER BY version_number DESC LIMIT 1",  # noqa: E501
                    (connector_id,),
                )
            row = cur.fetchone()
            if row:
                from core.datastream_setup_observations import (  # noqa: PLC0415
                    _connector_source_category,
                )

                result["connector_contract"] = {
                    "object_id": row[0],
                    "version_id": row[0],
                    "fingerprint": row[2],
                    "observed_at": row[3].isoformat(),
                    # The manifest's `public_catalog.category`, read by the SAME helper
                    # `source-options` uses -- one authority, no second spelling. It is
                    # here because it is half of what `derive_source_type` needs, and
                    # that pair is the only thing Tax & fees can state before the first
                    # publication. Fail-soft: an unreadable manifest yields None, which
                    # resolves to the typed UNKNOWN, never to a plausible category.
                    **_connector_source_category(connector_id),
                    "contract": _contract_for_configuration(
                        _json_value(row[1], {}),
                        operator_input=operator_input,
                        project_id=project_id,
                        connector_id=connector_id,
                    ),
                }
                _validate_connector_configuration(
                    operator_input, result["connector_contract"]["contract"]
                )

        cur.execute(
            "SELECT v.id,v.posture,v.dependency_fingerprint,v.created_at FROM app.projects p LEFT JOIN app.project_configuration_versions v ON v.id=p.active_configuration_version_id WHERE p.id=%s",  # noqa: E501
            (project_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            result["project_configuration"] = {
                "object_id": "project_configuration",
                "version_id": row[0],
                "fingerprint": row[2],
                "observed_at": row[3].isoformat(),
                "settings": _json_value(row[1], {}),
            }
        cur.execute(
            "SELECT capability_key,state,active_version_id,pending_change_set_id,updated_at FROM app.project_capabilities WHERE project_id=%s ORDER BY capability_key",  # noqa: E501
            (project_id,),
        )
        result["capabilities"] = [
            {
                "key": row[0],
                "state": row[1],
                "active_version_id": row[2],
                "pending_version_id": row[3],
                "observed_at": row[4].isoformat(),
                "fingerprint": canonical_hash(row[:4]),
            }
            for row in cur.fetchall()
        ]
    return result


def compile_draft(
    conn,
    *,
    project_id: str,
    draft_id: str,
    actor: str,
    idempotency_key: str,
    expected_revision: int,
) -> dict[str, Any]:
    """Persist one immutable deterministic proposal and advance only its draft pointer."""
    from core.data_identities import mint_data_id

    key_hash = canonical_hash(idempotency_key)
    with conn.cursor() as cur:
        cur.execute(_DRAFT_SELECT + _LOCK_DRAFT_ROW, (draft_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise PreconfigurationNotFound("Draft not found")
        cur.execute(
            "SELECT id FROM app.datastream_preconfiguration_proposals WHERE draft_id=%s AND idempotency_key_hash=%s",  # noqa: E501
            (draft_id, key_hash),
        )
        replay = cur.fetchone()
        if replay:
            return read_proposal(
                conn, project_id=project_id, draft_id=draft_id, proposal_id=replay[0], replay=True
            )
        if int(row[4] or 0) != expected_revision:
            raise PreconfigurationConflict(
                "Expected draft revision does not match current revision"
            )
        operator_input = _json_value(row[8], {})
        inputs = _load_compiler_inputs(
            conn,
            project_id=project_id,
            draft_id=draft_id,
            revision_id=row[3],
            operator_input=operator_input,
        )
        previous = None
        if row[5]:
            cur.execute(
                "SELECT proposal_payload FROM app.datastream_preconfiguration_proposals WHERE id=%s AND draft_id=%s AND project_id=%s",  # noqa: E501
                (row[5], draft_id, project_id),
            )
            prior = cur.fetchone()
            previous = _json_value(prior[0], {}) if prior else None
        proposal = compile_preconfiguration(inputs, previous_proposal=previous)
        cur.execute(
            "SELECT id FROM app.datastream_preconfiguration_proposals WHERE draft_id=%s AND dependency_fingerprint=%s AND content_hash=%s",  # noqa: E501
            (draft_id, proposal["dependency_fingerprint"], proposal["content_hash"]),
        )
        duplicate = cur.fetchone()
        if duplicate:
            return read_proposal(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                proposal_id=duplicate[0],
                replay=True,
            )
        proposal_id = mint_data_id("dspp")
        cur.execute(
            "SELECT COALESCE(MAX(proposal_number),0)+1 FROM app.datastream_preconfiguration_proposals WHERE draft_id=%s",  # noqa: E501
            (draft_id,),
        )
        number = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO app.datastream_preconfiguration_proposals (id,draft_id,project_id,draft_revision_id,proposal_number,schema_version,dependency_snapshot,dependency_fingerprint,section_fingerprints,proposal_payload,content_hash,idempotency_key_hash,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",  # noqa: E501
            (
                proposal_id,
                draft_id,
                project_id,
                row[3],
                number,
                proposal["schema_version"],
                _canonical(proposal["dependency_snapshot"]),
                proposal["dependency_fingerprint"],
                _canonical(proposal["section_fingerprints"]),
                _canonical(proposal),
                proposal["content_hash"],
                key_hash,
                actor,
            ),
        )
        seen = set()
        for proposal_section in proposal["sections"]:
            for item in proposal_section["items"]:
                for evidence in item["evidence_refs"]:
                    identity = (
                        item["key"],
                        evidence["kind"],
                        evidence["object_type"],
                        evidence["object_id"],
                        evidence["version_id"],
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    cur.execute(
                        "INSERT INTO app.datastream_preconfiguration_evidence_refs (proposal_id,draft_id,project_id,proposal_item_key,evidence_kind,object_type,object_id,version_id,fingerprint,observed_at,safe_metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",  # noqa: E501
                        (
                            proposal_id,
                            draft_id,
                            project_id,
                            item["key"],
                            evidence["kind"],
                            evidence["object_type"],
                            evidence["object_id"],
                            evidence["version_id"],
                            evidence["fingerprint"],
                            evidence["observed_at"],
                            _canonical({"bounded": True}),
                        ),
                    )
        cur.execute(
            "UPDATE app.datastream_setup_drafts SET current_proposal_id=%s,updated_at=NOW() WHERE id=%s AND project_id=%s",  # noqa: E501
            (proposal_id, draft_id, project_id),
        )
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_PRECONFIGURATION_COMPILED,
            project_id=project_id,
            draft_id=draft_id,
            evidence_hash=proposal["content_hash"],
        )
    return read_proposal(conn, project_id=project_id, draft_id=draft_id, proposal_id=proposal_id)


def read_proposal(
    conn, *, project_id: str, draft_id: str, proposal_id: str, replay: bool = False
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.draft_revision_id,p.dependency_fingerprint,p.proposal_payload,"
            "d.current_revision_id,r.normalized_operator_input "
            "FROM app.datastream_preconfiguration_proposals p "
            "JOIN app.datastream_setup_drafts d ON d.id=p.draft_id AND d.project_id=p.project_id "
            "LEFT JOIN app.datastream_setup_draft_revisions r ON r.id=d.current_revision_id "
            "WHERE p.id=%s AND p.draft_id=%s AND p.project_id=%s",
            (proposal_id, draft_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise PreconfigurationNotFound("Proposal not found")
    payload = _json_value(row[2], {})
    operator_input = _json_value(row[4], {})
    current_inputs = _load_compiler_inputs(
        conn,
        project_id=project_id,
        draft_id=draft_id,
        revision_id=row[3],
        operator_input=operator_input,
    )
    current_dependencies = _dependencies(current_inputs)
    stored_dependencies = payload.get("dependency_snapshot") or {}
    changed = sorted(
        key
        for key in current_dependencies
        if stored_dependencies.get(key) != current_dependencies.get(key)
    )
    current_causes = [
        {
            "dependency": dependency,
            "cause": "dependency_fingerprint_changed",
            "affected_sections": sorted(
                key
                for key, dependencies in SECTION_DEPENDENCIES.items()
                if dependency in dependencies
            ),
        }
        for dependency in changed
    ]
    stale = row[0] != row[3] or canonical_hash(current_dependencies) != row[1]
    invalidation_causes = list(payload.get("invalidation_causes") or [])
    known_causes = {canonical_hash(item) for item in invalidation_causes}
    invalidation_causes.extend(
        item for item in current_causes if canonical_hash(item) not in known_causes
    )
    payload["invalidation_causes"] = invalidation_causes
    return {
        **payload,
        "draft_ref": draft_id,
        "draft_revision_ref": row[0],
        "proposal_ref": proposal_id,
        "is_stale": stale,
        "resume_href": "",
        "resume_reference": _resume_reference(),
        "idempotent_replay": replay,
    }
