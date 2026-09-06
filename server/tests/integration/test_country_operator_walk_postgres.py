"""The Country operator path, walked as ONE sequence through the mounted routes.

Story 37.6, Task 5. The last open item of Epic 37 is "the real operator path
(Project Settings activation -> Governance editing -> published version), walked on
the deployment". This file walks it everywhere except the deployment: through
``build_asgi_app()``, against a live PostgreSQL, in the order a person clicks.

WHY IT DOES NOT DUPLICATE WHAT EXISTS. Every piece of this path already has a proof,
and none of them is this one:

* ``test_country_activation_fanout_postgres.py`` calls ``project_settings`` and
  ``country_activation`` DIRECTLY. It proves the transaction; it never sends a
  request, so it cannot prove a route is mounted, that its authorization gate is the
  right one, or that the payload one step returns is the payload the next step
  accepts.
* ``test_country_workspace_api_seam.py`` sends requests but PATCHES
  ``run_country_workspace_command``. It proves the route reaches the audited
  command; it never publishes anything.
* Nobody joins them. A person enabling Country crosses two surfaces
  (Governance > Master Data, then Project Settings) and four authorization levels
  (view, edit, manage, and a single-use confirmation). Between those steps is where
  a contract breaks without any single-step test noticing.

WHAT IS STILL NOT PROVEN HERE, and it is written down rather than implied: the
DEPLOYED console. This walk runs against the app object in-process. The deployment
runs ``TOOROW_AUTH_MODE=oauth`` and needs an interactive Google sign-in, and the
choice of which real Project becomes the first to govern Country is an operator
decision. Story 37.6 keeps that item open.

THE FIXTURE FABRICATES NOTHING IT THEN ASSERTS. It seeds what a platform seeds (the
ISO vocabulary, the presets) and what an org has (an organization, a project, a
person with a grant, one active Datastream). Every Country object the assertions read
is minted by the walk itself, through the routes.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date

import pytest
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

TOKEN = "country-operator-walk-local-token"
SUBJECT = "operator@example.com"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _intent() -> dict:
    """A managed feed whose declared grain already carries `country`.

    That is the `preserved_full_grain` case of `compile_geographic_intent`: there is no
    provider report to widen, so the column is in the grain or it is not.
    """
    return {
        "contract_version": "1",
        "source": {
            "kind": "managed_feed",
            "writer_kind": "toorow",
            "managed_feed": {
                "format": "csv",
                "channel": "file_upload",
                "source_ref": _id("dsa_"),
                "template_ref": _id("fst_"),
            },
            "selection": {
                "selection_mode": "subset",
                "metrics": ["cost"],
                "dimensions": ["date", "campaign_id", "country"],
                "grain": ["date", "campaign_id", "country"],
                "filters": [],
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {"start": "2026-01-01T00:00:00Z", "end_exclusive": "2026-07-20T00:00:00Z"},
        "schedule": {
            "mode": "daily",
            "interval_minutes": 1440,
            "timezone": "Europe/Paris",
            "watermark": {"kind": "date_window", "delay_minutes": 120},
            "late_arrival": {"lookback_minutes": 4320},
            "retry": {
                "max_attempts": 5,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 7},
        },
    }


def _profile(sample: str) -> dict:
    return {
        "nullable": False,
        "unique": False,
        # LOAD-BEARING, and it is what the projection estimator reads. Without a
        # signal every grain column is `unknown` (1 000 assumed distinct) and the
        # candidate is refused for cardinality -- correctly.
        "cardinality_signal": "low",
        "sample_values": [sample],
        "confidence": 0.9,
    }


def _suggestion(role: str, aggregation: str) -> dict:
    return {
        "semantic_role": role,
        "aggregation": aggregation,
        "non_additive": False,
        "currency": "EUR",
        "sensitivity": "none",
        # `const: "suggested"` in the schema: the operator's confirmation lives on
        # the BINDING, never on the suggestion.
        "status": "suggested",
        "evidence": ["observed_schema"],
    }


def _binding(canonical_target: str) -> dict:
    return {
        "canonical_target": canonical_target,
        # `mdm_target` must be an `mdm_` id or null; a canonical NAME here is refused
        # by the schema, which is the mapping layer keeping the two apart.
        "mdm_target": None,
        "status": "confirmed",
        "blocking_reason": None,
        "confirmed_by": "pytest",
        "confirmed_reason": "Confirmed in Datastream final review",
    }


def _field(
    field_id: str,
    physical_type: str,
    role: str,
    aggregation: str,
    target: str,
    sample: str,
):
    return {
        "field_id": field_id,
        "physical_type": physical_type,
        "profile": _profile(sample),
        "suggestion": _suggestion(role, aggregation),
        "binding": _binding(target),
    }


def _mapping_payload(plan_version_id: str) -> dict:
    return {
        "mapping_contract_version": "1",
        "source_schema_hash": "0" * 64,
        "plan_version_id": plan_version_id,
        # SOURCE field ids, not canonical names -- see the module docstring.
        "grain": ["Campaign", "Country", "Date"],
        "ambiguities": [],
        "fields": [
            _field("Date", "date", "dimension", "none", "date", "2026-01-01"),
            _field("Campaign", "string", "dimension", "none", "campaign_id", "cmp-1"),
            _field("Country", "string", "dimension", "none", "country", "FR"),
            _field("Cost", "number", "measure", "sum", "cost", "1000.00"),
        ],
    }

# ---------------------------------------------------------------------------
# What a platform and an organization already have before anyone clicks.
# ---------------------------------------------------------------------------


@pytest.fixture()
def console(live_postgres, monkeypatch):
    """One signed-in operator who holds `manage` on one project of one org.

    Static auth mode with an email-shaped subject: `authenticate_canonical_principal`
    resolves it to a canonical `person_<ULID>` (creating it), which is the identity
    `app.org_members` carries and `resolve_strict_resource_access` compares against.
    Canonical identity is switched ON because the confirmation steps of this walk go
    through `_check_canonical_principal` whatever the flag says -- running the rest of
    the walk on a different identity would prove two people, not one.
    """

    from core import api_auth
    from core.canonical_identity import resolve_canonical_identity

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", TOKEN)
    monkeypatch.setenv("TOOROW_STATIC_SUBJECT", SUBJECT)
    monkeypatch.setenv("PLATFORM_DB_URL", live_postgres.info.dsn)
    # Required by `analyze_feedback` as soon as authentication is on; unrelated to
    # this walk, but `build_asgi_app()` refuses to start without it.
    monkeypatch.setenv(
        "TOOROW_FEEDBACK_CONTEXT_SECRET", "country-walk-local-secret-0123456789abcdef"
    )
    api_auth.reset_verifier_cache()

    conn = live_postgres
    principal = resolve_canonical_identity(
        conn, issuer="static://toorow", subject=SUBJECT, verified_email=SUBJECT
    )
    org_id, project_id = _id("org_walk_"), _id("proj_walk_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.organizations (id, name, slug, status, created_by)
            VALUES (%s, 'Country walk', %s, 'active', 'pytest')
            ON CONFLICT (id) DO NOTHING
            """,
            (org_id, _id("cw-")),
        )
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, 'Country walk', %s, 'active', 'pytest', %s)
            """,
            (project_id, _id("cw-"), org_id),
        )
        cur.execute(
            """
            INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at)
            VALUES (%s, %s, %s, 'owner', 'active', NOW())
            ON CONFLICT DO NOTHING
            """,
            (_id("omem_"), org_id, principal.person_id),
        )

    _seed_platform_country_catalogue(conn)
    _confirm_the_project_foundations(conn, org_id, project_id)
    stream = _active_datastream(conn, project_id, org_id)
    conn.commit()

    from core.main import build_asgi_app

    with TestClient(build_asgi_app()) as client:
        yield _Console(client, conn, org_id, project_id, principal.person_id, stream)


class _Console:
    """The console's side of the walk: one client, one project, one bearer."""

    def __init__(self, client, conn, org_id, project_id, person_id, stream):
        self.client = client
        self.conn = conn
        self.org_id = org_id
        self.project_id = project_id
        self.person_id = person_id
        self.stream = stream

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def get(self, path: str):
        return self.client.get(f"/api/projects/{self.project_id}{path}", headers=self._headers())

    def post(self, path: str, body: dict, *, key: str):
        return self.client.post(
            f"/api/projects/{self.project_id}{path}",
            headers=self._headers(key),
            json=body,
        )

    def country(self, action: str, payload: dict, *, key: str):
        return self.post(
            "/governance/master-data/country",
            {"action": action, "payload": payload},
            key=key,
        )


