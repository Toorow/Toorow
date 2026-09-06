"""Story 38.18 -- Reprocess retained raw evidence, without asking for it again.

A file arrived, its mapping was wrong, the mapping was repaired. The operator
must be able to re-run THAT file. Not ask the sender to resend it -- the sender
is a person with a mail client and no obligation to help -- and not mutate what
already happened.

WHAT THIS MODULE IS NOT, because the repository already owns two of the three
pieces and a third engine would be the failure this epic exists to avoid:

  * It is NOT a second import engine. Reprocessing drives the same
    ``ingest_inbound_file`` -> ``run_import`` path a delivery does, so a
    reprocessed file and a freshly delivered one cannot produce different
    governance.
  * It is NOT a second recovery vocabulary. ``core/bounded_recovery.py`` already
    owns synchronize / reload / reprocess for Story 12.11 -- but its reprocess
    starts from a retained EXECUTION and reapplies a mapping to data already in
    the warehouse. This one starts from retained BYTES in quarantine, which is a
    different origin and the only one that survives a mapping that never
    produced a row in the first place.
  * It is NOT a mutation. The prior execution, the prior ledger row and the
    original ``inbound_raw_imports`` row are untouched -- the last is enforced
    by the migration-171 trigger, not by this module's good intentions.

THE THREE REFUSALS, and each exists because the alternative is worse than a
failure:

  * RETENTION EXPIRED. The bytes are gone by policy. The answer is an explicit,
    bounded reason -- never a silent request to the provider, and never a
    fabricated empty result (E38-NFR15).
  * OBJECT UNREADABLE. The row says the bytes are there and the store disagrees.
    Reporting that as "nothing to reprocess" would make a storage fault look
    like a retention decision.
  * INTEGRITY MISMATCH. The object read back does not hash to the
    ``content_hash`` recorded at receipt. That is the one case where continuing
    would be actively wrong: the operator would be reprocessing bytes that are
    not the bytes the evidence describes, under an audit trail claiming they
    are.

A SCOPE IS A NAMED SET, NOT A BULK BUTTON (AC1, << and a governed scope >>).
``prepare_reprocess_scope`` / ``execute_reprocess_scope`` follow the pattern
`docs/product-architecture/datastream-workbench-and-wizard.md` fixes for every
durable repair -- << Every destructive or durable repair follows `Prepare >
Review exact scope and consequences > Confirm > Execute as a new durable
operation` >> -- with the count stated BEFORE the act and the objects it names
beside it (<< le compte AVANT l'acte >>, on the same page's Mapping row). Replaying one file inside
a scope goes through ``_replay_one``, the same body the single replay uses: a
scope is not a second engine any more than a reprocess is.

IDEMPOTENCY IS AT TWO LEVELS, and both are needed (AC5). The operation seam
replays on its idempotency key, and the ``message_id`` handed to ``run_import``
is DERIVED from that same key -- so a retried reprocess replays the original
import instead of creating a second execution for one confirmed operation. A
reprocess with a genuinely new key gets a genuinely new execution, which is the
point.

The CALLER owns the transaction. ASCII-only source (AI-03).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Stable, operator-facing refusal codes. They reach MCP and the console.
UNAVAILABLE_NOT_FOUND = "raw_import_not_found"
UNAVAILABLE_RETENTION_EXPIRED = "retention_expired"
UNAVAILABLE_LEGAL_HOLD_RELEASED = "legal_hold_released_and_expired"
UNAVAILABLE_NO_OBJECT = "quarantine_object_unavailable"
UNAVAILABLE_INTEGRITY = "content_integrity_mismatch"
UNAVAILABLE_NO_URI = "no_quarantine_reference"
#: A SCOPE refusal, not a member one: the set the operator confirmed is no
#: longer the set that would run. See ``execute_reprocess_scope``.
UNAVAILABLE_SCOPE_MOVED = "scope_moved"

#: THE DECLARED BOUND OF A SCOPE. A named ceiling that the proposal states and
#: the execution enforces, because the alternative is a silent truncation: a
#: person who asked for "every delivery of last month" and got the first N
#: without being told would believe the rest had been replayed.
MAX_SCOPE_MEMBERS = 50


class ReprocessValidationError(ValueError):
    """Raised before any work when the request itself is malformed."""


class ReprocessUnavailable(RuntimeError):
    """Raised when the retained evidence cannot support a reprocess.

    Carries the stable ``code`` so a caller can render the reason without
    parsing a message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReprocessAvailability:
    """Can this raw import be reprocessed, and on what evidence?

    ``available`` is the decision. ``reason`` is None when it is True, and one of
    the stable codes above otherwise -- never a free-text explanation, because
    this value is matched on.
    """

    available: bool
    reason: str | None
    raw_import_id: str
    datastream_id: str
    content_hash: str | None = None
    size_bytes: int | None = None
    filename: str | None = None
    retention_expires_at: str | None = None
    legal_hold: bool = False
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "raw_import_id": self.raw_import_id,
            "datastream_id": self.datastream_id,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "filename": self.filename,
            "retention_expires_at": self.retention_expires_at,
            "legal_hold": self.legal_hold,
            "detail": self.detail,
        }


def _open_store(store):  # noqa: ANN001
    if store is not None:
        return store
    from core.inbound_quarantine import open_quarantine_store  # noqa: PLC0415

    return open_quarantine_store()


