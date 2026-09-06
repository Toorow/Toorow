"""The seven capability adapters that plug into the common proposal contract (Story 48.1).

Each adapter answers one question for one Datastream -- *does this capability
apply here, and what does it change?* -- by reading its owner's existing
versioned evidence. It never invents the capability's semantics: Country,
Currency & FX, Reporting Timezone, Tax & Fees and Competitors are detailed by
Stories 48.2 through 48.5, Placement Mapping by story 61.5, Analytics Alignment
by story 70.3 (`core/analytics_alignment.py`), and this file is where the common
compiler consumes their contract, not where that contract is defined.

Two rules hold for all seven, and they are the reason this file is small:

* **Fail closed.** When the governed evidence a capability requires is absent,
  the verdict is ``unavailable`` with an explicit blocker and a canonical repair
  route. It is never an optimistic ``complete`` and never a silent skip. Missing
  evidence and inapplicable scope are NOT the same answer: a Datastream a
  capability cannot reach at all is ``not_applicable`` and leaves the
  denominator, because counting it would lower a percentage for a question that
  was never asked of it.
* **No provider vocabulary.** Nothing here names a connector. Support is read
  from the installed adapter contract and from the Datastream's own plan and
  mapping, which is what makes the common core source-agnostic.

Where a capability's Governance object does not yet have a Governance-owned
version id, its version is content-addressed over the exact evidence read --
a stable, verifiable identity that a confirmation can recheck. That is stated in
each adapter rather than hidden, because the difference matters when Epic 49
lands the object workbenches that will own those ids.
"""

from __future__ import annotations

from typing import Any

from core.capability_proposals import (
    NOT_APPLICABLE,
    CapabilityAssessment,
    CapabilityBlocker,
    CapabilityException,
    CompileContext,
    GovernanceOwner,
    canonical_hash,
    governance_owner_reference,
    register_compiler,
)

# ---------------------------------------------------------------------------
# Shared shapes.
# ---------------------------------------------------------------------------


def _disabled(capability_key: str) -> CapabilityAssessment:
    """An optional capability the Project has not enabled has no applicable scope."""
    return CapabilityAssessment(
        applicability=NOT_APPLICABLE,
        coverage_state=NOT_APPLICABLE,
        reason=f"{capability_key} is not enabled for this Project.",
        detected_support_selection={"state": "not_requested"},
    )


def _missing_governance(
    capability_key: str, what: str, *, repair_object_type: str
) -> CapabilityAssessment:
    """Required governed evidence is absent: unavailable, with where to fix it."""
    return CapabilityAssessment(
        applicability="applicable",
        coverage_state="unavailable",
        reason=f"Required {what} is not defined in Governance.",
        detected_support_selection={"state": "unavailable", "missing": what},
        blockers=(
            CapabilityBlocker(
                code="missing_governance_evidence",
                message=f"{what} must be defined before {capability_key} can be activated.",
            ),
        ),
        repair=governance_owner_reference(capability_key, object_type=repair_object_type),
    )


def _plan_block(datastream: dict[str, Any], key: str) -> dict[str, Any]:
    block = (datastream.get("plan_payload") or {}).get(key)
    return block if isinstance(block, dict) else {}


def _mapping_targets(datastream: dict[str, Any]) -> set[str]:
    """The canonical targets this Datastream's current mapping actually binds."""
    payload = datastream.get("mapping_payload") or {}
    targets: set[str] = set()
    for item in payload.get("fields") or []:
        if not isinstance(item, dict):
            continue
        target = item.get("target") or item.get("canonical") or item.get("canonical_name")
        if isinstance(target, dict):
            target = target.get("canonical") or target.get("canonical_name")
        if isinstance(target, str) and target.strip():
            targets.add(target.strip())
    return targets


# ---------------------------------------------------------------------------
# Country. Governance owns countries, markets, regions and aliases; Data must
# compile country into every applicable compatible Datastream plan.
# ---------------------------------------------------------------------------

_COUNTRY_COMPLETE_STATUSES = frozenset({"country_complete", "preserved_full_grain"})


class CountryCompiler:
    capability_key = "country"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """Read the exact governed Country hierarchy and its Market projection."""

        from core.country_registry import fetch_country_registry  # noqa: PLC0415
        from core.master_data import require_version  # noqa: PLC0415

        registry = fetch_country_registry(conn, project_id=project_id)
        if registry is None or not registry.get("current_version_id"):
            return {
                "registry_id": registry["id"] if registry else None,
                "hierarchy_version_id": None,
                "vocabulary_version_id": None,
                "evidence_hash": None,
                "assigned_market_count": 0,
                "markets": [],
            }
        version = require_version(
            conn, project_id=project_id, version_id=str(registry["current_version_id"])
        )
        from core.country_registry import load_projection  # noqa: PLC0415

        projection = load_projection(
            conn, project_id=project_id, version_id=str(version["id"])
        )
        countries_by_market: dict[str, list[str]] = {}
        if projection:
            for country_code, market_id in projection.market_of_value.items():
                countries_by_market.setdefault(str(market_id), []).append(
                    str(country_code)
                )
        markets = [
            {
                "id": market_id,
                "label": str(projection.labels.get(market_id) or market_id),
                "country_codes": sorted(country_codes),
            }
            for market_id, country_codes in sorted(countries_by_market.items())
        ] if projection else []
        return {
            "registry_id": registry["id"],
            "hierarchy_version_id": version["id"],
            "vocabulary_version_id": version["vocabulary_version_id"],
            "evidence_hash": version["content_hash"],
            "assigned_market_count": len(markets),
            "markets": markets,
        }

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        if context.requested_state != "enabled":
            return _disabled(self.capability_key)
        evidence = context.project_evidence
        if not evidence.get("hierarchy_version_id"):
            return _missing_governance(
                self.capability_key,
                "a published Country hierarchy version",
                repair_object_type="registry",
            )
        if not evidence.get("assigned_market_count"):
            return _missing_governance(
                self.capability_key,
                "at least one Market with assigned countries",
                repair_object_type="registry",
            )
        owner = (
            GovernanceOwner(
                object_type="registry",
                object_id=str(evidence["registry_id"]),
                version_id=str(evidence["hierarchy_version_id"]),
                evidence_hash=str(evidence["evidence_hash"]),
            ),
        )

        if not datastream.get("current_mapping_version_id"):
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason="The active Datastream has no current governed mapping.",
                detected_support_selection={"state": "unavailable"},
                governance_owners=owner,
                blockers=(
                    CapabilityBlocker(
                        code="country_mapping_unavailable",
                        message=(
                            "Publish an executable Datastream mapping before activating Country."
                        ),
                    ),
                ),
                repair=_datastream_repair(datastream, "mapping"),
            )
        plan = datastream.get("plan_payload") or {}
        capabilities = None
        source = plan.get("source") if isinstance(plan.get("source"), dict) else {}
        if source.get("kind") == "connector_pull":
            from core.source_capabilities import (  # noqa: PLC0415
                SourceCapabilitiesNotFound,
                SourceCapabilitiesUnavailable,
                get_scoped_source_capabilities,
            )

            try:
                if context.loaded_modules is None:
                    raise SourceCapabilitiesUnavailable(
                        "loaded module catalog is required"
                    )
                capabilities = get_scoped_source_capabilities(
                    project_id=context.project_id,
                    connection_ref_id=str(
                        source.get("connection_ref_id")
                        or datastream.get("connection_ref_id")
                        or ""
                    ),
                    identity=context.actor,
                    loaded_modules=context.loaded_modules,
                    conn=conn,
                    module_name=str(
                        source.get("module")
                        or datastream.get("module_name")
                        or ""
                    )
                    or None,
                )
            except (SourceCapabilitiesNotFound, SourceCapabilitiesUnavailable):
                return CapabilityAssessment(
                    applicability="applicable",
                    coverage_state="unavailable",
                    reason="The connector capability descriptor cannot be read in this Project.",
                    detected_support_selection={"state": "unavailable"},
                    governance_owners=owner,
                    blockers=(
                        CapabilityBlocker(
                            code="country_capability_unavailable",
                            message=(
                                "Restore the connector capability descriptor before "
                                "activating Country."
                            ),
                        ),
                    ),
                    repair=_datastream_repair(datastream, "source"),
                )

        from core.country_activation import compile_country_plan  # noqa: PLC0415
        from core.datastream_intents import normalize_intent  # noqa: PLC0415

        compiled = compile_country_plan(
            plan,
            evidence=evidence,
            capabilities=capabilities,
            enabled=True,
        )
        status = str((compiled.get("geographic") or {}).get("compilation_status") or "")
        country_field = (compiled.get("geographic") or {}).get(
            "effective_country_field"
        )
        proposed_hash = normalize_intent(compiled)[1]
        support = {
            "state": "detected" if status in _COUNTRY_COMPLETE_STATUSES else "unavailable",
            "compilation_status": status or None,
            "effective_country_field": country_field,
            "selected": bool(country_field),
            "proposed_plan_content_hash": proposed_hash,
        }
        if status not in _COUNTRY_COMPLETE_STATUSES:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason="This Datastream cannot compile a canonical-country joint grain.",
                detected_support_selection=support,
                governance_owners=owner,
                blockers=(
                    CapabilityBlocker(
                        code="country_grain_unavailable",
                        message=(
                            "Select a country-compatible report, or repair this "
                            "Datastream before activating Country."
                        ),
                    ),
                ),
                repair=_datastream_repair(datastream, "mapping"),
            )
        selection = (compiled.get("source") or {}).get("selection") or {}
        grain_after = sorted(str(item) for item in selection.get("grain") or ())
        current_status = str(
            ((plan.get("geographic") or {}).get("compilation_status") or "")
        )
        retained = bool(datastream.get("current_published_execution_id")) and (
            current_status in _COUNTRY_COMPLETE_STATUSES
        )
        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="complete" if retained else "partial",
            reason=(
                "Country is already retained by the published execution."
                if retained
                else "The future plan is Country-compatible; coverage begins after publication."
            ),
            detected_support_selection=support,
            grain_after=grain_after,
            governance_owners=owner,
            exceptions=()
            if retained
            else (
                CapabilityException(
                    kind="exception",
                    severity="degrading",
                    reason_code="country_history_not_retained",
                    reason="Country coverage begins at the first publication that includes it.",
                    owner_kind="data",
                ),
            ),
            repair=None if retained else _datastream_repair(datastream, "runs"),
        )