def _seed_platform_country_catalogue(conn) -> None:
    """The ISO vocabulary and the qualified presets: platform seeding, not the walk."""

    from core import country_registry as registry_service

    registry_service.import_country_vocabulary(
        conn, actor="pytest", source_version="ISO-3166-1:2026-01", effective_date=date(2026, 1, 1)
    )
    registry_service.seed_country_presets(conn)


def _confirm_the_project_foundations(conn, org_id: str, project_id: str) -> None:
    """A Reporting Timezone Policy and a Money Policy, both PUBLISHED.

    NOT a convenience. `_compile_all_proposals` recompiles the two always-present
    capabilities on every Change Set -- "foundations, not modules a Change Set may
    leave uncounted" -- so a Project whose timezone policy is unconfirmed cannot
    enable ANY capability: prepare comes back `missing_governance_evidence` for
    `reporting_timezone`, and confirm refuses with `change_set_blocked`.

    MEASURED IN PRODUCTION, 2026-08-23: all 32 Projects carry `currency_fx` and
    `reporting_timezone` at `draft`. Not one has a confirmed policy, so the very
    first click of the Country operator walk is refused today on every Project of
    the estate. That is the product being correct and the estate being unfinished;
    it is recorded in Story 37.6 because it is a precondition of the walk that
    nobody had written down. `test_a_project_without_its_foundations_cannot_enable_country`
    below is the same fact as an assertion.
    """

    from core import timezone_vocabulary as tzv
    from core.governance_rule_sets import draft_version, ensure_rule_set, publish_version
    from core.money_policy import (
        FAMILY_MONEY,
        FAMILY_TIMEZONE,
        POLICY_NAME,
        PROFILE_MONEY,
        PROFILE_TIMEZONE,
    )

    # Each profile pins the exact vocabulary snapshot its choice was made from -- an
    # unpinned currency or zone silently changes meaning when the snapshot moves.
    for family, profile, label, payload, requires in (
        (
            FAMILY_TIMEZONE,
            PROFILE_TIMEZONE,
            "Reporting Timezone Policy",
            {"reporting_timezone": "Europe/Paris"},
            [
                {
                    "kind": "timezone_vocabulary_version",
                    "object_id": "tzdb",
                    "version_id": tzv.tzdb_version(),
                }
            ],
        ),
        (
            FAMILY_MONEY,
            PROFILE_MONEY,
            "Money Policy",
            {
                "reporting_currency": "EUR",
                "rounding": "half_even",
                "max_staleness_days": 7,
                "rate_source_priority": ["ecb"],
                "allow_triangulation": False,
            },
            [
                {
                    "kind": "currency_vocabulary_version",
                    "object_id": "iso-4217",
                    "version_id": "ISO-4217:2026-01",
                }
            ],
        ),
    ):
        head = ensure_rule_set(
            conn,
            org_id=org_id,
            project_id=project_id,
            family=family,
            name=POLICY_NAME,
            label=label,
            actor="pytest",
        )
        draft = draft_version(
            conn,
            project_id=project_id,
            rule_set_id=str(head["id"]),
            profile=profile,
            label=label,
            payload=payload,
            actor="pytest",
            requires=requires,
        )
        publish_version(
            conn,
            project_id=project_id,
            rule_set_id=str(head["id"]),
            version_id=str(draft["id"]),
            actor="pytest",
        )