def evaluate_reprocess(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    store=None,  # noqa: ANN001
    verify_integrity: bool = True,
) -> ReprocessAvailability:
    """Decide whether the retained bytes can be reprocessed -- and prove it.

    ``verify_integrity`` actually READS the object and re-hashes it. That costs
    one object read, and it is the difference between a preparation that
    promises a reprocess and one that has checked it can happen. A preparation
    that succeeds and an execution that then fails on unreadable bytes is the
    worst of both: the operator confirmed something that was never possible.

    Never raises for an unavailable answer: unavailability is a VALUE here, and
    the caller renders it. Only a malformed request raises.
    """
    if not isinstance(raw_import_id, str) or not raw_import_id.strip():
        raise ReprocessValidationError("raw_import_id is required")
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise ReprocessValidationError("datastream_id is required")

    from core.inbound_raw_imports import get_raw_import  # noqa: PLC0415

    raw = get_raw_import(
        conn, raw_import_id=raw_import_id.strip(), datastream_id=datastream_id.strip()
    )
    if raw is None:
        # Absent and belonging-to-another-tenant are the same answer on purpose.
        return ReprocessAvailability(
            available=False,
            reason=UNAVAILABLE_NOT_FOUND,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
        )

    def _no(reason: str, detail: str | None = None) -> ReprocessAvailability:
        return ReprocessAvailability(
            available=False,
            reason=reason,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            content_hash=raw.get("content_hash"),
            size_bytes=raw.get("size_bytes"),
            filename=raw.get("filename"),
            retention_expires_at=raw.get("retention_expires_at"),
            legal_hold=bool(raw.get("legal_hold")),
            detail=detail,
        )

    quarantine_uri = raw.get("quarantine_uri")
    if not quarantine_uri:
        return _no(UNAVAILABLE_NO_URI, "the record carries no object reference")

    # Retention. A legal hold OVERRIDES expiry -- that is what a hold is for --
    # so the order of these two checks is the policy, not an accident.
    expires_at = raw.get("retention_expires_at")
    if expires_at and not raw.get("legal_hold"):
        import datetime as _dt  # noqa: PLC0415

        try:
            deadline = _dt.datetime.fromisoformat(str(expires_at))
            now = _dt.datetime.now(deadline.tzinfo) if deadline.tzinfo else _dt.datetime.now()
            if deadline < now:
                return _no(
                    UNAVAILABLE_RETENTION_EXPIRED,
                    "the retained object passed its retention boundary",
                )
        except ValueError:  # pragma: no cover -- a malformed stored timestamp
            logger.warning(
                "inbound_reprocess: unparseable retention_expires_at on %s",
                raw_import_id,
            )

    if not verify_integrity:
        return ReprocessAvailability(
            available=True,
            reason=None,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            content_hash=raw.get("content_hash"),
            size_bytes=raw.get("size_bytes"),
            filename=raw.get("filename"),
            retention_expires_at=expires_at,
            legal_hold=bool(raw.get("legal_hold")),
        )

    try:
        data = _open_store(store).get(quarantine_uri)
    except Exception as exc:  # noqa: BLE001 -- a storage fault is not a policy answer
        logger.warning(
            "inbound_reprocess: quarantine read failed for %s: %s",
            raw_import_id,
            type(exc).__name__,
        )
        return _no(
            UNAVAILABLE_NO_OBJECT,
            "the retained object could not be read from quarantine",
        )

    actual = hashlib.sha256(data).hexdigest()
    if actual != raw.get("content_hash"):
        # Reprocessing here would run bytes that are not the bytes the audit
        # trail describes, under a record claiming they are.
        return _no(
            UNAVAILABLE_INTEGRITY,
            "the retained object does not match its recorded content hash",
        )

    return ReprocessAvailability(
        available=True,
        reason=None,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        content_hash=raw.get("content_hash"),
        size_bytes=raw.get("size_bytes"),
        filename=raw.get("filename"),
        retention_expires_at=expires_at,
        legal_hold=bool(raw.get("legal_hold")),
    )


def _project_of(conn, *, datastream_id: str) -> str | None:
    """The Datastream's project, or None. Fail-closed: unknown scope is no scope."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM app.datastreams WHERE id = %s", (datastream_id,)
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- a proposal never fails on its extras
        return None
    return row[0] if row and row[0] else None


def _template_facts(
    conn, *, datastream_id: str, project_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The pinned Template version, and the versions that exist beside it.

    The list is empty for every state but ``bound``: there is no `template_code`
    to enumerate under when nothing resolved, and inventing an empty list with a
    reassuring shape would say "no other version exists" where the truth is "the
    question could not be asked".
    """
    from core.inbound_ingest import (  # noqa: PLC0415
        TEMPLATE_BOUND,
        list_reprocess_template_versions,
        read_reprocess_template_version,
    )

    template_version = read_reprocess_template_version(
        conn, datastream_id=datastream_id, project_id=project_id
    )
    if template_version.get("state") != TEMPLATE_BOUND:
        return template_version, []
    # A catalog Template has no per-project row set to enumerate; the pair IS the
    # binding, and listing project rows under its code would mix two registries.
    if template_version.get("kind") == "catalog":
        return template_version, []
    return template_version, list_reprocess_template_versions(
        conn,
        project_id=project_id,
        template_code=str(template_version.get("template_code") or ""),
        pinned_template_id=template_version.get("template_id"),
    )


