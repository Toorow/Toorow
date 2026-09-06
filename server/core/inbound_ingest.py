"""toorow -- Inbound ingress worker: server-driven managed-feed file ingestion.

Ingress parity keystone. A file delivered by an inbound transport (opaque
``channel`` data such as 'email' or 'webhook') for a managed-feed Datastream is
driven through the EXACT SAME import pipeline as a direct upload, so an inbound
file and a direct upload of the same bytes produce IDENTICAL results (same
ledger row shape, same execution candidate, same DQ/rejection gates).

This module is PURE ORCHESTRATION given resolved inputs:
    (datastream_id, project_id, file bytes, filename, channel, message_id, actor).
The transport wiring (delivery, quarantine, attachment extraction) is OUT of
scope -- it is the caller's concern. This function receives the already-extracted
bytes and drives ``csv_excel_import.run_import`` with the SAME arguments the
direct-upload route (``admin_api._confirm_csv_excel_import``) resolves, but
WITHOUT operator interaction: the plan version, mapping version, projection plan,
and parsing contract are all resolved from durable server-side state.

Design invariants:
  - AD-2: source-agnostic throughout. NO transport/vendor vocabulary appears in
    code, comments, or docstrings. ``channel`` is opaque data validated only
    against the Datastream's own ``config.channels`` allowlist -- this module
    never special-cases any particular channel value.
  - Fail-closed: an inbound file is UNATTENDED, so every precondition that a human
    would have satisfied interactively (correct source_kind, enabled Datastream,
    a locked plan + mapping, an allowed channel) is asserted up front and raises a
    typed error before ANY parsing or ledger write.
  - Parity: the pipeline inputs (plan_version_id, mapping_version_id,
    projection_plan, contract, write_mode, preferences) are resolved to be
    IDENTICAL to what the direct-upload path passes, so ``run_import`` cannot tell
    an inbound file from an upload.
  - Transaction boundaries: the caller owns the delivery transaction, while
    run_import deliberately commits durable dispatch phases around cross-store
    landing/promotion. Those commits are part of the reconciliation protocol.
  - run_import's own typed exceptions (ImportPayloadConflict, ImportInProgress,
    CsvExcelImportError, AppendUnavailable, InvalidImportContract) propagate
    UNCHANGED to the caller so the inbound worker maps them the same way the
    direct-upload route does.

ASCII-only source (AI-03). No private framework attributes (AI-02). Lazy imports
inside function bodies (no import cycle with core.main), matching the
import_templates.py / managed_feed_ledger.py style.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Source-agnostic identifiers used in idempotency + provenance metadata.
_SOURCE_KIND_MANAGED_FEED = "managed_feed"
_WRITE_MODE_REPLACE = "replace"
_DIRECT_UPLOAD_CHANNEL = "upload"


# ---------------------------------------------------------------------------
# Custom exceptions (fail-closed preconditions).
# ---------------------------------------------------------------------------


class InboundIngestValidationError(ValueError):
    """An input to the inbound worker is malformed (missing bytes, channel, etc.).

    A ValueError subclass so callers that already map 4xx off ValueError keep
    working; distinct type so the inbound worker's own guards are separable.
    """


class DatastreamNotIngestable(RuntimeError):
    """The Datastream cannot accept an unattended inbound import (fail closed).

    Raised when the Datastream is not found, is not a managed_feed, is disabled,
    does not permit the requested channel, or is not yet configured (missing a
    locked plan or mapping version). No parsing or ledger write is attempted.
    """


class SenderNotAllowed(DatastreamNotIngestable):
    """The delivery's sender is not in the Datastream's DECLARED allowlist.

    A subclass, so every caller that already catches `DatastreamNotIngestable`
    refuses this too rather than letting one through by omission. It carries its
    own `code` because "DatastreamNotIngestable" on a receipt would send the
    owner looking at the Datastream's configuration instead of at who sent the
    file (story 57.3).
    """

    code = "sender_not_allowed"


class InboundContractReviewRequired(DatastreamNotIngestable):
    """Raw evidence must remain quarantined until one contract is confirmed."""

    code = "inbound_contract_review_required"


# ---------------------------------------------------------------------------
# Internal read helpers.
# ---------------------------------------------------------------------------


def _load_ingestable_datastream(
    conn,
    *,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Read the Datastream's ingestion-relevant fields, scoped to project_id.

    Returns a dict with source_kind, enabled, config, current_plan_version_id,
    current_mapping_version_id. Returns None when the row does not exist for this
    (id, project) pair (AD-5: cross-project reads return not-found, not a leak).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_kind, enabled, config,
                   current_plan_version_id, current_mapping_version_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "source_kind": row[0],
        "enabled": row[1],
        "config": row[2] or {},
        "current_plan_version_id": row[3],
        "current_mapping_version_id": row[4],
    }


def _fetch_projection_plan(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the executable projection plan for the current plan version.

    PARITY SOURCE: the direct-upload route does not persist a standalone
    executable projection on the Datastream -- the operator UI compiles it fresh
    (POST /projection/compile) and posts it in the request body. The ONLY durable
    executable projection is the one recorded on the most recent
    ``app.datastream_executions`` row for the current plan version
    (``projection_plan_ref``). Reusing that exact blob guarantees the inbound path
    drives ``run_import`` with the SAME projection a prior direct upload used for
    this plan version -- true ingress parity, no re-compilation (which would need
    operator-only inputs: the elected dimension_projection + the source's current
    canonical breakdown).

    Returns the projection dict, or None when no prior execution exists for the
    current plan version (an un-imported, freshly-configured Datastream). The
    caller fails closed on None.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT projection_plan_ref
            FROM app.datastream_executions
            WHERE datastream_id = %s
              AND project_id = %s
              AND plan_version_id = %s
              -- `::text`, jamais un placeholder IS NULL nu : psycopg 3 envoie une str
              -- (et un `%%s` ecrit dans CE commentaire compte comme un placeholder :
              -- << the query has 6 placeholders but 5 parameters >>, AI-321, 2026-08-29)
              -- ET un None sans type, `$n IS NULL` ne donne aucun contexte au
              -- serveur, et la requete entiere tombe en IndeterminateDatatype.
              -- Mesure 2026-08-04 sur cluster jetable (str -> erreur, None ->
              -- erreur, int/float -> OK). Cf. AI-186.
              AND (%s::text IS NULL OR mapping_version_id = %s)
              AND projection_plan_ref IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                datastream_id,
                project_id,
                plan_version_id,
                mapping_version_id,
                mapping_version_id,
            ),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    raw = row[0]
    if isinstance(raw, dict):
        return raw
    import json  # noqa: PLC0415

    return json.loads(raw)


def _fetch_active_parsing_contract(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    include_evidence: bool = False,
) -> dict[str, Any]:
    """Return the sole active, immutable and explicitly confirmed contract.

    The count is computed in the same snapshot as the row. Zero or multiple
    active choices are review states, never an auto-detection or newest-wins rule.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.contract, c.id, c.fingerprint, confirmation.confirmed_by,
                   confirmation.confirmed_at, COUNT(*) OVER ()
            FROM app.csv_excel_import_contracts c
            LEFT JOIN app.csv_excel_import_contract_confirmations confirmation
              ON confirmation.contract_id = c.id
            WHERE c.datastream_id = %s
              AND c.project_id = %s
              AND c.is_active = TRUE
            ORDER BY c.created_at DESC
            LIMIT 2
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise InboundContractReviewRequired(
            "No active confirmed parsing contract; retain the raw file for review."
        )
    # Compatibility for offline pre-192 fakes; real rows always carry six fields.
    if len(row) == 1:
        raw = row[0]
        contract_id = None
        fingerprint = None
        confirmed_by = "legacy_test_fixture"
        confirmed_at = "legacy_test_fixture"
    else:
        raw, contract_id, fingerprint, confirmed_by, confirmed_at, active_count = row
        if active_count != 1 or not confirmed_by or confirmed_at is None:
            raise InboundContractReviewRequired(
                "Parsing contract selection is ambiguous or unconfirmed; retain raw evidence."
            )
        if not contract_id or not fingerprint:
            raise InboundContractReviewRequired("Parsing contract identity is incomplete.")
    if not isinstance(raw, dict):
        import json  # noqa: PLC0415

        raw = json.loads(raw)
    if include_evidence:
        return {
            "contract": raw,
            "contract_id": contract_id,
            "contract_fingerprint": fingerprint,
            "confirmed_by": confirmed_by,
            "confirmed_at": (
                confirmed_at.isoformat()
                if hasattr(confirmed_at, "isoformat")
                else str(confirmed_at)
            ),
        }
    return raw


