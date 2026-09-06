"""Exact prepare/review/confirm flow for non-live Datastream changes."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from ulid import ULID

# WHAT A CHANGE IS FROZEN AGAINST lives next door, in `datastream_change_base`:
# whether the base of a review is the version IN FORCE or the head of the ledger,
# and what each of the two costs the confirmation. It is imported rather than
# inlined because this file stood at 991 lines with it, and because that question
# has its own tests -- `tests/core/test_datastream_change_base.py`.
#
# `DatastreamChangeError` is DEFINED there and re-exported here, so every
# existing `from core.datastream_change import DatastreamChangeError` keeps
# naming the same class object and every `except` clause keeps catching.
from core.datastream_change_base import (
    DatastreamChangeError,
    base_version_statement,
    require_base_unmoved,
    require_changeable,
    resolve_base,
    rollback_path,
)

__all__ = [
    "CONFIRMATION_TTL",
    "DatastreamChangeError",
    "DatastreamChangeHostRefused",
    "confirm_change",
    "console_continuation",
    "prepare_change",
    "require_confirming_host",
]

logger = logging.getLogger(__name__)

#: How long one AD-27 confirmation stays confirmable. ONE constant, because the
#: window was declared twice -- `timedelta(minutes=15)` on the persisted
#: `expires_at` and a hard-coded `900` on the wire -- and two declarations of one
#: fact drift. `tests/integration/test_datastream_change_engine_pg.py` measures
#: the persisted interval, so widening this without moving the bound is red.
CONFIRMATION_TTL = timedelta(minutes=15)


class DatastreamChangeHostRefused(DatastreamChangeError):
    """A host that cannot carry an AD-27 confirmation asked to commit one.

    Separate from `DatastreamChangeError` because the answer is not "your request
    was invalid" but "this surface is not where this is authorized" -- and the
    response carries a console continuation reference, which a generic 422 has
    nowhere to put (Story 38.17 AC5).
    """

    code = "host_cannot_confirm"

    def __init__(self, message: str, *, continuation: dict[str, Any]) -> None:
        super().__init__(message)
        self.continuation = continuation


#: The ONE host that may carry an AD-27 confirmation today: the authenticated
#: console session. Every other declared host prepares and inspects, then hands
#: the person the console (Story 38.17 AC5; 38.15 AC4 says the same of MCP).
INTERACTIVE_CONFIRMATION_HOSTS = frozenset({"console"})


def console_continuation(datastream_id: str) -> dict[str, Any]:
    """The authenticated console destination a refused host is handed instead.

    Semantic, never a raw URL: the client builds the href from the canonical
    navigation registry, so a screen rename cannot strand a host on a path this
    module froze. Same shape and same builder as every other owner reference.
    """
    from core.capability_proposals import datastream_owner_reference  # noqa: PLC0415

    return {
        "kind": "console",
        "requires_authenticated_session": True,
        "owner_reference": datastream_owner_reference(
            datastream_id, tab="mapping", action="confirm-change"
        ),
    }


def require_confirming_host(host: str | None, *, datastream_id: str) -> None:
    """Refuse a host that has declared it cannot carry a trusted confirmation.

    WHAT THIS DOES AND DOES NOT PROVE. The declaration is the caller's own, and
    it is trusted in ONE direction only: saying "I am not an interactive surface"
    can only take authority away, so it is safe to believe. The opposite claim is
    NOT verified here -- REST authentication (`admin_api._check_auth`) carries no
    interactive-presence evidence, so an undeclared host is treated as the
    console session that authentication already established. The server-minted
    proof exists on the MCP side only
    (`mcp_profiles.interactive_presence_verified`); wiring an equivalent to REST
    is not done, and this docstring says so rather than implying a gate that is
    not here.
    """
    declared = (host or "").strip().lower()
    if declared and declared not in INTERACTIVE_CONFIRMATION_HOSTS:
        raise DatastreamChangeHostRefused(
            "This host cannot authorize a change; open the console to review and confirm",
            continuation=console_continuation(datastream_id),
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise DatastreamChangeError("Change evidence is invalid")
    return deepcopy(value)


def _canonical_names(cur, *, project_id: str) -> dict[str, str]:
    """Registry identity -> the name a person reads, or nothing at all.

    The SAVEPOINT is the same one every optional read in this module takes: an
    unreadable vocabulary must cost the review its NAMES, never the review. When
    it answers nothing the diff shows `mdm_6D13WZ…` — the identity, which is
    still a value and still the truth, rather than a name we could not confirm.
    """
    try:
        cur.execute("SAVEPOINT sp_change_vocabulary")
        cur.execute(
            "SELECT id,canonical_name FROM app.mdm_canonical_fields "
            "WHERE status='active' AND (project_id=%s OR project_id IS NULL)",
            (project_id,),
        )
        names = {str(row[0]): str(row[1]) for row in cur.fetchall()}
        cur.execute("RELEASE SAVEPOINT sp_change_vocabulary")
    except Exception:  # noqa: BLE001 -- an unreadable vocabulary is not an empty one
        cur.execute("ROLLBACK TO SAVEPOINT sp_change_vocabulary")
        return {}
    return names


def _value_diff(
    cur, *, kind: str, project_id: str, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """The readable half of the review, and it may never block the review.

    A failure here is a defect in a READING; the hashes, the impact counts and
    the confirmation itself are untouched by it. So it degrades to `unavailable`
    with the exception's own words — the state vocabulary this module already
    uses — instead of taking the preparation down with it.
    """
    from core.mapping_value_diff import value_diff  # noqa: PLC0415

    try:
        names = _canonical_names(cur, project_id=project_id)
        return value_diff(kind, before, after, canonical_names=names)
    except Exception as exc:  # noqa: BLE001 -- a reading that fails says so
        return {
            "state": "unavailable",
            "reason": f"the value-by-value reading could not be composed: {exc}",
        }


#: The two readings a binding-only change may move: which canonical field a
#: column names, and its binding state. Anything else -- a column that appears,
#: disappears or stops landing, a treatment, the grain, a contract path -- makes
#: the change a candidate run, as before.
_BINDING_READINGS = frozenset({"Binding state", "Governed target"})
_DERIVED_PATHS = frozenset({"$.capability_fingerprint"})
#: What the preparation MEASURES on a column's profile (`grain_profile`): a
#: fact about the landed rows, never a change to how they land. A version that
#: differs from the one in force only by these is still a binding-only change.
_MEASUREMENT_KEYS = ("cardinality_signal", "allowed_values")


#: Who confirmed a binding and why is provenance of the binding, not a change
#: to how a column lands; a binding-only change legitimately carries a new
#: confirmer (the reader's `Other readings` would otherwise count it).
_BINDING_PROVENANCE_KEYS = ("confirmed_by", "confirmed_reason", "blocking_reason")


def _without_measurements(payload: dict[str, Any]) -> dict[str, Any]:
    scrubbed = deepcopy(payload)
    for field in scrubbed.get("fields") or []:
        if not isinstance(field, dict):
            continue
        profile = field.get("profile") if isinstance(field.get("profile"), dict) else None
        if profile is not None:
            for key in _MEASUREMENT_KEYS:
                profile.pop(key, None)
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else None
        if binding is not None:
            for key in _BINDING_PROVENANCE_KEYS:
                binding.pop(key, None)
    return scrubbed


def binding_only_change(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when the change moves bindings and nothing that touches the landed rows.

    Read with the same reader the review shows a person (`mapping_value_diff`),
    so what confirms as an overlay is exactly what the review called a binding
    change (`governance.md`, amendment of 2026-09-05).
    """
    from core.mapping_value_diff import value_diff  # noqa: PLC0415

    try:
        entries = value_diff("mapping", _without_measurements(before), _without_measurements(after)).get("entries") or []
    except Exception:  # noqa: BLE001 -- an unreadable diff is not a binding-only one
        return False
    #  `capability_fingerprint` is DERIVED at save (`save_field_mapping` recomputes
    #  it from the payload), so it differs between a stored version and any
    #  proposal that does not carry it, without a single row moving.
    entries = [e for e in entries if str(e.get("subject")) not in _DERIVED_PATHS]
    if not entries:
        return False
    return all(str(entry.get("reading")) in _BINDING_READINGS for entry in entries)