def prepare_reprocess(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    target_mapping_version_id: str | None = None,
    store=None,  # noqa: ANN001
) -> dict[str, Any]:
    """The immutable proposal an operator confirms (AC2), under a CHOSEN version (AC1).

    States what WILL happen in the words of the effect, not of the mechanism:
    which file, under which pinned versions, whether the published pointer can
    move, and what the rollback is. Preparation has no side effect -- it reads
    the object to verify integrity and writes nothing.

    ``target_mapping_version_id`` is what AC1 calls << selects [...] target
    mapping/template versions >>. Absent means the Datastream's current pair,
    which is the behaviour every caller had before and still gets by default.
    Present means: replay this file under THAT version -- the candidate just
    authored in 38.16, or the one in force before a change that turned out
    wrong. Without it, the only question a reprocess could answer was << run
    today's mapping again >>, which recovers nothing when today's mapping is the
    problem.

    The proposal reports the ALTERNATIVES too. A choice with no list is a
    parameter, not a choice: an operator would have to already know a version id
    to type it, so the only reachable version would remain the current one.

    THE TEMPLATE VERSION IS REPORTED, NOT TAKEN AS A PARAMETER, and that is the
    same rule the plan version already follows. It is the Datastream's pin
    (`inbound_ingest.read_reprocess_template_version`), which
    `csv_excel_import.resolve_file_source_producer` reads with no override, and
    which `app.file_source_template_confirmations` binds one-to-one to the
    mapping version a human confirmed. It is stated here -- with its `state`, so
    "no Template", "unreadable" and "here it is" stay three answers -- because a
    person confirming a replay is entitled to know the contract it runs against.
    """
    availability = evaluate_reprocess(
        conn, raw_import_id=raw_import_id, datastream_id=datastream_id, store=store
    )

    plan_version_id = mapping_version_id = None
    write_mode = None
    target_error = None
    available_versions: list[dict[str, Any]] = []
    template_version: dict[str, Any] = {
        "state": "unknown",
        "reason": "the Datastream project could not be resolved",
    }
    available_template_versions: list[dict[str, Any]] = []
    project_id = _project_of(conn, datastream_id=datastream_id)
    if availability.available:
        from core.inbound_ingest import (  # noqa: PLC0415
            DatastreamNotIngestable,
            list_reprocess_target_versions,
            resolve_dispatch_bundle_for_target,
        )

        if project_id:
            available_versions = list_reprocess_target_versions(
                conn, datastream_id=datastream_id, project_id=project_id
            )
            template_version, available_template_versions = _template_facts(
                conn, datastream_id=datastream_id, project_id=project_id
            )
        if target_mapping_version_id and project_id:
            # LA MEME PORTE QUE L'EXECUTION, AU MOMENT DE LA PROPOSITION. Une
            # version refusee decouverte a l'execution serait un refus apres
            # confirmation -- c'est-a-dire apres qu'une personne a decide.
            try:
                bundle = resolve_dispatch_bundle_for_target(
                    conn,
                    datastream_id=datastream_id,
                    project_id=project_id,
                    mapping_version_id=target_mapping_version_id,
                )
                mapping_version_id = target_mapping_version_id
                plan_version_id = bundle.get("plan_version_id")
            except DatastreamNotIngestable as exc:
                target_error = str(exc)
        if mapping_version_id is None and target_error is None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_plan_version_id, current_mapping_version_id "
                    "FROM app.datastreams WHERE id = %s",
                    (datastream_id,),
                )
                row = cur.fetchone()
            if row:
                plan_version_id, mapping_version_id = row
        # The inbound path always runs REPLACE, exactly as a delivery does.
        # Saying so in the proposal is the difference between an operator who
        # knows the pointer can move and one who finds out.
        write_mode = "replace"

    return {
        "availability": availability.as_dict(),
        "raw_import_id": raw_import_id,
        "datastream_id": datastream_id,
        # The versions are read at PREPARE time and re-read at EXECUTE time; a
        # difference between the two is drift, and the caller compares them.
        "bound_versions": {
            "plan_version_id": plan_version_id,
            "mapping_version_id": mapping_version_id,
        },
        # AC1. `target_selected` distingue << j'ai choisi cette version >> de
        # << c'est celle en vigueur >>, deux etats qu'un meme identifiant ne
        # separe pas : la version courante EST un choix valide, et une personne
        # qui la selectionne explicitement doit lire qu'elle l'a fait.
        "target_selected": bool(target_mapping_version_id) and target_error is None,
        "target_refused": target_error,
        "available_versions": available_versions,
        # L'AUTRE MOITIE DE L'AC1 : << target mapping/TEMPLATE versions >>. La
        # version de Template n'est jamais un `null` nu -- elle porte son `state`,
        # parce que << ce Datastream n'epingle aucun Template >>, << le registre
        # n'a pas pu etre lu >> et << la voici >> sont trois phrases qu'une valeur
        # absente confond en une seule.
        "template_version": template_version,
        "available_template_versions": available_template_versions,
        "creates_new_execution": bool(availability.available) and target_error is None,
        "may_move_published_pointer": bool(availability.available) and target_error is None,
        "write_mode": write_mode,
        "mutates_prior_execution": False,
        "requires_provider_call": False,
        "rollback": (
            "The prior publication remains the last known good until the new "
            "candidate commits atomically; a failed or empty candidate does not "
            "advance the pointer."
        ),
    }