def _active_datastream(conn, project_id: str, org_id: str) -> dict[str, str]:
    """One enabled, active Datastream whose plan and mapping compile at country grain."""

    from core.datastream_field_mapping import save_field_mapping
    from core.datastream_intents import save_datastream_intent
    from core.datastream_projection import compile_projection

    datastream_id = _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, org_id, name, module_name, source_kind, enabled,
                 schedule_mode, refetch_days, date_window_days, config, created_by,
                 lifecycle_state, data_role)
            VALUES (%s, %s, %s, %s, NULL, 'managed_feed', TRUE, 'nightly',
                    3, 30, %s::jsonb, 'pytest', 'active', 'Spend')
            """,
            (datastream_id, project_id, org_id, f"Walk feed {datastream_id[-6:]}", json.dumps({})),
        )

    operation = _id("op_")
    plan = save_datastream_intent(
        datastream_id=datastream_id,
        project_id=project_id,
        intent=_intent(),
        identity="pytest",
        idempotency_key=f"{operation}:plan",
        conn=conn,
        commit=False,
        advance_pointer=False,
    )
    assert plan.get("executable"), plan
    mapping = save_field_mapping(
        datastream_id=datastream_id,
        project_id=project_id,
        mapping_payload=_mapping_payload(str(plan["id"])),
        identity="pytest",
        idempotency_key=f"{operation}:mapping",
        conn=conn,
        pinned_plan_version_id=str(plan["id"]),
        advance_pointer=False,
        commit=False,
    )
    assert mapping.get("executable"), mapping
    assert compile_projection(mapping).get("executable")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastreams
               SET current_plan_version_id = %s, current_mapping_version_id = %s
             WHERE id = %s AND project_id = %s
            """,
            (str(plan["id"]), str(mapping["id"]), datastream_id, project_id),
        )
    # The Datastream must have RUN once: `reporting_timezone` is always-present, and
    # its compiler refuses a Datastream whose publication never recorded a source day
    # boundary (`time_boundary_unobserved`). Without this, enabling Country is blocked
    # by a capability the operator never asked about -- another precondition of the
    # walk that no single-step test meets.
    from core.time_boundary import record_boundary_evidence

    record_boundary_evidence(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        execution_id=None,
        # A DATE-grain managed feed: legitimately not re-alignable, which the
        # compiler reports as `partial` -- not as the missing-observation BLOCKER.
        grain="date_only",
        observed_report_timezone="Europe/Paris",
        evidence_origin="publication_probe",
        plan_version_id=str(plan["id"]),
    )

    return {
        "datastream_id": datastream_id,
        "plan_version_id": str(plan["id"]),
        "mapping_version_id": str(mapping["id"]),
    }