def _top_level_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "path": f"$.{key}",
            "before_hash": _hash(before.get(key)),
            "after_hash": _hash(after.get(key)),
        }
        for key in sorted(set(before) | set(after))
        if _canonical(before.get(key)) != _canonical(after.get(key))
    ]


# ---------------------------------------------------------------------------
# What the confirmation SHOWS (story 38.17 AC1).
#
# THREE WORDS, AND THEY ARE NOT INTERCHANGEABLE. Every element below answers with
# a `state`, never with a bare number, because "there are none", "I could not
# read the store" and "this fact does not exist for this shape of Datastream" are
# three different sentences and a `0` says the first while meaning any of them.
#
#   counted / bound  -- the fact was established, and the value is next to it.
#   not_applicable   -- the fact does not exist here, with the reason it does not.
#   unavailable      -- the store could not be read, with the reason it could not.
#   unknown          -- the reference exists but resolves to nothing.
# ---------------------------------------------------------------------------


def _landing_field_ids(payload: dict[str, Any]) -> set[str]:
    """The source columns a mapping payload actually lands.

    `binding.status == "excluded"` is the SOLE authority of exclusion -- the
    ratified Mapping tab says so in as many words
    (`datastream-workbench-and-wizard.md:1019`), and the `included` boolean it
    replaced had one writer that hard-coded `True`.
    """
    ids: set[str] = set()
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        if (field.get("binding") or {}).get("status") == "excluded":
            continue
        field_id = field.get("field_id")
        if isinstance(field_id, str) and field_id:
            ids.add(field_id)
    return ids