def _fetch_mapping_bundle(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
) -> dict[str, Any]:
    """Resolve one coherent immutable plan and mapping bundle, fail closed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.mapping_payload, m.content_hash, m.source_schema_hash,
                   m.capability_fingerprint, p.content_hash,
                   p.capability_fingerprint, p.contract_version
            FROM app.datastream_mapping_versions m
            JOIN app.datastream_plan_versions p
              ON p.id = m.plan_version_id
             AND p.datastream_id = m.datastream_id
             AND p.project_id = m.project_id
            WHERE m.id = %s
              AND m.plan_version_id = %s
              AND m.datastream_id = %s
              AND m.project_id = %s
              AND m.executable = TRUE
              AND m.blocking_count = 0
              AND p.executable = TRUE
            """,
            (
                mapping_version_id,
                plan_version_id,
                datastream_id,
                project_id,
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise DatastreamNotIngestable(
            "the pinned plan and mapping do not form one executable scoped bundle"
        )
    payload = row[0]
    if not isinstance(payload, dict):
        import json  # noqa: PLC0415

        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise DatastreamNotIngestable(
                "the pinned mapping payload is not a valid immutable document"
            ) from exc
    if not isinstance(payload, dict) or not payload.get("fields"):
        raise DatastreamNotIngestable(
            "the pinned mapping payload has no executable field bindings"
        )
    # THE MANDATORY EVIDENCE is the two content hashes and the contract version.
    # The two CAPABILITY fingerprints are the connector's (`datastream_intents.
    # _validate_connector` computes one for a `connector_pull` plan and NOTHING
    # for a managed feed, which has no connector capability to fingerprint), so
    # they are None on every file Datastream the assistant materialises -- 24 of
    # the 102 plan versions in production on 2026-08-29. Demanding them here
    # refused every unattended import of every such Datastream, by
    # construction, as "incomplete fingerprint evidence" (AI-321, G9).
    mandatory = (row[1], row[2], row[4], row[6])
    if any(not isinstance(value, str) or not value.strip() for value in mandatory):
        raise DatastreamNotIngestable(
            "the pinned plan/mapping bundle has incomplete fingerprint evidence"
        )
    return {
        "mapping_payload": payload,
        "mapping_fingerprint": row[1],
        "source_schema_hash": row[2],
        "capability_fingerprint": row[3],
        "plan_fingerprint": row[4],
        "plan_capability_fingerprint": row[5],
        "plan_contract_version": row[6],
    }


def _resolve_server_dispatch_bundle(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
) -> dict[str, Any]:
    """Resolve one complete server-authoritative dispatch contract."""
    projection_plan = _fetch_projection_plan(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
    )
    if projection_plan is None:
        raise DatastreamNotIngestable(
            "the pinned versions have no executable projection evidence"
        )
    governed = _fetch_mapping_bundle(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
    )
    from core.csv_excel_import import resolve_file_source_producer  # noqa: PLC0415

    producer = resolve_file_source_producer(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        mapping_version_id=mapping_version_id,
    )
    parser: dict[str, Any] | None = None
    template: dict[str, Any] | None = None
    if producer is None:
        parser = _fetch_active_parsing_contract(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            include_evidence=True,
        )
    else:
        template = {
            "template": producer.template,
            "mapping": producer.mapping,
            "template_id": producer.template_id,
            "confirmation_operation_id": producer.confirmation_operation_id,
            "confirmation_evidence": producer.confirmation_evidence,
            # WHETHER THE CATALOG GOVERNS IT travels with the bundle (AI-321,
            # 2026-08-29): a catalog Template answers `confirmed_for` True by
            # construction, and the producer rebuilt from this frozen bundle
            # forgot the flag -- so every catalog-bound import was refused
            # `file_source_confirmation_required` one link after the resolver
            # had accepted it.
            "catalog_governed": bool(getattr(producer, "catalog_governed", False)),
        }
    return {
        "schema": "managed-file-dispatch-bundle-v1",
        "datastream_id": datastream_id,
        "project_id": project_id,
        "plan_version_id": plan_version_id,
        "mapping_version_id": mapping_version_id,
        "projection_plan": projection_plan,
        "mapping_payload": governed.pop("mapping_payload"),
        "governed_evidence": governed,
        "parser": parser,
        "template": template,
    }


def resolve_dispatch_bundle_for_acceptance(
    conn,
    *,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Resolve the current governed pins for the atomic ACCEPTED transition."""
    ds = _load_ingestable_datastream(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
    )
    if (
        ds is None
        or ds["source_kind"] != _SOURCE_KIND_MANAGED_FEED
        or not ds["enabled"]
    ):
        raise DatastreamNotIngestable(
            "the Datastream is not eligible for managed-file dispatch"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_plan_version_id, current_mapping_version_id
            FROM app.datastreams
            WHERE id = %s AND project_id = %s
            FOR SHARE
            """,
            (datastream_id, project_id),
        )
        pins = cur.fetchone()
    if pins is None:
        raise DatastreamNotIngestable(
            "the Datastream disappeared before raw acceptance"
        )
    plan_version_id, mapping_version_id = pins
    if not plan_version_id or not mapping_version_id:
        raise DatastreamNotIngestable(
            "the Datastream has no locked plan and mapping for unattended dispatch"
        )
    return _resolve_server_dispatch_bundle(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
    )


def resolve_dispatch_bundle_for_target(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    mapping_version_id: str,
) -> dict[str, Any]:
    """Resolve the governed pins for a CHOSEN mapping version -- 38.18 AC1.

    `resolve_dispatch_bundle_for_acceptance` reads `current_*` off the
    Datastream, which is exactly right for a delivery arriving now. A reprocess
    is the other case: its whole purpose is to replay a retained file under a
    DIFFERENT version -- the candidate just authored, or the one in force before
    a change that turned out wrong. Binding it to `current_*` made the feature
    answer only << run today's mapping again >>.

    The plan version is NOT a second parameter. It is read FROM the chosen
    mapping row (`plan_version_id`, migration 032), because the pair is what was
    validated together: letting a caller cross a mapping of one version with the
    plan of another would produce a combination nothing ever reviewed.

    Fails closed on a version that is not this Datastream's. The lookup is
    scoped by (id, datastream_id, project_id) -- the exact composite the table
    declares unique -- so a version id from a neighbouring Datastream reads as
    absent rather than as a permission error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT plan_version_id, executable
            FROM app.datastream_mapping_versions
            WHERE id = %s AND datastream_id = %s AND project_id = %s
            """,
            (mapping_version_id, datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise DatastreamNotIngestable(
            "the requested mapping version does not belong to this Datastream"
        )
    plan_version_id, executable = row
    if not executable:
        # A NON-EXECUTABLE VERSION IS A DRAFT, and a reprocess may move the
        # published pointer. Letting one through would publish rows under a
        # mapping that was never validated -- and the refusal has to happen here
        # rather than downstream, where it would surface as a parse failure with
        # no explanation.
        raise DatastreamNotIngestable(
            "the requested mapping version is not executable"
        )
    if not plan_version_id:
        raise DatastreamNotIngestable(
            "the requested mapping version carries no plan version"
        )
    return _resolve_server_dispatch_bundle(
        conn,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
    )


def list_reprocess_target_versions(
    conn, *, datastream_id: str, project_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """The versions an operator may choose from -- 38.18 AC1, << selects >>.

    A choice needs a LIST. Without one the parameter exists and nobody can use
    it: an operator would have to already know a `dmv_` id to type, which means
    the only reachable version stays the current one.

    `executable` travels with each entry rather than being filtered out here.
    A draft that cannot be replayed is exactly what a person is looking for when
    they wonder why their candidate is not offered, and removing it from the
    list turns an explainable refusal into an absence.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.version_number, v.executable, v.blocking_count,
                   v.created_at, v.created_by,
                   (v.id = d.current_mapping_version_id) AS is_current
            FROM app.datastream_mapping_versions v
            JOIN app.datastreams d
              ON d.id = v.datastream_id AND d.project_id = v.project_id
            WHERE v.datastream_id = %s AND v.project_id = %s
            ORDER BY v.version_number DESC
            LIMIT %s
            """,
            (datastream_id, project_id, int(limit)),
        )
        rows = cur.fetchall()
    return [
        {
            "mapping_version_id": version_id,
            "version_number": version_number,
            "executable": bool(executable),
            "blocking_count": int(blocking_count or 0),
            "created_at": created_at.isoformat() if created_at is not None else None,
            "created_by": created_by,
            "is_current": bool(is_current),
        }
        for (
            version_id, version_number, executable, blocking_count,
            created_at, created_by, is_current,
        ) in rows
    ]


# ---------------------------------------------------------------------------
# The TEMPLATE version -- the other half of 38.18 AC1's "target mapping/template
# versions".
#
# THREE FACTS, THREE WORDS, AND NEVER A `null` FOR ALL THREE. The vocabulary is
# not invented here: `datastream_change.py:120-129` fixed it for this exact
# question, and a second vocabulary for the same fact would be two answers free
# to disagree.
#
#   bound           -- the fact was established, and the version is next to it.
#   not_applicable  -- this Datastream pins no Template at all.
#   unavailable     -- the registry could not be read.
#   unknown         -- the pinned reference resolves to no row.
#
# WHY THIS IS READ AND NOT CHOSEN, which is a measured fact rather than a
# preference. The Template a replay runs under is `app.datastreams.config ->
# source_owner -> managed_feed_template_ref` (or the catalog pair
# `template_code`/`template_version`), read by
# `csv_excel_import.resolve_file_source_producer`, which takes no override: the
# pin IS a Datastream-level binding, and moving it is a governed Datastream
# change, not a reprocess parameter. `app.file_source_template_confirmations`
# (migration 188) then declares `UNIQUE (template_id, mapping_version_id)` -- a
# mapping version carries the human confirmation of ONE Template version, and
# crossing an unconfirmed pair is the named refusal
# `csv_excel_import.CONFIRMATION_TEMPLATE_MOVED`.
#
# So the same rule the plan version already follows applies here: it is read
# from what was validated together, and the proposal must SAY which one, because
# an operator confirming a replay is entitled to know the contract it will run
# against.
# ---------------------------------------------------------------------------

TEMPLATE_BOUND = "bound"
TEMPLATE_NOT_APPLICABLE = "not_applicable"
TEMPLATE_UNAVAILABLE = "unavailable"
TEMPLATE_UNKNOWN = "unknown"


def _datastream_config(conn, *, datastream_id: str, project_id: str) -> dict[str, Any] | None:
    """The Datastream's config as a dict, or None when it could not be read."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config FROM app.datastreams WHERE id = %s AND project_id = %s",
                (datastream_id, project_id),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- an unreadable store is not an absent config
        return None
    if row is None or row[0] is None:
        return {}
    config = row[0]
    if isinstance(config, str):
        import json  # noqa: PLC0415

        try:
            config = json.loads(config)
        except ValueError:
            return None
    return config if isinstance(config, dict) else {}


def read_reprocess_template_version(
    conn, *, datastream_id: str, project_id: str
) -> dict[str, Any]:
    """Which Template version a replay of this Datastream would run under.

    Never a bare identifier and never a bare ``None``: the answer always carries
    its ``state``, because "no Template here", "the registry would not read" and
    "the pin points at nothing" are three different sentences that a missing
    value collapses into one.
    """
    config = _datastream_config(
        conn, datastream_id=datastream_id, project_id=project_id
    )
    if config is None:
        return {
            "state": TEMPLATE_UNAVAILABLE,
            "reason": "the Datastream configuration could not be read",
        }

    owner = config.get("source_owner") or {}
    template_ref = str(owner.get("managed_feed_template_ref") or "").strip()
    if template_ref:
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_reprocess_template")
                cur.execute(
                    "SELECT template_code, version, is_active "
                    "FROM app.file_source_templates "
                    "WHERE id = %s AND project_id = %s",
                    (template_ref, project_id),
                )
                found = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_reprocess_template")
        except Exception:  # noqa: BLE001
            _rollback_savepoint(conn, "sp_reprocess_template")
            return {
                "state": TEMPLATE_UNAVAILABLE,
                "reason": "the Template registry could not be read",
                "template_id": template_ref,
            }
        if found is None:
            return {
                "state": TEMPLATE_UNKNOWN,
                "reason": "the pinned Template reference resolves to no row",
                "template_id": template_ref,
            }
        return {
            "state": TEMPLATE_BOUND,
            "kind": "project",
            "template_id": template_ref,
            "template_code": found[0],
            "version": int(found[1]),
            "is_active": bool(found[2]),
        }

    # The catalog binding. `import_templates.create_inbound_datastream` writes
    # the pair on the config itself, and `_catalog_template_producer` reads it:
    # a Datastream bound this way names a Template correctly, in the shape that
    # path writes, and reporting `not_applicable` for it would be false.
    template_code = str(config.get("template_code") or "").strip()
    template_version = config.get("template_version")
    if template_code and isinstance(template_version, int):
        from core.import_templates import get_template  # noqa: PLC0415

        try:
            catalog = get_template(conn, template_code, template_version)
        except Exception:  # noqa: BLE001
            return {
                "state": TEMPLATE_UNAVAILABLE,
                "reason": "the Template catalog could not be read",
                "template_id": f"template:{template_code}:{template_version}",
            }
        if not catalog:
            return {
                "state": TEMPLATE_UNKNOWN,
                "reason": "the pinned catalog Template resolves to no entry",
                "template_id": f"template:{template_code}:{template_version}",
            }
        return {
            "state": TEMPLATE_BOUND,
            "kind": "catalog",
            "template_id": f"template:{template_code}:{template_version}",
            "template_code": template_code,
            "version": int(template_version),
            "is_active": True,
        }

    return {
        "state": TEMPLATE_NOT_APPLICABLE,
        "reason": "this Datastream is not pinned to a file-source Template",
    }


def _rollback_savepoint(conn, name: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:  # noqa: BLE001 -- the caller is already reporting a failure
        pass


def list_reprocess_template_versions(
    conn,
    *,
    project_id: str,
    template_code: str,
    pinned_template_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """The Template versions that exist beside the pinned one.

    They are listed for the same reason `list_reprocess_target_versions` keeps
    its non-executable rows: an operator wondering why only one Template version
    is reachable gets an EXPLAINABLE refusal instead of an absence. Every entry
    other than the pin carries ``replayable: False`` and the reason -- moving the
    pin is a governed change on the Datastream, and this path replays, it does
    not repin.
    """
    if not template_code:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_reprocess_template_list")
            cur.execute(
                """
                SELECT id, version, is_active, created_at, created_by
                FROM app.file_source_templates
                WHERE project_id = %s AND template_code = %s
                ORDER BY version DESC
                LIMIT %s
                """,
                (project_id, template_code, int(limit)),
            )
            rows = cur.fetchall()
            cur.execute("RELEASE SAVEPOINT sp_reprocess_template_list")
    except Exception:  # noqa: BLE001
        _rollback_savepoint(conn, "sp_reprocess_template_list")
        return []
    out: list[dict[str, Any]] = []
    for template_id, version, is_active, created_at, created_by in rows or []:
        pinned = pinned_template_id is not None and template_id == pinned_template_id
        out.append(
            {
                "template_id": template_id,
                "version": int(version),
                "is_active": bool(is_active),
                "is_pinned": pinned,
                "replayable": pinned,
                "not_replayable_reason": (
                    None
                    if pinned
                    else "this Datastream pins another Template version; "
                    "moving the pin is a governed Datastream change, not a "
                    "reprocess parameter"
                ),
                "created_at": created_at.isoformat() if created_at is not None else None,
                "created_by": created_by,
            }
        )
    return out


def _load_frozen_dispatch_bundle(
    conn, *, raw_import_id: str, datastream_id: str, project_id: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.dispatch_bundle, r.dispatch_bundle_fingerprint
            FROM app.inbound_raw_imports r
            JOIN app.datastreams d
              ON d.id = r.datastream_id AND d.project_id = %s
            WHERE r.id = %s AND r.datastream_id = %s
            """,
            (project_id, raw_import_id, datastream_id),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    bundle = row[0]
    if not isinstance(bundle, dict):
        import json  # noqa: PLC0415

        bundle = json.loads(bundle)
    from core.managed_file_dispatch import fingerprint_bundle  # noqa: PLC0415

    if not isinstance(bundle, dict) or fingerprint_bundle(bundle) != row[1]:
        raise DatastreamNotIngestable("raw-import dispatch bundle evidence diverged")
    if bundle.get("datastream_id") != datastream_id or bundle.get("project_id") != project_id:
        raise DatastreamNotIngestable("raw-import dispatch bundle scope mismatch")
    return bundle


def _freeze_dispatch_bundle(
    conn,
    *,
    raw_import_id: str,
    datastream_id: str,
    project_id: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    from core.managed_file_dispatch import fingerprint_bundle  # noqa: PLC0415
    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415

    fingerprint = fingerprint_bundle(bundle)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    org_id = str(row[0]) if row and row[0] is not None else None

    def mutation(op_id: str) -> MutationResult:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.inbound_raw_imports r
                SET dispatch_bundle = %s::jsonb,
                    dispatch_bundle_fingerprint = %s,
                    operation_id = %s,
                    updated_at = NOW()
                FROM app.datastreams d
                WHERE r.id = %s AND r.datastream_id = %s
                  AND d.id = r.datastream_id AND d.project_id = %s
                  AND r.state = 'ACCEPTED'
                  AND r.dispatch_bundle IS NULL
                """,
                (
                    json.dumps(bundle, sort_keys=True),
                    fingerprint,
                    op_id,
                    raw_import_id,
                    datastream_id,
                    project_id,
                ),
            )
            updated = cur.rowcount == 1
        return MutationResult(
            outcome="succeeded" if updated else "no_op",
            before_hash=None,
            after_hash=fingerprint,
            result={"updated": updated},
            outbox_payload={
                "datastream_id": datastream_id,
                "raw_import_id": raw_import_id,
                "dispatch_bundle_fingerprint": fingerprint,
            },
        )

    spec = OperationSpec(
        command_type="inbound_raw_import.freeze_dispatch_bundle",
        actor="inbound-ingest",
        effective_org_id=org_id,
        resource_path=(
            "datastreams",
            datastream_id,
            "raw_imports",
            raw_import_id,
            "dispatch_bundle",
        ),
        idempotency_key=f"freeze_bundle:{raw_import_id}:{fingerprint}",
        host_context={},
        versions={
            "policy": "inbound-raw-import-v1",
            "catalog": "inbound-raw-import-v1",
            "tool": "rest-v1",
        },
        request_payload={
            "raw_import_id": raw_import_id,
            "datastream_id": datastream_id,
            "dispatch_bundle_fingerprint": fingerprint,
        },
        provider_references={},
        confirmation_mode="none",
        confirmation_reference=None,
        trace_id=None,
    )

    op_result = execute_operation(conn, spec, mutation=mutation)
    updated = (op_result.result or {}).get("updated", False)
    if updated:
        return bundle
    existing = _load_frozen_dispatch_bundle(
        conn,
        raw_import_id=raw_import_id,
        datastream_id=datastream_id,
        project_id=project_id,
    )
    if existing is None or fingerprint_bundle(existing) != fingerprint:
        raise DatastreamNotIngestable(
            "raw-import dispatch bundle could not be frozen without divergence"
        )
    return existing


def _lands_in_plan_store(producer: Any) -> bool:
    """Does this producer's Template declare the plan-store landing?

    THE TEMPLATE'S OWN SENTENCE, read from the sealed contract -- never inferred
    from the rows, and never re-derived from a second place. `import_runner`
    asks the same question of the same key; asking it differently here would be
    two answers to "where does this file land".
    """
    template = getattr(producer, "template", None)
    if not isinstance(template, dict):
        return False
    contract = template.get("contract")
    if not isinstance(contract, dict):
        return False
    from core.file_source_template import LANDING_PLAN_STORE  # noqa: PLC0415

    return contract.get("landing_target") == LANDING_PLAN_STORE


def _producer_from_frozen_bundle(bundle: dict[str, Any]):  # noqa: ANN202
    template = bundle.get("template")
    if template is None:
        return None
    from core.csv_excel_import import FileSourceProducer  # noqa: PLC0415

    return FileSourceProducer(
        template["template"],
        template.get("mapping") or {},
        template["template_id"],
        confirmation_operation_id=template.get("confirmation_operation_id"),
        confirmation_evidence=template.get("confirmation_evidence") or {},
        catalog_governed=bool(template.get("catalog_governed", False)),
    )


def _read_allow_empty_publication(conn, project_id: str) -> bool:
    """Read the project-scoped allow_empty_publication preference (fail closed).

    Mirrors the direct-upload route's ``_read_allow_empty_publication_pref``:
    defaults to False when the row/column is absent or on any read error, so the
    inbound and upload paths apply the SAME empty-publication policy.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allow_empty_publication FROM app.project_preferences WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - fail closed on any read error.
        return False
    if row is None or row[0] is None:
        return False
    return bool(row[0])


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def ingest_inbound_file(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    file_bytes: bytes,
    filename: str | None,
    channel: str,
    message_id: str,
    actor: str,
    trace_id: str | None = None,
    raw_import_id: str | None = None,
    dispatch_bundle_override: dict[str, Any] | None = None,
    force_new_execution: bool = False,
    run_origin: str | None = None,
) -> dict[str, Any]:
    """Drive an inbound file through the direct-upload import pipeline (parity).

    Given the resolved inputs from an inbound transport, this resolves the SAME
    pipeline arguments the direct-upload route resolves -- WITHOUT operator
    interaction -- and calls ``csv_excel_import.run_import``. The result is
    byte-for-byte the outcome a direct upload of the same bytes would produce.

    Args:
      conn: caller-managed psycopg connection. The nested import may commit its
        durable dispatch phases; the caller finalizes the delivery transaction.
      datastream_id / project_id: the target managed-feed Datastream (AD-5 scoped).
      file_bytes: the already-extracted file payload.
      filename: the delivered filename (used for format detection); may be None.
      channel: opaque inbound channel identifier (e.g. 'email' | 'webhook'),
        validated ONLY against the Datastream's config.channels allowlist.
      message_id: the transport's idempotency/replay key. Redelivery of the SAME
        message replays cleanly (same idempotency_key -> run_import no-op/replay).
      actor: the service identity driving the ingest (e.g. 'inbound-worker').
      trace_id: optional correlation id (forwarded for logging only).

    Returns run_import's result dict, augmented with
    ``{"datastream_id", "channel", "message_id"}``.

    Raises:
      InboundIngestValidationError: file_bytes/channel/message_id are missing/blank.
      DatastreamNotIngestable: not found, not managed_feed, disabled, channel not
        allowed, or missing a locked plan/mapping version (unconfigured).
      (propagated) AppendUnavailable, CsvExcelImportError, InvalidImportContract,
      ImportPayloadConflict, ImportInProgress -- run_import's own errors are NOT
      caught here so the caller maps them exactly like the direct-upload route.
    """
    # ------------------------------------------------------------------
    # Input guards (fail closed before any read).
    # ------------------------------------------------------------------
    if not file_bytes:
        raise InboundIngestValidationError("file_bytes is empty")
    channel = (channel or "").strip().lower()
    if not channel:
        raise InboundIngestValidationError("channel is required")
    message_id = (message_id or "").strip()
    if not message_id:
        raise InboundIngestValidationError("message_id is required")

    # ------------------------------------------------------------------
    # Step 1: load the Datastream and assert every unattended precondition.
    # ------------------------------------------------------------------
    ds = _load_ingestable_datastream(conn, datastream_id=datastream_id, project_id=project_id)
    if ds is None:
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' not found in project '{project_id}'"
        )
    if ds["source_kind"] != _SOURCE_KIND_MANAGED_FEED:
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' is not a managed_feed "
            f"(source_kind={ds['source_kind']!r}); inbound ingest is not applicable"
        )
    if not ds["enabled"]:
        raise DatastreamNotIngestable(
            f"datastream '{datastream_id}' is disabled; inbound ingest is refused"
        )

    config = ds["config"] if isinstance(ds["config"], dict) else {}
    allowed = config.get("channels") or []
    allowed_set = {str(ch).strip().lower() for ch in allowed}
    if channel not in allowed_set:
        raise DatastreamNotIngestable(
            f"channel '{channel}' is not enabled for datastream '{datastream_id}' "
            f"(allowed: {sorted(allowed_set)})"
        )

    # WHO MAY SEND, checked HERE because here is where both import paths meet
    # (story 57.3). A delivery arriving drives this function; a retained delivery
    # being replayed by `core.inbound_reprocess` drives this same function. The
    # first version of the policy guarded only the arrival, which left the replay
    # as a way around it -- and a file received before a list was declared is
    # precisely what a list is declared for. Guarding the branch that revealed
    # the defect instead of the point they share is the fault this repository
    # keeps paying for; the channel allowlist just above already sits here.
    from core.inbound_sender_policy import (  # noqa: PLC0415
        allowed_senders_from_config,
        delivery_is_allowed,
        read_delivery_sender,
    )

    declared_senders = allowed_senders_from_config(config)
    if declared_senders:
        sender_hash, sender_domain_hash = read_delivery_sender(
            conn, raw_import_id=raw_import_id or ""
        )
        if not delivery_is_allowed(
            allowed=declared_senders,
            sender_hash=sender_hash,
            sender_domain_hash=sender_domain_hash,
        ):
            # NO ADDRESS IN THE MESSAGE. The refusal is the answer; the sender is
            # the tenant's data and this string travels into logs and receipts.
            raise SenderNotAllowed(
                f"the sender of this delivery is not in the declared allowlist of "
                f"datastream '{datastream_id}'; nothing was imported"
            )

    frozen = dispatch_bundle_override
    if frozen is not None:
        from core.managed_file_dispatch import fingerprint_bundle  # noqa: PLC0415

        fingerprint_bundle(frozen)
        if (
            frozen.get("datastream_id") != datastream_id
            or frozen.get("project_id") != project_id
        ):
            raise DatastreamNotIngestable(
                "dispatch bundle override scope mismatch"
            )
    elif raw_import_id:
        frozen = _load_frozen_dispatch_bundle(
            conn,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            project_id=project_id,
        )

    if frozen is None:
        if raw_import_id:
            raise DatastreamNotIngestable(
                "accepted raw import has no frozen governed dispatch bundle"
            )
        plan_version_id = ds["current_plan_version_id"]
        mapping_version_id = ds["current_mapping_version_id"]
        if not plan_version_id or not mapping_version_id:
            raise DatastreamNotIngestable(
                "the Datastream has no locked plan and mapping for unattended dispatch"
            )
        frozen = _resolve_server_dispatch_bundle(
            conn,
            datastream_id=datastream_id,
            project_id=project_id,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
        )
    else:
        plan_version_id = str(frozen.get("plan_version_id") or "")
        mapping_version_id = str(frozen.get("mapping_version_id") or "")
        if not plan_version_id or not mapping_version_id:
            raise DatastreamNotIngestable("frozen dispatch pins are incomplete")

    projection_plan = frozen.get("projection_plan") or {}
    # WHY THIS RUN EXISTS, when the caller knows something this path cannot infer
    # (chantier 67-15b). An arrival stamps nothing: "a file was delivered" is what
    # the channel already says, and inventing an origin for it would be a sixth
    # vocabulary. A REPROCESS is different -- the same bytes, driven by a person
    # repairing a publication -- and a Runs tab that cannot tell the two apart
    # shows a treatment nobody can account for.
    #
    # `stamp_origin` rather than `plan["origin"] = ...`: it is the only function
    # that validates the key against the registry AND refuses a verb with no
    # engine, and the projection schema declares `origin` precisely so a stamped
    # plan stays re-validatable (`test_run_origin_registry.py`).
    if run_origin:
        from core.run_origins import stamp_origin  # noqa: PLC0415

        projection_plan = stamp_origin(projection_plan, run_origin)
    mapping_payload = frozen.get("mapping_payload") or {}
    governed_bundle = frozen.get("governed_evidence") or {}
    parser = frozen.get("parser")
    producer = _producer_from_frozen_bundle(frozen)
    if producer is None:
        if not isinstance(parser, dict) or not isinstance(parser.get("contract"), dict):
            raise InboundContractReviewRequired(
                "Frozen parsing contract evidence is incomplete."
            )
        contract = parser["contract"]
        governed_bundle = {
            **governed_bundle,
            "parser_contract_id": parser.get("contract_id"),
            "parser_contract_fingerprint": parser.get("contract_fingerprint"),
            "parser_confirmed_by": parser.get("confirmed_by"),
            "parser_confirmed_at": parser.get("confirmed_at"),
        }
    else:
        contract = {}

    # ------------------------------------------------------------------
    # Step 3-4: idempotency key + provenance metadata (source-agnostic).
    # Redelivery of the SAME message replays cleanly through run_import.
    # ------------------------------------------------------------------
    idempotency_key = f"inbound:{channel}:{datastream_id}:{message_id}"
    source_metadata = {
        "ingress_channel": channel,
        "message_id": message_id,
        "filename": filename,
        **({"raw_import_id": raw_import_id} if raw_import_id else {}),
    }

    # WHICH PLAN THIS DATASTREAM CARRIES, when its Template lands in the plan
    # store (ratified 2026-08-24, `file-source-ingestion.md`).
    #
    # RESOLVED HERE AND NOWHERE ELSE, because here is where both file ingresses
    # meet: an upload driven from the carrier's Workbench and a file delivered by
    # e-mail to the same Datastream drive this one function. `import_runner`
    # already refuses a plan-store import that carries no `plan_id`
    # (`plan_target_missing`); what it could not do was FIND one, because nothing
    # in the product said which plan a Datastream carried. The carrier link
    # (migration 303) is that sentence, and it is READ -- never created: a file
    # arriving at a Datastream that carries no plan is refused with the gesture
    # that repairs, and provisions nothing, which is exactly the auto-provisioning
    # the decision forbids.
    #
    # AND THE PUBLICATION CYCLE IS THE PLAN'S. `publish_candidate` below is
    # `bool(raw_import_id)` -- true for every governed upload -- while
    # `_resolve_landing_target` refuses a plan-store import that publishes,
    # because a plan version is published by the plan's own explicit act and the
    # warehouse pointer it would swap does not exist for a plan. Forcing it false
    # for this landing is what makes the two rules one rule instead of a dead end.
    plan_store_landing = _lands_in_plan_store(producer)
    if plan_store_landing:
        from core.mediaplan_store import get_carrier_plan  # noqa: PLC0415

        carried = get_carrier_plan(conn, datastream_id=datastream_id)
        if carried is None:
            raise DatastreamNotIngestable(
                "this Datastream's template lands in a media plan, and it carries "
                "no plan yet; create the media plan in this Datastream's Workbench, "
                "then send the file again"
            )
        source_metadata["plan_id"] = carried["id"]

    # Read the empty-publication preference exactly like the direct path so both
    # paths apply the same policy. force_empty_publish stays False: an unattended
    # inbound file never carries the operator's explicit empty-publish override.
    preferences = {"allow_empty_publication": _read_allow_empty_publication(conn, project_id)}

    # ------------------------------------------------------------------
    # Step 5: drive the SAME pipeline as a direct upload. Durable dispatch phase
    # commits happen inside run_import; its typed exceptions propagate unchanged.
    # ------------------------------------------------------------------
    from core.csv_excel_import import run_import  # noqa: PLC0415

    result = run_import(
        file_bytes,
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        projection_plan=projection_plan,
        actor=actor,
        idempotency_key=idempotency_key,
        source_metadata=source_metadata,
        contract=contract,
        conn=conn,
        write_mode=_WRITE_MODE_REPLACE,
        force_empty_publish=False,
        preferences=preferences,
        producer=producer,
        mapping_payload=mapping_payload,
        dispatch_bundle=frozen,
        publish_candidate=bool(raw_import_id) and not plan_store_landing,
        allow_snapshot_noop=not force_new_execution,
    )

    # ------------------------------------------------------------------
    # Step 6: augment the run result with the ingress provenance keys.
    # ------------------------------------------------------------------
    return {
        **result,
        "datastream_id": datastream_id,
        "channel": channel,
        "message_id": message_id,
    }
