"""Registered I/O adapters for Story 47.4 setup previews and first candidates.

The shared queue invokes this module after it has claimed a durable job.  Core
never imports a provider module: deployments register typed adapters here, and
an absent adapter fails closed instead of fabricating a sample or candidate.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Callable, Mapping

from core.account_topology import selected_account_pull_arguments
from core.datastream_activation import (
    ActivationValidationError,
    build_safe_preview,
    complete_candidate_from_adapter,
    persist_preview,
)
from core.datastream_preconfiguration import read_proposal
from core.datastream_setup_observations import read_observation
from core.datastream_workbench import append_phase_evidence, append_stage_evidence

ActivationAdapter = Callable[[dict[str, Any]], dict[str, Any]]
_ADAPTERS: dict[tuple[str, str, str], ActivationAdapter] = {}
_KINDS = {"setup_preview", "candidate_materialization"}
_MODES = {"connector_pull", "external_bq", "managed_feed"}
_RUNTIME_DRIVERS: dict[tuple[str, str], ActivationAdapter] = {}


def register_runtime_activation_driver(
    kind: str,
    mode: str,
    driver: ActivationAdapter,
) -> None:
    """Bind a deployment I/O driver behind the process-lifetime activation adapter."""
    if kind not in _KINDS or mode not in _MODES or not callable(driver):
        raise ActivationValidationError("Runtime activation driver registration is invalid")
    _RUNTIME_DRIVERS[(kind, mode)] = driver


def _runtime_adapter(kind: str, mode: str) -> ActivationAdapter:
    def execute(context: dict[str, Any]) -> dict[str, Any]:
        driver = _RUNTIME_DRIVERS.get((kind, mode))
        if driver is None:
            raise ActivationValidationError(f"No deployment {kind} driver is configured for {mode}")
        evidence = driver(deepcopy(context))
        if not isinstance(evidence, dict):
            raise ActivationValidationError("Runtime activation driver returned invalid evidence")
        return evidence

    return execute


def _install_runtime_activation_adapters() -> None:
    for kind in sorted(_KINDS):
        for mode in sorted(_MODES):
            _ADAPTERS.setdefault((kind, mode, "*"), _runtime_adapter(kind, mode))


def registered_activation_adapters() -> frozenset[tuple[str, str, str]]:
    """Expose the mounted runtime matrix for startup validation and tests."""
    _install_runtime_activation_adapters()
    return frozenset(_ADAPTERS)


def register_activation_adapter(
    kind: str,
    mode: str,
    adapter: ActivationAdapter,
    *,
    channel: str = "*",
) -> None:
    """Register one real adapter at the provider/inbound boundary."""
    if kind not in _KINDS or mode not in _MODES or not callable(adapter):
        raise ActivationValidationError("Activation adapter registration is invalid")
    _ADAPTERS[(kind, mode, channel or "*")] = adapter


def clear_activation_adapters() -> None:
    """Test-only registry reset; runtime registrations are process-lifetime."""
    _ADAPTERS.clear()


def _adapter(kind: str, mode: str, channel: str | None) -> ActivationAdapter:
    resolved = _ADAPTERS.get((kind, mode, channel or "*")) or _ADAPTERS.get((kind, mode, "*"))
    if resolved is None:
        raise ActivationValidationError(f"No verified {kind} adapter is registered for {mode}")
    return resolved


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ActivationValidationError("Activation job payload is invalid")
    return deepcopy(value)


def _preview_context(conn, job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    project_id = str(job["project_id"])
    draft_id = str(job["draft_id"])
    proposal_id = str(payload.get("proposal_id") or "")
    observation_id = str(payload.get("observation_id") or "")
    proposal = read_proposal(
        conn,
        project_id=project_id,
        draft_id=draft_id,
        proposal_id=proposal_id,
    )
    observation = read_observation(
        conn,
        project_id=project_id,
        draft_id=draft_id,
        observation_id=observation_id,
    )
    pins = _json(payload.get("pins"))
    if (
        proposal.get("is_stale")
        or proposal.get("draft_revision_ref") != pins.get("draft_revision_id")
        or proposal.get("proposal_ref") != pins.get("proposal_id")
        or observation.get("observation_ref") != pins.get("observation_id")
        or observation.get("draft_revision_ref") != pins.get("draft_revision_id")
    ):
        raise ActivationValidationError("Preview dependencies changed before execution")
    context = {
        "kind": "setup_preview",
        "project_id": project_id,
        "draft_id": draft_id,
        "mode": observation["mode"],
        "channel": payload.get("channel"),
        "pins": pins,
        "proposal": proposal,
        "observation": observation,
    }
    context.update(
        _source_identity(conn, project_id, str(pins.get("draft_revision_id") or ""),
                         observation["mode"])
    )
    return context


def _source_identity(conn, project_id: str, revision_id: str, mode: str) -> dict[str, Any]:
    """WHICH source the preview is of, read from the draft the operator filled.

    THE PREVIEW COULD NOT NAME ITS OWN CONNECTOR. Measured 2026-08-11 by walking
    the funnel against production: the job ran and failed with "Connector preview
    requires a Connector name" (`connector_pull_preview`), because this context
    carried the mode, the pins, the proposal and the observation -- and nothing
    that says WHAT is being previewed. The observation does not carry it either:
    it pins a `connector_contract_version_ref`, not a module. So no Connector
    pull could ever be previewed, which is the step before every activation.

    The draft's own answers are the authority -- they are what the person chose,
    and the proposal was compiled from them. A draft that names nothing yields an
    empty dict and the driver refuses by name, as it did before.
    """
    if not revision_id:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_operator_input FROM app.datastream_setup_draft_revisions "
            "WHERE id = %s AND project_id = %s",
            (revision_id, project_id),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        return {}
    operator = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    source = operator.get("source") if isinstance(operator.get("source"), dict) else {}
    identity: dict[str, Any] = {}
    if mode == "connector_pull":
        # A PREVIEW HAS TO ASK FOR A WINDOW. The operator's history intent is a
        # sentence ("last 30 days"), not a date pair, and the driver needs two
        # dates or the module `pull` raises TypeError and gets blamed for not
        # supporting preview. Seven complete days ending yesterday: bounded on
        # purpose, and the driver reports it back as `requested_interval` so the
        # screen shows the window the sample came from rather than implying the
        # whole history was read.
        from datetime import date, timedelta  # noqa: PLC0415

        end = date.today() - timedelta(days=1)
        identity["interval"] = {
            "from": (end - timedelta(days=6)).isoformat(),
            "date_from": (end - timedelta(days=6)).isoformat(),
            "to_exclusive": end.isoformat(),
            "date_to": end.isoformat(),
        }
        if source.get("connector_ref"):
            identity["module"] = str(source["connector_ref"])
        if source.get("report_ref"):
            identity["report_id"] = str(source["report_ref"])
        account_ref = str(source.get("source_account_ref") or "")
        if account_ref:
            identity["source_account_ref"] = account_ref
            with conn.cursor() as cur:
                # The connection the token is minted from, and the provider id of
                # the account -- the two arguments every module `pull` takes.
                cur.execute(
                    "SELECT credential_id, external_account_id "
                    "FROM app.credential_accounts WHERE source_account_id = %s",
                    (account_ref,),
                )
                account = cur.fetchone()
            if account:
                identity["connection_ref_id"] = account[0]
                # The chosen account under the ONE name its Connector declares.
                # Five guessed names covered five modules and silently missed the
                # rest; the manifest answers for all 39.
                identity["pull_arguments"] = selected_account_pull_arguments(
                    identity.get("module"), account[1]
                )
    elif mode == "external_bq":
        if source.get("access_ref"):
            identity["access_ref"] = str(source["access_ref"])
        if source.get("object_ref"):
            identity["object_ref"] = str(source["object_ref"])
    return identity


def _execute_preview(conn, job: dict[str, Any], payload: dict[str, Any]) -> None:
    context = _preview_context(conn, job, payload)
    mode = context["mode"]
    evidence = _adapter("setup_preview", mode, context.get("channel"))(deepcopy(context))
    fields = context["proposal"].get("confirmed_intent_bundle", {}).get("field_mappings") or []
    classifications = {
        str(field.get("field_id")): str(field.get("sensitivity") or "unknown")
        for field in fields
        if isinstance(field, dict) and field.get("field_id")
    }
    preview = build_safe_preview(
        mode=mode,
        pins=context["pins"],
        adapter_evidence=evidence,
        classifications=classifications,
    )
    persist_preview(
        conn,
        project_id=context["project_id"],
        draft_id=context["draft_id"],
        actor=str(job["requested_by"]),
        idempotency_key=str(job["correlation_id"]),
        preview=preview,
    )


def _driver_interval(payload: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise ONE interval vocabulary for the drivers.

    Three spellings exist in this repository and the driver reads only one:
    `connector_pull_candidate` maps `from`/`date_from` and
    `to_exclusive`/`date_to` (adapters/datastream_activation_drivers.py:461-462),
    while `operations_mcp._bounded_interval` produces `{from, to}` -- so its `to`
    was never read and every bounded refetch silently pulled the connector's
    DEFAULT window instead of the one that was asked for. Normalised here, once,
    for every mode.
    """
    raw = payload.get("interval") or projection.get("interval") or {}
    if not isinstance(raw, dict):
        return None
    date_from = raw.get("from") or raw.get("date_from")
    date_to = raw.get("to") or raw.get("to_exclusive") or raw.get("date_to")
    if not date_from or not date_to:
        return None
    return {"date_from": date_from, "date_to": date_to}