def _bound_targets(payload: dict[str, Any]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        field_id = field.get("field_id")
        if not isinstance(field_id, str) or not field_id:
            continue
        binding = field.get("binding") or {}
        targets[field_id] = _canonical([binding.get("canonical_target"), binding.get("mdm_target")])
    return targets


def _change_impact(kind: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The COUNT before the act, which is what makes an impact reviewable.

    The Mapping tab already sets this bar for its own controls -- "N of 46
    columns stop landing" before an exclusion is confirmed
    (`datastream-workbench-and-wizard.md:1019`). A confirmation dialog that shows
    a hash diff and no count asks a person to approve an arithmetic they have not
    been shown.
    """
    if kind != "mapping":
        return {
            "state": "not_applicable",
            "reason": "a processing change carries no field bindings to count",
        }
    landing_before = _landing_field_ids(before)
    landing_after = _landing_field_ids(after)
    targets_before, targets_after = _bound_targets(before), _bound_targets(after)
    return {
        "state": "counted",
        "fields_landing_before": len(landing_before),
        "fields_landing_after": len(landing_after),
        "fields_no_longer_landing": len(landing_before - landing_after),
        "fields_newly_landing": len(landing_after - landing_before),
        "fields_rebound": sum(
            1
            for field_id in landing_before & landing_after
            if targets_before.get(field_id) != targets_after.get(field_id)
        ),
    }


def _validation_evidence(kind: str, proposed: dict[str, Any]) -> dict[str, Any]:
    """Run the SAME normalizer the confirmation will run, and report its verdict.

    One authority per kind -- `normalize_mapping` for a mapping change,
    `normalize_intent` for a processing one -- because these are exactly the two
    functions `confirm_change` reaches through `save_field_mapping` and
    `_append_plan_version`. A second derivation of "is this payload acceptable"
    is a copy free to drift from the one that decides.

    It REPORTS; it does not refuse. `prepare_change` freezing a payload that the
    confirmation will reject is a real defect, but turning it into a refusal here
    changes which door says no, and that belongs to whoever owns the wizard's
    error surface -- not to a field on a review.
    """
    try:
        if kind == "mapping":
            from core.datastream_field_mapping import normalize_mapping  # noqa: PLC0415

            _, content_hash = normalize_mapping(proposed)
        else:
            from core.datastream_intents import normalize_intent  # noqa: PLC0415

            _, content_hash = normalize_intent(proposed)
    except Exception as exc:  # noqa: BLE001 -- every refusal shape is evidence here
        issues = getattr(exc, "issues", None)
        return {
            "state": "contract_violated",
            "checked_by": "normalize_mapping" if kind == "mapping" else "normalize_intent",
            "issue_count": len(issues) if isinstance(issues, list) else 1,
        }
    return {
        "state": "contract_satisfied",
        "checked_by": "normalize_mapping" if kind == "mapping" else "normalize_intent",
        "proposed_content_hash": content_hash,
    }


def _template_version(cur, *, datastream_id: str, project_id: str, config: Any) -> dict[str, Any]:
    """The Template version this Datastream is pinned to, or WHY there is none.

    The binding is `app.datastreams.config -> source_owner ->
    managed_feed_template_ref` (written by `datastream_activation.py:1126`, read
    by `server/core/file_source_resolution.py#resolve_file_source_producer`). A
    connector-pull Datastream has no Template
    at all, which is `not_applicable` and not a missing value.
    """
    owner = ((config if isinstance(config, dict) else {}).get("source_owner")) or {}
    template_ref = str(owner.get("managed_feed_template_ref") or "")
    if not template_ref:
        return {
            "state": "not_applicable",
            "reason": "this Datastream is not pinned to a file-source Template",
        }
    try:
        cur.execute("SAVEPOINT sp_change_template")
        cur.execute(
            "SELECT template_code,version FROM app.file_source_templates "
            "WHERE id=%s AND project_id=%s",
            (template_ref, project_id),
        )
        found = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT sp_change_template")
    except Exception:  # noqa: BLE001 -- an unreadable store is not an absent Template
        cur.execute("ROLLBACK TO SAVEPOINT sp_change_template")
        return {
            "state": "unavailable",
            "reason": "the Template registry could not be read",
            "template_id": template_ref,
        }
    if found is None:
        return {
            "state": "unknown",
            "reason": "the pinned Template reference resolves to no row",
            "template_id": template_ref,
        }
    return {
        "state": "bound",
        "template_id": template_ref,
        "template_code": found[0],
        "version": int(found[1]),
    }


def _affected_imports(cur, *, datastream_id: str) -> dict[str, Any]:
    """How many retained raw imports this Datastream would replay under the change.

    A COUNT, never the rows: `app.inbound_raw_imports` carries filenames and
    quarantine URIs, and a review is not a place to enumerate them.

    The SAVEPOINT is not decoration. `prepare_change` runs inside the caller's
    transaction; on a deployment whose epic-38 migrations have not landed this
    SELECT raises `UndefinedTable`, and without the savepoint that aborts the
    whole transaction -- turning "I could not count" into "the change could not
    be prepared".
    """
    try:
        cur.execute("SAVEPOINT sp_change_imports")
        cur.execute(
            "SELECT COUNT(*) FROM app.inbound_raw_imports WHERE datastream_id=%s",
            (datastream_id,),
        )
        total = int(cur.fetchone()[0])
        cur.execute("RELEASE SAVEPOINT sp_change_imports")
    except Exception:  # noqa: BLE001 -- see the docstring: 0 would be a lie here
        cur.execute("ROLLBACK TO SAVEPOINT sp_change_imports")
        return {
            "state": "unavailable",
            "reason": "the retained raw-import ledger could not be read",
        }
    return {"state": "counted", "retained_raw_imports": total}


def _raw_import_binding(
    cur,
    *,
    raw_import_id: str | None,
    datastream_id: str,
    project_id: str,
) -> dict[str, Any]:
    """Freeze one retained input, or state that this is a generic change."""
    if not raw_import_id:
        return {
            "state": "not_applicable",
            "reason": "this mapping change was not opened from a retained raw import",
        }
    cur.execute(
        """SELECT r.id,r.content_hash,r.state
             FROM app.inbound_raw_imports r
             JOIN app.inbound_receipts receipt ON receipt.id=r.receipt_id
             JOIN app.datastreams d ON d.id=receipt.datastream_id
            WHERE r.id=%s AND r.datastream_id=%s
              AND receipt.datastream_id=%s AND d.project_id=%s""",
        (raw_import_id, datastream_id, datastream_id, project_id),
    )
    row = cur.fetchone()
    if row is None:
        raise DatastreamChangeError(
            "The retained raw import does not belong to this Datastream and Project"
        )
    return {
        "state": "bound",
        "raw_import_id": str(row[0]),
        "content_hash": str(row[1]),
        "import_state": str(row[2]),
    }


def prepare_change(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    kind: str,
    proposed_payload: dict[str, Any],
    actor: str,
    idempotency_key: str,
    raw_import_id: str | None = None,
) -> dict[str, Any]:
    """Freeze a proposed full mapping/plan document against an exact base version.

    THE BASE IS THE POINTER WHEN THERE IS ONE, AND THE HEAD OF THE LEDGER WHEN
    THERE IS NOT -- `core.datastream_change_base` carries the whole rule, and
    `require_base_unmoved` what each of the two costs at confirmation time.
    Which one it was is written into the preparation row, never re-derived later:
    re-deriving it would ask the confirmation to guess what the person read.

    WHAT THE FROZEN `review` CARRIES, and why each element is there. Story 38.17
    AC1 enumerates them itself -- "organization, Datastream, template/base/
    candidate versions, affected imports, impact, validation evidence and
    rollback path" (`stories-epic-38-inbound-managed-file-connector.md:381`). It
    is a list, not a design decision, so every item below is either established
    or says with a `state` and a `reason` why it could not be.

    The `candidate` version is the one item that is structurally absent here: it
    does not exist until `confirm_change` mints it, so the review names where it
    will come from rather than leaving a null that reads as "none".
    """
    if kind not in {"mapping", "processing"} or not idempotency_key.strip():
        raise DatastreamChangeError("Change kind and Idempotency-Key are required")
    proposed = _json(proposed_payload)
    if kind == "mapping":
        # MEASURED, NOT ASSUMED (2026-09-05). A grain column whose profile is
        # unknown is priced at 1 000 distinct values by the projection, and two
        # of them refuse every plan -- so a flow minted without samples could
        # never mint a candidate again, not even to pin a shared identity. The
        # flow has landed rows: they are counted here, and the count becomes the
        # signal of the version being prepared. An unreadable relation leaves
        # the signal unknown and the refusal keeps naming the repair.
        from core.grain_profile import enrich_unknown_grain_signals, measure_grain_signals  # noqa: PLC0415

        measured = enrich_unknown_grain_signals(
            proposed,
            lambda columns: measure_grain_signals(
                conn, project_id=project_id, datastream_id=datastream_id, columns=columns
            ),
        )
        if measured:
            logger.info(
                "datastream_change: grain signals measured ds=%s %s", datastream_id, measured
            )
    with conn.cursor() as cur:
        cur.execute(
            """SELECT org_id,lifecycle_state,archived_at,
                      current_plan_version_id,current_mapping_version_id,config
                 FROM app.datastreams
                WHERE id=%s AND project_id=%s
                FOR UPDATE""",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise DatastreamChangeError("This Datastream does not exist in this Project")
        org_id, lifecycle_state, archived_at, plan_pointer, mapping_pointer, config = row
        require_changeable(lifecycle_state, archived_at)
        base = {
            axis: resolve_base(
                cur,
                axis,
                datastream_id=datastream_id,
                project_id=project_id,
                pointer=pointer,
            )
            for axis, pointer in (("plan", plan_pointer), ("mapping", mapping_pointer))
        }
        plan_base_id, plan_document, plan_in_force = base["plan"]
        mapping_base_id, mapping_document, mapping_in_force = base["mapping"]
        before = _json(mapping_document if kind == "mapping" else plan_document)
        diff = _top_level_diff(before, proposed)
        if not diff:
            raise DatastreamChangeError("The proposed change has no deterministic diff")
        template = _template_version(
            cur, datastream_id=datastream_id, project_id=project_id, config=config
        )
        # ONLY `unavailable` refuses. Story 38.17 measures THREE Template states
        # and counts fusing two of them as a defect (A9): `unknown` is a dangling
        # reference, a fact the review must NAME, not a reason to refuse the
        # review that would show it. It is freezable as what it is -- the
        # reference, with no version -- and confirmation refuses if a Template
        # appeared under it in the meantime. `unavailable` is different in kind:
        # the registry could not be read, so nothing about the Template can be
        # asserted, and freezing absence would be a claim we did not measure.
        if template["state"] == "unavailable":
            raise DatastreamChangeError(
                "The Datastream's Template version cannot be frozen for this review"
            )
        raw_import = _raw_import_binding(
            cur,
            raw_import_id=raw_import_id,
            datastream_id=datastream_id,
            project_id=project_id,
        )
        review = {
            "kind": kind,
            # ORGANIZATION. It was read as `row[0]` and written into the
            # preparation row all along; it simply never reached the person
            # confirming, who is the one being asked to act for it.
            "organization_id": org_id,
            "project_id": project_id,
            "datastream_id": datastream_id,
            # BASE versions -- the exact versions this proposal is frozen against.
            "expected_plan_version_id": plan_base_id,
            "expected_mapping_version_id": mapping_base_id,
            # AND WHETHER EACH ONE IS IN FORCE. Two ids alone cannot say it, and
            # the difference decides both what the person is approving and which
            # check the confirmation applies. `in_force` and `head_of_ledger` are
            # two sentences, never one id that means either.
            "base_versions": {
                axis: base_version_statement(version_id, in_force)
                for axis, (version_id, _document, in_force) in base.items()
            },
            # TEMPLATE version. Missing here for the same reason it is missing
            # from 38.16 AC2's "two bindings out of four": ONE hole, in one place.
            "template_version": template,
            "raw_import": raw_import,
            # CANDIDATE version -- structurally absent before the confirmation.
            "candidate_version": {
                "state": "not_yet_minted",
                "reason": "the candidate execution is minted by the confirmation, not the review",
            },
            "before_hash": _hash(before),
            "after_hash": _hash(proposed),
            "diff": diff,
            # THE SAME DIFFERENCE, IN VALUES — 2026-08-18.
            #
            # `diff` above is what `MutationResult` compares, and it is a hash
            # per top-level key: excluding one column of forty-six reached the
            # person confirming as ONE row reading `$.fields  9f2c…  4b70…`. The
            # values are composed beside it, never in place of it — see
            # `core.mapping_value_diff` for why the base and the vocabulary make this
            # the server's composition and not the console's.
            "value_diff": _value_diff(
                cur, kind=kind, project_id=project_id, before=before, after=proposed
            ),
            "impact": _change_impact(kind, before, proposed),
            "affected_imports": _affected_imports(cur, datastream_id=datastream_id),
            "validation": _validation_evidence(kind, proposed),
            # ROLLBACK path. It is not a new mechanism: `advance_pointer=False`
            # means the versions in force STAY in force, so the way back is to do
            # nothing further. Naming them is what turns that from an
            # implementation detail into a promise a reviewer can check.
            #
            # AND IT DOES NOT SAY "the live pointers do not move" WHEN NOTHING IS
            # LIVE. On a Datastream with no pointer in force that sentence is
            # true of nothing, and a promise about an object that does not exist
            # is the shape of reassurance this module exists to refuse.
            "rollback": rollback_path(
                plan_base_id, plan_in_force, mapping_base_id, mapping_in_force
            ),
            "consequence": "Append immutable non-live versions and dispatch one candidate",
        }
        review_hash = _hash(review)
        secret = secrets.token_urlsafe(32)
        preparation_id = f"dscp_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_change_preparations
               (id,org_id,project_id,datastream_id,change_kind,
                expected_plan_version_id,expected_mapping_version_id,
                expected_plan_pointer_in_force,expected_mapping_pointer_in_force,
                expected_raw_import_id,expected_template_id,expected_template_version,
                binding_snapshot_version,
                proposed_payload,
                review,review_hash,confirmation_secret_hash,idempotency_key_hash,
                prepared_by,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s::jsonb,%s::jsonb,
                       %s,%s,%s,%s,%s)""",
            (
                preparation_id,
                org_id,
                project_id,
                datastream_id,
                kind,
                plan_base_id,
                mapping_base_id,
                plan_in_force,
                mapping_in_force,
                raw_import.get("raw_import_id"),
                # The COLUMNS carry the resolved row only -- migration 265 checks
                # id and version are both present or both absent, and a dangling
                # reference resolves to no row to name. The `unknown` state is
                # frozen where it is already hashed, in `review.template_version`,
                # and confirmation revalidates it from there.
                template.get("template_id") if template["state"] == "bound" else None,
                template.get("version") if template["state"] == "bound" else None,
                _canonical(proposed),
                _canonical(review),
                review_hash,
                hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                actor,
                datetime.now(UTC) + CONFIRMATION_TTL,
            ),
        )
    return {
        "preparation_id": preparation_id,
        "confirmation_secret": secret,
        "review_hash": review_hash,
        "expires_in_seconds": int(CONFIRMATION_TTL.total_seconds()),
        "review": review,
    }


def _append_plan_version(
    conn,
    *,
    datastream_id: str,
    project_id: str,
    payload: dict[str, Any],
    actor: str,
    operation_id: str,
    current_plan_id: str,
) -> dict[str, Any]:
    from core.datastream_intents import normalize_intent  # noqa: PLC0415

    normalized, content_hash = normalize_intent(payload)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT source_kind,writer_kind,destination_policy,contract_version,
                      capability_contract_version,capability_fingerprint,executable
                 FROM app.datastream_plan_versions
                WHERE id=%s AND datastream_id=%s AND project_id=%s""",
            (current_plan_id, datastream_id, project_id),
        )
        current = cur.fetchone()
        if current is None:
            # `require_base_unmoved` has already proven this version is the base
            # -- pointer or head -- so reaching here means it was deleted under a
            # held row lock. It says the version, not "the active plan": the base
            # of a change is not always the plan in force.
            raise DatastreamChangeError("The plan version this change is based on has gone")
        if (
            normalized["source"]["kind"] != current[0]
            or normalized["destination"]["policy"] != current[2]
        ):
            raise DatastreamChangeError("Processing changes cannot change source ownership")
        cur.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM app.datastream_plan_versions "
            "WHERE datastream_id=%s AND project_id=%s",
            (datastream_id, project_id),
        )
        version = int(cur.fetchone()[0])
        plan_id = f"dsp_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
               (id,datastream_id,project_id,version_number,contract_version,source_kind,
                writer_kind,destination_policy,normalized_payload,content_hash,
                capability_contract_version,capability_fingerprint,executable,
                validation_issues,idempotency_key_hash,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,'[]'::jsonb,%s,%s)""",
            (
                plan_id,
                datastream_id,
                project_id,
                version,
                current[3],
                current[0],
                current[1],
                current[2],
                _canonical(normalized),
                content_hash,
                current[4],
                current[5],
                current[6],
                hashlib.sha256(f"{operation_id}:plan".encode()).hexdigest(),
                actor,
            ),
        )
    return {"id": plan_id, "normalized_payload": normalized, "content_hash": content_hash}