# ---------------------------------------------------------------------------
# The walk.
# ---------------------------------------------------------------------------


def _france_preset(presets) -> str:
    """The workspace envelope renames `preset_key` to `key`; read what it sends."""

    for preset in presets:
        if "france" in str(preset.get("key")):
            return str(preset["key"])
    raise AssertionError(f"no France preset in the workspace envelope: {presets}")


def _publish_a_hierarchy(console: _Console) -> dict[str, str]:
    """Governance > Master Data: preset -> draft -> confirmation -> published."""

    workspace = console.get("/governance/master-data/country")
    assert workspace.status_code == 200, workspace.text
    presets = workspace.json()["presets"]
    assert presets, "no qualified preset is offered, so the operator has no starting point"
    preset_key = _france_preset(presets)

    applied = console.country("apply_preset", {"preset_id": preset_key}, key=_id("walk_preset_"))
    assert applied.status_code in {200, 201}, applied.text
    result = applied.json()["result"]
    draft_version_id = str(result["draft_version_id"])
    draft_hash = str(result["draft_content_hash"])

    # ONE key for the pair. `issue_entry_confirmation` binds the confirmation to the
    # Idempotency-Key of the request that asked for it, and `consume` rechecks that
    # binding: a publish sent under a fresh key is `confirmation_invalid`. Measured
    # here, not assumed -- it is the kind of contract only a two-step walk meets.
    publish_key = _id("walk_publish_")
    prepared = console.country(
        "prepare_publish",
        {"version_id": draft_version_id, "expected_content_hash": draft_hash},
        key=publish_key,
    )
    assert prepared.status_code in {200, 201}, prepared.text
    confirmation = prepared.json()

    published = console.country(
        "publish",
        {
            "version_id": draft_version_id,
            "expected_content_hash": draft_hash,
            "confirmation_id": confirmation["confirmation_id"],
            "confirmation_secret": confirmation["confirmation_secret"],
        },
        key=publish_key,
    )
    assert published.status_code in {200, 201}, published.text
    return {"version_id": draft_version_id, "content_hash": draft_hash}


