"""Country capability compilation through the governed Datastream plan path.

The Country registry owns hierarchy meaning.  This module only projects that
published evidence onto the existing geographic compiler; connector vocabulary
continues to come from source capability descriptors.
"""

from __future__ import annotations

from typing import Any, Mapping

from core.datastream_intents import compile_geographic_intent
from core.geographic_reporting import GeographicPosture, Market


class CountryActivationError(ValueError):
    """Governed Country evidence cannot produce a deterministic plan."""


def posture_from_evidence(evidence: Mapping[str, Any], *, enabled: bool) -> GeographicPosture:
    """Build the legacy compiler posture from one pinned hierarchy version."""

    if not enabled:
        return GeographicPosture()
    if not evidence.get("hierarchy_version_id"):
        raise CountryActivationError("a published Country hierarchy version is required")
    markets: list[Market] = []
    for raw in evidence.get("markets") or ():
        market_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or "").strip()
        codes = tuple(
            sorted(
                {
                    str(code).strip().upper()
                    for code in raw.get("country_codes") or ()
                    if str(code).strip()
                }
            )
        )
        if not market_id or not label or not codes:
            raise CountryActivationError(
                "every governed Market requires an id, label and country assignment"
            )
        markets.append(Market(id=market_id, label=label, country_codes=codes))
    if not markets:
        raise CountryActivationError(
            "at least one governed Market with assigned countries is required"
        )
    return GeographicPosture(mode="local_markets", markets=tuple(markets))


#: The Country capability states in which a published hierarchy is authoritative.
#: The same two `reports._load_geography_projection` reads, and deliberately not a
#: third list: `degraded` still means "this meaning is published and current", it
#: means the evidence behind it has a gap the operator has been told about.
GOVERNED_CAPABILITY_STATES = frozenset({"ready", "degraded"})


def governed_posture(conn, *, project_id: str) -> GeographicPosture:
    """The Project's governed geography, as the compiler's posture. ONE reader.

    WHY THIS EXISTS (Story 37.9, second pass). `geographic_reporting.
    fetch_project_geographic_posture` reads `app.project_preferences.geographic_mode`
    / `local_markets` -- the columns `country_registry.py` says it "replaces
    outright" and which the Country capability confirmation never writes back. After
    the Tax cascade was moved off them, THREE live readers were still on them, and
    each failed the same silent way for a Project that governs Country properly:

    * `dq_monitors._check_country_conformance` returned early on `mode != local_markets`,
      so the country-conformance monitor NEVER RAN for a governed Project -- unmapped
      provider spellings produced no evidence at all, for exactly the Projects that
      published a Country meaning to compare them against;
    * `flows.upsert_flow` passed the posture into `save_datastream_intent`, so a plan
      saved through the flow surface compiled its geography from the empty preference
      and came out Global or `blocked`;
    * `report_mcp` read it, then handed it to a `render_report` argument that Story
      48.2 had already made a no-op -- dead weight that still described a Project's
      geography as Global in the coverage it derived.

    None of the three was broken code. All three were reading a retired authority,
    which is why the class is fixed here rather than three times over.

    A DISABLED capability reads as Global, and that is not a fallback: it is the
    answer. Deactivation must return reports to consolidated behaviour
    (`country.md`), so `GeographicPosture()` is what a Project with Country off
    means, and it is what an unreadable registry means too -- see below.

    AN UNREADABLE REGISTRY ALSO READS AS GLOBAL, and that is the one place this
    function is weaker than `country_registry.load_projection`, which returns `None`
    so a caller can tell "cannot decide" from "nothing to decide". The posture type
    has no third state to carry it. Callers that must distinguish -- the Tax cascade
    does -- read the projection directly; the three above degrade to consolidated
    reporting, which is what they did before this function existed and is safe: a
    monitor that does not fire and a report that does not group are visibly absent,
    not quietly wrong.
    """

    from core.country_registry import load_projection  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state FROM app.project_capabilities
            WHERE project_id = %s AND capability_key = 'country'
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or str(row[0]) not in GOVERNED_CAPABILITY_STATES:
        return GeographicPosture()

    projection = load_projection(conn, project_id=project_id)
    if projection is None:
        return GeographicPosture()

    from core.country_registry import MARKET  # noqa: PLC0415

    by_market: dict[str, list[str]] = {}
    for country_code, market_id in projection.market_of_value.items():
        node_id = str(market_id)
        # Rest of World is a governed catch-all over countries NOT assigned to an
        # explicit reporting market: its members are resolved, and they are not a
        # tracked market. Including them would make the tracked set -- and every
        # single-country inference drawn from it -- disagree with the Tax cascade.
        if projection.kinds.get(node_id) != MARKET:
            continue
        code = str(country_code).strip().upper()
        if code:
            by_market.setdefault(node_id, []).append(code)
    if not by_market:
        # A published version that defines no tracked market promises nothing
        # geographic, so it reads as Global rather than as an empty local_markets
        # posture -- which `normalize_geographic_posture` refuses outright.
        return GeographicPosture()

    return posture_from_evidence(
        {
            "hierarchy_version_id": projection.hierarchy_version_id,
            "markets": [
                {
                    "id": market_id,
                    "label": str(projection.labels.get(market_id) or market_id),
                    "country_codes": sorted(set(codes)),
                }
                for market_id, codes in sorted(by_market.items())
            ],
        },
        enabled=True,
    )