def _current_grain(datastream: dict[str, Any]) -> list[str]:
    payload = datastream.get("mapping_payload") or {}
    return [str(item) for item in (payload.get("grain") or [])]


def _datastream_repair(datastream: dict[str, Any], tab: str) -> dict[str, Any]:
    from core.capability_proposals import datastream_owner_reference  # noqa: PLC0415

    return datastream_owner_reference(str(datastream["id"]), tab=tab)


# ---------------------------------------------------------------------------
# Currency & FX. Always present. A Datastream with no monetary field is
# Not applicable -- not degraded: there is nothing for the capability to govern.
# ---------------------------------------------------------------------------


class CurrencyFxCompiler:
    capability_key = "currency_fx"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """Read the exact governed owners, or record that there are none.

        Two things changed in Story 48.3, and both were about believing the wrong
        source.

        The reporting currency came from ``app.project_preferences``, whose column
        carried a ``DEFAULT 'EUR'``: every Project was born looking decided. The
        owner version was ``canonical_hash`` over that mutable row -- a digest of a
        field, not an object anyone edited, so a confirmation rechecked a number
        rather than a decision. It is now the confirmed Money Policy version.

        Monetary classification came from ``currency_scope IS NOT NULL OR unit IS
        NOT NULL``. ``unit`` is populated for sessions, impressions and seconds, so
        that predicate classified most of the catalogue as money -- which AC8 names
        outright: "``unit IS NOT NULL``, field names and Connector guesses are not
        valid classifiers". It now reads the governed Semantic Model, where a
        Concept version declares ``value_type = 'money'``, and pins the exact
        version of every Concept it counted.
        """

        from core.fx_rate_sets import rate_freshness  # noqa: PLC0415
        from core.money_policy import try_resolve_money_policy  # noqa: PLC0415

        policy = try_resolve_money_policy(conn, project_id=project_id)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.id, c.name, v.id
                     FROM app.semantic_concepts c
                     JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
                    WHERE c.lifecycle_status = 'published'
                      AND (c.project_id = %s OR c.project_id IS NULL)
                      AND v.kind = 'metric'
                      AND v.value_type = 'money'
                    ORDER BY c.name""",
                (project_id,),
            )
            monetary = [
                {"concept_id": str(row[0]), "name": str(row[1]), "version_id": str(row[2])}
                for row in cur.fetchall()
            ]
        return {
            "money_policy": policy,
            "reporting_currency": policy.reporting_currency if policy else None,
            "money_policy_version_id": policy.version_id if policy else None,
            "monetary_concepts": monetary,
            "monetary_names": sorted(item["name"] for item in monetary),
            "rate_freshness": rate_freshness(conn, project_id=project_id),
        }

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        from core.money_evidence import latest_money_evidence  # noqa: PLC0415

        evidence = context.project_evidence
        monetary = {item["name"]: item for item in evidence.get("monetary_concepts") or []}
        bound = sorted(_mapping_targets(datastream) & set(monetary))
        if not bound:
            # `Not applicable`, not degraded: there is nothing here for the
            # capability to govern, and saying `Disabled` would be a different claim.
            return CapabilityAssessment(
                applicability=NOT_APPLICABLE,
                coverage_state=NOT_APPLICABLE,
                reason="This Datastream publishes no field classified as monetary.",
                detected_support_selection={"state": "detected", "monetary_fields": []},
            )

        policy = evidence.get("money_policy")
        if policy is None:
            return _missing_governance(
                self.capability_key,
                "a confirmed Money Policy with a reporting currency",
                repair_object_type="rule-set",
            )

        # The owners: the Money Policy version, plus the EXACT Concept version of
        # every monetary field this Datastream actually binds. A confirmation
        # rechecks the objects an operator edited.
        owners = (
            GovernanceOwner(
                object_type="rule-set",
                object_id=policy.rule_set_id,
                version_id=policy.version_id,
                evidence_hash=policy.content_hash,
            ),
            *(
                GovernanceOwner(
                    object_type="semantic-concept",
                    object_id=monetary[name]["concept_id"],
                    version_id=monetary[name]["version_id"],
                    evidence_hash=canonical_hash(monetary[name]),
                )
                for name in bound
            ),
        )

        observed = latest_money_evidence(
            conn, project_id=context.project_id, datastream_id=str(datastream["id"])
        )
        freshness = evidence.get("rate_freshness") or {}
        support = {
            "state": "detected",
            "monetary_fields": bound,
            "reporting_currency": policy.reporting_currency,
            "observed_native_currencies": sorted(observed.native_currencies) if observed else [],
            "rate_freshness": freshness,
            "selected": True,
        }

        if observed is None:
            # No publication has proved what currency and unit these rows carry.
            # `unavailable` with a repair route, never an optimistic `complete`.
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason=(
                    "No publication has recorded the native currency, unit and adapter "
                    "of this Datastream's monetary fields."
                ),
                detected_support_selection=support,
                governance_owners=owners,
                blockers=(
                    CapabilityBlocker(
                        code="money_evidence_unobserved",
                        message=(
                            "Run this Datastream so its published rows record their source "
                            "currency, unit and exact adapter."
                        ),
                    ),
                ),
                repair=_datastream_repair(datastream, "runs"),
            )

        if observed.gaps:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason=(
                    f"{len(observed.gaps)} monetary field(s) published without a resolvable "
                    "currency, unit or adapter, so their values cannot be compared."
                ),
                detected_support_selection=support,
                governance_owners=owners,
                blockers=tuple(
                    CapabilityBlocker(
                        code=str(gap.get("code") or "money_evidence_gap"),
                        message=str(gap.get("message") or "Unresolvable monetary evidence."),
                    )
                    for gap in observed.gaps
                ),
                repair=_datastream_repair(datastream, "mapping"),
            )

        if freshness.get("state") == "no_rate_set" and observed.needs_conversion(
            policy.reporting_currency
        ):
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason=(
                    "This Datastream publishes a currency other than the reporting currency "
                    "and no validated FX Rate Set exists, so no conversion has evidence."
                ),
                detected_support_selection=support,
                governance_owners=owners,
                blockers=(
                    CapabilityBlocker(
                        code="fx_rate_set_absent",
                        message="Confirm an FX ingestion policy and land a validated rate batch.",
                    ),
                ),
                repair=governance_owner_reference(self.capability_key, object_type="rule-set"),
            )

        on_last_known_good = freshness.get("state") == "on_last_known_good"
        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="partial" if on_last_known_good else "complete",
            reason=(
                "Monetary fields carry native currency, unit and adapter, and convert under "
                "the confirmed Money Policy."
                if not on_last_known_good
                else "Conversions are running on the last known good rate batch."
            ),
            detected_support_selection=support,
            governance_owners=owners,
            exceptions=()
            if not on_last_known_good
            else (
                CapabilityException(
                    kind="exception",
                    severity="degrading",
                    reason_code="fx_rate_set_last_known_good",
                    reason=(
                        "The current rate batch did not activate; conversions use the last "
                        "validated one and will be blocked once it exceeds the policy's "
                        "maximum staleness."
                    ),
                    owner_kind="governance",
                ),
            ),
            repair=None
            if not on_last_known_good
            else governance_owner_reference(self.capability_key, object_type="rule-set"),
        )


# ---------------------------------------------------------------------------
# Reporting Timezone. Always present. It SIGNALS a cross-source boundary gap;
# it does not realign anything -- the invariant is one DATE, never an hour.
# ---------------------------------------------------------------------------


class ReportingTimezoneCompiler:
    capability_key = "reporting_timezone"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """The confirmed Timezone Policy version, or nothing.

        This used to read ``app.project_preferences.reporting_timezone``, whose
        column carried ``DEFAULT 'Europe/Paris'``, and emit ``canonical_hash`` over
        it as the owner version. A default is not a decision and a digest of a
        mutable field is not an object; a confirmation therefore rechecked neither.
        """

        from core.money_policy import try_resolve_timezone_policy  # noqa: PLC0415

        policy = try_resolve_timezone_policy(conn, project_id=project_id)
        return {
            "timezone_policy": policy,
            "reporting_timezone": policy.reporting_timezone if policy else None,
            "tzdb_version": policy.tzdb_version if policy else None,
            "timezone_policy_version_id": policy.version_id if policy else None,
        }

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        from core.report_timezone import declared_zone  # noqa: PLC0415
        from core.time_boundary import latest_boundary_evidence  # noqa: PLC0415

        evidence = context.project_evidence
        policy = evidence.get("timezone_policy")
        if policy is None:
            return _missing_governance(
                self.capability_key,
                "a confirmed Reporting Timezone Policy",
                repair_object_type="rule-set",
            )
        owner = (
            GovernanceOwner(
                object_type="rule-set",
                object_id=policy.rule_set_id,
                version_id=policy.version_id,
                evidence_hash=policy.content_hash,
            ),
        )

        # The declaration says what the source CAN do; the observation says what it
        # DID. Both are reported, and only the second can carry coverage to
        # `complete` -- AC6, made structural rather than promised.
        declaration = _time_context(datastream)
        observed = latest_boundary_evidence(
            conn, project_id=context.project_id, datastream_id=str(datastream["id"])
        )
        lever = _lever(declaration, observed)
        support = {
            "state": "detected" if (declaration or observed) else "unavailable",
            "declared_locus": declaration.get("locus") if declaration else None,
            "declared_zone": declared_zone(declaration),
            "observed_zone": observed.observed_report_timezone if observed else None,
            "observed_grain": observed.grain if observed else None,
            "evidence_origin": observed.evidence_origin if observed else "none",
            "project_zone": policy.reporting_timezone,
            "tzdb_version": policy.tzdb_version,
            "adjustment_lever": lever,
            "selected": bool(observed and observed.observed_report_timezone),
        }

        if observed is None:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason=(
                    "No publication has recorded the clock this source drew its day "
                    "boundaries on."
                    if declaration
                    else "The installed Connector contract declares no time context, and no "
                    "publication has recorded one."
                ),
                detected_support_selection=support,
                governance_owners=owner,
                blockers=(
                    CapabilityBlocker(
                        code="time_boundary_unobserved",
                        message=(
                            "Run this Datastream so its publication records the source day "
                            "boundary. Until then its days cannot be declared equivalent to "
                            "another source's."
                        ),
                    ),
                ),
                repair=_datastream_repair(datastream, "runs"),
            )

        if not observed.is_observed:
            # A declaration was recorded as evidence. Real, useful and NOT proof:
            # it is what the Connector says, not what the pull showed.
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="partial",
                reason=(
                    "The day boundary is known from the Connector declaration only; no pull "
                    "or publication has confirmed it."
                ),
                detected_support_selection=support,
                governance_owners=owner,
                exceptions=(
                    CapabilityException(
                        kind="exception",
                        severity="degrading",
                        reason_code="time_boundary_declared_not_observed",
                        reason=(
                            "A Connector declaration states what a source can do; only a "
                            "publication states what it did."
                        ),
                        owner_kind="data",
                    ),
                ),
                repair=_datastream_repair(datastream, "runs"),
            )

        if not observed.observed_report_timezone:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason=(
                    "This Datastream published without a resolvable reporting timezone, so "
                    "its days cannot be placed on a clock."
                ),
                detected_support_selection=support,
                governance_owners=owner,
                blockers=(
                    CapabilityBlocker(
                        code=str(observed.gap_code or "source_report_timezone_unknown"),
                        message=(
                            "Record the source reporting timezone, or use the source-side "
                            "lever if one exists."
                        ),
                    ),
                ),
                repair=_datastream_repair(datastream, "processing"),
            )

        aligned = observed.observed_report_timezone == policy.reporting_timezone
        exceptions: list[CapabilityException] = []
        if observed.assumed:
            exceptions.append(
                CapabilityException(
                    kind="exception",
                    severity="degrading",
                    reason_code="time_boundary_assumed",
                    reason=(
                        "The boundary is an explicitly declared assumption, not a value read "
                        "from the source. It is versioned and visible, and it is still an "
                        "assumption."
                    ),
                    owner_kind="data",
                )
            )
        if not aligned:
            exceptions.append(
                CapabilityException(
                    kind="exception",
                    severity="degrading",
                    reason_code="timezone_offset_signalled",
                    reason=(
                        f"This source draws its day on {observed.observed_report_timezone}, "
                        f"the Project on {policy.reporting_timezone}. The offset is signalled "
                        "and never corrected: at DATE grain there is no hour to re-slice."
                    ),
                    owner_kind="governance",
                )
            )

        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="complete" if (aligned and not observed.assumed) else "partial",
            reason=(
                "A publication proves this source reports on the Project reporting timezone."
                if aligned and not observed.assumed
                else "The observed boundary is known and does not match the Project's, or rests "
                "on a declared assumption."
            ),
            detected_support_selection=support,
            governance_owners=owner,
            exceptions=tuple(exceptions),
            repair=None
            if (aligned and not observed.assumed)
            else governance_owner_reference(self.capability_key, object_type="rule-set"),
        )


def _lever(declaration: dict[str, Any], observed: Any) -> dict[str, Any]:
    """What the operator can actually change at the source, and where.

    DELEGATES to ``report_timezone.resolve_lever`` -- the single resolution (AI-132).
    This function used to own the answer, and a second one grew beside it for the
    day-offset signal: on the same declaration (``locus: network``) this screen said a
    lever exists while the mapping signal said "no lever available". The same product,
    two opposite answers about the same source. The precedence that lived here --
    observation beats declaration, because a declared lever no run ever exercised is a
    promise rather than a lever -- is preserved verbatim in the shared function, and an
    EXPLICIT ``adjustment_lever`` declaration now sits between the two.
    """
    from core.report_timezone import resolve_lever  # noqa: PLC0415

    return resolve_lever(declaration, observed)


def _time_context(datastream: dict[str, Any]) -> dict[str, Any]:
    """Read the module's declared ``time_context`` fail-soft, without a DB round trip."""
    config = datastream.get("config") or {}
    for holder in (config.get("source_capabilities"), config.get("capabilities")):
        if isinstance(holder, dict) and isinstance(holder.get("time_context"), dict):
            return dict(holder["time_context"])
    module_name = datastream.get("module_name")
    if not module_name:
        return {}
    try:
        from core.main import _loaded_modules  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- the loader is not available in every context
        return {}
    manifest = next(
        (
            module.manifest
            for module in _loaded_modules
            if getattr(module, "name", None) == module_name
        ),
        None,
    )
    if not isinstance(manifest, dict):
        return {}
    capabilities = manifest.get("source_capabilities")
    if not isinstance(capabilities, dict):
        return {}
    context = capabilities.get("time_context")
    return dict(context) if isinstance(context, dict) else {}