def _enable_country(console: _Console, version_id: str) -> dict:
    """Project Settings: change set -> prepare -> confirmation -> confirm."""

    created = console.post(
        "/settings/change-sets",
        {
            "intent": {
                "capabilities": {"country": "enabled"},
                "selected_owner_ids": {"country": [version_id]},
            }
        },
        key=_id("walk_cs_"),
    )
    assert created.status_code in {200, 201}, created.text
    change_set_id = created.json()["id"]

    prepared = console.post(
        f"/settings/change-sets/{change_set_id}/prepare", {}, key=_id("walk_cs_prepare_")
    )
    assert prepared.status_code == 200, prepared.text
    impact = prepared.json()
    assert not impact.get("blockers"), (
        "the prepared Change Set is blocked, so the operator cannot confirm: "
        f"{json.dumps(impact.get('blockers'), indent=2, default=str)}"
    )

    confirm_key = _id("walk_cs_confirm_")
    issued = console.post(
        f"/settings/change-sets/{change_set_id}/confirmations", {}, key=confirm_key
    )
    assert issued.status_code == 201, issued.text
    confirmation = issued.json()

    confirmed = console.post(
        f"/settings/change-sets/{change_set_id}/confirm",
        {
            "confirmation_id": confirmation["confirmation_id"],
            "confirmation_secret": confirmation["confirmation_secret"],
            "prepared_payload_hash": confirmation["prepared_payload_hash"],
        },
        key=confirm_key,
    )
    assert confirmed.status_code == 200, confirmed.text
    return {"change_set_id": change_set_id, "impact": impact, "confirmed": confirmed.json()}


def test_the_operator_walks_from_disabled_to_a_governed_country_project(console) -> None:
    """The whole sequence, in the order a person clicks it, through the routes."""

    before = console.get("/settings")
    assert before.status_code == 200, before.text
    country_before = _capability(before.json(), "country")
    assert country_before["availability"] == "optional"
    assert country_before["active"]["state"] == "disabled", (
        "Country must be off before anyone enables it -- an off-by-default capability "
        "that is already on proves nothing about activation."
    )

    hierarchy = _publish_a_hierarchy(console)
    _enable_country(console, hierarchy["version_id"])

    after = console.get("/settings")
    assert after.status_code == 200, after.text
    country_after = _capability(after.json(), "country")
    from core.project_capability_states import CAPABILITY_ACTIVE_STATES

    # `enabled` is NOT one of the five states migration 131 accepts, and asserting it
    # would be asserting a lens nobody can reach. `ready` and `degraded` are the two
    # that make the capability's evidence readable; which of them is correct depends
    # on coverage, and the row must SAY so rather than round up.
    assert country_after["active"]["state"] in CAPABILITY_ACTIVE_STATES, country_after
    assert country_after["active"]["version_id"], (
        "the capability is active and names no configuration version"
    )
    coverage = country_after["coverage"]
    assert coverage["applicable"] >= 1, coverage
    if country_after["active"]["state"] == "degraded":
        assert coverage["complete"] < coverage["applicable"], (
            "the capability calls itself degraded while every applicable Datastream "
            f"is complete: {coverage}"
        )

    projection = console.get("/capabilities/country/datastreams")
    assert projection.status_code == 200, projection.text
    assert hierarchy["version_id"] in json.dumps(projection.json(), default=str), (
        "the per-Datastream projection does not carry the hierarchy version the "
        "operator published -- the two surfaces do not agree on what was decided"
    )


def test_the_walk_prepares_a_country_candidate_and_moves_no_data_pointer(console) -> None:
    """AD-23, seen from the console rather than from the service.

    The fan-out test proves this at the transaction. Here the same invariant is
    checked after a request sequence: a person who enables Country has not, by that
    click, changed what any report reads.
    """

    datastream_id = console.stream["datastream_id"]
    before = _pointers(console.conn, datastream_id)

    hierarchy = _publish_a_hierarchy(console)
    _enable_country(console, hierarchy["version_id"])

    assert _pointers(console.conn, datastream_id) == before, (
        "a Data pointer moved during activation; candidates must be prepared and "
        "reviewed, never published by the act of enabling a capability"
    )