def _reconcile_unknown_outcome(
    conn, *, operation_id: str, candidate_execution_id: str | None
) -> None:
    """Resolve one uncertain confirmation FROM DURABLE EVIDENCE, never by redoing it.

    Story 38.17 AC4, second half. When the outbox dispatch of a confirmation ends
    uncertain, `operations.record_outbox_attempt(uncertain=True)` parks the row in
    `outcome_unknown` -- and the only safe question left is whether the candidate
    the mutation minted actually landed. That is read from
    `app.datastream_executions`, which is the effect itself, so the answer cannot
    be a second run of the effect.

    A missing candidate resolves to `failed`, not to silence: an operation left in
    `outcome_unknown` forever is the state that invites a blind resubmission.
    """
    from core.operations import reconcile_operation_outcome  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute("SELECT state FROM app.operations WHERE id=%s", (operation_id,))
        row = cur.fetchone()
    if row is None or row[0] != "outcome_unknown":
        return
    landed = False
    if candidate_execution_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM app.datastream_executions WHERE id=%s",
                (candidate_execution_id,),
            )
            landed = cur.fetchone() is not None
    reconcile_operation_outcome(
        conn,
        operation_id=operation_id,
        resolved_outcome="succeeded" if landed else "failed",
        evidence_hash=_hash(
            {"candidate_execution_id": candidate_execution_id, "candidate_landed": landed}
        ),
    )


