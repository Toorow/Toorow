"""The two Rule Set profiles Story 49.4 owns, on the Story 48.3 substrate.

Story 49.4's Technical Contract names ``governance_rule_sets`` /
``governance_rule_set_versions`` / ``rule_set_exceptions``. Story 48.3 built them
-- family-typed, ``family`` opaque, published versions frozen -- because its own
Implementation Gate forbade a currency-specific lifecycle. Story 48.4 has since
mounted Tax & Fees as a fourth family. So this module does not create a control
lifecycle; it registers the two families 49.4 owns on the one that exists:

* ``metric_reconciliation`` -- how several sources emitting the same metric
  combine, or refuse to. The safety logic in
  :mod:`core.metric_reconciliation` is reused verbatim; what changes is where the
  rule COMES FROM. It came from ``app.overlap_groups`` /
  ``app.reconciliation_rules``: mutable rows addressing their sources by
  CONNECTOR LABEL, resolved through a hidden Platform/Organization scope cascade.
  A label is not an identity -- it names zero, one or several Datastreams in a
  Project, and the cascade meant the same query answered differently depending on
  a scope nobody could see. A published version pins exact Concept versions and
  exact Data references, and the cascade is replaced by *materialize a template
  as an editable Project draft*, which is visible.

* ``dq_policy`` -- what a monitor checks and against which frozen baseline.
  ``app.dq_baselines`` held one mutable column set per Datastream and
  ``dq_monitors._write_dq_baseline`` OVERWROTE it after detecting drift, so the
  run after a drift passed: the check agreed with whatever the source had become.
  A baseline lives inside an immutable version here, so advancing it is a
  governed decision with a date and an actor.

Both validators REFUSE rather than normalize away a missing piece. A reconciliation
rule with no ordered source list, a `SUM` with no proof of non-overlap, a DQ policy
with no baseline where its check needs one: each is an exception, because each is a
question the runtime would otherwise answer by guessing.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from core import dq_monitor_registry
from core.governance_rule_sets import (
    RuleSetError,
    RuleSetFormField,
    RuleSetFormOption,
    RuleSetProfile,
    register_profile,
)

logger = logging.getLogger(__name__)

FAMILY_RECONCILIATION = "metric_reconciliation"
FAMILY_DQ = "dq_policy"

PROFILE_RECONCILIATION = "metric_reconciliation_v1"
PROFILE_DQ = "dq_policy_v1"

#: The five methods, taken from :mod:`core.metric_reconciliation` rather than
#: restated, so the policy vocabulary and the runtime vocabulary cannot drift.
METHOD_SUM = "SUM"
METHOD_PRIORITY = "PRIORITY"
METHOD_DEDUP_ID = "DEDUP_ID"
METHOD_ESTIMATE = "ESTIMATE"
METHOD_KEEP_SEPARATE = "KEEP_SEPARATE"
METHODS = (METHOD_SUM, METHOD_PRIORITY, METHOD_DEDUP_ID, METHOD_ESTIMATE, METHOD_KEEP_SEPARATE)

#: What a `SUM` must prove before it is allowed. Each is a way two sources can
#: look summable and not be, and each was previously assumed.
SUM_PROOFS = ("non_overlap", "compatible_additivity", "compatible_currency", "compatible_time")

#: The checks a published DQ policy may name, DERIVED from the one registry since
#: story 59.5. No migration was ever needed for a new one:
#: `dq_monitor_versions.check_profile` only CHECKs the SHAPE of the word
#: (`^[a-z][a-z0-9_]{1,63}$`, migration 145:350), and the vocabulary itself is
#: this tuple -- `_validate_dq_payload` below is what `publish_version` validates
#: through. A profile absent from here is refused at publication, which is where a
#: monitor that nothing can evaluate should be stopped.
#:
#: It is the PUBLISHABLE half of the registry and not the dispatched one: it holds
#: `geography`, which is evaluated per project and never by the per-Datastream
#: sweep, and it does NOT hold `arrival_timeliness`, which is reached through
#: `timeliness` and fires under its type.
DQ_CHECKS = dq_monitor_registry.PUBLISHABLE_KEYS
#: Checks whose verdict is meaningless without a frozen reference to compare to.
#: `null_rate` is deliberately NOT one of them: it carries a threshold, which is a
#: policy, not a frozen observation of what the source used to look like.
DQ_CHECKS_REQUIRING_BASELINE = frozenset({"schema", "volume"})


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleSetError(f"{label} is required")
    return value.strip()


def _exact_reference(entry: Mapping[str, Any], label: str) -> dict[str, Any]:
    """A reference that names an object AND its version, or a refusal.

    The whole point of the family. ``{"connector": "meta-ads"}`` was the previous
    shape: it identifies a provider, not a thing this Project publishes, and it
    carries no version, so the rule's meaning changed whenever the underlying
    object did without anything recording that it had.
    """
    kind = _require(entry.get("kind"), f"{label}.kind")
    object_id = _require(entry.get("object_id"), f"{label}.object_id")
    version_id = entry.get("version_id")
    if not isinstance(version_id, str) or not version_id.strip():
        raise RuleSetError(
            f"{label} names {kind}:{object_id} without a version_id. A reconciliation "
            "rule bound to 'the latest' changes meaning without a new version."
        )
    return {"kind": kind, "object_id": object_id, "version_id": version_id.strip()}


# ---------------------------------------------------------------------------
# metric_reconciliation
# ---------------------------------------------------------------------------


def _validate_reconciliation_rule(rule: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    label = f"ordered_rules[{ordinal}]"
    method = _require(rule.get("method"), f"{label}.method").upper()
    if method not in METHODS:
        raise RuleSetError(f"{label}.method must be one of {list(METHODS)}")

    concept = _exact_reference(rule.get("concept") or {}, f"{label}.concept")
    if concept["kind"] != "semantic_concept":
        raise RuleSetError(
            f"{label}.concept must be a semantic_concept: a reconciliation rule is about a "
            "governed metric, not about a mart column name."
        )

    sources = rule.get("sources")
    if not isinstance(sources, (list, tuple)) or not sources:
        raise RuleSetError(f"{label}.sources must list at least one exact Data reference")
    resolved_sources = [
        _exact_reference(item, f"{label}.sources[{index}]") for index, item in enumerate(sources)
    ]
    keys = [(item["kind"], item["object_id"]) for item in resolved_sources]
    if len(set(keys)) != len(keys):
        raise RuleSetError(f"{label}.sources names the same object twice")

    normalized: dict[str, Any] = {
        "method": method,
        "concept": concept,
        # Order is CONTENT for PRIORITY and stable evidence for the rest, so it is
        # preserved exactly as given and never sorted.
        "sources": resolved_sources,
        "note": str(rule.get("note") or "").strip() or None,
    }

    if method == METHOD_SUM:
        proofs = rule.get("proofs") or {}
        if not isinstance(proofs, Mapping):
            raise RuleSetError(f"{label}.proofs must be an object")
        missing = [name for name in SUM_PROOFS if not proofs.get(name)]
        if missing:
            raise RuleSetError(
                f"{label} is a SUM without proof of {', '.join(missing)}. Two sources that "
                "overlap, aggregate differently, hold different currencies or draw different "
                "days look summable and are not."
            )
        normalized["proofs"] = {name: str(proofs[name]) for name in SUM_PROOFS}

    elif method == METHOD_PRIORITY:
        if len(resolved_sources) < 2:
            raise RuleSetError(f"{label} is a PRIORITY over fewer than two sources")
        fallback = str(rule.get("fallback") or "refuse")
        if fallback not in {"refuse", "next_source"}:
            raise RuleSetError(f"{label}.fallback must be 'refuse' or 'next_source'")
        normalized["fallback"] = fallback
        verification = rule.get("verification_source")
        normalized["verification_source"] = (
            _exact_reference(verification, f"{label}.verification_source")
            if isinstance(verification, Mapping) and verification
            else None
        )

    elif method == METHOD_DEDUP_ID:
        join = rule.get("join")
        if not isinstance(join, Mapping) or not join:
            raise RuleSetError(
                f"{label} is a DEDUP_ID with no join binding. Deduplicating on an unnamed key "
                "is a guess about which rows are the same row."
            )
        normalized["join"] = _exact_reference(join, f"{label}.join")

    elif method == METHOD_ESTIMATE:
        formula = _require(rule.get("formula"), f"{label}.formula")
        assumption = _require(rule.get("assumption"), f"{label}.assumption")
        # An estimate that does not travel labelled as one becomes a measurement
        # the moment it is copied into a report.
        normalized.update({"formula": formula, "assumption": assumption, "labelled": "estimate"})

    return normalized


def _validate_reconciliation_rules(
    rules: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not rules:
        raise RuleSetError("a reconciliation Rule Set version needs at least one rule")
    normalized = [_validate_reconciliation_rule(rule, index) for index, rule in enumerate(rules)]
    # Equal precedence over the same Concept fails closed (AC3). Two rules that
    # both claim a metric and neither wins is a tie the runtime would break by
    # write order, which is the one thing the profile contract forbids.
    seen: dict[str, int] = {}
    for index, rule in enumerate(normalized):
        concept_id = rule["concept"]["object_id"]
        if concept_id in seen:
            raise RuleSetError(
                f"ordered_rules[{seen[concept_id]}] and ordered_rules[{index}] both govern "
                f"Concept {concept_id}. Equal precedence has no deterministic winner; split "
                "the scope or order them explicitly in one rule."
            )
        seen[concept_id] = index
    return normalized


def _validate_reconciliation_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    default = str(payload.get("default_behavior") or "keep_separate")
    if default not in {"keep_separate", "refuse"}:
        raise RuleSetError("default_behavior must be 'keep_separate' or 'refuse'")
    return {
        # What happens to a metric NO rule governs. Neither option invents a
        # total: `keep_separate` shows the per-source series, `refuse` says the
        # combination is unavailable.
        "default_behavior": default,
        "description": str(payload.get("description") or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# dq_policy
# ---------------------------------------------------------------------------


def _validate_dq_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    check = _require(payload.get("check"), "check")
    if check not in DQ_CHECKS:
        raise RuleSetError(f"check must be one of {list(DQ_CHECKS)}")

    severity = str(payload.get("severity") or "degrading")
    if severity not in {"blocking", "degrading", "informational"}:
        raise RuleSetError("severity must be blocking, degrading or informational")

    window = payload.get("window_days", 1)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise RuleSetError("window_days must be a positive integer")

    baseline = payload.get("baseline")
    if check in DQ_CHECKS_REQUIRING_BASELINE:
        if not isinstance(baseline, Mapping) or not baseline:
            raise RuleSetError(
                f"the {check} check needs a frozen baseline: a drift check with nothing to "
                "compare against passes by agreeing with whatever the source has become."
            )
        baseline = dict(baseline)
    else:
        baseline = dict(baseline) if isinstance(baseline, Mapping) else {}

    # Zero eligible members is `not_applicable`, never a pass -- the schema
    # enforces it too (ck_dq_evaluations_empty_is_not_a_pass). Stating it here as
    # well means the policy an operator reads says the same thing the database does.
    return {
        "check": check,
        "severity": severity,
        "window_days": window,
        "baseline": baseline,
        "empty_is": "not_applicable",
        "thresholds": dict(payload.get("thresholds") or {}),
        "description": str(payload.get("description") or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# What a version of each family decides (`governance.md`, "A Rule Set version is
# drafted, then published", 2026-08-24). Declared beside the validators above so
# the question a person is asked and the refusal they may get come from one file.
# ---------------------------------------------------------------------------

_RECONCILIATION_FORM = (
    RuleSetFormField(
        key="default_behavior",
        question="What happens to a metric no rule in this set governs?",
        kind="choice",
        options=(
            RuleSetFormOption(
                "keep_separate",
                "Show each source's own series",
                "Nothing is combined, so nothing is invented; the reader sees several numbers "
                "and decides.",
            ),
            RuleSetFormOption(
                "refuse",
                "State that the combination is unavailable",
                "No number is shown for the combination at all, which is louder and harder to "
                "misread than several series.",
            ),
        ),
        why="Neither answer invents a total. That is the point of the choice.",
    ),
    RuleSetFormField(
        key="description",
        question="What should a later reader know about this version?",
        required=False,
        why="Free text, kept with the immutable version.",
    ),
)

#: Built from the monitor registry, never retyped: `DQ_CHECKS` IS
#: `dq_monitor_registry.PUBLISHABLE_KEYS`, and a hand-written option list here
#: would be the seventh copy of that vocabulary and the first to fall behind.
_DQ_CHECK_OPTIONS = tuple(
    RuleSetFormOption(monitor.key, monitor.label)
    for monitor in dq_monitor_registry.DQ_MONITORS
    if monitor.publishable
)

_DQ_FORM = (
    RuleSetFormField(
        key="check",
        question="What does this policy check?",
        kind="choice",
        options=_DQ_CHECK_OPTIONS,
        why=(
            "Two of them — schema and volume — compare against a frozen baseline, and a "
            "version that names one without a baseline is refused: a drift check with nothing "
            "to compare against passes by agreeing with whatever the source has become."
        ),
    ),
    RuleSetFormField(
        key="severity",
        question="What does a failure of this check mean for the figures?",
        kind="choice",
        options=(
            RuleSetFormOption(
                "blocking", "The figures cannot be trusted", "Reading is stopped, not annotated."
            ),
            RuleSetFormOption(
                "degrading", "The figures are readable but weaker", "Read with the issue shown."
            ),
            RuleSetFormOption(
                "informational", "Worth knowing, nothing is at risk", "Recorded, nothing held."
            ),
        ),
    ),
    RuleSetFormField(
        key="window_days",
        question="Over how many days is one evaluation taken?",
        kind="integer",
        unit="days",
        why=(
            "A window with no eligible member is `not_applicable`, never a pass — here and in "
            "the database, which refuses an empty evaluation recorded as one."
        ),
    ),
    RuleSetFormField(
        key="description",
        question="What should a later reader know about this version?",
        required=False,
    ),
)


register_profile(
    RuleSetProfile(
        key=PROFILE_RECONCILIATION,
        family=FAMILY_RECONCILIATION,
        label="Metric Reconciliation",
        validate=_validate_reconciliation_payload,
        validate_rules=_validate_reconciliation_rules,
        form=_RECONCILIATION_FORM,
    )
)

register_profile(
    RuleSetProfile(
        key=PROFILE_DQ,
        family=FAMILY_DQ,
        label="Data Quality Policy",
        validate=_validate_dq_payload,
        form=_DQ_FORM,
    )
)


# ---------------------------------------------------------------------------
# Runtime resolution. The one path from a published version to a decision.
# ---------------------------------------------------------------------------


def resolve_reconciliation_rule(
    conn, *, project_id: str, concept_id: str
) -> dict[str, Any] | None:
    """The published rule governing one Concept, or ``None``.

    ``None`` means no rule governs it, which the caller renders through the
    version's ``default_behavior`` -- never as a silent SUM. Compare with the
    predecessor, ``metric_semantics``' scope cascade: it searched Project, then
    Organization, then Platform, so "no rule here" quietly became "a rule
    somewhere else", and which one applied was invisible.
    """

    from core.governance_rule_sets import active_version  # noqa: PLC0415

    found = active_version(
        conn,
        project_id=project_id,
        family=FAMILY_RECONCILIATION,
        name="project_reconciliation",
    )
    if found is None:
        return None
    _head, version = found
    for rule in version["ordered_rules"]:
        if rule.get("concept", {}).get("object_id") == concept_id:
            return {
                **rule,
                "rule_set_version_id": version["id"],
                "content_hash": version["content_hash"],
            }
    return None


def governed_runtime_rule(project_id: str, metric: str) -> dict[str, Any] | None:
    """The published rule for a metric, in the shape the runtime already consumes.

    This is what ``metric_reconciliation._resolve_rule`` now calls. The cutover was
    attempted, reverted, and then made on evidence rather than on caution, and the
    evidence is worth keeping: the platform cascade was answering for projects
    that DO NOT EXIST -- ``resolve_route("no_such_project", "conversions")``
    returned ``ROUTED_TO_MART cross_source_conversions``. The one real Project on
    this deployment has a single active Datastream and no published mapping, so it
    has nothing to reconcile and loses nothing. A Project that does have several
    sources reaches a rule through
    :func:`materialize_reconciliation_template` and a governed publication.

    It resolves in the honest direction, and that direction is the whole point of
    the change:

        policy names an exact Datastream  ->  runtime derives its connector

    The predecessor did the reverse. ``overlap_group_members.connector`` WAS the
    policy, so the rule could not say which of two Datastreams from the same
    provider it meant, and it silently followed the provider if the Project later
    added a second one. A connector label is a fact ABOUT a Datastream, so
    deriving it from the identity is safe; treating it as the identity is not.

    Returns ``None`` when no published rule governs the metric, which the caller
    renders through ``default_behavior`` -- never as a silent SUM.

    Fail-soft on an unreadable owner, matching the resolver it replaces: the
    reconciliation gate already refuses to combine sources without a rule, so a
    read failure degrades to "no rule" (keep separate), never to "sum them".
    """

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            concept_id = _concept_id_for_metric(conn, project_id=project_id, metric=metric)
            if concept_id is None:
                return None
            rule = resolve_reconciliation_rule(
                conn, project_id=project_id, concept_id=concept_id
            )
            if rule is None:
                return None
            return _as_runtime_rule(conn, rule)
    except Exception as exc:  # noqa: BLE001 -- an unreadable policy is "no rule"
        logger.warning(
            "controls_quality: governed reconciliation read failed project=%s metric=%s: %s",
            project_id,
            metric,
            exc,
        )
        return None


def _concept_id_for_metric(conn, *, project_id: str, metric: str) -> str | None:
    """The published Concept a canonical metric name resolves to, in Project scope."""

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM app.semantic_concepts
                WHERE name = %s AND lifecycle_status = 'published'
                  AND (project_id = %s OR project_id IS NULL)
                ORDER BY project_id NULLS LAST
                LIMIT 1""",
            (metric, project_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _as_runtime_rule(conn, rule: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a published rule to the dict the reconciliation engine reads.

    ``priority_order`` is the connector sequence the pre-computed marts ORDER BY.
    It is DERIVED here from the ordered exact sources, so the mart still gets the
    labels it is keyed on while the policy keeps naming identities.

    ``truth_connector`` is derived the same way, from the rule's
    ``verification_source`` (AI-295). The predecessor STORED it as a connector
    label on `reconciliation_rules`, which is the defect this whole family
    exists to remove: a label names zero, one or several Datastreams. Here the
    policy names an identity and the label is read back off it, so a Project
    with two Datastreams from the same provider cannot be answered by the wrong
    one. ``None`` when the method carries no verification source -- only
    PRIORITY does.
    """

    order: list[str] = []
    for source in rule.get("sources") or []:
        connector = _connector_for_source(conn, source)
        if connector and connector not in order:
            order.append(connector)
    join = rule.get("join") or {}
    verification = rule.get("verification_source")
    return {
        "method": rule["method"],
        "priority_order": order,
        "join_key": join.get("object_id"),
        "truth_connector": (
            _connector_for_source(conn, verification)
            if isinstance(verification, Mapping) and verification
            else None
        ),
        "target_mart": None,
        # The published version IS the scope. There is no cascade left to rank,
        # which is why this is a constant rather than a level read from a row.
        "scope_level": "PROJECT",
        # Kept so the engine's provenance fields stay populated; it now names the
        # governed version rather than a mutable group row.
        "overlap_group_id": rule.get("rule_set_version_id"),
        "rule_set_version_id": rule.get("rule_set_version_id"),
        "governed_sources": list(rule.get("sources") or []),
    }


def _connector_for_source(conn, source: Mapping[str, Any]) -> str | None:
    if source.get("kind") != "datastream":
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT module_name FROM app.datastreams WHERE id = %s",
            (source.get("object_id"),),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


# ---------------------------------------------------------------------------
# Template materialization. AC3's other half, and the cutover's precondition.
# ---------------------------------------------------------------------------

RECONCILIATION_RULE_SET_NAME = "project_reconciliation"

#: The platform template. A dbt seed file name, not a provider name (the same
#: status `metric_source_priority` already has in `metric_reconciliation`).
_PRIORITY_SEED = "metric_source_priority.csv"


def _seed_priorities(seeds_dir: Any = None) -> dict[str, list[str]]:
    """``{metric -> ordered connectors}`` from the platform seed. Pure, offline."""

    import csv  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    base = (
        Path(seeds_dir)
        if seeds_dir is not None
        else Path(__file__).resolve().parents[2] / "dbt" / "seeds"
    )
    ordered: dict[str, list[tuple[int, str]]] = {}
    with (base / _PRIORITY_SEED).open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            metric = (row.get("metric") or "").strip()
            connector = (row.get("connector") or "").strip()
            if not metric or not connector:
                continue
            try:
                rank = int((row.get("priority") or "0").strip())
            except ValueError:
                continue
            ordered.setdefault(metric, []).append((rank, connector))
    return {
        metric: [connector for _rank, connector in sorted(entries)]
        for metric, entries in ordered.items()
    }


def materialize_reconciliation_template(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    seeds_dir: Any = None,
) -> dict[str, Any]:
    """Turn the platform priority template into an editable Project DRAFT.

    AC3: "Platform or Organization defaults are versioned templates materialized
    as editable Project drafts. They are not a hidden runtime cascade." This is
    that sentence, and it is the precondition for retiring the cascade: a Project
    has to be able to publish the equivalent rule BEFORE the cascade is taken
    away, or the cutover is a switch-off.

    The resolution each template row needs is CONNECTOR LABEL -> exact Datastream,
    and it is the one the Implementation Gate says to refuse rather than guess:

    * zero Datastreams for a connector -> the row is dropped and REPORTED in
      ``unresolved``. A rule naming a source this Project does not have would
      resolve to nothing at runtime and look like a rule.
    * several Datastreams for one connector -> the row is REFUSED and reported in
      ``ambiguous``. Picking one is picking a meaning, and the template has no
      information with which to pick.
    * no published Concept for the metric -> reported in ``unresolved``.

    Nothing is published. The draft is returned for an operator to edit and a
    governed confirmation to publish, which is exactly the difference between a
    template and a cascade.
    """

    from core.governance_rule_sets import draft_version, ensure_rule_set  # noqa: PLC0415

    priorities = _seed_priorities(seeds_dir)
    by_connector = _datastreams_by_connector(conn, project_id=project_id)

    rules: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for metric in sorted(priorities):
        concept = _published_concept(conn, project_id=project_id, metric=metric)
        if concept is None:
            unresolved.append(
                {
                    "metric": metric,
                    "reason": "no_published_concept",
                    "detail": (
                        f"No published Semantic Concept is named '{metric}' in this Project "
                        "or at platform scope."
                    ),
                }
            )
            continue

        sources: list[dict[str, Any]] = []
        blocked = False
        for connector in priorities[metric]:
            candidates = by_connector.get(connector) or []
            if not candidates:
                unresolved.append(
                    {
                        "metric": metric,
                        "connector": connector,
                        "reason": "no_datastream",
                        "detail": f"This Project publishes nothing through '{connector}'.",
                    }
                )
                continue
            if len(candidates) > 1:
                ambiguous.append(
                    {
                        "metric": metric,
                        "connector": connector,
                        "reason": "several_datastreams",
                        "datastream_ids": [item["id"] for item in candidates],
                        "detail": (
                            f"'{connector}' feeds {len(candidates)} Datastreams here. The "
                            "template says which provider comes first, not which of its "
                            "Datastreams -- choosing one would be choosing a meaning."
                        ),
                    }
                )
                blocked = True
                continue
            only = candidates[0]
            if not only["mapping_version_id"]:
                unresolved.append(
                    {
                        "metric": metric,
                        "connector": connector,
                        "reason": "no_published_mapping",
                        "detail": "This Datastream has no published mapping version to pin.",
                    }
                )
                continue
            sources.append(
                {
                    "kind": "datastream",
                    "object_id": only["id"],
                    "version_id": only["mapping_version_id"],
                }
            )

        # A PRIORITY over one source is not a priority; it is a single source and
        # needs no rule at all.
        if blocked or len(sources) < 2:
            if not blocked and sources:
                unresolved.append(
                    {
                        "metric": metric,
                        "reason": "single_source",
                        "detail": "Only one source resolves, so no reconciliation is needed.",
                    }
                )
            continue

        rules.append(
            {
                "method": METHOD_PRIORITY,
                "concept": concept,
                "sources": sources,
                "fallback": "next_source",
                "note": f"Materialized from the platform {_PRIORITY_SEED} template.",
            }
        )

    head = ensure_rule_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        family=FAMILY_RECONCILIATION,
        name=RECONCILIATION_RULE_SET_NAME,
        label="Metric reconciliation",
        actor=actor,
    )
    draft = None
    if rules:
        draft = draft_version(
            conn,
            project_id=project_id,
            rule_set_id=head["id"],
            profile=PROFILE_RECONCILIATION,
            label="Materialized from the platform template",
            payload={
                "default_behavior": "keep_separate",
                "description": (
                    "Draft materialized from the platform priority template. Nothing here "
                    "is in force until it is published."
                ),
            },
            ordered_rules=rules,
            actor=actor,
        )
    return {
        "rule_set_id": head["id"],
        "draft_version_id": draft["id"] if draft else None,
        "rule_count": len(rules),
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        # Stated rather than implied: a draft is not a policy.
        "published": False,
    }


def _datastreams_by_connector(conn, *, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Active Datastreams grouped by connector, with their published mapping version."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.module_name, d.current_mapping_version_id
            FROM app.datastreams d
            WHERE d.project_id = %s AND d.archived_at IS NULL
            ORDER BY d.module_name, d.id
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for datastream_id, module_name, mapping_version_id in rows:
        grouped.setdefault(str(module_name), []).append(
            {"id": str(datastream_id), "mapping_version_id": mapping_version_id}
        )
    return grouped


def _published_concept(conn, *, project_id: str, metric: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.current_version_id FROM app.semantic_concepts c
                WHERE c.name = %s AND c.lifecycle_status = 'published'
                  AND c.current_version_id IS NOT NULL
                  AND (c.project_id = %s OR c.project_id IS NULL)
                ORDER BY c.project_id NULLS LAST
                LIMIT 1""",
            (metric, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "kind": "semantic_concept",
        "object_id": str(row[0]),
        "version_id": str(row[1]),
    }


def reconciliation_default(conn, *, project_id: str) -> str:
    """What happens to a metric no rule governs. ``keep_separate`` when unset.

    Not a hidden default: the value comes from a published version when one
    exists, and the fallback is the SAFE option rather than the convenient one --
    showing the per-source series never invents a total.
    """

    from core.governance_rule_sets import active_version  # noqa: PLC0415

    found = active_version(
        conn,
        project_id=project_id,
        family=FAMILY_RECONCILIATION,
        name="project_reconciliation",
    )
    if found is None:
        return "keep_separate"
    return str(found[1]["payload"].get("default_behavior") or "keep_separate")