def test_the_activation_preview_computes_its_coverage_rather_than_claiming_it(console) -> None:
    """AC1: no count, exception or compatibility claim unless the read model computed it."""

    hierarchy = _publish_a_hierarchy(console)
    outcome = _enable_country(console, hierarchy["version_id"])

    impact = outcome["impact"]
    assert impact, "prepare returned nothing to show the operator"
    body = json.dumps(impact)
    assert console.stream["datastream_id"] in body, (
        "the impact the operator is asked to confirm names no Datastream, so the "
        "count on screen would be a claim rather than a computation"
    )

    projection = console.get("/capabilities/country/datastreams")
    assert projection.status_code == 200, projection.text
    rows = projection.json()
    assert console.stream["datastream_id"] in json.dumps(rows)


def test_publishing_twice_with_one_confirmation_is_refused(console) -> None:
    """The confirmation is single-use, and the route is where that has to hold."""

    workspace = console.get("/governance/master-data/country")
    preset_key = _france_preset(workspace.json()["presets"])
    applied = console.country("apply_preset", {"preset_id": preset_key}, key=_id("walk_preset_"))
    result = applied.json()["result"]
    version_id = str(result["draft_version_id"])
    content_hash = str(result["draft_content_hash"])

    publish_key = _id("walk_publish_")
    prepared = console.country(
        "prepare_publish",
        {"version_id": version_id, "expected_content_hash": content_hash},
        key=publish_key,
    )
    assert prepared.status_code in {200, 201}, prepared.text
    confirmation = prepared.json()
    publish_body = {
        "version_id": version_id,
        "expected_content_hash": content_hash,
        "confirmation_id": confirmation["confirmation_id"],
        "confirmation_secret": confirmation["confirmation_secret"],
    }

    first = console.country("publish", publish_body, key=publish_key)
    assert first.status_code in {200, 201}, first.text

    # A DIFFERENT key on purpose: replaying the same key is the idempotent path and
    # would legitimately answer the first result. What must be refused is a SECOND
    # publication asking to act again on a secret already spent.
    replay = console.country("publish", publish_body, key=_id("walk_publish_again_"))
    assert replay.status_code >= 400, (
        "a spent confirmation published a second time; a single-use secret that can "
        "be replayed is not a confirmation, it is a token"
    )


def test_a_project_without_its_foundations_cannot_enable_country(console) -> None:
    """The precondition nobody had written down, and it holds for the whole estate.

    `_compile_all_proposals` recompiles the two always-present capabilities on every
    Change Set -- "foundations, not modules a Change Set may leave uncounted" -- so a
    Project whose Reporting Timezone Policy is unconfirmed cannot enable ANY optional
    capability. The refusal is correct, and it must NAME its repair: an operator sent
    to a blocker with no owner would have nowhere to go.

    MEASURED IN PRODUCTION 2026-08-23: all 32 Projects carry `currency_fx` and
    `reporting_timezone` at `draft`. Not one has a confirmed policy, so the first
    click of the Country operator walk is refused today on every Project of the
    estate. That is the estate being unfinished, not the product being wrong -- and
    it is the reason Story 37.6's walk on the deployment needs a decision before it
    needs a sign-in.
    """

    from core.governance_rule_sets import fetch_rule_set
    from core.money_policy import FAMILY_TIMEZONE, POLICY_NAME

    head = fetch_rule_set(
        console.conn, project_id=console.project_id, family=FAMILY_TIMEZONE, name=POLICY_NAME
    )
    assert head is not None
    with console.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.governance_rule_sets SET current_version_id = NULL WHERE id = %s",
            (str(head["id"]),),
        )
    console.conn.commit()

    hierarchy = _publish_a_hierarchy(console)
    created = console.post(
        "/settings/change-sets",
        {
            "intent": {
                "capabilities": {"country": "enabled"},
                "selected_owner_ids": {"country": [hierarchy["version_id"]]},
            }
        },
        key=_id("walk_nofound_cs_"),
    )
    assert created.status_code in {200, 201}, created.text
    change_set_id = created.json()["id"]

    prepared = console.post(
        f"/settings/change-sets/{change_set_id}/prepare", {}, key=_id("walk_nofound_prep_")
    )
    assert prepared.status_code == 200, prepared.text
    blockers = prepared.json().get("blockers") or []
    codes = {blocker.get("code") for blocker in blockers}
    assert "missing_governance_evidence" in codes, blockers

    timezone_blocker = next(
        blocker
        for blocker in blockers
        if blocker.get("capability_key") == "reporting_timezone"
        and blocker.get("code") == "missing_governance_evidence"
    )
    # A message that names the gesture that repairs, and an owner to walk to.
    assert "Reporting Timezone Policy" in timezone_blocker["message"]
    owner = timezone_blocker.get("owner_reference") or {}
    assert owner.get("workspace") == "governance", timezone_blocker
    assert owner.get("section") == "controls-quality", timezone_blocker

    confirmed = console.post(
        f"/settings/change-sets/{change_set_id}/confirmations", {}, key=_id("walk_nofound_conf_")
    )
    assert confirmed.status_code >= 400, (
        "a blocked Change Set issued a confirmation; the blocker on screen would be "
        "decoration"
    )
    assert confirmed.json()["code"] == "change_set_blocked", confirmed.text