def _declared_window(conn, *, project_id: str, datastream: Mapping[str, Any]) -> dict[str, str]:
    """The window this Datastream DECLARES, for a candidate that pinned none.

    MEASURED, 2026-08-12: `app.datastream_activation_jobs` held 17 jobs of kind
    `candidate_materialization`, 17 in `dead_letter` and none ever `done` -- the
    first-candidate materialization had never succeeded on this product, for any
    connector. Every one died in 0.4 s, before any network call, with
    `TypeError: pull_audience_demographics() missing 2 required positional
    arguments: 'date_from' and 'date_to'`.

    THE CHAIN. Three of the four callers of `enqueue_activation_work(kind=
    "candidate_materialization")` pin no interval -- `datastream_activation.py`
    (the wizard's own publish), `datastream_change.py` and
    `country_activation.py`; only `datastream_first_candidate.py` does. The
    wizard pins none because it never asks for a window, which is right: the
    Datastream already declares one. `_driver_interval` above then answers None
    honestly, the driver's `context.get("interval") or {}` leaves `date_from`
    and `date_to` at None, and its final `if v is not None` DROPS both keys. 222
    pull functions across 37 of the 39 connectors declare `date_from: str` with
    no default, so requiring the window is the norm and the missing link was
    here, for everyone.

    WHY THE DATASTREAM'S OWN WINDOW AND NOT A CONSTANT. A first candidate must
    show what the Datastream will actually collect. Any other number would make
    the sample the operator reviews describe a period no scheduled run will ever
    fetch. So this is the SAME resolver the nightly dispatcher calls -- retrieval
    window, legacy alias, extraction offset, cadence floor (`core.pull_window`,
    AI-46/AI-145/AI-217) -- ending on the project's own last complete day
    (AI-117). Nothing is decided here; the arbitration is read.

    An unresolvable window is NOT patched with a guess: this returns what it
    could build, and the driver refuses by name when the pull needs more.
    """
    from core.pull_window import resolve_window  # noqa: PLC0415
    from core.scheduler import project_timezone, project_yesterday  # noqa: PLC0415

    end_reference = project_yesterday(project_timezone(conn, project_id))
    return resolve_window(
        datastream,
        end_reference=end_reference,
        cadence=datastream.get("schedule_mode"),
    ).as_interval()