# ---------------------------------------------------------------------------
# Tax & Fees. Governance owns the ordered rule set; a proposed rule stays inert
# until confirmed, so only confirmed rules are governed evidence here.
# ---------------------------------------------------------------------------


def _tax_fee_preset_proposals(
    conn, *, project_id: str, org_id: str
) -> list[dict[str, Any]]:
    """Qualified rule candidates for this Project, or an empty list, honestly.

    Completeness criterion [0] is "activation opens a blank rule editor WHEN A
    QUALIFIED PROPOSAL IS POSSIBLE". `propose_from_presets` has existed since this
    story's first commit and answered exactly that question -- but it was reachable
    only from `core.fee_tax_mcp`, so the activation path the criterion names never
    asked. An operator with fifteen published presets covering their own countries
    and an operator with none were told the same thing: something is missing.

    The narrowing is by JURISDICTION and by DATE only, and it never concludes: each
    proposal carries its unproven qualifications, and adopting one is an operator's
    act. A preset whose `jurisdiction_kind` is `none` -- an agency or platform fee
    that no country governs -- matches whether or not Country is enabled, which is
    the AC5 asymmetry.

    Returns `[]` rather than raising when the Project has no Country hierarchy: a
    Project that reports on nothing geographic still legitimately gets the
    jurisdiction-independent proposals, and no proposal at all is a truthful answer
    that leaves the blank editor in place.
    """

    from datetime import date  # noqa: PLC0415

    from core.country_registry import fetch_country_registry, load_projection  # noqa: PLC0415
    from core.master_data import require_version  # noqa: PLC0415
    from core.tax_fee_presets import (  # noqa: PLC0415
        list_preset_versions,
        propose_from_presets,
    )

    presets = list_preset_versions(conn, org_id=org_id)
    if not presets:
        return []

    # The jurisdictions this Project actually governs, pinned to the exact
    # hierarchy version, so an adopted draft rule cites a version rather than a
    # copied label (AC5). No hierarchy means no pin, and `_draft_rule` then leaves
    # the jurisdiction deliberately incomplete so publishing it is REFUSED by the
    # ladder profile rather than silently pinning nothing.
    codes: list[str] = []
    hierarchy_version_id: str | None = None
    registry = fetch_country_registry(conn, project_id=project_id)
    if registry and registry.get("current_version_id"):
        version = require_version(
            conn, project_id=project_id, version_id=str(registry["current_version_id"])
        )
        hierarchy_version_id = str(version["id"])
        projection = load_projection(
            conn, project_id=project_id, version_id=hierarchy_version_id
        )
        if projection:
            codes = sorted({str(code).upper() for code in projection.market_of_value})

    proposals = propose_from_presets(
        presets,
        jurisdiction_codes=codes,
        hierarchy_version_id=hierarchy_version_id,
        on_date=date.today(),
    )
    return [proposal.as_payload() for proposal in proposals]