def _replay_one(
    operation_conn,  # noqa: ANN001
    *,
    raw_import_id: str,
    datastream_id: str,
    project_id: str,
    raw: dict[str, Any],
    quarantine_uri: str,
    message_id: str,
    actor: str,
    reason: str,
    target_mapping_version_id: str | None,
    store,  # noqa: ANN001
    trace_id: str | None,
    run_origin: str | None = None,
) -> dict[str, Any]:
    """Replay ONE retained file inside an open operation. THE ONLY BODY.

    ``run_origin`` is the one thing a Datastream-level caller knows that this
    body cannot infer: `core.datastream_reprocess` drives exactly this replay for
    the `Reprocess` verb and stamps `bounded_reprocess`, so the Runs tab can say
    why that execution exists. Absent for a file-level reprocess, which is what
    it has always been -- an operator repairing one delivery.

    A scope is not a second engine either. If replaying one file inside a scope
    went down a different path than replaying it alone, the two would drift on
    the exact thing the epic exists to prevent -- the scan that runs again, the
    channel that is read rather than assumed, the reason that survives -- and
    the drift would show up as a governed act behaving differently depending on
    how many files an operator ticked.

    Returns the three pieces the caller assembles into a ``MutationResult``:
    ``result`` (durable, read back on replay), ``after_hash_seed`` and
    ``outbox_payload``. It raises nothing of its own: a scan refusal is a RESULT,
    because the operation genuinely happened and its outcome was a refusal.
    """
    from core.inbound_ingest import (  # noqa: PLC0415
        ingest_inbound_file,
        resolve_dispatch_bundle_for_acceptance,
        resolve_dispatch_bundle_for_target,
    )
    from core.inbound_scan import scan_bytes  # noqa: PLC0415

    data = _open_store(store).get(quarantine_uri)

    # The scan runs again rather than trusting the verdict stored at receipt:
    # the bounds are POLICY, policy changes, and a file accepted under last
    # month's limits must not bypass this month's.
    verdict = scan_bytes(data, declared_type=raw.get("media_type_declared"))
    if not verdict.accepted:
        return {
            "after_hash_seed": {
                "raw_import_id": raw_import_id,
                "refused": verdict.reason,
            },
            "result": {
                "status": "rejected",
                "raw_import_id": raw_import_id,
                "error_code": verdict.reason,
                "import_ledger_id": None,
                # Meme regle sur la branche de refus : un rejeu refuse garde
                # la raison pour laquelle on l'avait demande.
                "reason": reason,
                "message_id": message_id,
            },
            "outbox_payload": {
                "datastream_id": datastream_id,
                "raw_import_id": raw_import_id,
                "status": "rejected",
                "error_code": verdict.reason,
            },
        }

    # LA VERSION CHOISIE, OU CELLE EN VIGUEUR. Les deux passent par le meme
    # `_resolve_server_dispatch_bundle`, donc un rejeu sous une version
    # ancienne est gouverne exactement comme une livraison d'aujourd'hui --
    # ce n'est pas un chemin de secours a cote du chemin normal.
    current_bundle = (
        resolve_dispatch_bundle_for_target(
            operation_conn,
            datastream_id=datastream_id,
            project_id=project_id,
            mapping_version_id=target_mapping_version_id,
        )
        if target_mapping_version_id
        else resolve_dispatch_bundle_for_acceptance(
            operation_conn,
            datastream_id=datastream_id,
            project_id=project_id,
        )
    )
    result = ingest_inbound_file(
        operation_conn,
        datastream_id=datastream_id,
        project_id=project_id,
        file_bytes=data,
        filename=raw.get("filename"),
        # The channel is the one the bytes originally arrived on: a reprocess
        # does not invent a new transport, and the Datastream's channel
        # allowlist must still be satisfied.
        channel=_origin_channel(operation_conn, raw_import_id=raw_import_id),
        message_id=message_id,
        actor=actor,
        trace_id=trace_id,
        raw_import_id=raw_import_id,
        dispatch_bundle_override=current_bundle,
        # A reprocess NEVER no-ops on an unchanged snapshot: the bytes are
        # unchanged by definition -- that is what "reprocess" means -- and the
        # act being repaired is downstream of them.
        force_new_execution=True,
        run_origin=run_origin,
    )

    ledger = result.get("ledger")
    ledger_id = ledger.get("id") if isinstance(ledger, dict) else None
    status = "rejected" if result.get("blocked") else "landed"
    # CE QUI ARRIVE A L'OPERATEUR DOIT ETRE ACTIONNABLE, PAS UN CODE SEUL.
    # `run_import` construit un `repair` a l'endroit exact du refus --
    # << ajoute des lignes >>, << rebranche le Template >>, << confirme le
    # contrat >>. Un rejeu qui rend seulement `empty_import_blocked` laisse
    # la personne devant le meme fichier sans savoir quoi en faire.
    repair = result.get("repair") if isinstance(result.get("repair"), dict) else None

    return {
        "after_hash_seed": {
            "raw_import_id": raw_import_id,
            "import_ledger_id": ledger_id,
        },
        "result": {
            "status": status,
            "raw_import_id": raw_import_id,
            "import_ledger_id": ledger_id,
            "error_code": result.get("reason"),
            "repair": repair,
            # LA RAISON EST EXIGEE, PUIS ELLE N'ETAIT NULLE PART. Le module
            # refuse un rejeu sans raison et la met dans le `request_payload`,
            # mais `execute_operation` ne stocke que le `request_hash` et ecrit
            # `'{}'` en metadata d'audit : aucune ligne ne portait la PHRASE.
            # Exiger une justification puis la jeter, c'est demander a
            # quelqu'un d'ecrire pour personne. `app.operations.result` est
            # durable et relu au rejeu -- c'est la que la raison de CE rejeu
            # appartient.
            "reason": reason,
            "message_id": message_id,
        },
        "outbox_payload": {
            "datastream_id": datastream_id,
            "raw_import_id": raw_import_id,
            "status": status,
            "import_ledger_id": ledger_id,
        },
    }