def compile_country_plan(
    intent: dict[str, Any],
    *,
    evidence: Mapping[str, Any],
    capabilities: dict[str, Any] | None,
    enabled: bool,
) -> dict[str, Any]:
    """Compile the future immutable plan without mutating the current one."""

    return compile_geographic_intent(
        intent,
        posture_from_evidence(evidence, enabled=enabled),
        capabilities,
    )


def _assert_governance_pin(proposals: list[dict[str, Any]], evidence: Mapping[str, Any]) -> None:
    """Refuse a confirmation if the governed hierarchy moved after preview."""

    expected = {
        (
            str(reference.get("object_id") or ""),
            str(reference.get("version_id") or ""),
            str(reference.get("evidence_hash") or ""),
        )
        for proposal in proposals
        for reference in proposal.get("governance_owner_references") or ()
        if reference.get("object_type") == "registry"
    }
    live = {
        (
            str(evidence.get("registry_id") or ""),
            str(evidence.get("hierarchy_version_id") or ""),
            str(evidence.get("evidence_hash") or ""),
        )
    }
    if expected and expected != live:
        from core.project_settings import ProjectSettingsStale  # noqa: PLC0415

        raise ProjectSettingsStale(
            "Review is stale: the governed Country hierarchy changed after prepare"
        )