class TaxFeesCompiler:
    capability_key = "tax_fees"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """Read the EXACT governed ladder version, the Money Policy and the
        observed Data evidence -- and nothing mutable.

        Three things changed in Story 48.4, and all three were about attesting to
        something that could not be reproduced.

        The rules came from ``app.fee_tax_rules`` filtered on a per-row
        ``status = 'confirmed'``. Those rows mutate in place, so a proposal pinned a
        state that no longer existed by the time anyone read it. They now come from
        the published version of the ``tax_fee`` Rule Set, which is frozen by trigger.

        The owner was ``GovernanceOwner(object_id=<one rule id>, version_id=<hash of
        a partial row list>)`` -- one FAKE owner per rule, and a version id no
        workbench could open. There is one owner now, and it is the Rule Set: the
        object an operator actually edits.

        The drift hash covered eight columns. Rate, amount, currency, conditions,
        jurisdiction, effective dates, source and approvals were ALL outside it, so
        an operator could change a VAT rate and the proposal would report no drift.
        It is now the version's own content hash, which is computed over the
        normalized ladder including every one of those -- plus the Money Policy,
        geography and Data evidence versions the composition depends on (AC8).
        """

        from core.money_policy import try_resolve_money_policy  # noqa: PLC0415
        from core.tax_evidence import tax_evidence_for_project  # noqa: PLC0415
        from core.tax_fee_rule_set import try_resolve_tax_fee_ladder  # noqa: PLC0415

        ladder = try_resolve_tax_fee_ladder(conn, project_id=project_id)
        policy = try_resolve_money_policy(conn, project_id=project_id)
        observed = tax_evidence_for_project(conn, project_id=project_id)
        return {
            "ladder": ladder,
            "money_policy": policy,
            "tax_evidence_by_datastream": observed,
            # Completeness criterion [0]. Read ONLY to answer "was a qualified
            # proposal possible?" when no ladder is published; deliberately absent
            # from `drift_dimensions` below, because a proposal is an offer and
            # confirming one must not be invalidated by another preset appearing.
            "preset_proposals": _tax_fee_preset_proposals(
                conn, project_id=project_id, org_id=org_id
            ),
            # Every dimension AC8 names, in one place, so a reader can check the
            # list against the acceptance criterion rather than against the code.
            "drift_dimensions": {
                "rule_set_version_id": ladder.version_id if ladder else None,
                "rule_set_content_hash": ladder.content_hash if ladder else None,
                "money_policy_version_id": policy.version_id if policy else None,
                "geography_hierarchy_version_ids": (
                    list(ladder.pinned_hierarchy_versions()) if ladder else []
                ),
                "tax_evidence_version_ids": sorted(
                    item.evidence_version_id for item in observed.values()
                ),
            },
        }

    def _proposal_pending(
        self, missing: str, proposals: list[dict[str, Any]]
    ) -> CapabilityAssessment:
        """Unavailable, but with something to review rather than an empty page.

        Still `unavailable` and still `selected: False`: narrowing a preset by the
        Project's own countries does not prove that VAT, DST, a withholding or a
        platform pass-through actually applies to it (AC3). Nothing composes until
        an operator answers the open qualifications and publishes a ladder. What
        changes is that the operator is shown WHAT may apply, WHY, and WHICH facts
        they must confirm -- which is the whole distance between a proposal and a
        blank form.
        """
        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="unavailable",
            reason=(
                f"{len(proposals)} qualified rule proposal(s) match this Project's "
                "jurisdictions and are waiting to be reviewed, edited or rejected."
            ),
            detected_support_selection={
                "state": "proposed",
                "missing": missing,
                "preset_proposals": proposals,
                "selected": False,
            },
            blockers=(
                CapabilityBlocker(
                    code="qualified_proposal_available",
                    message=(
                        f"Review the {len(proposals)} proposed rule(s) in the Tax & Fee "
                        "Rule Ladder, then publish a version. Each carries its source, "
                        "its jurisdiction and the qualifications you must confirm."
                    ),
                ),
            ),
            repair=governance_owner_reference(
                self.capability_key, object_type="rule-set"
            ),
        )

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        if context.requested_state != "enabled":
            return _disabled(self.capability_key)
        evidence = context.project_evidence
        ladder = evidence.get("ladder")
        if ladder is None or not ladder.rules:
            # An EMPTY published ladder is the blank editor with extra steps, so the
            # two cases answer alike; only the sentence naming what is missing differs.
            missing = (
                "a published Tax & Fee Rule Ladder version"
                if ladder is None
                else "at least one ordered rule in the published Rule Ladder"
            )
            proposals = evidence.get("preset_proposals") or []
            if proposals:
                return self._proposal_pending(missing, proposals)
            return _missing_governance(
                self.capability_key, missing, repair_object_type="rule-set"
            )
        # AC1 lists Currency & FX as a hard dependency: every component is composed
        # in exact money, so a ladder with no confirmed Money Policy has no currency
        # to state its totals in.
        policy = evidence.get("money_policy")
        if policy is None:
            return _missing_governance(
                self.capability_key,
                "a confirmed Money Policy with a reporting currency",
                repair_object_type="rule-set",
            )

        # ONE owner: the Rule Set, at its exact published version, with the version's
        # own content hash. A confirmation rechecks the object an operator edited.
        owners = (
            GovernanceOwner(
                object_type="rule-set",
                object_id=ladder.rule_set_id,
                version_id=ladder.version_id,
                evidence_hash=ladder.content_hash,
            ),
            GovernanceOwner(
                object_type="rule-set",
                object_id=policy.rule_set_id,
                version_id=policy.version_id,
                evidence_hash=policy.content_hash,
            ),
        )

        datastream_id = str(datastream["id"])
        observed = (evidence.get("tax_evidence_by_datastream") or {}).get(datastream_id)

        # AC4: source type comes from an OBSERVED publication, never from a table
        # nothing writes. No evidence is `unavailable` with a repair route -- it was
        # previously read as "no declared type", which matched every scoped rule.
        if observed is None:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason=(
                    "No publication has recorded this Datastream's source type, tax "
                    "posture or the physical inputs the ladder needs."
                ),
                detected_support_selection={
                    "state": "unavailable",
                    "declared_source_type": None,
                    "selected": False,
                },
                governance_owners=owners,
                blockers=(
                    CapabilityBlocker(
                        code="tax_evidence_unobserved",
                        message=(
                            "Run this Datastream so its published rows record their source "
                            "type, tax posture and required inputs."
                        ),
                    ),
                ),
                repair=_datastream_repair(datastream, "runs"),
            )

        matched, refused = _match_ladder(ladder, datastream, observed)
        support = {
            "state": "detected",
            "declared_source_type": observed.source_type,
            "source_type_origin": observed.source_type_origin,
            "source_type_confidence": observed.source_type_confidence,
            "tax_posture": observed.tax_posture,
            "matched_rule_keys": [rule["rule_key"] for rule in matched],
            "refused_rules": refused,
            "cascade_phases": sorted({rule["cascade_phase"] for rule in matched}),
            "rule_set_version_id": ladder.version_id,
            "tax_evidence_version_id": observed.evidence_version_id,
            "selected": bool(matched),
            # Task 3, second bullet: WHAT THE PUBLICATION ACTUALLY LANDED. The
            # Datastream tabs could show that a CPM rule was refused for want of
            # measured impressions, and could not show which inputs did arrive --
            # a verdict without its evidence. `available_inputs` is the compiler's
            # own reading of the same dict and is carried separately because the
            # two differ exactly where it matters: a key present with a falsy value
            # is ABSENT for a rule, and printing `0` as a measurement is how a
            # verification fee becomes a confident zero.
            "observed_inputs": dict(observed.observed_inputs),
            "available_inputs": sorted(observed.available_inputs),
            "geography_evidence": dict(observed.geography_evidence),
            "observed_gaps": [dict(gap) for gap in observed.gaps],
        }

        if not matched:
            # AC8: "No matched rule is `Not applicable` ONLY when sufficient evidence
            # proves that conclusion." A Datastream whose source type is UNKNOWN has
            # not proved anything -- it is unresolved, and saying Not applicable
            # would be a verdict drawn from a gap.
            if observed.source_type == "UNKNOWN" or observed.gaps:
                return CapabilityAssessment(
                    applicability="applicable",
                    coverage_state="unavailable",
                    reason=(
                        "No rule could be matched or refused: this Datastream's source "
                        "type or required inputs are unresolved."
                    ),
                    detected_support_selection=support,
                    governance_owners=owners,
                    blockers=(
                        CapabilityBlocker(
                            code="tax_applicability_unresolved",
                            message=(
                                "Confirm this Datastream's source type and the fields the "
                                "ladder's rules read, or record an explicit exclusion."
                            ),
                        ),
                    ),
                    repair=_datastream_repair(datastream, "mapping"),
                )
            return CapabilityAssessment(
                applicability=NOT_APPLICABLE,
                coverage_state=NOT_APPLICABLE,
                reason=(
                    f"No rule in version {ladder.version_number} is scoped to a "
                    f"{observed.source_type} Datastream."
                ),
                detected_support_selection=support,
            )

        # AC5: a declared-but-undecided geography posture is not a complete answer.
        # The ladder is complete as a document and incomplete as a result, and those
        # are two different states a surface must be able to show apart.
        unresolved = [
            rule
            for rule in matched
            if rule.get("rest_of_world_posture") == "unresolved"
            or rule.get("unknown_posture") == "unresolved"
        ]
        if unresolved:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="partial",
                reason=(
                    f"{len(unresolved)} matched rule(s) leave Rest of World or Unknown "
                    "undecided, so no complete headline total can be stated."
                ),
                detected_support_selection=support,
                governance_owners=owners,
                exceptions=(
                    CapabilityException(
                        kind="exception",
                        severity="degrading",
                        reason_code="geography_posture_unresolved",
                        reason=(
                            "Rows outside the named countries, and rows with no country "
                            "at all, are neither included nor excluded by these rules."
                        ),
                        owner_kind="governance",
                        evidence_version_id=ladder.version_id,
                    ),
                ),
                repair=governance_owner_reference(
                    self.capability_key,
                    object_type="rule-set",
                    object_id=ladder.rule_set_id,
                    version_id=ladder.version_id,
                ),
            )

        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="complete",
            reason=(
                f"{len(matched)} rule(s) from version {ladder.version_number} resolve "
                "against this Datastream's observed evidence."
            ),
            detected_support_selection=support,
            governance_owners=owners,
        )