def execute_reprocess(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    actor: str,
    idempotency_key: str,
    reason: str,
    target_mapping_version_id: str | None = None,
    store=None,  # noqa: ANN001
    trace_id: str | None = None,
    host_context: dict | None = None,
) -> dict[str, Any]:
    """Re-run one retained file through the delivery pipeline, as a NEW execution.

    Raises ``ReprocessUnavailable`` when the retained evidence cannot support it
    -- with a stable ``code``, so the refusal is renderable rather than a string
    to parse. Raises ``ReprocessValidationError`` on a malformed request.

    The whole write goes through ``operations.execute_operation``: audit and
    outbox commit with the effect, and a retry on the same idempotency key
    returns the original result instead of importing twice.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ReprocessValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ReprocessValidationError("idempotency_key is required")
    if not isinstance(reason, str) or not reason.strip():
        # A reprocess without a stated reason is an unexplained change to
        # published data. The audit trail is worth one sentence.
        raise ReprocessValidationError("reason is required")

    availability = evaluate_reprocess(
        conn, raw_import_id=raw_import_id, datastream_id=datastream_id, store=store
    )
    if not availability.available:
        raise ReprocessUnavailable(
            availability.reason or "unavailable",
            availability.detail or "the retained evidence cannot be reprocessed",
        )

    from core.inbound_raw_imports import (  # noqa: PLC0415
        _resolve_org_id,
        get_raw_import,
    )
    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415

    raw = get_raw_import(
        conn, raw_import_id=raw_import_id, datastream_id=datastream_id
    )
    quarantine_uri = raw["quarantine_uri"]
    org_id = _resolve_org_id(conn, datastream_id=datastream_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM app.datastreams WHERE id = %s", (datastream_id,)
        )
        project_row = cur.fetchone()
    if project_row is None or not project_row[0]:
        raise ReprocessUnavailable(
            UNAVAILABLE_NOT_FOUND, "the Datastream could not be resolved"
        )
    project_id = project_row[0]

    # THE MESSAGE ID IS DERIVED FROM THE IDEMPOTENCY KEY, and that is the whole
    # of AC5. `run_import` is idempotent on it, so:
    #   - the same confirmed operation replayed -> the same message_id -> the
    #     original import is returned, not a second one;
    #   - a genuinely new reprocess -> a new key -> a new message_id -> a new
    #     execution, which is what a reprocess is.
    # Reusing the ORIGINAL delivery's message_id would have made every reprocess
    # a silent no-op that looked like a success.
    attempt = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    message_id = f"reprocess:{raw_import_id}:{attempt}"

    # LA VERSION DE TEMPLATE VOYAGE AVEC L'ACTE, comme la version de mapping.
    # Elle n'est pas choisie ici -- c'est l'epingle du Datastream -- mais deux
    # rejeux du meme fichier sous deux epingles differentes sont deux actes
    # differents, et une trace qui ne les distingue pas ne repond pas a la seule
    # question qu'on lui posera. Elle porte son `state`, jamais un `null` nu.
    from core.inbound_ingest import read_reprocess_template_version  # noqa: PLC0415

    template_binding = read_reprocess_template_version(
        conn, datastream_id=datastream_id, project_id=project_id
    )

    spec = OperationSpec(
        command_type="inbound.raw_import.reprocessed",
        actor=actor.strip(),
        effective_org_id=org_id,
        resource_path=(
            f"datastream:{datastream_id}",
            f"raw_import:{raw_import_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "inbound-reprocess-v1",
            "catalog": "inbound-reprocess-v1",
            "tool": "rest-v1",
        },
        # The content hash identifies the payload exactly; no byte of it, and no
        # attacker-controlled filename, enters the audit trail.
        request_payload={
            "raw_import_id": raw_import_id,
            "datastream_id": datastream_id,
            "content_hash": raw.get("content_hash"),
            "reason": reason.strip(),
            # LA VERSION CHOISIE EST UNE PARTIE DE L'ACTE, pas un detail
            # d'execution. Deux rejeux du meme fichier sous deux versions
            # differentes sont deux actes differents, et une trace qui ne les
            # distingue pas ne repond pas a << pourquoi ces chiffres ont change >>.
            "target_mapping_version_id": target_mapping_version_id,
            "template_version": template_binding,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-reprocess:{raw_import_id}:{attempt}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        replay = _replay_one(
            operation_conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            project_id=project_id,
            raw=raw,
            quarantine_uri=quarantine_uri,
            message_id=message_id,
            actor=actor,
            reason=reason,
            target_mapping_version_id=target_mapping_version_id,
            store=store,
            trace_id=trace_id,
        )
        result = dict(replay["result"])
        result["template_version"] = template_binding
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(replay["after_hash_seed"]),
            result=result,
            outbox_payload=replay["outbox_payload"],
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    out = dict(op_result.result or {})
    out["operation_id"] = op_result.operation_id
    # True when the operation seam replayed rather than re-ran (AC5).
    out["replayed"] = bool(getattr(op_result, "replayed", False))
    return out


# ---------------------------------------------------------------------------
# THE GOVERNED SCOPE -- 38.18 AC1, << and a governed scope >>.
#
# The ratified pattern is not invented here. It is in
# `docs/product-architecture/datastream-workbench-and-wizard.md`, verbatim
# (measured 2026-08-10 at `:2191`; the line number MOVES -- the same sentence
# sat at `:2130` two commits earlier this session, so the sentence is the
# citation and the number is only where it was found):
#
#   << Every destructive or durable repair follows `Prepare > Review exact scope
#      and consequences > Confirm > Execute as a new durable operation`. >>
#
# and the same document fixes what "review exact scope" means in practice, on
# the Mapping row of its surface table (measured 2026-08-10 at `:1080`):
# << ... avec le compte AVANT l'acte ("N of 46 columns stop landing") >>.
#
# So a governed scope is four things, and dropping any one of them turns it back
# into a bulk button:
#
#   1. THE OPERATOR SAYS WHAT. Either an explicit set of retained files, or a
#      bounded criterion that the preparation RESOLVES to an explicit set. The
#      confirmation is always bound to the enumerated objects, never to the
#      criterion -- a criterion re-evaluated at commit time could name a
#      different set than the one a person read.
#   2. THE COUNT COMES BEFORE THE ACT, with the objects it names. Not "12
#      deliveries" alone: the twelve, each with its filename, its hash and
#      whether it can be replayed at all.
#   3. THE BOUND IS DECLARED AND SAID. `scan_truncated` is the repository's own
#      word for it -- `core/context_search.py` carries it on its own bounded
#      superset scan. A silent truncation would let
#      someone believe a set was replayed that never was.
#   4. ONE DURABLE OPERATION CARRIES THE SCOPE. Not N silent ones: N operations
#      would make "what did that repair do" unanswerable, and a retry would
#      replay some members and not others.
# ---------------------------------------------------------------------------

_CRITERION_KEYS = frozenset({"received_from", "received_to", "states"})


def _validate_criterion(criterion: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on anything the criterion language does not declare.

    An unrecognised key silently ignored would widen a scope without saying so,
    which is the one thing a governed scope may not do.
    """
    if not isinstance(criterion, dict):
        raise ReprocessValidationError("criterion must be an object")
    unknown = sorted(set(criterion) - _CRITERION_KEYS)
    if unknown:
        raise ReprocessValidationError(
            "unsupported criterion keys: " + ", ".join(unknown)
        )
    out: dict[str, Any] = {}
    for key in ("received_from", "received_to"):
        value = criterion.get(key)
        if value in (None, ""):
            continue
        import datetime as _dt  # noqa: PLC0415

        try:
            _dt.datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ReprocessValidationError(
                f"{key} must be an ISO-8601 timestamp"
            ) from exc
        out[key] = str(value)
    states = criterion.get("states")
    if states not in (None, [], ()):
        if not isinstance(states, (list, tuple)) or not all(
            isinstance(s, str) for s in states
        ):
            raise ReprocessValidationError("states must be a list of strings")
        from core.inbound_raw_imports import RAW_IMPORT_STATES  # noqa: PLC0415

        unknown_states = sorted({s for s in states} - set(RAW_IMPORT_STATES))
        if unknown_states:
            raise ReprocessValidationError(
                "unknown raw-import states: " + ", ".join(unknown_states)
            )
        out["states"] = [str(s) for s in states]
    if not out:
        raise ReprocessValidationError(
            "a criterion must bound the scope by at least one of "
            "received_from, received_to or states"
        )
    return out