def _refuse_resubmission(_tx, _operation_id: str):
    """The mutation a replay must never reach.

    `execute_operation` returns the stored operation before calling this whenever
    the idempotency key is already bound, so reaching it means the replay path
    was about to run the effect a second time.
    """
    raise DatastreamChangeError("A confirmed change is never resubmitted")


def _what_got_worse(
    conn, *, datastream_id: str, project_id: str, after: dict[str, Any]
) -> list[str]:
    """What this change DEGRADES, compared with the mapping in force. Empty = nothing.

    The comparison is made against the ACTIVE mapping version, not against an
    empty plan: a Datastream whose estimate already exceeds its ceiling keeps
    exceeding it, and that is not something this change did.

    Unreadable "before" is treated as "nothing to compare", deliberately: refusing
    a repair because the previous state could not be read would reinstate the
    dead end this function exists to remove, and the plan's own issues still
    travel on the candidate for anyone to see.
    """
    from core.datastream_projection import compile_projection  # noqa: PLC0415

    #  LA LECTURE ENTIERE est protegee, pas seulement la compilation : « l'etat
    #  anterieur est illisible » couvre aussi le cas ou la requete elle-meme ne
    #  passe pas. Ne proteger que la compilation faisait remonter une panne de
    #  lecture en 503 « Datastream evidence is unavailable » -- soit exactement
    #  le cul-de-sac que cette fonction retire, sous un autre code. Mesure du
    #  2026-08-13, sur le flux que ce correctif devait justement debloquer.
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.plan_version_id, v.source_schema_hash, v.capability_fingerprint,
                       v.executable, v.mapping_payload
                  FROM app.datastreams d
                  JOIN app.datastream_mapping_versions v ON v.id = d.current_mapping_version_id
                 WHERE d.id = %s AND d.project_id = %s
                """,
                (datastream_id, project_id),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- an unreadable before is not a degradation
        return []
    if row is None:
        return []
    columns = (
        "id",
        "plan_version_id",
        "source_schema_hash",
        "capability_fingerprint",
        "executable",
        "mapping_payload",
    )
    try:
        before = compile_projection(dict(zip(columns, row)))
    except Exception:  # noqa: BLE001 -- an unreadable before is not a degradation
        return []

    #  AN ISSUE THAT WAS ALREADY THERE HAS NOT APPEARED. The signature used to
    #  carry the field ids beside the code, so a `cardinality_over_limit` that
    #  named four unprofiled columns before and one expensive column after --
    #  because the change MEASURED the others (grain_profile, 2026-09-05) -- read
    #  as a new issue, and the guard refused the very change that made the
    #  estimate smaller. The code alone says whether a refusal appeared; the
    #  estimate comparison below says whether the numbers got worse.
    def signature(plan: dict[str, Any]) -> set[str]:
        return {str(issue.get("code")) for issue in plan.get("issues") or []}

    fields_of = {
        str(issue.get("code")): tuple(issue.get("field_ids") or [])
        for issue in after.get("issues") or []
    }
    worse: list[str] = []
    for code in sorted(signature(after) - signature(before)):
        named = ", ".join(fields_of.get(code) or ())
        worse.append(f"{code} appears{' on ' + named if named else ''}")
    old_estimate = before.get("estimate") or {}
    new_estimate = after.get("estimate") or {}
    for key, label in (
        ("estimated_grain_cardinality", "grain rows"),
        ("estimated_scan_bytes", "scanned bytes"),
    ):
        old_value = int(old_estimate.get(key) or 0)
        new_value = int(new_estimate.get(key) or 0)
        if new_value > old_value:
            worse.append(f"{label} rise from {old_value} to {new_value}")
    return worse


def confirm_change(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    preparation_id: str,
    confirmation_secret: str,
    actor: str,
) -> dict[str, Any]:
    """Consume one exact confirmation and dispatch one non-live candidate atomically.

    THE POINTER DOES NOT MOVE HERE, and that is the ratified division of labour,
    not an omission: `datastream-workbench-and-wizard.md:1019` gives the Mapping
    tab `Prepare mapping change` then `Review and confirm`, and
    `:2128` gives the pointer move to a LATER, separate step -- "`Publish and
    activate` atomically makes it current". Its single writer is
    `datastream_activation.py:695`. This function appends immutable versions and
    dispatches one candidate; `active_versions_unchanged` is the promise, not a
    consolation.

    THE OPTIMISTIC LOCK IS PER AXIS, AND IT KNOWS WHAT IT IS LOCKING AGAINST.
    `require_base_unmoved` reads the `expected_*_pointer_in_force` flags the
    preparation was written with (migration 255) and applies the check that
    matches: a base that was IN FORCE must still be in force and unchanged; a
    base that was the head of the ledger must still be the head AND still have
    nothing live above it. A publication that happened while the review was open
    refuses in both shapes.

    A RETRY IS A REPLAY, NEVER A SECOND EFFECT (AC4). Once the preparation is
    `confirmed`, the same secret returns the ORIGINAL operation through
    `execute_operation`'s idempotent path -- an error would have been the one
    answer that is not idempotent. An operation parked in `outcome_unknown` is
    reconciled from durable evidence first, so the caller never has to guess.
    """
    from core.datastream_field_mapping import save_field_mapping  # noqa: PLC0415
    from core.datastream_projection import compile_projection  # noqa: PLC0415
    from core.datastream_publication import create_execution  # noqa: PLC0415
    from core.operations import MutationResult, OperationSpec, execute_operation  # noqa: PLC0415
    from core.queue import enqueue_activation_work  # noqa: PLC0415
    from core.run_origins import MAPPING_CHANGE, PLAN_CHANGE, stamp_origin  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """SELECT org_id,change_kind,expected_plan_version_id,expected_mapping_version_id,
                      proposed_payload,review,review_hash,confirmation_secret_hash,state,
                      expires_at,operation_id,candidate_execution_id,
                      expected_plan_pointer_in_force,expected_mapping_pointer_in_force,
                      expected_raw_import_id,expected_template_id,expected_template_version,
                      binding_snapshot_version
                 FROM app.datastream_change_preparations
                WHERE id=%s AND project_id=%s AND datastream_id=%s FOR UPDATE""",
            (preparation_id, project_id, datastream_id),
        )
        row = cur.fetchone()
    if row is None:
        raise DatastreamChangeError("Change preparation was not found")
    # THE SECRET IS CHECKED FIRST, and in constant time. Checked after the state
    # branch, a caller holding no secret at all could still tell a spent
    # preparation from a live one by the sentence it got back; and `!=` on a
    # digest compares byte by byte with an early exit.
    if not hmac.compare_digest(
        hashlib.sha256(confirmation_secret.encode("utf-8")).hexdigest(), row[7]
    ):
        raise DatastreamChangeError("Change confirmation is invalid")
    if row[8] == "confirmed":
        if row[10]:
            _reconcile_unknown_outcome(conn, operation_id=row[10], candidate_execution_id=row[11])
    elif row[8] != "prepared" or row[9] <= datetime.now(UTC):
        raise DatastreamChangeError("Change preparation is no longer confirmable")
    proposed = _json(row[4])
    review = _json(row[5])
    expected_plan, expected_mapping = row[2], row[3]
    # Whether each base WAS in force at preparation time. Written by
    # `prepare_change`, never re-derived: migration 255 defaults both to TRUE, so
    # a preparation frozen before that migration is checked exactly the way it was
    # frozen -- against pointers, which is what it was.
    expected_plan_in_force, expected_mapping_in_force = bool(row[12]), bool(row[13])
    expected_raw_import, expected_template, expected_template_version = row[14:17]
    binding_snapshot_version = int(row[17])
    # The Template state exactly as the person read it. `bound` is also carried by
    # the two columns above; `unknown` and `not_applicable` exist only here,
    # because migration 265 has no encoding for "a reference that resolves to no
    # row". The review is hashed, so reading the expectation from it is reading
    # the frozen document, not re-deriving it.
    frozen_template = (review.get("template_version") or {}) if isinstance(review, dict) else {}
    frozen_template_state = str(frozen_template.get("state") or "")
    frozen_template_ref = frozen_template.get("template_id") or None

    def mutation(tx, operation_id: str) -> MutationResult:
        with tx.cursor() as cur:
            cur.execute(
                """SELECT current_plan_version_id,current_mapping_version_id,source_kind,config
                     FROM app.datastreams WHERE id=%s AND project_id=%s FOR UPDATE""",
                (datastream_id, project_id),
            )
            current = cur.fetchone()
            if current is None:
                raise DatastreamChangeError("This Datastream does not exist in this Project")
            # THE OPTIMISTIC LOCK, one axis at a time, under the row lock taken
            # just above. `require_base_unmoved` carries what each of the four
            # cases means; the only thing decided here is that BOTH axes are
            # checked -- a processing change pins the mapping it re-saves just as
            # hard as the plan it appends to.
            for axis, expected_id, in_force, pointer in (
                ("plan", expected_plan, expected_plan_in_force, current[0]),
                ("mapping", expected_mapping, expected_mapping_in_force, current[1]),
            ):
                require_base_unmoved(
                    cur,
                    axis,
                    datastream_id=datastream_id,
                    project_id=project_id,
                    expected_id=expected_id,
                    pointer_in_force=in_force,
                    current_pointer=pointer,
                )
            if binding_snapshot_version == 1:
                config = _json(current[3])
                current_template = str(
                    ((config.get("source_owner") or {}).get("managed_feed_template_ref")) or ""
                ) or None
                # WHICH reference the Datastream names must be the one reviewed,
                # in all three states. `bound` compares against the column;
                # `unknown` against the reference the review displayed; both
                # against NULL when the review said no Template was applicable.
                reviewed_ref = expected_template or frozen_template_ref
                if current_template != reviewed_ref:
                    raise DatastreamChangeError(
                        "The Datastream Template binding changed after this review"
                    )
                if reviewed_ref:
                    cur.execute(
                        "SELECT version FROM app.file_source_templates "
                        "WHERE id=%s AND project_id=%s",
                        (reviewed_ref, project_id),
                    )
                    template_row = cur.fetchone()
                    if frozen_template_state == "unknown":
                        # The opposite of the usual drift: what must refuse here
                        # is a Template APPEARING under a reference the reviewer
                        # was shown as resolving to nothing, which would give the
                        # change a contract nobody read.
                        if template_row is not None:
                            raise DatastreamChangeError(
                                "A Template appeared under this reference after the review"
                            )
                    elif template_row is None or int(template_row[0]) != int(
                        expected_template_version
                    ):
                        raise DatastreamChangeError(
                            "The Datastream Template version changed after this review"
                        )
                if expected_raw_import:
                    cur.execute(
                        """SELECT 1 FROM app.inbound_raw_imports r
                             JOIN app.inbound_receipts receipt ON receipt.id=r.receipt_id
                             JOIN app.datastreams d ON d.id=receipt.datastream_id
                            WHERE r.id=%s AND r.datastream_id=%s
                              AND receipt.datastream_id=%s AND d.project_id=%s""",
                        (
                            expected_raw_import,
                            datastream_id,
                            datastream_id,
                            project_id,
                        ),
                    )
                    if cur.fetchone() is None:
                        raise DatastreamChangeError(
                            "The retained raw import changed after this review"
                        )
        if row[1] == "processing":
            plan = _append_plan_version(
                tx,
                datastream_id=datastream_id,
                project_id=project_id,
                payload=proposed,
                actor=actor,
                operation_id=operation_id,
                current_plan_id=expected_plan,
            )
            with tx.cursor() as cur:
                cur.execute(
                    "SELECT mapping_payload FROM app.datastream_mapping_versions "
                    "WHERE id=%s AND datastream_id=%s AND project_id=%s",
                    (expected_mapping, datastream_id, project_id),
                )
                mapping_payload = _json(cur.fetchone()[0])
            mapping = save_field_mapping(
                datastream_id=datastream_id,
                project_id=project_id,
                mapping_payload=mapping_payload,
                identity=actor,
                idempotency_key=f"{operation_id}:mapping",
                conn=tx,
                pinned_plan_version_id=plan["id"],
                advance_pointer=False,
                commit=False,
            )
        else:
            plan = {"id": expected_plan}
            mapping = save_field_mapping(
                datastream_id=datastream_id,
                project_id=project_id,
                mapping_payload=proposed,
                identity=actor,
                idempotency_key=f"{operation_id}:mapping",
                conn=tx,
                pinned_plan_version_id=expected_plan,
                advance_pointer=False,
                commit=False,
            )
        #  A BINDING-ONLY CHANGE IS AN OVERLAY, NEVER A RE-PULL (2026-09-05). What
        #  moved is which canonical field a column names, or its binding state;
        #  the landed rows are untouched, so the mapping version is published over
        #  the current publication -- a new execution that pulls nothing -- and no
        #  candidate is minted. Measured on the reference project: nine flows
        #  pinned `date` and `channel_id`, every candidate re-pulled the source and
        #  died on the provider's 403.
        if row[1] == "mapping" and expected_mapping:
            from core.datastream_activation import (  # noqa: PLC0415
                OverlayNotApplicable,
                publish_mapping_overlay,
            )

            with tx.cursor() as cur:
                cur.execute(
                    "SELECT mapping_payload FROM app.datastream_mapping_versions "
                    "WHERE id=%s AND datastream_id=%s AND project_id=%s",
                    (expected_mapping, datastream_id, project_id),
                )
                base_row = cur.fetchone()
            base_payload = _json(base_row[0]) if base_row and base_row[0] is not None else {}
            if base_payload and binding_only_change(base_payload, proposed):
                try:
                    overlay = publish_mapping_overlay(
                        tx,
                        project_id=project_id,
                        datastream_id=datastream_id,
                        mapping_version_id=mapping["id"],
                        actor=actor,
                        operation_id=operation_id,
                    )
                except OverlayNotApplicable as why:
                    logger.info("datastream_change: overlay not applicable ds=%s: %s", datastream_id, why)
                    overlay = None
                if overlay is not None:
                    with tx.cursor() as cur:
                        cur.execute(
                            """UPDATE app.datastream_change_preparations
                                  SET state='confirmed',confirmed_at=NOW(),operation_id=%s
                                WHERE id=%s AND state='prepared'""",
                            (operation_id, preparation_id),
                        )
                    result = {
                        "preparation_id": preparation_id,
                        "operation_id": operation_id,
                        "candidate_execution_id": None,
                        "candidate_job_id": None,
                        "no_candidate_reason": (
                            "binding-only change: published as an overlay of execution "
                            f"{overlay['overlay_of']}, nothing re-pulled"
                        ),
                        "overlay_execution_id": overlay["execution_id"],
                        "overlay_of": overlay["overlay_of"],
                        "publication_log_id": overlay["publication_log_id"],
                        "plan_version_id": plan["id"],
                        "mapping_version_id": mapping["id"],
                        "active_versions_unchanged": False,
                    }
                    return MutationResult(
                        outcome="succeeded",
                        before_hash=review["before_hash"],
                        after_hash=review["after_hash"],
                        result=result,
                        outbox_payload={"event": "datastream.change.overlay_published", **result},
                    )
        projection = compile_projection(mapping)
        #  UNE GARDE N'INTERDIT QUE CE QU'ELLE EMPIRE. « Ce changement est-il
        #  valide » n'est pas « ce plan est-il executable » : le second est une
        #  propriete du FLUX, qui peut etre fausse depuis longtemps ; le premier
        #  est une propriete du GESTE. Les fondre rendait un flux abime
        #  definitivement ineditable -- mesure du 2026-08-13 sur « audience by
        #  age and gender », ou meme epingler une identite partagee, qui n'ajoute
        #  pas une ligne au scan, etait refuse par un depassement de cardinalite
        #  preexistant. Un cul-de-sac qui ne se repare que par la suppression.
        if not projection.get("executable"):
            worse = _what_got_worse(
                tx,
                datastream_id=datastream_id,
                project_id=project_id,
                after=projection,
            )
            if worse:
                raise DatastreamChangeError(
                    "The proposed change makes this Datastream worse: " + "; ".join(worse)
                )
        # Story 63.7: WHY this candidate exists, stamped from the ONE registry.
        # `change_kind` already tells the two apart -- a processing change
        # appends a plan version, anything else re-saves the mapping -- and the
        # Workbench shows "Plan change" or "Mapping change" instead of a
        # treatment nobody can account for.
        projection = stamp_origin(
            projection, PLAN_CHANGE if row[1] == "processing" else MAPPING_CHANGE
        )
        #  ENREGISTRER N'EST PAS EXECUTER, et c'est la meme distinction qu'un
        #  cran plus haut. Une exécution candidate fait TOURNER le plan, et un
        #  plan non executable ne doit rien faire tourner : `create_execution`
        #  refuse, a juste titre. Mais exiger un candidat pour CONFIRMER faisait
        #  retomber la reparation dans le cul-de-sac -- mesure du 2026-08-13 :
        #  `PublicationError: projection_not_executable`, un pas apres la garde
        #  que ce fichier venait d'ouvrir.
        #
        #  La version se pose donc seule. Le pointeur n'avance pas davantage
        #  qu'avant, rien ne tourne, et la reponse DIT pourquoi il n'y a pas de
        #  candidat -- un `null` muet se lirait comme une panne.
        candidate: dict[str, Any] | None = None
        job: dict[str, Any] | None = None
        no_candidate_reason: str | None = None
        if projection.get("executable"):
            candidate = create_execution(
                datastream_id,
                project_id,
                plan["id"],
                mapping["id"],
                projection,
                actor,
                f"{operation_id}:candidate",
                tx,
            )
            job = enqueue_activation_work(
                kind="candidate_materialization",
                project_id=project_id,
                datastream_id=datastream_id,
                execution_id=candidate["id"],
                correlation_id=candidate["id"],
                payload={"mode": current[2], "change_preparation_id": preparation_id},
                requested_by=actor,
                conn=tx,
            )
        else:
            no_candidate_reason = (
                "the change is recorded, and no candidate run was created because this "
                "plan is not executable: "
                + ", ".join(
                    sorted({str(issue.get("code")) for issue in projection.get("issues") or []})
                )
            )
        with tx.cursor() as cur:
            cur.execute(
                """UPDATE app.datastream_change_preparations
                      SET state='confirmed',confirmed_at=NOW(),operation_id=%s,
                          candidate_execution_id=%s
                    WHERE id=%s AND state='prepared'""",
                (operation_id, (candidate or {}).get("id"), preparation_id),
            )
        result = {
            "preparation_id": preparation_id,
            "operation_id": operation_id,
            "candidate_execution_id": (candidate or {}).get("id"),
            "candidate_job_id": (job or {}).get("job_id"),
            "no_candidate_reason": no_candidate_reason,
            "plan_version_id": plan["id"],
            "mapping_version_id": mapping["id"],
            "active_versions_unchanged": True,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=review["before_hash"],
            after_hash=review["after_hash"],
            result=result,
            outbox_payload={"event": "datastream.change.candidate_dispatched", **result},
        )

    operation = execute_operation(
        conn,
        OperationSpec(
            command_type=f"datastream.{row[1]}.change",
            actor=actor,
            effective_org_id=row[0],
            resource_path=("projects", project_id, "datastreams", datastream_id),
            idempotency_key=preparation_id,
            host_context={},
            versions={},
            request_payload={
                "preparation_id": preparation_id,
                "review_hash": row[6],
                "expected_plan_version_id": expected_plan,
                "expected_mapping_version_id": expected_mapping,
                "expected_raw_import_id": expected_raw_import,
                "expected_template_id": expected_template,
                "expected_template_version": expected_template_version,
                "binding_snapshot_version": binding_snapshot_version,
            },
            provider_references={},
            confirmation_mode="human",
            confirmation_reference=confirmation_secret,
            trace_id=None,
        ),
        # A spent preparation replays; it never re-enters the effect. Passing the
        # real mutation on that branch would make the guarantee depend on
        # `execute_operation` short-circuiting, which is exactly the kind of
        # promise that survives a deletion nobody notices.
        mutation=_refuse_resubmission if row[8] == "confirmed" else mutation,
    )
    # `outcome` is on the wire because AC4 asks a caller to reconcile
    # `outcome_unknown` rather than resubmit -- a caller that cannot SEE the
    # uncertainty has no way to obey.
    return {**operation.result, "replayed": operation.replayed, "outcome": operation.outcome}