def _match_ladder(
    ladder: Any, datastream: dict[str, Any], observed: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the ladder into rules that resolve here and rules that are refused.

    Refusals are RETURNED, not dropped. AC8 requires a proposal to show
    "matched/refused rules"; a scope filter that silently discards a rule leaves an
    operator unable to tell "this rule does not apply here" from "this rule was
    never considered", and those need different repairs.
    """

    datastream_id = str(datastream["id"])
    plan_version_id = datastream.get("current_plan_version_id")
    matched: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []

    for rule in ladder.rules:
        scope = rule.get("source_type_scope") or []
        if scope and observed.source_type not in scope:
            refused.append(
                {
                    "rule_key": rule["rule_key"],
                    "code": "source_type_out_of_scope",
                    "reason": (
                        f"scoped to {', '.join(sorted(scope))}; this Datastream is "
                        f"{observed.source_type}"
                    ),
                }
            )
            continue
        if rule["scope_kind"] == "datastream" and rule.get("scope_ref") != datastream_id:
            refused.append(
                {
                    "rule_key": rule["rule_key"],
                    "code": "scoped_to_another_datastream",
                    "reason": "the rule names a different Datastream",
                }
            )
            continue
        if rule["scope_kind"] == "plan_version" and rule.get("scope_ref") != plan_version_id:
            refused.append(
                {
                    "rule_key": rule["rule_key"],
                    "code": "scoped_to_another_plan_version",
                    "reason": "the rule names a different plan version",
                }
            )
            continue
        missing = _missing_inputs(rule, observed)
        if missing:
            refused.append(
                {
                    "rule_key": rule["rule_key"],
                    "code": "required_input_unavailable",
                    "reason": "missing observed input(s): " + ", ".join(missing),
                }
            )
            continue
        matched.append(rule)

    return matched, refused


#: What each calculation form must be able to READ before it can produce a number.
#: Data, not an if/elif chain, for the reason `_CATEGORY_FORMS` is data: a chain is
#: where the next form gets forgotten and falls through as zero.
_FORM_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "CPM": ("measured_impressions",),
    "PER_TRANSACTION": ("transaction_count",),
}


def _missing_inputs(rule: dict[str, Any], observed: Any) -> list[str]:
    """Physical inputs the rule needs and the publication did not show.

    AC4: "Missing required input cannot become zero or false Complete." A CPM
    verification fee over impressions nobody measured is not a fee of zero; it is a
    fee nobody can compute.
    """
    required = list(_FORM_REQUIRED_INPUTS.get(str(rule.get("form")), ()))
    if rule.get("geography_dependent"):
        required.append("country")
    return [name for name in required if name not in observed.available_inputs]


# ---------------------------------------------------------------------------
# Competitors. The organization owns entity identity; Governance owns Project
# roles and commensurability. Identity alignment never authorizes comparison.
# ---------------------------------------------------------------------------


class CompetitorsCompiler:
    """Story 48.5 replaced this adapter's three load-bearing mistakes.

    It decided a Datastream was applicable by comparing ``module_name`` to a
    binding's opaque ``source`` string. Two strings matching is not a capability:
    every Datastream of that connector looked applicable whether or not its
    selected report could return an entity, and any Datastream of a connector
    with no binding was reported ``unavailable`` rather than Not applicable --
    so the denominator was wrong in both directions.

    It marked a Datastream ``complete`` on the existence of a binding. Nothing
    checked that the binding named a governed source identity, that anything had
    been observed, or that the binding was published rather than merely proposed.
    One organization-wide row could make a whole Project Complete.

    And its dependency fingerprint was a hash of the Project's role list. An
    alias change, a retired external identifier, a changed account, a new
    Connector contract or a local exception left the fingerprint identical, so a
    stale proposal stayed confirmable.
    """

    capability_key = "competitors"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """Read the exact governed owners, or record that there are none."""

        from core.entity_bindings import list_bindings, observation_summary  # noqa: PLC0415
        from core.master_data import fetch_org_registry, list_aliases  # noqa: PLC0415
        from core.tracked_entities import (  # noqa: PLC0415
            OBJECT_KIND,
            list_decisions,
            list_source_identities,
            project_roles,
        )

        registry = fetch_org_registry(conn, org_id=org_id, object_kind=OBJECT_KIND)
        roles = project_roles(conn, project_id=project_id)
        node_ids = sorted({str(role["node_id"]) for role in roles})
        aliases = list_aliases(conn, org_id=org_id, node_ids=node_ids) if node_ids else []
        identities = (
            list_source_identities(conn, org_id=org_id, node_ids=node_ids) if node_ids else []
        )
        bindings = list_bindings(conn, project_id=project_id)
        decisions = list_decisions(conn, org_id=org_id, project_id=project_id)

        by_datastream: dict[str, list[dict[str, Any]]] = {}
        for binding in bindings:
            by_datastream.setdefault(str(binding["datastream_id"]), []).append(binding)
        identities_by_node: dict[str, list[dict[str, Any]]] = {}
        for identity in identities:
            identities_by_node.setdefault(str(identity["node_id"]), []).append(identity)

        return {
            "registry_id": registry["id"] if registry else None,
            "project_roles": [
                {
                    "association_id": role["id"],
                    "node_id": role["node_id"],
                    "role": role["project_role"],
                    "pinned_version_id": role["node_version_id"],
                }
                for role in roles
            ],
            "identities_by_node": identities_by_node,
            "bindings_by_datastream": by_datastream,
            "observations": observation_summary(conn, project_id=project_id),
            "unresolved_decisions": [
                {"id": item["id"], "value": item["normalized_value"], "state": item["state"]}
                for item in decisions
                if item["state"] == "proposed"
            ],
            # Every dimension a confirmation must recheck, in ONE fingerprint. A
            # changed alias, external identity, role pin, binding, exception or
            # decision moves it -- which is what the role-list hash never did.
            "evidence_hash": canonical_hash(
                {
                    "roles": [
                        [role["node_id"], role["project_role"], role["node_version_id"]]
                        for role in roles
                    ],
                    "aliases": [
                        [alias["node_id"], alias["relation"], alias["normalized_value"]]
                        for alias in aliases
                    ],
                    "identities": [
                        [
                            identity["node_id"],
                            identity["connector_name"],
                            identity["account_scope"],
                            identity["external_id"],
                            identity["version_number"],
                        ]
                        for identity in identities
                    ],
                    "bindings": [
                        [
                            binding["datastream_id"],
                            binding["node_id"],
                            binding["application_state"],
                            binding["content_hash"],
                            binding["exception_reason_code"],
                        ]
                        for binding in bindings
                    ],
                    "decisions": [
                        [item["raw_value_hash"], item["state"], item["node_id"]]
                        for item in decisions
                    ],
                }
            ),
        }

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        from core.entity_bindings import (  # noqa: PLC0415
            STATE_EXCLUDED,
            STATE_PUBLISHED,
            connector_fingerprint,
            detect_support,
        )

        if context.requested_state != "enabled":
            return _disabled(self.capability_key)
        evidence = context.project_evidence

        # 1. Can this SOURCE do it? Read from the Connector's declaration, never
        #    inferred from its name or from a field spelling.
        support = detect_support(datastream)
        if not support.applicable:
            return CapabilityAssessment(
                applicability=NOT_APPLICABLE,
                coverage_state=NOT_APPLICABLE,
                reason=_SUPPORT_REASONS.get(
                    support.reason_code,
                    "This Datastream declares no tracked-entity contract.",
                ),
                detected_support_selection=support.as_dict(),
            )

        datastream_id = str(datastream["id"])
        bindings = (evidence.get("bindings_by_datastream") or {}).get(datastream_id) or []
        excluded = [item for item in bindings if item["application_state"] == STATE_EXCLUDED]
        detected = support.as_dict() | {
            "fingerprint": connector_fingerprint(datastream, support),
            "observed": (evidence.get("observations") or {}).get(datastream_id, {}),
        }

        # 2. A reasoned local exclusion is its own coverage state. It is not a
        #    gap to repair, and it is not Complete either.
        if excluded and len(excluded) >= len(bindings):
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="excluded",
                reason="Every tracked entity is explicitly excluded from this Datastream.",
                detected_support_selection=detected,
                exceptions=tuple(
                    CapabilityException(
                        kind="exclusion",
                        severity="informational",
                        reason_code=str(item["exception_reason_code"]),
                        reason=str(item["exception_reason"]),
                        owner_kind="data",
                    )
                    for item in excluded
                ),
            )

        # 3. Is there anything to bind? A Project with no role has no intent yet;
        #    that is missing governed evidence, not a broken Datastream.
        roles = evidence.get("project_roles") or []
        if not roles:
            return _missing_governance(
                self.capability_key,
                "at least one tracked entity with a Project role",
                repair_object_type="registry",
            )

        owner = tuple(
            GovernanceOwner(
                object_type="registry",
                object_id=str(evidence.get("registry_id") or "tracked-entities"),
                version_id=str(role["pinned_version_id"] or evidence["evidence_hash"]),
                evidence_hash=str(evidence["evidence_hash"]),
            )
            for role in roles
        )

        # 4. Complete requires, for EVERY intended role: a governed source
        #    identity this Connector can use, and a PUBLISHED binding. A
        #    candidate binding is an approved intent, not a collected fact.
        identities_by_node = evidence.get("identities_by_node") or {}
        published = {
            str(item["node_id"]): item
            for item in bindings
            if item["application_state"] == STATE_PUBLISHED
        }
        excluded_nodes = {str(item["node_id"]) for item in excluded}
        intended = [role for role in roles if str(role["node_id"]) not in excluded_nodes]

        missing_identity = [
            role
            for role in intended
            if not any(
                str(identity["connector_name"]) == support.connector_name
                for identity in identities_by_node.get(str(role["node_id"]), [])
            )
        ]
        unpublished = [
            role
            for role in intended
            if str(role["node_id"]) not in published
            and role not in missing_identity
        ]
        blockers: list[CapabilityBlocker] = []
        if missing_identity and support.direction == "collect":
            blockers.append(
                CapabilityBlocker(
                    code="source_identity_absent",
                    message=(
                        f"{len(missing_identity)} tracked entity/entities have no governed "
                        "identifier for this source. Confirm one, or exclude them here."
                    ),
                )
            )
        pending_decisions = evidence.get("unresolved_decisions") or []
        if pending_decisions:
            blockers.append(
                CapabilityBlocker(
                    code="candidate_decision_unresolved",
                    message=(
                        f"{len(pending_decisions)} observed value(s) are waiting for a "
                        "human decision."
                    ),
                    required=False,
                )
            )

        # An observed population the provider may truncate is recorded as a
        # degrading exception, never folded into the coverage number: a value
        # withheld for privacy is not evidence that nothing was there.
        exceptions: list[CapabilityException] = [
            CapabilityException(
                kind="exception",
                severity="informational",
                reason_code="identity_is_not_commensurability",
                reason=(
                    "Identity alignment resolves who is observed; it does not authorize "
                    "metric comparison across sources."
                ),
                owner_kind="governance",
            )
        ]
        if (support.population or {}).get("completeness") == "reportable_subset":
            exceptions.append(
                CapabilityException(
                    kind="exception",
                    severity="degrading",
                    reason_code="observed_population_is_a_subset",
                    reason=str(
                        (support.population or {}).get("note")
                        or "This report may withhold rows, so observed candidates are "
                        "not the complete set of entities."
                    ),
                    owner_kind="data",
                )
            )
        # `exclusion`, not `exception`: the proposal contract refuses `excluded`
        # coverage that carries no exclusion decision, precisely so an absence of
        # support can never be laundered into a deliberate one.
        exceptions.extend(
            CapabilityException(
                kind="exclusion",
                severity="informational",
                reason_code=str(item["exception_reason_code"]),
                reason=str(item["exception_reason"]),
                owner_kind="data",
            )
            for item in excluded
        )

        if not intended:
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="excluded",
                reason="Every tracked entity is explicitly excluded from this Datastream.",
                detected_support_selection=detected,
                governance_owners=owner,
                exceptions=tuple(exceptions),
            )
        if missing_identity or unpublished:
            covered = len(intended) - len(missing_identity) - len(unpublished)
            state = "partial" if covered else "unavailable"
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state=state,
                reason=(
                    f"{covered} of {len(intended)} tracked entity/entities have a published "
                    "binding on this Datastream."
                ),
                detected_support_selection=detected
                | {"covered": covered, "intended": len(intended)},
                governance_owners=owner,
                blockers=tuple(blockers),
                exceptions=tuple(exceptions),
                repair=_datastream_repair(datastream, "mapping"),
            )
        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="complete",
            reason=(
                f"{len(intended)} tracked entity/entities have a published binding on this "
                "Datastream."
            ),
            detected_support_selection=detected
            | {"covered": len(intended), "intended": len(intended)},
            governance_owners=owner,
            blockers=tuple(blocker for blocker in blockers if not blocker.required),
            exceptions=tuple(exceptions),
        )


#: Why a Datastream is Not applicable, in a sentence naming where to repair it.
#: Three different absences, because they are three different repairs.
_SUPPORT_REASONS = {
    "connector_declares_no_tracked_entity_support": (
        "This Connector declares no tracked-entity contract, so the platform makes no "
        "claim about whether it can observe or collect entities."
    ),
    "selected_report_declares_no_tracked_entity_support": (
        "This Connector can track entities, but not through the report this Datastream "
        "selected. Choose a compatible report, or leave this Datastream out."
    ),
    "datastream_has_no_selected_report": (
        "This Datastream has selected no report yet, so nothing can be detected."
    ),
}


# ---------------------------------------------------------------------------
# Placement Mapping -- the sixth capability (story 61.5).
#
# READ THIS BEFORE CALLING IT UNFINISHED. `not_applicable` on every Datastream is
# the RIGHT answer today, not a stub waiting to be replaced by a real one:
#
# * No table of observed placements exists anywhere in the schema
#   (`\dt *placement*` -> nothing, measured by story 61.1), so there is no
#   evidence this compiler could read and no verdict it could reach by reading.
# * 37 of the 39 connector manifests declare no placement dimension at all. Had
#   this returned `unavailable`, those Datastreams would have stayed INSIDE the
#   applicable denominator -- `aggregate_coverage` takes only `not_applicable`
#   out of it -- and the coverage percentage of every Project would have fallen
#   for a capability that does not apply to them. A number counting Datastreams
#   the capability cannot reach is a number that lies.
# * Answering at all is what makes the capability reachable: `compiler_for`
#   raises `no compiler registered` the moment an activation is requested, so
#   without this class the sixth row would show `Off` on every screen and could
#   never be switched on.
#
# What stories 61.1 to 61.4 add is the evidence -- observed placements, the four
# matching states, the resolution level, the planned-versus-actual figures. When
# a Datastream can carry a placement, the verdict here stops being
# `not_applicable`. Until then, this compiler states an absence with its reason,
# which is exactly what the ratified contract asks of a capability that cannot
# apply somewhere.
# ---------------------------------------------------------------------------

_NO_PLACEMENT_EVIDENCE = (
    "No placement dimension is declared for this Datastream, and no observed "
    "placement is stored yet, so no plan line can be matched here."
)

#: What this capability knows how to count today, and the answer is NOTHING --
#: said with ``None``, never with ``0``. Zero is a measurement over a table that
#: exists and held no row; there is no placement table at all, so nobody counted.
#: The two blocks below are built from this ONE dict so the Project-wide read and
#: the per-Datastream verdict cannot answer the same absence differently.
_UNCOUNTED_PLACEMENTS: dict[str, Any] = {
    "observed_placements": None,
    "mapped_plan_lines": None,
}


class PlacementMappingCompiler:
    capability_key = "placement_mapping"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """Nothing to resolve yet, and the block says so rather than being empty.

        A Project-wide read would have to open a store of observed placements.
        None exists, so this reports the absence instead of pretending to have
        looked -- and a later story replaces the value, not the shape.
        """
        return {**_UNCOUNTED_PLACEMENTS, "reason": _NO_PLACEMENT_EVIDENCE}

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        if context.requested_state != "enabled":
            return _disabled(self.capability_key)
        return CapabilityAssessment(
            applicability=NOT_APPLICABLE,
            coverage_state=NOT_APPLICABLE,
            reason=_NO_PLACEMENT_EVIDENCE,
            detected_support_selection={"state": NOT_APPLICABLE, **_UNCOUNTED_PLACEMENTS},
        )


# ---------------------------------------------------------------------------
# Analytics Alignment. The seventh, story 70.3, and the second of the family that
# ADDS COLUMNS to an aggregation.
#
# WHY THIS COMPILER READS AND THE SIXTH DOES NOT. `PlacementMappingCompiler`
# answers `not_applicable` everywhere because there is no store it could read.
# This capability has three: the MDM common keys of the Project, the published
# Semantic View relationships that pin their versions, and the Currency & FX
# state. All three exist and are governed, so a verdict here is a MEASUREMENT --
# and a `not_applicable` returned without reading them would be a claim nobody
# made.
#
# WHAT `applicable` MEANS HERE, and it is not what it means for Country. Country
# applies to a Datastream on its own. An alignment applies to a PAIR: a
# Datastream nothing can be crossed with cannot carry the capability at all, and
# it leaves the denominator instead of lowering a percentage for a question that
# was never asked of it. A Datastream that IS one side of an approved
# relationship is `applicable`, and every unmet dependency of that pair is a
# named blocker.
# ---------------------------------------------------------------------------


class AnalyticsAlignmentCompiler:
    capability_key = "analytics_alignment"

    def project_evidence(self, conn, *, project_id: str, org_id: str) -> dict[str, Any]:
        """The pairs a published relationship already authorizes, and the FX state.

        Derived at read and never stored, on the rule `mdm_common_keys` states
        about its own coverage: which Datastreams can be crossed changes the next
        time a Semantic View is published, and a stored figure would be wrong
        silently.
        """
        from core.analytics_alignment import (  # noqa: PLC0415
            DEPENDENCY_CURRENCY_FX,
            NO_ALIGNABLE_PAIR,
            alignable_pairs,
        )
        from core.project_capability_states import read_capability_state  # noqa: PLC0415

        pairs = alignable_pairs(conn, project_id=project_id)
        return {
            "alignable_pairs": pairs,
            "currency_fx_state": read_capability_state(
                conn, project_id=project_id, capability_key=DEPENDENCY_CURRENCY_FX
            ),
            # An absence with its reason rather than an empty block, so a
            # Project-wide read never has to invent what `[]` meant.
            "reason": None if pairs else NO_ALIGNABLE_PAIR,
        }

    def assess(
        self, conn, *, context: CompileContext, datastream: dict[str, Any]
    ) -> CapabilityAssessment:
        from core.analytics_alignment import (  # noqa: PLC0415
            ADDED_COLUMNS,
            NO_ALIGNABLE_PAIR,
            no_alignable_pair_dependencies,
            resolve_dependencies,
        )

        if context.requested_state != "enabled":
            # OFF ADDS NOTHING, and this branch is what makes that checkable: the
            # three columns are named in NO payload a disabled capability
            # produces.
            return _disabled(self.capability_key)

        datastream_id = str(datastream["id"])
        pairs = [
            pair
            for pair in context.project_evidence.get("alignable_pairs") or []
            if datastream_id
            in (pair.get("left_datastream_id"), pair.get("right_datastream_id"))
        ]
        if not pairs:
            project_pairs = context.project_evidence.get("alignable_pairs") or []
            if not project_pairs:
                # THE PROJECT HAS NO ALIGNABLE PAIR AT ALL, and the capability is
                # requested `enabled` (the disabled branch returned above). A
                # Datastream-by-Datastream `not_applicable` here would leave every
                # proposal blockerless, `required_blockers` empty, the Change Set
                # `prepared` instead of `blocked`, and the capability activating
                # `ready` -- the three dependencies the card declares BLOCKING
                # blocking nothing in the exact state where none is met. So the
                # activation is REFUSED with the gesture named. This is distinct
                # from the `not_applicable` case just below, which is the ratified
                # coverage denominator: a Datastream in no pair while the Project
                # HAS a pair elsewhere is a question never asked of it, not a
                # refused activation.
                missing = no_alignable_pair_dependencies(
                    context.project_evidence.get("currency_fx_state") or ""
                )
                return CapabilityAssessment(
                    applicability="applicable",
                    coverage_state="unavailable",
                    reason="; ".join(item.reason for item in missing),
                    detected_support_selection={
                        "state": "unavailable",
                        "alignable_pairs": 0,
                        "added_columns": None,
                    },
                    blockers=tuple(
                        CapabilityBlocker(code=item.code, message=item.gesture)
                        for item in missing
                    ),
                    repair=governance_owner_reference(
                        self.capability_key, object_type="rule-set"
                    ),
                )
            return CapabilityAssessment(
                applicability=NOT_APPLICABLE,
                coverage_state=NOT_APPLICABLE,
                reason=NO_ALIGNABLE_PAIR,
                detected_support_selection={
                    "state": NOT_APPLICABLE,
                    "alignable_pairs": 0,
                    # Named at `None` and never at `0`: nothing counted columns
                    # here, and a zero is a measurement over something that ran.
                    "added_columns": None,
                },
            )

        pair = pairs[0]
        other = (
            pair["right_datastream_id"]
            if pair["left_datastream_id"] == datastream_id
            else pair["left_datastream_id"]
        )
        dependencies = resolve_dependencies(
            conn,
            project_id=context.project_id,
            left_datastream_id=datastream_id,
            right_datastream_id=other,
        )
        support = {
            "state": "detected",
            "alignable_pairs": len(pairs),
            "paired_with": other,
            "dependencies": dependencies.as_dict(),
            "added_columns": list(ADDED_COLUMNS),
            "selected": True,
        }
        if dependencies.missing:
            # EVERY unmet dependency is a blocker, not the first one: a refusal
            # that reveals them one at a time makes a person fix, retry, and be
            # refused again.
            return CapabilityAssessment(
                applicability="applicable",
                coverage_state="unavailable",
                reason="; ".join(item.reason for item in dependencies.missing),
                detected_support_selection=support,
                blockers=tuple(
                    CapabilityBlocker(code=item.code, message=item.gesture)
                    for item in dependencies.missing
                ),
                repair=governance_owner_reference(self.capability_key, object_type="rule-set"),
            )
        return CapabilityAssessment(
            applicability="applicable",
            coverage_state="complete",
            reason=(
                "A declared common key covers both Datastreams, a published Semantic View "
                "relationship approves crossing on it, and Currency & FX is ready."
            ),
            # The three columns an aggregation gains travel on
            # `detected_support_selection`, which IS one of `IMPACT_BLOCKS`, and
            # not on an `impact_overrides` key of their own: `serialize_proposal`
            # filters overrides to the declared blocks, so a fabricated block name
            # would be dropped silently and a reviewer would confirm without ever
            # seeing the columns.
            detected_support_selection=support,
        )


for _compiler in (
    CountryCompiler(),
    CurrencyFxCompiler(),
    ReportingTimezoneCompiler(),
    TaxFeesCompiler(),
    CompetitorsCompiler(),
    PlacementMappingCompiler(),
    AnalyticsAlignmentCompiler(),
):
    register_compiler(_compiler)