def _scope_rows(
    conn,
    *,
    datastream_id: str,
    raw_import_ids: list[str] | None,
    criterion: dict[str, Any] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """The candidate rows and whether the declared bound cut the set short.

    Scoped by ``datastream_id`` in the WHERE clause, exactly as
    ``inbound_raw_imports._load_row`` is: an identifier from a neighbouring
    Datastream reads as absent rather than as a refusal that would confirm it
    exists.

    The bound is fetched as ``limit + 1`` rows so truncation is OBSERVED rather
    than guessed from a full page -- a set of exactly ``limit`` members is not
    truncated, and saying it was would be its own small lie.
    """
    clauses = ["datastream_id = %s"]
    params: list[Any] = [datastream_id]
    if raw_import_ids is not None:
        clauses.append("id = ANY(%s)")
        params.append(list(raw_import_ids))
    if criterion:
        if criterion.get("received_from"):
            clauses.append("created_at >= %s")
            params.append(criterion["received_from"])
        if criterion.get("received_to"):
            clauses.append("created_at <= %s")
            params.append(criterion["received_to"])
        if criterion.get("states"):
            clauses.append("state = ANY(%s)")
            params.append(list(criterion["states"]))
    params.append(int(limit) + 1)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, filename, content_hash, size_bytes, state, created_at "
            "FROM app.inbound_raw_imports WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, ordinal ASC LIMIT %s",
            tuple(params),
        )
        rows = cur.fetchall() or []

    truncated = len(rows) > int(limit)
    rows = list(rows)[: int(limit)]
    return (
        [
            {
                "raw_import_id": row[0],
                "filename": row[1],
                "content_hash": row[2],
                "size_bytes": row[3],
                "state": row[4],
                "created_at": (
                    row[5].isoformat() if hasattr(row[5], "isoformat") else row[5]
                ),
            }
            for row in rows
        ],
        truncated,
    )