def apply_country_plan_fan_out(
    conn,
    *,
    project_id: str,
    change_set_id: str,
    proposals: list[dict[str, Any]],
    actor: str,
    enabled: bool,
    loaded_modules: list[Any] | None,
) -> dict[str, Any]:
    """Prepare one isolated AD-23 candidate per reviewed active Datastream.

    The Project capability may advance in the caller transaction, but Data keeps
    publication authority: current plan, mapping, execution and enabled state do
    not move until each materialized candidate passes its normal review.
    """

    country_proposals = [
        proposal
        for proposal in proposals
        if proposal.get("capability_key") == "country"
        and (not enabled or proposal.get("applicability") == "applicable")
    ]
    if not country_proposals:
        return {"plan_versions": [], "candidates": [], "data_pointers_moved": False}

    from core.capability_compilers import CountryCompiler  # noqa: PLC0415
    from core.datastream_field_mapping import save_field_mapping  # noqa: PLC0415
    from core.datastream_intents import save_datastream_intent  # noqa: PLC0415
    from core.datastream_projection import compile_projection  # noqa: PLC0415
    from core.datastream_publication import create_execution  # noqa: PLC0415
    from core.queue import enqueue_activation_work  # noqa: PLC0415
    from core.run_origins import COUNTRY_ACTIVATION, stamp_origin  # noqa: PLC0415

    evidence = CountryCompiler().project_evidence(conn, project_id=project_id, org_id="")
    if enabled:
        _assert_governance_pin(country_proposals, evidence)
    posture = posture_from_evidence(evidence, enabled=enabled)
    results: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for proposal in sorted(
        country_proposals, key=lambda item: str(item.get("datastream_id") or "")
    ):
        datastream_id = str(proposal["datastream_id"])
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.current_plan_version_id, plan.normalized_payload,
                       d.current_mapping_version_id, mapping.mapping_payload
                FROM app.datastreams d
                JOIN app.datastream_plan_versions plan
                  ON plan.id = d.current_plan_version_id
                 AND plan.datastream_id = d.id
                 AND plan.project_id = d.project_id
                JOIN app.datastream_mapping_versions mapping
                  ON mapping.id = d.current_mapping_version_id
                 AND mapping.datastream_id = d.id
                 AND mapping.project_id = d.project_id
                WHERE d.id = %s AND d.project_id = %s
                  AND d.enabled = TRUE
                  AND d.lifecycle_state = 'active'
                FOR UPDATE
                """,
                (datastream_id, project_id),
            )
            row = cur.fetchone()
        if row is None:
            from core.project_settings import ProjectSettingsStale  # noqa: PLC0415

            raise ProjectSettingsStale(
                f"Review is stale: Datastream {datastream_id} is not active or has incomplete pins"
            )
        previous_plan_version_id, intent, previous_mapping_version_id, mapping_payload = row
        expected_previous = proposal.get("current_plan_version_id") or (
            proposal.get("dependency_snapshot") or {}
        ).get("current_plan_version_id")
        if expected_previous and str(previous_plan_version_id) != str(expected_previous):
            from core.project_settings import ProjectSettingsStale  # noqa: PLC0415

            raise ProjectSettingsStale(
                f"Review is stale: Datastream {datastream_id} plan changed after prepare"
            )
        expected_mapping = proposal.get("current_mapping_version_id") or (
            proposal.get("dependency_snapshot") or {}
        ).get("current_mapping_version_id")
        if expected_mapping and str(previous_mapping_version_id) != str(expected_mapping):
            from core.project_settings import ProjectSettingsStale  # noqa: PLC0415

            raise ProjectSettingsStale(
                f"Review is stale: Datastream {datastream_id} mapping changed after prepare"
            )
        if isinstance(intent, str):
            import json  # noqa: PLC0415

            intent = json.loads(intent)
        saved = save_datastream_intent(
            datastream_id=datastream_id,
            project_id=project_id,
            intent=intent,
            identity=actor,
            idempotency_key=f"country:{change_set_id}:{datastream_id}",
            conn=conn,
            loaded_modules=loaded_modules,
            geographic_posture=posture,
            reason=(
                "country_capability_confirmed" if enabled else "country_capability_deactivated"
            ),
            commit=False,
            advance_pointer=False,
        )
        if not saved.get("executable"):
            raise CountryActivationError(
                f"Datastream {datastream_id} compiled a non-executable Country plan"
            )
        expected_hash = (
            (proposal.get("impact") or {})
            .get("detected_support_selection", {})
            .get("proposed_plan_content_hash")
        )
        if enabled and expected_hash and saved["content_hash"] != expected_hash:
            from core.project_settings import ProjectSettingsStale  # noqa: PLC0415

            raise ProjectSettingsStale(
                f"Review is stale: Datastream {datastream_id} compiled plan changed"
            )
        if isinstance(mapping_payload, str):
            import json  # noqa: PLC0415

            mapping_payload = json.loads(mapping_payload)
        mapping = save_field_mapping(
            datastream_id=datastream_id,
            project_id=project_id,
            mapping_payload=mapping_payload,
            identity=actor,
            idempotency_key=f"country:{change_set_id}:{datastream_id}:mapping",
            conn=conn,
            pinned_plan_version_id=str(saved["id"]),
            advance_pointer=False,
            commit=False,
        )
        projection = compile_projection(mapping)
        if not mapping.get("executable") or not projection.get("executable"):
            raise CountryActivationError(
                f"Datastream {datastream_id} cannot materialize an executable Country candidate"
            )
        # Story 63.7: a Country change touches several Datastreams at once, so
        # each candidate it mints names the change that produced it.
        candidate = create_execution(
            datastream_id,
            project_id,
            str(saved["id"]),
            str(mapping["id"]),
            stamp_origin(projection, COUNTRY_ACTIVATION),
            actor,
            f"country:{change_set_id}:{datastream_id}:candidate",
            conn,
        )
        job = enqueue_activation_work(
            kind="candidate_materialization",
            project_id=project_id,
            datastream_id=datastream_id,
            execution_id=str(candidate["id"]),
            correlation_id=str(candidate["id"]),
            payload={
                "mode": "country_activation" if enabled else "country_deactivation",
                "change_set_id": change_set_id,
                "backfill": (proposal.get("impact") or {}).get("backfill") or {},
                "publication_policy": "explicit_review_required",
            },
            requested_by=actor,
            conn=conn,
        )
        results.append(
            {
                "datastream_id": datastream_id,
                "previous_plan_version_id": previous_plan_version_id,
                "plan_version_id": saved["id"],
                "version_number": saved["version_number"],
                "content_hash": saved["content_hash"],
                "executable": saved["executable"],
            }
        )
        candidates.append(
            {
                "datastream_id": datastream_id,
                "mapping_version_id": mapping["id"],
                "candidate_execution_id": candidate["id"],
                "candidate_job_id": job["job_id"],
                "backfill": (proposal.get("impact") or {}).get("backfill") or {},
            }
        )
    return {
        "plan_versions": results,
        "candidates": candidates,
        "data_pointers_moved": False,
    }