def test_a_viewer_can_read_the_walk_and_can_take_none_of_its_steps(console) -> None:
    """The authorization levels of the walk, checked where they are crossed.

    Reading is `view`, editing the hierarchy is `edit`, publishing and enabling are
    `manage`. Demoting the operator must close the last two and leave the first open
    -- closing everything would pass for the wrong reason.

    THE GRANT IS EXPLICIT, and the fixture says so rather than discovering it: only
    `owner` has a floor (`owner_floor` -> manage). Every other role needs a row in
    `app.resource_grants` for the exact scope; without one the answer is 404
    (`grant_required`), because a project the caller may not see must not be
    distinguishable from one that does not exist.
    """

    with console.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.org_members SET role = 'viewer' WHERE org_id = %s AND identity = %s",
            (console.org_id, console.person_id),
        )
        cur.execute(
            """
            INSERT INTO app.resource_grants
                (id, org_id, identity, scope_type, scope_id, capability, granted_by)
            VALUES (%s, %s, %s, 'project', %s, 'view', 'pytest')
            ON CONFLICT DO NOTHING
            """,
            (_id("rg_"), console.org_id, console.person_id, console.project_id),
        )
    console.conn.commit()

    assert console.get("/settings").status_code == 200, "a viewer may still READ the settings"

    workspace = console.get("/governance/master-data/country")
    assert workspace.status_code == 200, "a viewer may still READ the Country workspace"

    preset_key = _france_preset(workspace.json()["presets"])
    edit = console.country("apply_preset", {"preset_id": preset_key}, key=_id("walk_viewer_"))
    assert edit.status_code >= 400, "a viewer materialized a Country draft"

    created = console.post(
        "/settings/change-sets",
        {"intent": {"capabilities": {"country": "enabled"}}},
        key=_id("walk_viewer_cs_"),
    )
    assert created.status_code >= 400, "a viewer opened a capability Change Set"


def _capability(settings: dict, key: str) -> dict:
    """`GET /settings` returns capabilities as a LIST of rows, each with `active`.

    Read from the payload rather than assumed: the shape is
    `{"key", "availability", "active": {"state", "version_id"}, "pending",
    "coverage", "exceptions", "blockers", "owner_links"}`.
    """

    for row in settings.get("capabilities") or ():
        if isinstance(row, dict) and row.get("key") == key:
            return row
    raise AssertionError(f"the settings payload carries no `{key}` capability: {settings}")


def _pointers(conn, datastream_id: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_plan_version_id, current_mapping_version_id,
                   current_published_execution_id, enabled, lifecycle_state
            FROM app.datastreams WHERE id = %s
            """,
            (datastream_id,),
        )
        return cur.fetchone()