def prepare_reprocess_scope(
    conn,
    *,
    datastream_id: str,
    raw_import_ids: list[str] | None = None,
    criterion: dict[str, Any] | None = None,
    target_mapping_version_id: str | None = None,
    store=None,  # noqa: ANN001
    limit: int = MAX_SCOPE_MEMBERS,
) -> dict[str, Any]:
    """The scope an operator reviews before confirming -- the count, and the objects.

    Writes nothing. Reads and re-hashes every candidate object, for the same
    reason ``prepare_reprocess`` does: a proposal that promises twelve replays
    and an execution that then fails on the fourth is worse than a refusal,
    because a person already decided.

    The result always enumerates its members. A bare count would let the screen
    say "12 deliveries" while the operator cannot tell WHICH twelve -- and the
    ratified pattern is the count WITH the objects it names.
    """
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise ReprocessValidationError("datastream_id is required")
    if not isinstance(limit, int) or limit < 1 or limit > MAX_SCOPE_MEMBERS:
        raise ReprocessValidationError(
            f"limit must be an integer in 1..{MAX_SCOPE_MEMBERS}"
        )

    explicit = raw_import_ids is not None
    if explicit and criterion:
        # Two selections in one request is an ambiguity, and resolving it
        # silently either way would replay a set nobody asked for.
        raise ReprocessValidationError(
            "give either raw_import_ids or a criterion, not both"
        )
    if not explicit and not criterion:
        raise ReprocessValidationError(
            "a scope needs either raw_import_ids or a bounded criterion"
        )
    if explicit:
        if not isinstance(raw_import_ids, (list, tuple)) or not raw_import_ids:
            raise ReprocessValidationError("raw_import_ids must be a non-empty list")
        if len(set(raw_import_ids)) != len(raw_import_ids):
            raise ReprocessValidationError("raw_import_ids contains duplicates")
        if len(raw_import_ids) > MAX_SCOPE_MEMBERS:
            raise ReprocessValidationError(
                f"a scope is bounded at {MAX_SCOPE_MEMBERS} retained files"
            )
        raw_import_ids = [str(i).strip() for i in raw_import_ids]

    normalised = _validate_criterion(criterion) if criterion else None
    rows, truncated = _scope_rows(
        conn,
        datastream_id=datastream_id,
        raw_import_ids=list(raw_import_ids) if explicit else None,
        criterion=normalised,
        limit=limit,
    )

    members: list[dict[str, Any]] = []
    for row in rows:
        availability = evaluate_reprocess(
            conn,
            raw_import_id=row["raw_import_id"],
            datastream_id=datastream_id,
            store=store,
        )
        members.append(
            {
                **row,
                "available": availability.available,
                "reason": availability.reason,
                "detail": availability.detail,
            }
        )

    # A REQUESTED ID THAT NO ROW MATCHED IS NOT SILENTLY DROPPED. It reads as
    # `raw_import_not_found` -- the same answer a foreign identifier gets, so a
    # scope cannot be used to enumerate what exists in a neighbouring Datastream.
    if explicit:
        seen = {m["raw_import_id"] for m in members}
        for missing in [i for i in (raw_import_ids or []) if i not in seen]:
            members.append(
                {
                    "raw_import_id": missing,
                    "filename": None,
                    "content_hash": None,
                    "size_bytes": None,
                    "state": None,
                    "created_at": None,
                    "available": False,
                    "reason": UNAVAILABLE_NOT_FOUND,
                    "detail": None,
                }
            )

    reprocessable = [m for m in members if m["available"]]
    plan_version_id = mapping_version_id = None
    target_error = None
    available_versions: list[dict[str, Any]] = []
    template_version: dict[str, Any] = {
        "state": "unknown",
        "reason": "the Datastream project could not be resolved",
    }
    available_template_versions: list[dict[str, Any]] = []
    project_id = _project_of(conn, datastream_id=datastream_id)
    if project_id:
        from core.inbound_ingest import (  # noqa: PLC0415
            DatastreamNotIngestable,
            list_reprocess_target_versions,
            resolve_dispatch_bundle_for_target,
        )

        available_versions = list_reprocess_target_versions(
            conn, datastream_id=datastream_id, project_id=project_id
        )
        template_version, available_template_versions = _template_facts(
            conn, datastream_id=datastream_id, project_id=project_id
        )
        if target_mapping_version_id:
            try:
                bundle = resolve_dispatch_bundle_for_target(
                    conn,
                    datastream_id=datastream_id,
                    project_id=project_id,
                    mapping_version_id=target_mapping_version_id,
                )
                mapping_version_id = target_mapping_version_id
                plan_version_id = bundle.get("plan_version_id")
            except DatastreamNotIngestable as exc:
                target_error = str(exc)
        if mapping_version_id is None and target_error is None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_plan_version_id, current_mapping_version_id "
                    "FROM app.datastreams WHERE id = %s",
                    (datastream_id,),
                )
                row = cur.fetchone()
            if row:
                plan_version_id, mapping_version_id = row

    will_run = bool(reprocessable) and target_error is None
    return {
        "schema": "inbound-reprocess-scope-v1",
        "datastream_id": datastream_id,
        "selection": {
            "mode": "explicit" if explicit else "criterion",
            "criterion": normalised,
            # The confirmation is bound to THESE ids, not to the criterion.
            "confirm_raw_import_ids": [m["raw_import_id"] for m in reprocessable],
        },
        # LE COMPTE AVANT L'ACTE. `counted` rather than a bare integer, for the
        # same reason every other count in this product carries its state: a `0`
        # that means "none matched" and a `0` that means "the store would not
        # answer" are two different sentences.
        "scope": {
            "state": "counted",
            "examined": len(members),
            "reprocessable": len(reprocessable),
            "refused": len(members) - len(reprocessable),
        },
        "members": members,
        # LA BORNE EST DITE. Jamais une troncature muette.
        "scan_truncated": truncated,
        "scan_limit": int(limit),
        "bound_versions": {
            "plan_version_id": plan_version_id,
            "mapping_version_id": mapping_version_id,
        },
        "target_selected": bool(target_mapping_version_id) and target_error is None,
        "target_refused": target_error,
        "available_versions": available_versions,
        "template_version": template_version,
        "available_template_versions": available_template_versions,
        "creates_new_execution": will_run,
        "may_move_published_pointer": will_run,
        "write_mode": "replace" if will_run else None,
        "mutates_prior_execution": False,
        "requires_provider_call": False,
        "rollback": (
            "The prior publication remains the last known good until each new "
            "candidate commits atomically; a failed or empty candidate does not "
            "advance the pointer."
        ),
    }