def _candidate_context(conn, job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.state,e.plan_version_id,e.mapping_version_id,e.projection_plan_ref,
                      p.normalized_payload,m.mapping_payload,r.review_snapshot,d.org_id,
                      d.module_name,d.connection_ref_id,d.report_profile_id,d.source_kind,
                      -- The WINDOW the Datastream declares. Selected here because
                      -- this is where the candidate's context is built and the
                      -- only place that knows both the row and the job: a
                      -- candidate that pins no interval is resolved from these
                      -- four columns rather than dying on a TypeError.
                      d.date_window_days,d.refetch_days,d.window_offset_days,
                      d.schedule_mode,
                      -- WHICH object this Datastream reads. The candidate context
                      -- carried the authorization and not the selection, so the
                      -- first collection pulled the token's default object.
                      ca.external_account_id,
                      -- THE DRAFT the staged asset belongs to (AI-321). The
                      -- file driver resolves the asset by (id, draft, project);
                      -- a context without the draft cannot find any asset.
                      sm.draft_id
                 FROM app.datastream_executions e
                 JOIN app.datastreams d ON d.id=e.datastream_id AND d.project_id=e.project_id
                 LEFT JOIN app.credential_accounts ca
                   ON ca.source_account_id = d.source_account_id
                 JOIN app.datastream_plan_versions p ON p.id=e.plan_version_id
                 JOIN app.datastream_mapping_versions m ON m.id=e.mapping_version_id
                 LEFT JOIN app.datastream_setup_materializations sm
                   ON sm.candidate_execution_id=e.id AND sm.datastream_id=e.datastream_id
                 LEFT JOIN app.datastream_setup_final_reviews r ON r.id=sm.final_review_id
                WHERE e.id=%s AND e.datastream_id=%s AND e.project_id=%s""",
            (job["execution_id"], job["datastream_id"], job["project_id"]),
        )
        row = cur.fetchone()
    if row is None or row[0] not in {"created", "loading"}:
        raise ActivationValidationError("Candidate job no longer owns a resumable execution")
    review = _json(row[6] or {})
    if review.get("final_review_ref") not in {None, payload.get("final_review_id")}:
        raise ActivationValidationError("Candidate job final-review binding changed")
    projection = _json(row[3])
    declared = {
        "date_window_days": row[12],
        "refetch_days": row[13],
        "window_offset_days": row[14],
        "schedule_mode": row[15],
    }
    # A pinned interval always wins -- a caller that named a window asked for
    # THAT window. The declaration only answers when nobody did.
    interval = _driver_interval(payload, projection) or _declared_window(
        conn, project_id=str(job["project_id"]), datastream=declared
    )
    last: dict[str, Any] = {}
    if not (job.get("draft_id") and (payload.get("channel") or review.get("channel"))):
        last = _last_materialization(
            conn, project_id=str(job["project_id"]), datastream_id=str(job["datastream_id"])
        )
    return {
        "kind": "candidate_materialization",
        "org_id": str(row[7]),
        "project_id": str(job["project_id"]),
        "datastream_id": str(job["datastream_id"]),
        "execution_id": str(job["execution_id"]),
        # The job's own draft first; the materialization row second, because
        # the row is written in the same transaction as the candidate and is
        # the one place that binds this execution to its draft (AI-321).
        "draft_id": str(job.get("draft_id") or row[17] or last.get("draft_id") or ""),
        # `source_kind` is the LAST fallback, not the first: a job that names its
        # own mode wins. Without it a Datastream born outside the wizard has no
        # draft review to read a mode from, and adapter resolution fails on an
        # empty string before it ever reaches a driver.
        "mode": str(payload.get("mode") or review.get("mode") or row[11] or ""),
        # THE CHANNEL, or the driver refuses by name. A candidate minted by a
        # workbench change (AI-321, 2026-08-29) has no materialization row of
        # its own, so `review` is empty and the payload names only the mode;
        # `managed_feed_candidate` then routed to the remote refusal ("known
        # gap") for a plain file upload. The channel of a Datastream does not
        # change between two candidates: it is the one its LAST final review
        # recorded, read here when nobody else names it.
        "channel": payload.get("channel") or review.get("channel") or last.get("channel"),
        # The Connector identity the pull driver REQUIRES and that this context
        # never carried: `connector_pull_candidate` reads `context["module"]` and
        # refuses "Candidate materialization requires a Connector name"
        # (adapters/datastream_activation_drivers.py:440-443). It lives on
        # app.datastreams, and this SELECT is where it enters the pipeline. The
        # driver's own docstring admits driver and completion "never met" -- so
        # NO connector_pull candidate could pull, not even the wizard's. This is
        # the class, not the instance (AI-142).
        "module": row[8],
        "module_name": row[8],
        "connection_ref_id": row[9],
        "report_id": row[10],
        "pull_arguments": selected_account_pull_arguments(row[8], row[16]),
        "interval": interval,
        "plan_version_id": row[1],
        "mapping_version_id": row[2],
        "projection_plan": projection,
        "plan_intent": _json(row[4]),
        "mapping": _json(row[5]),
        # LE CONTRAT D'ANALYSE, que le pilote managed_feed EXIGE et que ce
        # contexte ne portait pas : `managed_feed_candidate` passe
        # `context.get("import_contract") or {}` a `run_import`, qui refuse alors
        # `invalid_import_contract: 'format' must be one of [...]; got None`. Le
        # contrat confirme etait pourtant EN BASE, actif, a cote.
        #
        # C'est exactement la classe d'AI-142 un cran plus loin : le contexte est
        # l'endroit ou les pilotes recoivent ce dont ils ont besoin, et un pilote
        # dont le besoin n'y entre jamais ne peut pas travailler. Le lecteur est
        # celui d'`inbound_ingest`, jamais une seconde lecture -- deux facons de
        # choisir un contrat actif finiraient par en choisir deux differents.
        "import_contract": _active_parsing_contract(
            conn,
            datastream_id=str(job["datastream_id"]),
            project_id=str(job["project_id"]),
        ),
        "final_review": review,
    }


def _last_materialization(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """The draft and the channel of the Datastream's most recent materialization.

    A candidate minted by a workbench change has no materialization row of its
    own (the join above is on the candidate's execution), so its context knew
    neither the draft the staged asset lives in nor the delivery channel -- and
    the file driver refused both by name (AI-321, 2026-08-29). Neither changes
    between two candidates of one Datastream: they are read from the last
    materialization, when nobody else names them.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT sm.draft_id, r.review_snapshot
                 FROM app.datastream_setup_materializations sm
                 JOIN app.datastream_setup_final_reviews r ON r.id = sm.final_review_id
                WHERE sm.datastream_id = %s AND sm.project_id = %s
                ORDER BY sm.id DESC
                LIMIT 1""",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    review = _json(row[1] or {})
    return {"draft_id": row[0], "channel": review.get("channel")}


def _active_parsing_contract(conn, *, datastream_id: str, project_id: str) -> dict:
    """Le contrat confirme du flux, ou un vide qui laissera le pilote refuser.

    Une absence n'est pas rattrapee ici : `run_import` refuse deja un contrat
    invalide avec un message qui nomme ce qui manque, et le doubler d'une seconde
    phrase depuis le contexte dirait la meme chose deux fois -- moins bien.
    """
    from core.inbound_ingest import _fetch_active_parsing_contract  # noqa: PLC0415

    try:
        return _fetch_active_parsing_contract(
            conn, datastream_id=datastream_id, project_id=project_id
        )
    except Exception:  # noqa: BLE001 -- l'absence de contrat est le refus du pilote
        return {}


def _execute_candidate(conn, job: dict[str, Any], payload: dict[str, Any]) -> None:
    context = _candidate_context(conn, job, payload)
    try:
        result = _adapter("candidate_materialization", context["mode"], context.get("channel"))(
            deepcopy(context)
        )
        complete_candidate_from_adapter(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            execution_id=context["execution_id"],
            actor=str(job["requested_by"]),
            adapter_result=result,
        )
        for stage in result.get("stage_evidence") or []:
            append_stage_evidence(
                conn,
                org_id=context["org_id"],
                project_id=context["project_id"],
                datastream_id=context["datastream_id"],
                execution_id=context["execution_id"],
                stage=str(stage.get("stage") or ""),
                actor=str(job["requested_by"]),
                plan_version_id=context["plan_version_id"],
                mapping_version_id=context["mapping_version_id"],
                evidence=stage,
            )
        for phase in result.get("phase_evidence") or []:
            append_phase_evidence(
                conn,
                org_id=context["org_id"],
                project_id=context["project_id"],
                datastream_id=context["datastream_id"],
                execution_id=context["execution_id"],
                phase=str(phase.get("phase") or ""),
                actor=str(job["requested_by"]),
                plan_version_id=context["plan_version_id"],
                mapping_version_id=context["mapping_version_id"],
                evidence=phase,
            )
    finally:
        from core.raw_landing import clear_candidate_landing  # noqa: PLC0415

        clear_candidate_landing(context["execution_id"])


def execute_activation_job(conn, job: dict[str, Any]) -> None:
    """Execute one already-claimed durable job without committing it."""
    payload = _json(job.get("payload"))
    kind = str(job.get("kind") or "")
    if kind == "setup_preview":
        _execute_preview(conn, job, payload)
        return
    if kind == "candidate_materialization":
        _execute_candidate(conn, job, payload)
        return
    raise ActivationValidationError("Activation job kind is unsupported")


_install_runtime_activation_adapters()