def execute_reprocess_scope(
    conn,
    *,
    datastream_id: str,
    raw_import_ids: list[str],
    actor: str,
    idempotency_key: str,
    reason: str,
    target_mapping_version_id: str | None = None,
    store=None,  # noqa: ANN001
    trace_id: str | None = None,
    host_context: dict | None = None,
) -> dict[str, Any]:
    """Replay a NAMED scope as ONE durable operation.

    The scope is the enumerated set the operator confirmed, never a criterion
    re-evaluated here.

    THE COUNT IS BINDING, and that is what makes the count-before-the-act more
    than decoration. If any confirmed member is no longer reprocessable, the
    whole scope refuses with ``scope_moved`` and nothing is replayed: executing
    eleven of the twelve a person confirmed would make the number they read a
    lie, and they would have no way to learn it.

    Raises ``ReprocessValidationError`` on a malformed request and
    ``ReprocessUnavailable`` when the scope moved.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ReprocessValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ReprocessValidationError("idempotency_key is required")
    if not isinstance(reason, str) or not reason.strip():
        # Same rule as the single replay: a scope without a stated reason is an
        # unexplained change to published data, N times over.
        raise ReprocessValidationError("reason is required")
    if not isinstance(raw_import_ids, (list, tuple)) or not raw_import_ids:
        raise ReprocessValidationError("raw_import_ids must be a non-empty list")
    ids = [str(i).strip() for i in raw_import_ids]
    if any(not i for i in ids):
        raise ReprocessValidationError("raw_import_ids contains a blank identifier")
    if len(set(ids)) != len(ids):
        # A duplicate would replay one file twice inside a scope whose count a
        # person already read.
        raise ReprocessValidationError("raw_import_ids contains duplicates")
    if len(ids) > MAX_SCOPE_MEMBERS:
        raise ReprocessValidationError(
            f"a scope is bounded at {MAX_SCOPE_MEMBERS} retained files"
        )

    unavailable: list[dict[str, Any]] = []
    for raw_import_id in ids:
        availability = evaluate_reprocess(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            store=store,
        )
        if not availability.available:
            unavailable.append(
                {
                    "raw_import_id": raw_import_id,
                    "reason": availability.reason,
                    "detail": availability.detail,
                }
            )
    if unavailable:
        refusal = ReprocessUnavailable(
            UNAVAILABLE_SCOPE_MOVED,
            f"{len(unavailable)} of {len(ids)} confirmed files can no longer be "
            "reprocessed; nothing was replayed. Prepare the scope again to see "
            "the current count.",
        )
        refusal.members = unavailable  # type: ignore[attr-defined]
        raise refusal

    from core.inbound_raw_imports import (  # noqa: PLC0415
        _resolve_org_id,
        get_raw_import,
    )
    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415

    org_id = _resolve_org_id(conn, datastream_id=datastream_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM app.datastreams WHERE id = %s", (datastream_id,)
        )
        project_row = cur.fetchone()
    if project_row is None or not project_row[0]:
        raise ReprocessUnavailable(
            UNAVAILABLE_NOT_FOUND, "the Datastream could not be resolved"
        )
    project_id = project_row[0]

    raws = {
        raw_import_id: get_raw_import(
            conn, raw_import_id=raw_import_id, datastream_id=datastream_id
        )
        for raw_import_id in ids
    }

    attempt = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    # The scope's own stable name: the sorted members, hashed. Two confirmations
    # of the same set address the same scope; adding one file makes a different
    # one, which is exactly what it should be.
    scope_hash = hashlib.sha256(
        "\n".join(sorted(ids)).encode("utf-8")
    ).hexdigest()[:16]

    from core.inbound_ingest import read_reprocess_template_version  # noqa: PLC0415

    template_binding = read_reprocess_template_version(
        conn, datastream_id=datastream_id, project_id=project_id
    )

    spec = OperationSpec(
        command_type="inbound.raw_import.scope_reprocessed",
        actor=actor.strip(),
        effective_org_id=org_id,
        resource_path=(
            f"datastream:{datastream_id}",
            f"raw_import_scope:{scope_hash}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "inbound-reprocess-scope-v1",
            "catalog": "inbound-reprocess-scope-v1",
            "tool": "rest-v1",
        },
        request_payload={
            "datastream_id": datastream_id,
            # The members are named IN the audited request, not summarised by a
            # count: "twelve files" is not an answer to "which twelve".
            "raw_import_ids": sorted(ids),
            "scope_size": len(ids),
            "reason": reason.strip(),
            "target_mapping_version_id": target_mapping_version_id,
            "template_version": template_binding,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-reprocess-scope:{scope_hash}:{attempt}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        outcomes: list[dict[str, Any]] = []
        seeds: list[dict[str, Any]] = []
        for raw_import_id in ids:
            raw = raws[raw_import_id] or {}
            replay = _replay_one(
                operation_conn,
                raw_import_id=raw_import_id,
                datastream_id=datastream_id,
                project_id=project_id,
                raw=raw,
                quarantine_uri=raw.get("quarantine_uri"),
                # Same derivation as the single replay, and the raw import is
                # already in the identifier -- so two members of one scope
                # cannot collide, and a retried scope replays each member's
                # original import instead of creating a second.
                message_id=(
                    f"reprocess:{raw_import_id}:"
                    + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
                ),
                actor=actor,
                reason=reason,
                target_mapping_version_id=target_mapping_version_id,
                store=store,
                trace_id=trace_id,
            )
            outcomes.append(replay["result"])
            seeds.append(replay["after_hash_seed"])

        landed = [o for o in outcomes if o.get("status") == "landed"]
        if len(landed) == len(outcomes):
            status = "landed"
        elif landed:
            # THREE WORDS, NOT TWO. "partial" is its own outcome: reporting it
            # as landed hides the members that refused, and reporting it as
            # rejected hides the ones that published.
            status = "partial"
        else:
            status = "rejected"

        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash({"scope": scope_hash, "members": seeds}),
            result={
                "status": status,
                "datastream_id": datastream_id,
                "scope": {
                    "state": "counted",
                    "confirmed": len(ids),
                    "landed": len(landed),
                    "refused": len(outcomes) - len(landed),
                },
                "members": outcomes,
                "reason": reason,
                "target_mapping_version_id": target_mapping_version_id,
                "template_version": template_binding,
            },
            outbox_payload={
                "datastream_id": datastream_id,
                "scope": scope_hash,
                "status": status,
                "landed": len(landed),
                "confirmed": len(ids),
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    out = dict(op_result.result or {})
    out["operation_id"] = op_result.operation_id
    out["replayed"] = bool(getattr(op_result, "replayed", False))
    return out


def _origin_channel(conn, *, raw_import_id: str) -> str:
    """The channel the bytes originally arrived on.

    Read from the receipt rather than assumed, because the Datastream's channel
    allowlist is checked again on the way through `ingest_inbound_file`: a
    reprocess of a webhook file must not slip in as an email one, and vice
    versa. Falls back to 'email' only if the join yields nothing, which cannot
    happen while the FK holds -- and if it somehow did, the allowlist check
    downstream is what refuses.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rx.channel FROM app.inbound_raw_imports r "
            "JOIN app.inbound_receipts rx ON rx.id = r.receipt_id "
            "WHERE r.id = %s",
            (raw_import_id,),
        )
        row = cur.fetchone()
    return (row[0] if row and row[0] else "email")
