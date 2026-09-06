"""The Context Hub AI Path owner: immutable OBSERVED execution evidence.

Story 49.6 AC6/AC7, and the owner Story 50.1 AC7 consumes.

Story 49.5 registered ``context_ai_path`` as a declared-but-undelivered Evidence
producer rather than pointing the address at ``app.context_path_resolutions``,
which answers a different question: *which path should this take*, not *which
path did this take*. This module is that missing owner. Four properties it
exists to guarantee, in this order:

1. **Nothing is inferred.** Only observable calls, identifiers, versions, order
   and outcomes are recorded. There is no parameter, column or return field for
   model reasoning. Absent instrumentation produces
   :data:`VERDICT_UNVERIFIABLE`, never a pass and never a reconstructed step.
2. **`No AI path` is a literal, not a missing value.** Human-only work has no
   path at all. :func:`ai_path_reference` returns the exact string
   :data:`NO_AI_PATH` so a caller stores that, rather than ``null`` or
   ``deferred`` — the three substitutions Story 50.1 AC7 refuses by name.
3. **Assessment is derived from the PINNED policy.** The pre-query snapshot is
   frozen on the header before the execution runs, and
   :func:`assess` reads that snapshot, never today's policy. A stored verdict
   would let a historical path re-judge itself under rules written after it.
4. **Finalized evidence is frozen.** The database refuses it
   (``trg_ai_paths_immutable``, ``trg_ai_path_steps_append_only``, migration
   150); this module never asks it to bend.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from datetime import datetime
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

#: The exact literal Story 50.1 AC7 requires for a Result produced without AI
#: involvement. Not a sentinel row, not `null`, not `deferred`. Callers store
#: this string.
NO_AI_PATH = "No AI path"

LIFECYCLE_RECORDING = "recording"
LIFECYCLE_FINALIZED = "finalized"

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOMES = (OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_REFUSED, OUTCOME_UNAVAILABLE)

OBSERVED_AI_PATH_SCHEMA_VERSION = "observed-ai-path.v1"
AI_PATH_CONTENT_V2 = "ai-path-content.v2"
OBSERVED_AI_PATH_MAX_STEPS = 200
OBSERVED_AI_PATH_MAX_BYTES = 262_144
RETRIEVAL_BRANCH_EVIDENCE_SCHEMA_VERSION = "retrieval-branch-evidence.v1"
BRANCH_EVIDENCE_MAX_CANDIDATES_PER_STEP = 24
BRANCH_EVIDENCE_MAX_CANDIDATES_PER_PATH = 200
BRANCH_EVIDENCE_MAX_BYTES_PER_STEP = 8_192
BRANCH_EVIDENCE_MAX_BYTES_PER_PATH = 65_536
OBSERVED_MISSING_CONTEXT = frozenset(
    {"required_but_missing", "evidence_unverifiable", "not_recorded", "unavailable"}
)
_SKILL_RESOLUTIONS = frozenset(
    {"resolved", "unavailable", "unknown_version", "no_sequence", "unknown_step"}
)

STEP_KINDS = (
    "tool_call",
    "knowledge_read",
    "skill_step",
    "semantic_query",
    "data_read",
    "handoff",
)

OWNER_WORKSPACES = ("data", "governance", "analyze", "context-hub", "test")

#: The seven distinctions Story 49.6 AC6 requires a finalized path to express.
#: They are FINDING kinds, not verdicts: one path can carry several.
FINDING_REQUIRED_OBSERVED = "required_and_observed"
FINDING_REQUIRED_MISSING = "required_but_missing"
FINDING_FORBIDDEN_OBSERVED = "observed_but_forbidden"
FINDING_APPROVED_ALTERNATIVE = "approved_alternative"
FINDING_VERSION_MISMATCH = "version_mismatch"
FINDING_OUT_OF_ORDER = "out_of_order"
FINDING_UNVERIFIABLE = "evidence_unverifiable"
#: 2026-09-05 -- the Skill's own expectation, pinned at opening (`expected_skill_steps`).
FINDING_SKILL_STEP_CROSSED = "required_skill_step_crossed"
FINDING_SKILL_STEP_SKIPPED = "required_skill_step_skipped"
FINDING_SKILL_STEP_EXPECTED_SKIPPED = "expected_skill_step_skipped"
FINDING_SKILL_STEPS_OUT_OF_ORDER = "skill_steps_out_of_order"

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNVERIFIABLE = "unverifiable"

#: Findings that make a verdict fail. `approved_alternative` is deliberately
#: absent: an approved alternative IS the policy being followed, and counting it
#: as a deviation is how a correct execution starts reading as a violation.
_FAILING_FINDINGS = frozenset(
    {FINDING_REQUIRED_MISSING, FINDING_FORBIDDEN_OBSERVED, FINDING_VERSION_MISMATCH,
     FINDING_OUT_OF_ORDER, FINDING_SKILL_STEP_SKIPPED, FINDING_SKILL_STEPS_OUT_OF_ORDER}
)

_TRACE_ID_LENGTH = 32
_BRANCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_BRANCH_TOOLS = {
    "search_context": "context_search",
    "briefing_context_event": "briefing_context_event",
}


class AiPathError(ValueError):
    """An AI Path operation was rejected."""

    code = "invalid_ai_path_operation"


class AiPathNotFound(AiPathError):
    code = "ai_path_not_found"


class AiPathFinalized(AiPathError):
    code = "ai_path_finalized"


def _mint(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AiPathError(f"{label} is required")
    return value.strip()


def _optional(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require(value, label)


def _detail_json(detail: Mapping[str, Any] | None, sanitize) -> str | None:
    """Serialise a recorded detail map, or NULL when the step judged nothing.

    Sanitising HERE and not at the call site is deliberate: ``append_step`` has
    one caller today and the whole point of migration 176's CHECK is that a
    second one will arrive. An empty map after sanitising is NULL, not ``{}`` --
    "everything it offered was refused" and "it judged nothing" are the same
    fact for a reader, and a stored ``{}`` would decode to `no_branch_judged`
    (a zero) instead of `branches_not_recorded`.
    """
    cleaned = sanitize(detail)
    if not cleaned:
        return None
    return canonical_json(cleaned)


def _validate_trace_id(value: Any) -> str | None:
    """A W3C trace id is opaque correlation, never identity.

    All-zero is the W3C *invalid* value. Storing it would correlate every
    uninstrumented execution into one apparent trace, which is worse than
    storing nothing.
    """
    if value is None:
        return None
    candidate = _require(value, "w3c_trace_id").lower()
    if len(candidate) != _TRACE_ID_LENGTH or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise AiPathError("w3c_trace_id must be 32 lowercase hexadecimal characters")
    if candidate == "0" * _TRACE_ID_LENGTH:
        raise AiPathError("w3c_trace_id must not be the all-zero invalid trace id")
    return candidate


#: What `ai_path_steps.owner_object_type` accepts (migration 150) -- held HERE, by the
#: writer, so no emitter can hand the column a word it refuses and lose the row to
#: the observer's swallow (AI-376, class closed on Opus review F1).
OWNER_OBJECT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,60}$")


def _node_key(
    workspace: Any, object_type: Any, object_id: Any
) -> tuple[str, str, str] | None:
    """The identity a policy entry and an observed step are compared on.

    The object type is the COLUMN's word on both sides (AI-376, F2): an author who
    writes the detail vocabulary (`schema_doc`, `context_event`) in an expected node
    is compared against the stored `schema-doc` / `context-event`, not silently
    never matched.
    """
    if not workspace or not object_type or not object_id:
        return None
    from core.candidate_emission import owner_object_type_for  # noqa: PLC0415

    return (str(workspace), str(owner_object_type_for(str(object_type))), str(object_id))


# ---------------------------------------------------------------------------
# Recording.
# ---------------------------------------------------------------------------


def begin_path(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    policy_snapshot: Mapping[str, Any] | None = None,
    w3c_trace_id: str | None = None,
    execution_correlation: str | None = None,
    model_ref: str | None = None,
    tool_catalog_version: str | None = None,
) -> dict[str, Any]:
    """Open one recording path and PIN its pre-query policy.

    The snapshot is taken here, before the execution runs, because it is the
    thing the execution will be judged against. Reading it afterwards would
    judge a past run by present rules.
    """
    snapshot = dict(policy_snapshot or {})
    path_id = _mint("aip")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.ai_paths
                (id, org_id, project_id, actor, w3c_trace_id, execution_correlation,
                 model_ref, tool_catalog_version, policy_snapshot, policy_snapshot_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id, lifecycle, started_at
            """,
            (
                path_id,
                _require(org_id, "org_id"),
                _require(project_id, "project_id"),
                _require(actor, "actor"),
                _validate_trace_id(w3c_trace_id),
                _optional(execution_correlation, "execution_correlation"),
                _optional(model_ref, "model_ref"),
                _optional(tool_catalog_version, "tool_catalog_version"),
                canonical_json(snapshot),
                content_hash(snapshot),
            ),
        )
        row = cur.fetchone()
    return {"id": row[0], "lifecycle": row[1], "started_at": row[2]}


def append_step(
    conn,
    *,
    path_id: str,
    project_id: str,
    step_kind: str,
    outcome: str,
    owner_workspace: str | None = None,
    owner_object_type: str | None = None,
    owner_object_id: str | None = None,
    owner_version_id: str | None = None,
    skill_version_id: str | None = None,
    skill_step_id: str | None = None,
    tool_name: str | None = None,
    evidence_record_id: str | None = None,
    observed_at: datetime | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one observed call. Order is allocated server-side, never by the caller.

    A step that reached nothing governed keeps its owner fields NULL instead of
    being dropped: an unrepresented tool call must stay visible in the path
    rather than have a graph object invented for it.

    ``detail`` (migration 176) is what this crossing JUDGED -- the Story 54.2
    candidate lists and walk descriptor. It goes through ``sanitize_detail``, the
    same lock the wire uses, so a recorded step and an emitted one carry the same
    shape; ``ck_ai_path_steps_detail_recordable`` restates that lock in the
    database for any writer that arrives without it.
    """
    from core.ai_path_recorder import sanitize_detail  # noqa: PLC0415
    if step_kind not in STEP_KINDS:
        raise AiPathError(f"step_kind must be one of {list(STEP_KINDS)}")
    if outcome not in OUTCOMES:
        raise AiPathError(f"outcome must be one of {list(OUTCOMES)}")
    if owner_workspace is not None and owner_workspace not in OWNER_WORKSPACES:
        raise AiPathError(f"owner_workspace must be one of {list(OWNER_WORKSPACES)}")
    if owner_object_type is not None and not OWNER_OBJECT_TYPE_PATTERN.match(str(owner_object_type)):
        # Said by the writer, not by the column (AI-376): the column's refusal was
        # swallowed by the observer and the row lost; the writer's is a named error.
        raise AiPathError("owner_object_type must match ^[a-z][a-z0-9-]{2,60}$ (the column's word, hyphens never underscores)")
    if (skill_version_id is None) != (skill_step_id is None):
        raise AiPathError("skill_version_id and skill_step_id are pinned together or not at all")
    if observed_at is not None and (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise AiPathError("observed_at must be a timezone-aware datetime")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, lifecycle FROM app.ai_paths WHERE id = %s AND project_id = %s",
            (_require(path_id, "path_id"), _require(project_id, "project_id")),
        )
        header = cur.fetchone()
        if header is None:
            raise AiPathNotFound("AI Path not found")
        if header[1] != LIFECYCLE_RECORDING:
            raise AiPathFinalized("cannot append a step to a finalized AI Path")

        cur.execute(
            "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM app.ai_path_steps WHERE path_id = %s",
            (path_id,),
        )
        ordinal = cur.fetchone()[0]

        step_id = _mint("aps")
        cur.execute(
            """
            INSERT INTO app.ai_path_steps
                (id, path_id, org_id, project_id, ordinal, step_kind, owner_workspace,
                 owner_object_type, owner_object_id, owner_version_id, skill_version_id,
                 skill_step_id, tool_name, outcome, evidence_record_id, observed_at, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE(%s, NOW()), %s::jsonb)
            RETURNING id, ordinal
            """,
            (
                step_id,
                path_id,
                header[0],
                project_id,
                ordinal,
                step_kind,
                owner_workspace,
                _optional(owner_object_type, "owner_object_type"),
                _optional(owner_object_id, "owner_object_id"),
                _optional(owner_version_id, "owner_version_id"),
                _optional(skill_version_id, "skill_version_id"),
                _optional(skill_step_id, "skill_step_id"),
                _optional(tool_name, "tool_name"),
                outcome,
                _optional(evidence_record_id, "evidence_record_id"),
                observed_at,
                _detail_json(detail, sanitize_detail),
            ),
        )
        row = cur.fetchone()
    return {"id": row[0], "ordinal": row[1]}


def path_content_preimage(
    *,
    path_id: str,
    outcome: str,
    policy_snapshot_hash: str,
    steps: Sequence[Mapping[str, Any]],
    content_hash_contract: str | None = None,
) -> dict[str, Any]:
    """Return the frozen hash preimage without mutating stored step values.

    The historical contract deliberately omits ``detail``. V2 is exactly that
    shape plus its discriminator and one explicit detail value per step, where
    absence is represented by ``null`` rather than by a missing key.
    """
    if content_hash_contract not in (None, AI_PATH_CONTENT_V2):
        raise AiPathError(f"unknown AI Path content hash contract {content_hash_contract!r}")
    projected_steps = [
        {
            "ordinal": step["ordinal"],
            "step_kind": step["step_kind"],
            "owner": [
                step["owner_workspace"],
                step["owner_object_type"],
                step["owner_object_id"],
                step["owner_version_id"],
            ],
            "skill": [step["skill_version_id"], step["skill_step_id"]],
            "tool_name": step["tool_name"],
            "outcome": step["outcome"],
        }
        for step in steps
    ]
    preimage: dict[str, Any] = {
        "path_id": path_id,
        "outcome": outcome,
        "policy_snapshot_hash": policy_snapshot_hash,
        "steps": projected_steps,
    }
    if content_hash_contract == AI_PATH_CONTENT_V2:
        preimage["schema_version"] = AI_PATH_CONTENT_V2
        for projected, source in zip(projected_steps, steps, strict=True):
            projected["detail"] = source.get("detail")
    return preimage


def canonical_json_v2(value: Any) -> str:
    """Pure v2 serializer: sorted, compact, UTF-8 text with no ASCII escaping."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _content_hash_v2(value: Any) -> str:
    return hashlib.sha256(canonical_json_v2(value).encode("utf-8")).hexdigest()


def finalize_path(
    conn, *, path_id: str, project_id: str, outcome: str
) -> dict[str, Any]:
    """Freeze the path with an honest outcome and an integrity hash.

    A failed execution is finalized as ``failed`` WITH the steps it managed to
    record. Discarding those would delete the only evidence of how it failed.
    """
    if outcome not in OUTCOMES:
        raise AiPathError(f"outcome must be one of {list(OUTCOMES)}")

    with conn.cursor() as cur:
        cur.execute(
            # LOCKED WHILE IT IS HASHED (round 2, finding 6): a step appended by a
            # parallel call between this read and the update would leave a finalized
            # path whose content hash does not describe its steps. `FOR UPDATE` makes
            # the trigger's read and the appending insert wait for this outcome.
            "SELECT lifecycle, policy_snapshot_hash, policy_snapshot FROM app.ai_paths "
            "WHERE id = %s AND project_id = %s FOR UPDATE",
            (_require(path_id, "path_id"), _require(project_id, "project_id")),
        )
        header = cur.fetchone()
        if header is None:
            raise AiPathNotFound("AI Path not found")
        if header[0] == LIFECYCLE_FINALIZED:
            raise AiPathFinalized("AI Path is already finalized")

        steps = _load_steps(cur, path_id)
        snapshot = header[2] if isinstance(header[2], Mapping) else {}
        contract = snapshot.get("content_hash_contract")
        preimage = path_content_preimage(
            path_id=path_id,
            outcome=outcome,
            policy_snapshot_hash=header[1],
            steps=steps,
            content_hash_contract=contract,
        )
        digest = _content_hash_v2(preimage) if contract == AI_PATH_CONTENT_V2 else content_hash(
            preimage
        )
        ended_at_sql = "clock_timestamp()" if contract == AI_PATH_CONTENT_V2 else "NOW()"
        cur.execute(
            f"""
            UPDATE app.ai_paths
               SET lifecycle = 'finalized', outcome = %s,
                   ended_at = {ended_at_sql}, content_hash = %s
             WHERE id = %s AND project_id = %s
            RETURNING id, outcome, ended_at, content_hash
            """,
            (outcome, digest, path_id, project_id),
        )
        row = cur.fetchone()
    return {"id": row[0], "outcome": row[1], "ended_at": row[2], "content_hash": row[3]}


# ---------------------------------------------------------------------------
# Reading.
# ---------------------------------------------------------------------------

_STEP_COLUMNS = (
    "id, ordinal, step_kind, owner_workspace, owner_object_type, owner_object_id, "
    "owner_version_id, skill_version_id, skill_step_id, tool_name, outcome, "
    "evidence_record_id, observed_at, detail"
)


def _load_steps(cur, path_id: str) -> list[dict[str, Any]]:
    cur.execute(
        f"SELECT {_STEP_COLUMNS} FROM app.ai_path_steps WHERE path_id = %s ORDER BY ordinal",
        (path_id,),
    )
    names = (
        "id", "ordinal", "step_kind", "owner_workspace", "owner_object_type",
        "owner_object_id", "owner_version_id", "skill_version_id", "skill_step_id",
        "tool_name", "outcome", "evidence_record_id", "observed_at", "detail",
    )
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _resolve_skill_pins(conn, steps: list[dict[str, Any]]) -> None:
    """Poser `skill` sur chaque pas epingle -- une resolution par PAIRE DISTINCTE.

    Un chemin repasse le meme pas plusieurs fois ; le resoudre a chaque
    occurrence ferait N allers-retours pour une reponse identique, le cout que
    `record_fates` a mesure puis refuse a cote (8.29 ms contre 2.62 ms).

    `skill` reste ABSENT quand rien n'est epingle. Un dictionnaire vide se
    lirait << resolu, et il n'y avait rien >>.
    """
    from core import skill_steps  # noqa: PLC0415

    cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for step in steps:
        version = step.get("skill_version_id")
        reference = step.get("skill_step_id")
        if not version or not reference:
            continue
        key = (version, reference)
        if key not in cache:
            cache[key] = skill_steps.resolve_pin(
                conn, skill_version_id=version, skill_step_id=reference
            )
        step["skill"] = cache[key]


def load_path(conn, *, path_id: str, project_id: str, assess_over_interaction: bool = True) -> dict[str, Any]:
    """Return the header, its ordered steps and the derived assessment.

    Project scope is part of the lookup, not a filter applied afterwards: a
    foreign id and a nonexistent id both raise the same
    :class:`AiPathNotFound`, so neither confirms that the other Project's path
    exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, org_id, project_id, lifecycle, outcome, w3c_trace_id, actor,
                   execution_correlation, started_at, ended_at, model_ref,
                   tool_catalog_version, policy_snapshot, policy_snapshot_hash, content_hash
              FROM app.ai_paths
             WHERE id = %s AND project_id = %s
            """,
            (_require(path_id, "path_id"), _require(project_id, "project_id")),
        )
        row = cur.fetchone()
        if row is None:
            raise AiPathNotFound("AI Path not found")
        names = (
            "id", "org_id", "project_id", "lifecycle", "outcome", "w3c_trace_id",
            "actor", "execution_correlation", "started_at", "ended_at", "model_ref",
            "tool_catalog_version", "policy_snapshot", "policy_snapshot_hash",
            "content_hash",
        )
        header = dict(zip(names, row))
        steps = _load_steps(cur, path_id)

    # Story 45.7 (AC1/AC2) -- CE QUE LE PAS DISAIT, resolu ICI et une seule fois.
    #
    # Mesure de la relecture adversariale du 2026-08-05 : `skill_steps.resolve()`
    # n'avait aucun lecteur sur une surface de chemin -- la console recevait
    # `skill_version_id` brut, `trace_observation` le couple brut, ni intitule ni
    # action. Le referent existait, etait teste, et ne servait personne.
    #
    # POURQUOI DANS `load_path` ET PAS DANS CHAQUE SURFACE. Les deux lecteurs de
    # ce chemin (`ai_paths_api._get_ai_path` par `wire_step_projection`, et
    # `trace_observation.load_trace_observation` par `compose_lenses`) passent
    # tous deux par ici. Resoudre dans chacun aurait fait deux resolutions de la
    # meme paire -- le second dialecte que ce depot ferme partout ailleurs.
    #
    # ⚠️ CONTRE LA VERSION SERVIE, JAMAIS CONTRE LA COURANTE : le couple porte
    # `procedure_id@version_number`, et `app.procedures_versions` en garde une
    # copie immuable. Une Skill qui avance ne re-etiquette pas une execution
    # deja faite -- c'est l'AC2, et c'est structurel, pas promis.
    _resolve_skill_pins(conn, steps)
    header["steps"] = steps
    # JUDGED OVER THE INTERACTION when the policy is a Skill's (2026-09-05): a
    # Result's execution is its own path and the calls around it another, under
    # one trace; the Skill was followed across both or not at all. The path's
    # own steps stay what they are -- only the reading widens.
    judged = steps
    snapshot = header.get("policy_snapshot")
    if assess_over_interaction and judged_over_interaction(snapshot, header.get("w3c_trace_id")):
        try:
            judged = interaction_steps(conn, project_id=header["project_id"], path=header)
        except Exception:  # noqa: BLE001 -- a sibling that cannot be read narrows the reading, never breaks it
            judged = steps
    header["assessment"] = assess(header["policy_snapshot"], judged, outcome=header["outcome"])
    return header


def _unavailable_observed_ai_path() -> dict[str, str]:
    return {
        "schema_version": OBSERVED_AI_PATH_SCHEMA_VERSION,
        "state": "unavailable",
        "reason": "unavailable",
    }


def _bounded_projection_text(value: Any, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("unsafe observed AI Path text")
    return value


def _rfc3339(value: Any) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed AI Path timestamps must carry a timezone")
    return value.isoformat()


def _project_observed_owner(step: Mapping[str, Any]) -> dict[str, str] | None:
    step_kind = _bounded_projection_text(step.get("step_kind"), required=True)
    outcome = _bounded_projection_text(step.get("outcome"), required=True)
    if step_kind not in STEP_KINDS or outcome not in OUTCOMES:
        raise ValueError("unsafe observed AI Path vocabulary")
    workspace = step.get("owner_workspace")
    object_type = step.get("owner_object_type")
    object_id = step.get("owner_object_id")
    owner_values = (workspace, object_type, object_id)
    if all(value is None for value in owner_values):
        owner = None
    elif all(value is not None for value in owner_values):
        workspace = _bounded_projection_text(workspace, required=True)
        if workspace not in OWNER_WORKSPACES:
            raise ValueError("unsafe observed AI Path owner workspace")
        owner = {
            "workspace": workspace,
            "object_type": _bounded_projection_text(object_type, required=True),
            "object_id": _bounded_projection_text(object_id, required=True),
        }
        owner_version = _bounded_projection_text(step.get("owner_version_id"))
        if owner_version is not None:
            owner["version_id"] = owner_version
    else:
        raise ValueError("partial observed AI Path owner")
    return owner


def _project_missing_context(step: Mapping[str, Any]) -> list[str]:
    detail = step.get("detail")
    raw_missing = detail.get("missing_context") if isinstance(detail, Mapping) else None
    if isinstance(raw_missing, str):
        raw_missing = [raw_missing]
    missing_context = []
    if isinstance(raw_missing, (list, tuple)):
        for item in raw_missing:
            if item in OBSERVED_MISSING_CONTEXT and item not in missing_context:
                missing_context.append(item)
    return missing_context


def _branch_state(state: str) -> dict[str, str]:
    return {
        "schema_version": RETRIEVAL_BRANCH_EVIDENCE_SCHEMA_VERSION,
        "state": state,
    }


def _branch_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("unsafe branch integer")
    return value


def _branch_number(value: Any, *, nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("unsafe branch number")
    return value


def _branch_text(value: Any, *, title: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("unsafe branch text")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError("unsafe branch control")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("branch text was not frozen as NFC")
    if title:
        if " ".join(value.split()) != value or len(value) > 200:
            raise ValueError("unsafe branch title")
    elif len(value) > 128:
        raise ValueError("unsafe branch descriptor")
    return value


def _project_marked_branch_detail(
    detail: Mapping[str, Any], *, producer: str
) -> dict[str, Any]:
    """Revalidate one stored flat producer value and return its closed public form."""
    from core import candidate_emission, candidate_fate  # noqa: PLC0415

    sentinel_keys = {"branch_schema_version", "branch_producer", "branch_state"}
    if set(detail) == sentinel_keys:
        if (
            detail.get("branch_schema_version") == candidate_emission.BRANCH_SCHEMA_VERSION
            and detail.get("branch_producer") == producer
            and detail.get("branch_state") == candidate_emission.BRANCH_STATE_UNAVAILABLE
        ):
            return _branch_state("unavailable")
        raise ValueError("invalid branch sentinel")

    list_keys = {
        field: "candidate_matched" if field == "matched" else f"candidate_{field}s"
        for field in candidate_emission.CANDIDATE_FIELDS
    }
    common_keys = {
        "branch_schema_version",
        "branch_producer",
        "retrieval_mode",
        "graph_hop_depth",
        "semantic_recall",
        "limit",
        "not_reached_enumerated",
        "reached_count",
        "selected_count",
        "rejected_count",
        "candidates_listed",
        "listing_truncated",
        *list_keys.values(),
    }
    tier_keys = {"tiers_title", "tiers_description", "tiers_neighbor"}
    expected_keys = common_keys | (tier_keys if producer == "context_search" else set())
    if set(detail) != expected_keys:
        raise ValueError("unknown or missing stored branch field")
    if (
        detail.get("branch_schema_version") != candidate_emission.BRANCH_SCHEMA_VERSION
        or detail.get("branch_producer") != producer
    ):
        raise ValueError("stored branch discriminator mismatch")

    mode = _branch_text(detail.get("retrieval_mode"))
    graph_depth = _branch_int(detail.get("graph_hop_depth"))
    selection_limit = _branch_int(detail.get("limit"))
    semantic_recall = detail.get("semantic_recall")
    listing_truncated = detail.get("listing_truncated")
    if not isinstance(semantic_recall, bool) or not isinstance(listing_truncated, bool):
        raise ValueError("unsafe branch boolean")
    if detail.get("not_reached_enumerated") is not False:
        raise ValueError("v1 cannot enumerate unreached candidates")

    judged = _branch_int(detail.get("reached_count"))
    selected = _branch_int(detail.get("selected_count"))
    rejected = _branch_int(detail.get("rejected_count"))
    raw_listed = _branch_int(detail.get("candidates_listed"))
    if judged != selected + rejected or raw_listed > judged or selected > selection_limit:
        raise ValueError("branch counts disagree")
    if " ".join(mode.split()) != mode:
        raise ValueError("branch retrieval mode was not frozen canonically")
    if listing_truncated is not (raw_listed < judged):
        raise ValueError("branch truncation disagrees with counts")

    arrays: dict[str, list[Any]] = {}
    for field, key in list_keys.items():
        value = detail.get(key)
        if not isinstance(value, list) or len(value) != raw_listed:
            raise ValueError("branch arrays are not aligned real lists")
        arrays[field] = value

    if producer == "context_search":
        allowed_kinds = {
            candidate_emission.OBJECT_TYPE_TOPIC,
            candidate_emission.OBJECT_TYPE_PROCEDURE,
            candidate_emission.OBJECT_TYPE_SCHEMA_DOC,
        }
        allowed_reasons = set(candidate_fate.TREE_WALK_REASONS)
        tier_scale: dict[str, float] | None = {
            name: float(_branch_number(detail.get(f"tiers_{name}")))
            for name in ("title", "description", "neighbor")
        }
    else:
        allowed_kinds = {candidate_emission.OBJECT_TYPE_CONTEXT_EVENT}
        allowed_reasons = set(candidate_fate.BRIEFING_CONTEXT_EVENT_REASONS)
        tier_scale = None

    branches: list[dict[str, Any]] = []
    previous_rank = 0
    listed_selected = 0
    listed_rejected = 0
    for index in range(raw_listed):
        candidate_id = arrays["id"][index]
        if not isinstance(candidate_id, str) or _BRANCH_ID.fullmatch(candidate_id) is None:
            raise ValueError("unsafe branch id")
        kind = arrays["kind"][index]
        if kind not in allowed_kinds:
            raise ValueError("unsafe producer branch kind")
        title = _branch_text(arrays["title"][index], title=True)
        rank = _branch_int(arrays["rank"][index])
        if rank == 0 or rank <= previous_rank:
            raise ValueError("branch ranks are not strictly increasing")
        previous_rank = rank
        fate = arrays["fate"][index]
        reason = arrays["reason"][index]
        if fate == candidate_fate.FATE_SELECTED and reason is None:
            listed_selected += 1
        elif fate == candidate_fate.FATE_REJECTED and reason in allowed_reasons:
            listed_rejected += 1
        else:
            raise ValueError("unsafe producer fate or reason")

        if producer == "context_search":
            score = float(_branch_number(arrays["score"][index]))
            tier = arrays["tier"][index]
            matched = arrays["matched"][index]
            if tier not in tier_scale or not isinstance(matched, bool):
                raise ValueError("unsafe context-search score metadata")
        else:
            score = arrays["score"][index]
            tier = arrays["tier"][index]
            matched = arrays["matched"][index]
            if score is not None or tier is not None or matched is not None:
                raise ValueError("briefing score metadata must be null")

        if index < BRANCH_EVIDENCE_MAX_CANDIDATES_PER_STEP:
            branches.append(
                {
                    "id": candidate_id,
                    "kind": kind,
                    "title": title,
                    "score": score,
                    "tier": tier,
                    "matched": matched,
                    "rank": rank,
                    "fate": fate,
                    "reason": reason,
                }
            )

    if listed_selected > selected or listed_rejected > rejected:
        raise ValueError("listed branch fates exceed exact counts")
    listed_count = len(branches)
    if judged and not listed_count:
        raise ValueError("a judged v1 walk cannot have an empty listing")
    state = "branches_listed" if listed_count else "no_branch_judged"
    return {
        "schema_version": RETRIEVAL_BRANCH_EVIDENCE_SCHEMA_VERSION,
        "state": state,
        "walk": {
            "producer": producer,
            "mode": mode,
            "graph_hop_depth": graph_depth,
            "semantic_recall": semantic_recall,
            "selection_limit": selection_limit,
            "judged_count": judged,
            "selected_count": selected,
            "rejected_count": rejected,
            "listed_count": listed_count,
            "listing_truncated": listing_truncated or raw_listed > listed_count,
            "not_reached_enumerated": False,
            "tier_scale": tier_scale,
        },
        "branches": branches,
    }


def _project_branch_evidence(step: Mapping[str, Any]) -> dict[str, Any] | None:
    if step.get("step_kind") != "knowledge_read":
        return None
    producer = _BRANCH_TOOLS.get(step.get("tool_name"))
    if producer is None:
        return None
    detail = step.get("detail")
    if not isinstance(detail, Mapping) or not detail:
        return _branch_state("branches_not_recorded")
    if "branch_schema_version" not in detail and "branch_producer" not in detail:
        return _branch_state("branches_not_recorded")
    try:
        return _project_marked_branch_detail(detail, producer=producer)
    except (TypeError, ValueError):
        return _branch_state("unavailable")


def project_branch_evidence_for_steps(
    steps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any] | None]:
    """Allocate whole normalized evidence steps in persisted ordinal order."""
    values = list(steps)
    projected: list[dict[str, Any] | None] = [None] * len(values)
    eligible = [
        index
        for index, step in enumerate(values)
        if step.get("step_kind") == "knowledge_read" and step.get("tool_name") in _BRANCH_TOOLS
    ]
    eligible.sort(
        key=lambda index: (
            values[index].get("ordinal")
            if isinstance(values[index].get("ordinal"), int)
            and not isinstance(values[index].get("ordinal"), bool)
            else 2**63,
            index,
        )
    )
    candidates_used = 0
    bytes_used = 0
    aggregate_closed = False
    for index in eligible:
        if aggregate_closed:
            projected[index] = _branch_state("unavailable")
            continue
        evidence = _project_branch_evidence(values[index])
        assert evidence is not None
        size = len(canonical_json_v2(evidence).encode("utf-8"))
        if size > BRANCH_EVIDENCE_MAX_BYTES_PER_STEP:
            projected[index] = _branch_state("unavailable")
            continue
        candidate_count = len(evidence.get("branches", ()))
        if (
            candidates_used + candidate_count > BRANCH_EVIDENCE_MAX_CANDIDATES_PER_PATH
            or bytes_used + size > BRANCH_EVIDENCE_MAX_BYTES_PER_PATH
        ):
            projected[index] = _branch_state("unavailable")
            aggregate_closed = True
            continue
        projected[index] = evidence
        candidates_used += candidate_count
        bytes_used += size
    return projected


def project_observed_ai_path_progress_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """Project one provisional crossing through the final evidence allowlist.

    A live sequence is not a persisted ordinal and carries no database timestamp
    or Skill resolution. Only fields already eligible for the finalized
    ``observed-ai-path.v1`` step survive this strict, closed projection.
    """
    if not isinstance(step, Mapping):
        raise ValueError("unsafe observed AI Path step")
    allowed = {
        "step_kind",
        "outcome",
        "tool_name",
        "owner_workspace",
        "owner_object_type",
        "owner_object_id",
        "owner_version_id",
        "evidence_record_id",
        "detail",
    }
    if any(not isinstance(key, str) or key not in allowed for key in step):
        raise ValueError("unknown observed AI Path progress field")
    step_kind = _bounded_projection_text(step.get("step_kind"), required=True)
    outcome = _bounded_projection_text(step.get("outcome"), required=True)
    if step_kind not in STEP_KINDS or outcome not in OUTCOMES:
        raise ValueError("unsafe observed AI Path vocabulary")
    owner = _project_observed_owner(step)
    projected = {
        "step_kind": step_kind,
        "outcome": outcome,
        "missing_context": _project_missing_context(step),
    }
    tool_name = _bounded_projection_text(step.get("tool_name"))
    if tool_name is not None:
        projected["tool_name"] = tool_name
    if owner is not None:
        projected["owner"] = owner
    evidence_record_id = _bounded_projection_text(step.get("evidence_record_id"))
    if evidence_record_id is not None:
        projected["evidence_record_id"] = evidence_record_id
    return projected


def _project_observed_step(
    step: Mapping[str, Any], *, branch_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    ordinal = step.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ValueError("unsafe observed AI Path ordinal")
    step_kind = _bounded_projection_text(step.get("step_kind"), required=True)
    outcome = _bounded_projection_text(step.get("outcome"), required=True)
    if step_kind not in STEP_KINDS or outcome not in OUTCOMES:
        raise ValueError("unsafe observed AI Path vocabulary")
    owner = _project_observed_owner(step)

    skill_version = step.get("skill_version_id")
    skill_step = step.get("skill_step_id")
    if skill_version is None and skill_step is None:
        skill = None
    elif skill_version is not None and skill_step is not None:
        resolution = (step.get("skill") or {}).get("state")
        if resolution not in _SKILL_RESOLUTIONS:
            raise ValueError("unsafe observed AI Path Skill resolution")
        skill = {
            "version_id": _bounded_projection_text(skill_version, required=True),
            "step_id": _bounded_projection_text(skill_step, required=True),
            "state": resolution,
        }
        label = _bounded_projection_text((step.get("skill") or {}).get("label"))
        if label is not None:
            skill["label"] = label
    else:
        raise ValueError("partial observed AI Path Skill pin")

    projected = {
        "ordinal": ordinal,
        "step_kind": step_kind,
        "outcome": outcome,
        "observed_at": _rfc3339(step.get("observed_at")),
        "missing_context": _project_missing_context(step),
    }
    tool_name = _bounded_projection_text(step.get("tool_name"))
    if tool_name is not None:
        projected["tool_name"] = tool_name
    if owner is not None:
        projected["owner"] = owner
    if skill is not None:
        projected["skill"] = skill
    evidence_record_id = _bounded_projection_text(step.get("evidence_record_id"))
    if evidence_record_id is not None:
        projected["evidence_record_id"] = evidence_record_id
    # `chose` is NOT on this projection: the Result tab's decoder holds an exact
    # key set (`ui/cards/shell/src/viz/aiPathCapability.tsx`), and a key it does
    # not know is a malformed capability. The choice travels on the wire
    # projection (console API) and on `get_ai_path`; the Result tab follows
    # when the shell renderer is extended, with its identity and fixtures.
    if branch_evidence is not None:
        projected["branch_evidence"] = dict(branch_evidence)
    return projected


def project_observed_ai_path(
    conn, *, project_id: str, ai_path: Any
) -> dict[str, Any]:
    """Project one Result-owned path into the sole safe surface contract.

    The exact human literal is evidence in its own right. Every other missing,
    foreign, provisional, malformed or over-budget input converges on the same
    nondisclosing state so this reader cannot become an identity oracle.
    """
    if ai_path == NO_AI_PATH:
        return {
            "schema_version": OBSERVED_AI_PATH_SCHEMA_VERSION,
            "state": "human_absent",
            "literal": NO_AI_PATH,
        }
    if not isinstance(ai_path, str) or not ai_path or not isinstance(project_id, str):
        return _unavailable_observed_ai_path()
    try:
        # The assessment is not projected here: no widening to the siblings (round 3).
        path = load_path(conn, path_id=ai_path, project_id=project_id, assess_over_interaction=False)
        if path.get("lifecycle") != LIFECYCLE_FINALIZED or path.get("outcome") not in OUTCOMES:
            return _unavailable_observed_ai_path()
        raw_steps = path.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) > OBSERVED_AI_PATH_MAX_STEPS:
            return _unavailable_observed_ai_path()
        snapshot = path.get("policy_snapshot")
        branch_evidence = (
            project_branch_evidence_for_steps(raw_steps)
            if isinstance(snapshot, Mapping)
            and snapshot.get("content_hash_contract") == AI_PATH_CONTENT_V2
            else [
                _branch_state("branches_not_recorded")
                if step.get("step_kind") == "knowledge_read"
                and step.get("tool_name") in _BRANCH_TOOLS
                else None
                for step in raw_steps
            ]
        )
        steps = [
            _project_observed_step(step, branch_evidence=evidence)
            for step, evidence in zip(raw_steps, branch_evidence)
        ]
        # THE READER'S WORDS (2026-09-05): the owner's own name beside its id, and
        # what the call chose. Both bounded, both optional -- a name that cannot
        # be read is absent, and the decoder of every surface accepts absence.
        try:
            labels = owner_labels(conn, raw_steps)
        except Exception:  # noqa: BLE001
            labels = {}
        for projected, raw in zip(steps, raw_steps):
            owner = projected.get("owner")
            if isinstance(owner, dict):
                label = labels.get((str(owner.get("object_type")), str(owner.get("object_id"))))
                if label:
                    owner["label"] = label
            # Bounded in BYTES here: this projection lives under
            # OBSERVED_AI_PATH_MAX_BYTES, and an unbounded `chose` tripped the wall
            # at 26 steps of 200 (round 3, N3). The full choice is on the path page.
            chose = project_choices(raw.get("detail"), max_bytes=CHOSE_OBSERVED_MAX_BYTES)
            if chose:
                projected["chose"] = chose
        if any(
            steps[index]["ordinal"] >= steps[index + 1]["ordinal"]
            for index in range(len(steps) - 1)
        ):
            return _unavailable_observed_ai_path()
        projection = {
            "schema_version": OBSERVED_AI_PATH_SCHEMA_VERSION,
            "state": "completed" if path["outcome"] == OUTCOME_SUCCEEDED else "failed",
            "path_id": _bounded_projection_text(path.get("id"), required=True),
            "lifecycle": LIFECYCLE_FINALIZED,
            "outcome": path["outcome"],
            "steps": steps,
        }
        encoded = json.dumps(
            projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded) > OBSERVED_AI_PATH_MAX_BYTES:
            return _unavailable_observed_ai_path()
        return projection
    except Exception as exc:  # noqa: BLE001 -- evidence cannot break the Result it supports
        logger.warning("ai_paths: observed projection unavailable (%s)", type(exc).__name__)
        return _unavailable_observed_ai_path()


#: What a step's recorded choices look like on any surface: a handful of
#: dotted keys, short values. The stored detail may hold up to 32 keys; a
#: reader gets the first CHOSE_MAX_KEYS, and the count of the rest.
CHOSE_MAX_KEYS = 10
CHOSE_VALUE_MAX_CHARS = 80
#: A key longer than this is a payload, not a choice (the renderer's decoder refuses it).
CHOSE_KEY_MAX_CHARS = 80
#: Bytes of one step's `chose` inside a Result's observed projection (200 steps stay under the wall).
CHOSE_OBSERVED_MAX_BYTES = 384
_NOT_A_CHOICE = frozenset({"recorded_by", "missing_context", "candidate_ids", "reached_count", "rejected_count", "retrieval_mode", "tiers_title"})
_PROSE_KEYS = frozenset({"analysis", "answer", "reasoning", "rationale", "explanation", "summary", "narrative", "comment", "note", "notes", "prose", "text", "message"})


def project_choices(detail: Any, *, max_bytes: int | None = None) -> dict[str, Any] | None:
    """The bounded `chose` of one step, or None when the step recorded no choice.

    Keys longer than `CHOSE_KEY_MAX_CHARS` are skipped (a decoder that refuses
    one key would otherwise lose the whole path -- round 3, N2). With `max_bytes`,
    the projection stops before that many JSON bytes. Whatever is left out is
    counted, exactly, under the `…` key -- the renderer shows that count.
    """
    import json  # noqa: PLC0415

    if not isinstance(detail, Mapping) or not detail:
        return None
    chose: dict[str, Any] = {}
    left_out = 0
    for key, value in detail.items():
        # Recorder-owned entries and prose-shaped keys are not choices: the
        # recorder refuses the latter at write time, and an older row may still
        # carry them (`missing_context`, `reasoning` in the strict-contract fixture).
        if not isinstance(key, str) or not key or key in _NOT_A_CHOICE or key in _PROSE_KEYS:
            continue
        # A key the decoder refuses or a value it cannot carry IS a choice left out,
        # and left out means counted (round 4, F3): the doc promised the count.
        if len(key) > CHOSE_KEY_MAX_CHARS:
            left_out += 1
            continue
        if isinstance(value, (list, tuple)):
            bounded: Any = [(item[:CHOSE_VALUE_MAX_CHARS] if isinstance(item, str) else item) for item in list(value)[:12]]
        elif isinstance(value, str):
            bounded = value[:CHOSE_VALUE_MAX_CHARS]
        elif value is None or isinstance(value, (bool, int, float)):
            bounded = value
        else:
            left_out += 1
            continue
        if len(chose) >= CHOSE_MAX_KEYS:
            left_out += 1
            continue
        if max_bytes is not None:
            def _over(candidate: Any) -> bool:
                return len(json.dumps({**chose, key: candidate}, default=str, ensure_ascii=False).encode("utf-8")) > max_bytes

            # A list is shortened before it is left out: fewer items, never none.
            while _over(bounded) and isinstance(bounded, list) and len(bounded) > 1:
                bounded = bounded[:-1]
            if _over(bounded):
                left_out += 1
                continue
        chose[key] = bounded
    if left_out:
        chose["…"] = f"{left_out} more"
    return chose or None


def _safe_label(value: Any) -> str | None:
    """A name the renderer's decoder accepts: no control characters, no edge whitespace, bounded.

    Round 3, N2: a Skill named with a trailing space or a newline produced a label
    the product's own decoder refused, and the reader lost the whole AI Path
    instead of one name. A name that cannot be made safe is absent, never invented.
    """
    if value is None:
        return None
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = " ".join(text.split())[:LABEL_MAX_CHARS].strip()
    return text or None


#: How many sibling paths of an interaction a reading widens over (the index itself lists up to 10).
INTERACTION_SIBLING_LIMIT = 4


def judged_over_interaction(snapshot: Any, trace_id: Any) -> bool:
    """True when a path's verdict spans its interaction: a Skill is pinned AND the path has a trace.

    ONE HOME for the rule (round 9, F2; round 10, N1): `load_path`, the detail route,
    the MCP reading and `previous_walks` all judge by it; two carried their own copy
    and one widened unconditionally. « Was the
    Skill followed » is the one question that spans several paths; a plain node
    policy is judged on the path's own steps everywhere.
    """
    return bool(isinstance(snapshot, Mapping) and snapshot.get("expected_skill_steps") and trace_id)


def assessment_for_read(path: Mapping[str, Any], interaction: Sequence[Mapping[str, Any]]) -> Any:
    """The assessment a reading serves: over the interaction when `judged_over_interaction`, else the loader's own."""
    snapshot = path.get("policy_snapshot")
    if judged_over_interaction(snapshot, path.get("w3c_trace_id")):
        return assess(snapshot, interaction, outcome=path.get("outcome"))
    return path.get("assessment")


def ordered_by_moment(steps: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The steps in the order they happened: moment, then path, then the path's own ordinal."""
    return sorted(
        steps,
        key=lambda step: (_moment(step.get("observed_at")), str(step.get("path_id") or ""), int(step.get("ordinal") or 0)),
    )


def _moment(value: Any):
    """A comparable moment: the datetime itself, an ISO string parsed, else the epoch (never a string)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    if hasattr(value, "isoformat") and hasattr(value, "tzinfo"):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=UTC)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text or None


def _wire_step_projection(
    step: Mapping[str, Any], branch_evidence: Mapping[str, Any] | None
) -> dict[str, Any]:
    projected = {
        # The loader names this column `ordinal`. Reading `step_order` here
        # matched no key, so every step went out as null and the screen silently
        # fell back to its index -- shifting the recorded inspection by one
        # against `app.ai_path_steps.ordinal`, which is 0-based
        # (append_step allocates COALESCE(MAX(ordinal)+1, 0)) and is what
        # idx_evidence_inspections_path joins on. AI-134.
        "step_order": step.get("ordinal"),
        "step_kind": step.get("step_kind"),
        "outcome": step.get("outcome"),
        # When it happened, so a merged interaction can be ordered and read. Tolerant:
        # the wire projection never raises on a row the store already holds.
        "observed_at": _iso_or_none(step.get("observed_at")),
        "tool_name": step.get("tool_name"),
        "owner_workspace": step.get("owner_workspace"),
        "owner_object_type": step.get("owner_object_type"),
        "owner_object_id": step.get("owner_object_id"),
        "owner_version_id": step.get("owner_version_id"),
        "skill_version_id": step.get("skill_version_id"),
        # Story 45.7 -- LE COUPLE, ET CE QU'IL DESIGNE. La projection ne portait
        # que la moitie version : `ck_ai_path_steps_skill_pin` refuse d'ecrire
        # une demi-epingle, et une surface qui n'en recoit que la moitie ne peut
        # pas la resoudre. `skill` est ce que la version SERVIE declarait, avec
        # son etat -- `unavailable` quand la lecture a echoue, jamais un vide qui
        # se lirait << ce pas n'existe pas >>.
        "skill_step_id": step.get("skill_step_id"),
        "skill": step.get("skill"),
        "evidence_record_id": step.get("evidence_record_id"),
        # What the call chose (2026-09-05): the arguments' ids and short values.
        "chose": project_choices(step.get("detail")),
    }
    if branch_evidence is not None:
        projected["branch_evidence"] = dict(branch_evidence)
    return projected


def wire_steps_projection(
    steps: Sequence[Mapping[str, Any]],
    *,
    branch_evidence_frozen: bool = True,
    content_hash_contract: str | None = AI_PATH_CONTENT_V2,
) -> list[dict[str, Any]]:
    """Return the bounded path-level wire shape without stored flat detail."""
    values = list(steps)
    if branch_evidence_frozen and content_hash_contract == AI_PATH_CONTENT_V2:
        evidence = project_branch_evidence_for_steps(values)
    elif branch_evidence_frozen:
        evidence = [
            _branch_state("branches_not_recorded")
            if step.get("step_kind") == "knowledge_read"
            and step.get("tool_name") in _BRANCH_TOOLS
            else None
            for step in values
        ]
    else:
        evidence = [
            _branch_state("unavailable")
            if step.get("step_kind") == "knowledge_read"
            and step.get("tool_name") in _BRANCH_TOOLS
            else None
            for step in values
        ]
    return [
        _wire_step_projection(step, branch)
        for step, branch in zip(values, evidence)
    ]


def wire_step_projection(step: dict[str, Any]) -> dict[str, Any]:
    """Compatibility projection for one step; path readers use the allocator."""
    return wire_steps_projection([step])[0]


def list_paths(
    conn, *, project_id: str, limit: int = 50, cursor: str | None = None
) -> list[dict[str, Any]]:
    """Bounded, Project-scoped, keyset-ordered. No unbounded collection read."""
    bounded = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, lifecycle, outcome, actor, started_at, ended_at, w3c_trace_id
              FROM app.ai_paths
             WHERE project_id = %s
               AND (%s::text IS NULL OR id < %s::text)
             ORDER BY id DESC
             LIMIT %s
            """,
            (_require(project_id, "project_id"), cursor, cursor, bounded),
        )
        names = ("id", "lifecycle", "outcome", "actor", "started_at", "ended_at", "w3c_trace_id")
        return [dict(zip(names, row)) for row in cur.fetchall()]


def path_stats(
    conn, *, project_id: str, days: int = 30, verdict_window: int = 200
) -> dict[str, Any]:
    """Aggregate outcomes and verdicts over a bounded window (drift signal).

    Two reads, both bounded. The outcome split is SQL over the window -- the
    column is stored at finalization, so the aggregate is cheap. The verdict
    split is derived: `assess()` needs the steps and the pinned policy, so it
    is computed over at most `verdict_window` recent finalized paths and the
    response SAYS that window -- a verdict rate over "the last 200" is honest,
    the same rate silently presented as "all time" would not be. Paths whose
    walk is not readable count as `unverifiable`, never skipped: skipping
    them would flatter the rate.
    """
    days = max(1, min(int(days), 90))
    verdict_window = max(1, min(int(verdict_window), 500))
    project_id = _require(project_id, "project_id")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT outcome, COUNT(*)
              FROM app.ai_paths
             WHERE project_id = %s
               AND lifecycle = 'finalized'
               AND started_at >= now() - (%s * INTERVAL '1 day')
             GROUP BY outcome
            """,
            (project_id, days),
        )
        outcomes = {str(row[0]): int(row[1]) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT COUNT(*)
              FROM app.ai_paths
             WHERE project_id = %s AND lifecycle = 'recording'
            """,
            (project_id,),
        )
        recording = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT id
              FROM app.ai_paths
             WHERE project_id = %s AND lifecycle = 'finalized'
             ORDER BY id DESC
             LIMIT %s
            """,
            (project_id, verdict_window),
        )
        recent_ids = [str(row[0]) for row in cur.fetchall()]
    verdicts: dict[str, int] = {"pass": 0, "fail": 0, "unverifiable": 0}
    for path_id in recent_ids:
        try:
            path = load_path(conn, path_id=path_id, project_id=project_id, assess_over_interaction=False)
            verdict = str((path.get("assessment") or {}).get("verdict") or "unverifiable")
        except Exception:  # noqa: BLE001 -- an unreadable walk IS unverifiable
            verdict = "unverifiable"
        verdicts[verdict if verdict in verdicts else "unverifiable"] += 1
    return {
        "window_days": days,
        "outcomes": outcomes,
        "recording": recording,
        "verdicts": verdicts,
        "verdict_window": verdict_window,
        "verdicts_assessed": len(recent_ids),
    }


def ai_path_reference(conn, *, path_id: str | None, project_id: str) -> str:
    """What Story 50.1 AC7 stores on a Result.

    Either the exact id of an accepted finalized path, or the exact literal
    :data:`NO_AI_PATH`. A recording path is not an acceptable reference: it can
    still grow steps, so a Result pinned to it would describe evidence that
    changes after the fact.
    """
    if path_id is None:
        return NO_AI_PATH
    with conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle FROM app.ai_paths WHERE id = %s AND project_id = %s",
            (_require(path_id, "path_id"), _require(project_id, "project_id")),
        )
        row = cur.fetchone()
    if row is None:
        raise AiPathNotFound("AI Path not found")
    if row[0] != LIFECYCLE_FINALIZED:
        raise AiPathError("an unfinalized AI Path cannot be referenced as evidence")
    return path_id


# ---------------------------------------------------------------------------
# Assessment, derived from the pinned snapshot.
# ---------------------------------------------------------------------------


def assess(
    policy_snapshot: Mapping[str, Any] | None,
    steps: Sequence[Mapping[str, Any]],
    *,
    outcome: str | None = None,
) -> dict[str, Any]:
    """Compare observed steps against the PINNED policy.

    The verdict is deliberately three-valued. Two situations that a boolean
    would merge, and that must not merge:

    * a required node was reached, but the step carries no version, so nothing
      proves WHICH version answered — ``unverifiable``;
    * a required node was reached with a version that differs from the pinned
      one — ``fail``.

    The first is missing evidence, the second is evidence of a deviation.
    Reporting either as a pass is the defect this function exists to prevent.
    """
    snapshot = dict(policy_snapshot or {})
    # ONE ORDERING, OWNED HERE (round 7, B1): the reading is order-sensitive (the
    # first row of a node decides its version, the first crossing of a step its
    # moment), and two callers handed the same rows in two orders -- own path then
    # siblings, or by the moment -- and read two verdicts. The moment decides,
    # then the path, then the path's own ordinal; a caller cannot change it.
    steps = ordered_by_moment(steps)
    required = list(snapshot.get("required") or [])
    forbidden = list(snapshot.get("forbidden") or [])
    ordered = bool(snapshot.get("ordered"))

    findings: list[dict[str, Any]] = []

    observed: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for step in steps:
        key = _node_key(
            step.get("owner_workspace"), step.get("owner_object_type"), step.get("owner_object_id")
        )
        if key is not None:
            observed.setdefault(key, []).append(step)

    # An execution the instrumentation never described cannot be judged. Saying
    # so is the honest answer; a pass over zero observations is not.
    expected_declared = bool(snapshot.get("expected_skill_steps"))
    if outcome == OUTCOME_UNAVAILABLE or (not steps and (required or expected_declared)):
        findings.append(
            {
                "finding": FINDING_UNVERIFIABLE,
                "detail": "no observed steps were recorded for this execution",
            }
        )
        return {"verdict": VERDICT_UNVERIFIABLE, "findings": findings}

    # The symmetric case, and it was missing: steps exist but the snapshot
    # declares NOTHING to judge them against. Every check below then finds no
    # violation, and the function returned `pass` -- a verdict nobody earned.
    #
    # It mattered immediately. `ai_path_recorder` pins
    # `{"recorded_by": "mcp_tool_middleware"}` because a middleware has no
    # expectation to declare, so EVERY recorded path read `pass`, and the Story
    # 49.6 screen displayed that as an assessment. `trace_observation` can pass
    # `{}` the same way.
    #
    # Jean's arbitration (2026-07-31) put relevance HERE rather than in a
    # heuristic inside `core.adherence`: a path is judged against a pinned
    # expectation, never against a guess. The corollary is this branch -- no
    # declared expectation means the question was not asked, which is
    # `unverifiable`, exactly as this function's docstring says missing evidence
    # must never merge with a pass.
    # THE SKILL'S EXPECTATION (2026-09-05). A trace that took a Skill pinned
    # the served version's steps; the required ones are judged, the others are
    # reported. Judged against `skill_step` rows -- the recorder writes one when a
    # call crosses a declared step -- never against tool names, which would let a
    # call outside the Skill count as the Skill.
    expected_steps = [e for e in (snapshot.get("expected_skill_steps") or []) if isinstance(e, Mapping)]
    pinned_silent = False
    if expected_steps:
        # ORDER IS THE MOMENT, NOT THE ORDINAL: the reading spans paths and each
        # path allocates its own ordinals from 0 (Opus review, finding 4). The
        # moment is compared as a datetime -- a string comparison sorted a DST
        # fall-back backwards (round 2, residual A).
        crossed: dict[tuple[str, str], tuple[Any, int]] = {}
        for step in steps:
            version = step.get("skill_version_id")
            if version and step.get("skill_step_id") is not None:
                key = (str(version), str(step.get("skill_step_id")))
                crossed.setdefault(key, (_moment(step.get("observed_at")), int(step.get("ordinal") or 0)))
        pinned_versions = {str(e.get("skill_version")) for e in expected_steps}
        pinned_silent = not any(version in pinned_versions for version, _step in crossed)
        if pinned_silent:
            # THE SEQUENCE WAS PINNED AND NO CROSSING OF IT WAS OBSERVED (round 3:
            # a crossing of some OTHER Skill's steps is not evidence about this one). That is the
            # recorder's silence -- the process that served the Skill is not the one
            # that saw the calls, or its memory expired -- not a walk that skipped
            # every step (round 2, finding 3). Said as such, never as `fail`.
            # AND NEVER AS A RETURN (round 4, F1): the step judgement is withheld,
            # the node checks below still run -- a forbidden object touched by a
            # walk we cannot judge is still a deviation, and a deviation reported
            # as missing evidence is the defect this function exists to prevent.
            findings.append(
                {
                    "finding": FINDING_UNVERIFIABLE,
                    "detail": (
                        "the served Skill's sequence was pinned but no step crossing was observed: "
                        "the calls were recorded by a process that had not served the Skill, or its "
                        "memory of it had expired -- the walk cannot be judged"
                    ),
                }
            )
            expected_steps = []
        required_seen_by_skill: dict[str, list[tuple[Any, int]]] = {}
        any_required = False
        for entry in expected_steps:
            key = (str(entry.get("skill_version")), str(entry.get("step")))
            subject = f"{key[0]} step {key[1]}"
            is_required = bool(entry.get("required"))
            any_required = any_required or is_required
            if key in crossed:
                if is_required:
                    findings.append({"finding": FINDING_SKILL_STEP_CROSSED, "skill_step": subject, "tool": entry.get("tool")})
                    required_seen_by_skill.setdefault(key[0], []).append(crossed[key])
            elif is_required:
                findings.append({"finding": FINDING_SKILL_STEP_SKIPPED, "skill_step": subject, "tool": entry.get("tool")})
            else:
                findings.append({"finding": FINDING_SKILL_STEP_EXPECTED_SKIPPED, "skill_step": subject, "tool": entry.get("tool")})
        # ORDER IS JUDGED WITHIN ONE SKILL: a Skill that cites another interleaves
        # two sequences, each in order (round 2, residual B).
        if snapshot.get("ordered_skill_steps"):
            for skill_version, seen in required_seen_by_skill.items():
                if seen != sorted(seen):
                    findings.append(
                        {
                            "finding": FINDING_SKILL_STEPS_OUT_OF_ORDER,
                            "skill_version": skill_version,
                            "observed_at": [m.isoformat() if hasattr(m, "isoformat") else str(m) for m, _ in seen],
                        }
                    )
        if not any_required and not required and not forbidden and not pinned_silent:
            findings.append(
                {
                    "finding": FINDING_UNVERIFIABLE,
                    "detail": (
                        "the Skill taken declares no required step, so its walk is reported, not judged: "
                        "mark the steps that must be crossed `required: true`"
                    ),
                }
            )
            return {"verdict": VERDICT_UNVERIFIABLE, "findings": findings}
    if not required and not forbidden and not expected_steps and not pinned_silent:
        findings.append(
            {
                "finding": FINDING_UNVERIFIABLE,
                "detail": (
                    "the pinned policy declares no required or forbidden node, so "
                    "there is nothing to judge this execution against"
                ),
            }
        )
        return {"verdict": VERDICT_UNVERIFIABLE, "findings": findings}

    # ORDER IS THE MOMENT, NOT THE ORDINAL (round 6, B5): the reading may span the
    # paths of one interaction and each allocates its ordinals from 0 -- the Skill
    # branch already compares moments; the node branch compared ordinals and failed
    # a walk that respected the prescribed order. The ordinals are still REPORTED.
    matched_order: list[tuple[Any, int]] = []

    for entry in required:
        key = _node_key(
            entry.get("owner_workspace"),
            entry.get("owner_object_type"),
            entry.get("owner_object_id"),
        )
        alternatives = [
            _node_key(
                alternative.get("owner_workspace"),
                alternative.get("owner_object_type"),
                alternative.get("owner_object_id"),
            )
            for alternative in (entry.get("alternatives") or [])
        ]

        hit_key = key if key in observed else None
        used_alternative = False
        if hit_key is None:
            for alternative in alternatives:
                if alternative in observed:
                    hit_key = alternative
                    used_alternative = True
                    break

        if hit_key is None:
            if pinned_silent:
                # THE RECORD IS DECLARED UNTRUSTWORTHY (round 5): a required node
                # not seen is an inference from ABSENCE, and absence is exactly
                # what the silence cannot vouch for. A forbidden node SEEN is
                # positive evidence and still fails below.
                continue
            findings.append(
                {"finding": FINDING_REQUIRED_MISSING, "node": list(key) if key else None}
            )
            continue

        step = observed[hit_key][0]
        matched_order.append((_moment(step.get("observed_at")), int(step["ordinal"])))

        expected_version = entry.get("owner_version_id")
        actual_version = step.get("owner_version_id")
        if expected_version is not None:
            if actual_version is None:
                findings.append(
                    {
                        "finding": FINDING_UNVERIFIABLE,
                        "node": list(hit_key),
                        "detail": "the step records no version, so the pinned version is unproven",
                    }
                )
                continue
            if str(actual_version) != str(expected_version):
                findings.append(
                    {
                        "finding": FINDING_VERSION_MISMATCH,
                        "node": list(hit_key),
                        "expected_version": str(expected_version),
                        "observed_version": str(actual_version),
                    }
                )
                continue

        findings.append(
            {
                "finding": FINDING_APPROVED_ALTERNATIVE if used_alternative
                else FINDING_REQUIRED_OBSERVED,
                "node": list(hit_key),
            }
        )

    for entry in forbidden:
        key = _node_key(
            entry.get("owner_workspace"),
            entry.get("owner_object_type"),
            entry.get("owner_object_id"),
        )
        if key is not None and key in observed:
            findings.append({"finding": FINDING_FORBIDDEN_OBSERVED, "node": list(key)})

    if ordered and matched_order != sorted(matched_order):
        findings.append(
            {"finding": FINDING_OUT_OF_ORDER, "observed_ordinals": [ordinal for _moment_, ordinal in matched_order]}
        )

    kinds = {finding["finding"] for finding in findings}
    if kinds & _FAILING_FINDINGS:
        verdict = VERDICT_FAIL
    elif FINDING_UNVERIFIABLE in kinds:
        verdict = VERDICT_UNVERIFIABLE
    else:
        verdict = VERDICT_PASS
    return {"verdict": verdict, "findings": findings}


# ---------------------------------------------------------------------------
# The Knowledge Graph overlay (Story 49.6 AC7).
# ---------------------------------------------------------------------------

#: What `GET .../overlay` announces itself as. A reader filters on it rather
#: than guessing from the shape.
OVERLAY_SCHEMA_VERSION = "ai-path-graph-overlay.v1"

#: THE ONE TRANSLATION SITE between two vocabularies that were never the same.
#:
#: An observed step names its owner in the EVIDENCE vocabulary, which is
#: hyphenated and open-ended (`semantic-view-version`, `datastream-execution`,
#: `ai-path-step`). The Knowledge Graph draws the GRAPH vocabulary, which is
#: underscored and closed at ten (`KnowledgeGraphPage.tsx`, `NodeTypeKey`).
#: Three values are spelled identically in both -- `topic`, `procedure`,
#: `schema_doc` -- and they are exactly what `context_search` records for a
#: context walk, so the corpus the mindmap actually draws joins without a hop.
#:
#: The one rule for adding a row: the two spellings must denote THE SAME
#: OBJECT, with the same id space. A hyphenated alias is allowed on that basis
#: alone -- `semantic-view` and `semantic_view` are one row of
#: `app.semantic_views`. A `*-version` type is NOT, and that is the trap this
#: rule exists to close: `semantic-view-version` names a version whose id is a
#: version id, so mapping it onto `semantic_view` would decorate a canvas node
#: with an id that never identified it.
#:
#: What is NOT here is the rest of the point. An evidence type with no graph
#: node type is not translated to the nearest one and not drawn: it is COUNTED,
#: and `unrepresented_steps` carries the count. Migration 150 states the rule
#: on the columns themselves -- "an unrepresented step stays visible in the
#: path instead of inventing a graph object for it" -- and AC7 repeats it.
OVERLAY_NODE_TYPES: dict[str, str] = {
    # Spelled identically in both vocabularies. `topic`, `procedure` and
    # `schema_doc` are exactly what `context_search` records for a context
    # walk, so the corpus the mindmap draws joins without a hop.
    "topic": "topic",
    "procedure": "procedure",
    "schema_doc": "schema_doc",
    "target_field": "target_field",
    "business_domain": "business_domain",
    "business_classification": "business_classification",
    "report_view": "report_view",
    "datastream": "datastream",
    "semantic_view": "semantic_view",
    "semantic_concept": "semantic_concept",
    # Same objects, hyphenated -- the evidence vocabulary's own spelling
    # (`evidence_index.py`). Their ids are the graph's ids.
    "target-field": "target_field",
    "business-domain": "business_domain",
    "business-classification": "business_classification",
    "report-view": "report_view",
    "semantic-view": "semantic_view",
    "semantic-concept": "semantic_concept",
    "schema-doc": "schema_doc",
}

#: An Event is NOT a graph node, and deliberately so (Story 49.6 AC9). The
#: mindmap's vocabulary is closed at ten types and carries none; AC9 says a
#: graph or path may "DISPLAY an Event", never draw one, and `data.md` keeps the
#: Event owned by its Datastream. So an observed Event leaves the projection as
#: a REFERENCE to resolve, in its own list -- neither decorated onto a node that
#: does not exist, nor swept into `unrepresented_steps`, which would report a
#: reachable owner as unreachable.
#: The owner object type an Event step carries in `ai_path_steps` -- the COLUMN's
#: vocabulary (hyphen), `candidate_emission.owner_object_type_for("context_event")`.
#: AI-376: the constant said `context_event`, which the column's CHECK refuses, so
#: no Event step was ever stored and this branch was unreachable from the store.
OVERLAY_EVENT_OBJECT_TYPE = "context-event"

OVERLAY_STATE_USED = "used"
OVERLAY_STATE_MISSING = "missing"
OVERLAY_STATE_VERSION_MISMATCH = "version_mismatch"
OVERLAY_STATE_FORBIDDEN = "forbidden"
OVERLAY_STATE_UNVERIFIABLE = "unverifiable"

#: Read worst-first. One node can carry several findings (a required node that
#: was reached at the wrong version is both `used` and `version_mismatch`), and
#: a canvas draws ONE state per node -- so the order is a decision, not a
#: detail. `used` is last because a deviation that renders as a plain visit is
#: the whole defect `assess` exists to prevent.
_OVERLAY_STATE_ORDER = (
    OVERLAY_STATE_FORBIDDEN,
    OVERLAY_STATE_VERSION_MISMATCH,
    OVERLAY_STATE_MISSING,
    OVERLAY_STATE_UNVERIFIABLE,
    OVERLAY_STATE_USED,
)

#: A finding that speaks about one node maps to the state that node draws in.
#: `required_and_observed` and `approved_alternative` are both `used`: an
#: approved alternative IS the policy being followed (see `_FAILING_FINDINGS`),
#: and drawing it as a deviation would contradict its own verdict.
_FINDING_STATES = {
    FINDING_FORBIDDEN_OBSERVED: OVERLAY_STATE_FORBIDDEN,
    FINDING_VERSION_MISMATCH: OVERLAY_STATE_VERSION_MISMATCH,
    FINDING_REQUIRED_MISSING: OVERLAY_STATE_MISSING,
    FINDING_UNVERIFIABLE: OVERLAY_STATE_UNVERIFIABLE,
    FINDING_REQUIRED_OBSERVED: OVERLAY_STATE_USED,
    FINDING_APPROVED_ALTERNATIVE: OVERLAY_STATE_USED,
}


def graph_overlay(path: Mapping[str, Any]) -> dict[str, Any]:
    """Project ONE loaded path onto Knowledge Graph node keys.

    A projection, not a second opinion. Every state here is read from the
    steps the owner recorded and the findings `assess` already derived from
    the snapshot pinned before the run -- nothing is judged again. AC7 says
    the overlay "decorates the current canonical graph and does not mutate
    it", and a decoration that recomputed the verdict would be a second
    authority on what a path is worth.

    Three properties the caller can rely on:

    * a node appears at most ONCE, in its worst state (`_OVERLAY_STATE_ORDER`);
    * `missing` nodes are the ones the pinned policy REQUIRED and no step
      reached, so they have no ordinals and may not be on the canvas at all --
      a graph that cannot draw them must say so, not drop them silently;
    * every step that reached no drawable node is counted in
      `unrepresented_steps` and never invented as one.

    Events leave separately, in `event_references`, as ids and walk order only.
    Resolving them is the Data owner's job (AC9) and happens at the route.
    """
    steps = list(path.get("steps") or [])
    assessment = path.get("assessment") or {}
    findings = list(assessment.get("findings") or [])

    #: node key -> the accumulating decoration
    decorated: dict[tuple[str, str], dict[str, Any]] = {}
    #: observed event id -> the ordinals that reached it
    events: dict[str, list[int]] = {}
    unrepresented = 0

    def _slot(object_type: Any, object_id: Any, workspace: Any) -> dict[str, Any] | None:
        node_type = OVERLAY_NODE_TYPES.get(str(object_type or ""))
        if not node_type or not object_id:
            return None
        key = (node_type, str(object_id))
        slot = decorated.get(key)
        if slot is None:
            slot = {
                "node_type": node_type,
                "node_id": str(object_id),
                "owner_workspace": str(workspace) if workspace else None,
                "states": set(),
                "ordinals": [],
            }
            decorated[key] = slot
        return slot

    # 1. What was OBSERVED. A step that reached a drawable node visits it; one
    #    that reached an Event leaves as a reference; one that reached nothing
    #    of either kind is counted, never placed.
    for step in steps:
        if step.get("owner_object_type") == OVERLAY_EVENT_OBJECT_TYPE:
            event_id = step.get("owner_object_id")
            if event_id:
                ordinal = step.get("ordinal")
                bucket = events.setdefault(str(event_id), [])
                if ordinal is not None:
                    bucket.append(int(ordinal))
                continue
            # An Event step naming no Event reached nothing that can be shown.
            unrepresented += 1
            continue
        slot = _slot(
            step.get("owner_object_type"),
            step.get("owner_object_id"),
            step.get("owner_workspace"),
        )
        if slot is None:
            unrepresented += 1
            continue
        slot["states"].add(OVERLAY_STATE_USED)
        ordinal = step.get("ordinal")
        if ordinal is not None:
            slot["ordinals"].append(int(ordinal))

    # 2. What the PINNED POLICY says about those nodes -- including the ones no
    #    step reached, which is the only way `missing` can ever be drawn.
    for finding in findings:
        state = _FINDING_STATES.get(str(finding.get("finding")))
        node = finding.get("node")
        if state is None or not node or len(node) != 3:
            # `out_of_order` names ordinals, not a node, and an unverifiable
            # path names none at all. Both stay in the path detail.
            continue
        workspace, object_type, object_id = node
        slot = _slot(object_type, object_id, workspace)
        if slot is None:
            unrepresented += 1
            continue
        slot["states"].add(state)
        if state == OVERLAY_STATE_VERSION_MISMATCH:
            slot["expected_version"] = finding.get("expected_version")
            slot["observed_version"] = finding.get("observed_version")

    nodes = []
    for slot in decorated.values():
        states = slot.pop("states")
        slot["state"] = next(s for s in _OVERLAY_STATE_ORDER if s in states)
        slot["ordinals"] = sorted(slot["ordinals"])
        nodes.append(slot)
    nodes.sort(key=lambda n: (n["node_type"], n["node_id"]))

    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "path_id": path.get("id"),
        # Carried so a canvas can say WHY it is decorated without a second
        # fetch, and so a `recording` path cannot be read as settled evidence.
        "lifecycle": path.get("lifecycle"),
        "outcome": path.get("outcome"),
        "verdict": assessment.get("verdict"),
        "nodes": nodes,
        # Ids and walk order ONLY. This module does not know what an Event is,
        # and must not: `data.md` keeps it owned by its Datastream, and a
        # resolution written here would be Context Hub becoming its second
        # owner through a helper.
        "event_references": [
            {"event_id": event_id, "ordinals": sorted(ordinals)}
            for event_id, ordinals in sorted(events.items())
        ],
        "unrepresented_steps": unrepresented,
    }


def steps_digest(conn, *, path_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """path id -> {"steps": count, "tools": [distinct tool names, ordered by first use]}.

    One statement for a page of paths, so a list read costs one round trip
    beside `list_paths` and never one per path (`get_ai_path`, 2026-09-05).
    """
    ids = [str(path_id) for path_id in path_ids if path_id]
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT path_id, count(*),
                   array_agg(tool_name ORDER BY ordinal) FILTER (WHERE tool_name IS NOT NULL)
              FROM app.ai_path_steps
             WHERE path_id = ANY(%s)
             GROUP BY path_id
            """,
            (ids,),
        )
        rows = cur.fetchall()
    digest: dict[str, dict[str, Any]] = {}
    for path_id, count, tools in rows:
        seen: list[str] = []
        for tool in tools or []:
            if tool not in seen:
                seen.append(str(tool))
        digest[str(path_id)] = {"steps": int(count), "tools": seen}
    return digest


def paths_sharing_trace(
    conn, *, project_id: str, trace_id: str | None, exclude: str | None = None
) -> list[dict[str, Any]]:
    """The other paths of the same interaction: same Project, same W3C trace id.

    A Result's execution is recorded as its own finalized path while the calls
    that led to it (discovery, composition, the reads after) stay on the
    interaction's `recording` path under the same trace. A reader of one is
    handed the other, so « my own path » is the whole interaction and not the
    one step that produced a number (`get_ai_path`, 2026-09-05).
    """
    if not trace_id:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, lifecycle, outcome
              FROM app.ai_paths
             WHERE project_id = %s AND w3c_trace_id = %s AND (%s::text IS NULL OR id <> %s::text)
             ORDER BY id
             LIMIT 10
            """,
            (project_id, trace_id, exclude, exclude),
        )
        return [{"path_id": str(r[0]), "state": r[1], "outcome": r[2]} for r in cur.fetchall()]


def _prescribed_steps(conn, *, procedure_id: str, version_number: int) -> tuple[str | None, list[dict[str, Any]]]:
    """The steps a served Skill version declares -- (skill name, steps), or (None, []) when unreadable."""
    from core.context_store import validate_procedure_frontmatter  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT frontmatter_yaml, name FROM app.procedures_versions WHERE procedure_id = %s AND version_number = %s",
            (procedure_id, version_number),
        )
        row = cur.fetchone()
    if row is None:
        return None, []
    frontmatter, name = row
    try:
        parsed = validate_procedure_frontmatter(frontmatter or "")
    except ValueError:
        return str(name) if name else None, []
    steps = []
    for step in parsed.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        steps.append(
            {
                "step": str(step.get("step")),
                "label": str(step.get("label") or "")[:160],
                "tool": step.get("tool"),
                "action": step.get("action"),
            }
        )
    return str(name) if name else None, steps


def skill_coverage(conn, steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What each Skill the path took prescribed, and which of it the walk crossed.

    Jean, 2026-09-05: « tu devrais voir quel skill a été utilisé ». The path
    records a `skill_step` when a call crosses a step the served Skill version
    declares with `tool: X`; this reads that version's whole sequence back and
    says, step by step: crossed, skipped (declared with a tool the walk never
    called), or unobservable (a read of a target the recorder cannot see). A
    verdict is not derived from it -- the pinned policy owns verdicts -- but a
    reader who wants to know whether the Skill was followed reads it here.
    """
    from core.skill_steps import parse_version_reference, step_reference  # noqa: PLC0415

    crossed: dict[str, list[str]] = {}
    order: list[str] = []
    for step in steps:
        version = step.get("skill_version_id")
        if not version:
            continue
        version = str(version)
        if version not in crossed:
            crossed[version] = []
            order.append(version)
        reference = step_reference(str(step.get("skill_step_id")))
        if reference not in crossed[version]:
            crossed[version].append(reference)
    coverage: list[dict[str, Any]] = []
    for version in order[:4]:
        parsed = parse_version_reference(version)
        if parsed is None:
            continue
        procedure_id, version_number = parsed
        try:
            name, prescribed = _prescribed_steps(conn, procedure_id=procedure_id, version_number=version_number)
        except Exception as exc:  # noqa: BLE001 -- a reading never breaks the path
            logger.debug("ai_paths: skill coverage unreadable %s: %s", version, type(exc).__name__)
            name, prescribed = None, []
        lines = []
        for entry in prescribed:
            reference = step_reference(entry["step"])
            if reference in crossed[version]:
                state = "crossed"
            elif entry.get("tool"):
                state = "skipped"
            else:
                state = "unobservable"
            lines.append({**entry, "state": state})
        coverage.append(
            {
                "skill_version": version,
                "procedure_id": procedure_id,
                "version_number": version_number,
                "skill_name": name,
                "prescribed": len(prescribed),
                "crossed": [line["step"] for line in lines if line["state"] == "crossed"],
                "skipped": [line["step"] for line in lines if line["state"] == "skipped"],
                "unobservable": [line["step"] for line in lines if line["state"] == "unobservable"],
                "steps": lines,
                "sequence_readable": bool(prescribed),
            }
        )
    return coverage


def interaction_steps(
    conn,
    *,
    project_id: str,
    path: Mapping[str, Any],
    limit: int = INTERACTION_SIBLING_LIMIT,
    siblings: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The path's steps plus those of the other paths of its interaction (same trace).

    A Result's execution and the calls around it are two paths; a Skill's
    coverage read on one alone says « step 2 crossed » here and « step 6
    crossed » there. Read over the interaction, it says both -- which is what
    « was the Skill followed » means (2026-09-05).
    """
    steps = [dict(step) for step in (path.get("steps") or []) if isinstance(step, Mapping)]
    # A caller that already read the sibling index hands it over (round 8): one
    # read of `paths_sharing_trace` per reading, never one per consumer.
    index = (
        list(siblings)
        if siblings is not None
        else paths_sharing_trace(conn, project_id=project_id, trace_id=path.get("w3c_trace_id"), exclude=str(path.get("id") or ""))
    )
    for sibling in index[:limit]:
        # THE SIBLING'S STEPS, NEVER `load_path`: that loader assesses, and an
        # assessment over a Skill's policy widens to the siblings -- which would
        # call back here. The Opus review of efe127b7 measured the cycle at 997
        # SQL statements for two paths and no termination for three.
        try:
            steps.extend(
                {**step, "path_id": sibling["path_id"]}
                for step in load_steps(conn, path_id=sibling["path_id"], project_id=project_id)
            )
        except Exception:  # noqa: BLE001 -- a sibling that cannot be read adds nothing
            continue
    for step in steps:
        step.setdefault("path_id", path.get("id"))
    # In the order they happened, whoever reads them (round 7, B1).
    return ordered_by_moment(steps)


def load_steps(conn, *, path_id: str, project_id: str) -> list[dict[str, Any]]:
    """The ordered steps of one path of this Project, with their Skill pins resolved -- and nothing derived."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app.ai_paths WHERE id = %s AND project_id = %s", (path_id, project_id))
        if cur.fetchone() is None:
            raise AiPathNotFound("AI Path not found")
        steps = _load_steps(cur, path_id)
    _resolve_skill_pins(conn, steps)
    return steps


def previous_walks(conn, *, project_id: str, procedure_id: str, limit: int = 3) -> list[dict[str, Any]]:
    """The last interactions of this Project that took this Skill, with what they crossed and skipped.

    Served with the procedure itself (`get_procedure`, 2026-09-05) so the model
    reads its last walk BEFORE walking again -- « last time here you skipped the
    reading of the Result » -- which is where an iteration improves. Derived,
    bounded, never stored.
    """
    bounded = max(1, min(int(limit), 20))
    # `@` separates the procedure id from its version number; an id that carried it
    # would be conflated with a version of its prefix (round 4, F6). Ids are minted
    # `proc_<ulid>` and never do -- the invariant is held here, not assumed.
    if not procedure_id or "@" in procedure_id:
        return []
    # NO LIKE AT ALL (round 3, measured on 3 000 seeded paths): under row-level
    # security a non-leakproof operator (`~~`) cannot become an index condition,
    # so the pattern-ops half of migration 350's index filtered nothing and the
    # read cost 638 ms. The version is `{procedure_id}@{n}` with n an integer, so
    # the prefix is a RANGE on the pattern-ops operators (leakproof, indexable):
    # `[proc@, proc@:)` -- `:` follows `9`. `_` is no wildcard here, nothing to
    # escape. Scoped by the steps' project (the FK ties the path to the same
    # project), joined on the path's id alone so the probe is the unique index;
    # one walk per trace decided IN SQL, before the bound. 7 ms on the same seed.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, w3c_trace_id, started_at
              FROM (
                   SELECT DISTINCT ON (COALESCE(p.w3c_trace_id, p.id))
                          p.id, p.w3c_trace_id, p.started_at
                     FROM app.ai_path_steps s
                     JOIN app.ai_paths p ON p.id = s.path_id
                    WHERE s.project_id = %s
                      AND s.skill_version_id ~>=~ %s
                      AND s.skill_version_id ~<~ %s
                    ORDER BY COALESCE(p.w3c_trace_id, p.id), p.started_at DESC
                   ) newest_per_trace
             ORDER BY started_at DESC
             LIMIT %s
            """,
            (project_id, f"{procedure_id}@", f"{procedure_id}@:", bounded),
        )
        rows = cur.fetchall()
    seen_traces: set[str] = set()
    chosen = []
    for path_id, trace, started_at in rows:
        if trace:
            if trace in seen_traces:
                continue
            seen_traces.add(trace)
        chosen.append((path_id, trace, started_at))
        if len(chosen) >= bounded:
            break
    walks: list[dict[str, Any]] = []
    for path_id, _trace, started_at in chosen:
        try:
            # ONE widening per walk: the header without its own widening, the
            # interaction once, the verdict and the coverage both read from it.
            path = load_path(conn, path_id=str(path_id), project_id=project_id, assess_over_interaction=False)
            steps = interaction_steps(conn, project_id=project_id, path=path)
            # The SAME rule as every other reader (round 10, N1): a walk whose snapshot
            # pins no Skill is judged on its own steps -- a sibling's forbidden touch
            # failed it here and nowhere else.
            path["assessment"] = assessment_for_read(path, steps)
            coverage = skill_coverage(conn, steps)
        except Exception:  # noqa: BLE001
            continue
        mine = next((c for c in coverage if str(c.get("procedure_id")) == procedure_id), None)
        walks.append(
            {
                "path_id": str(path_id),
                "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
                "verdict": (path.get("assessment") or {}).get("verdict"),
                "skill_version": mine.get("skill_version") if mine else None,
                "crossed": mine.get("crossed") if mine else [],
                "skipped": mine.get("skipped") if mine else [],
                "skipped_labels": [
                    str(line.get("label") or "")[:80] for line in (mine.get("steps") if mine else []) if line.get("state") == "skipped"
                ][:3],
                # The per-step lines, for the aggregate a Skill's page draws (iteration 3).
                "steps": [
                    {"step": line.get("step"), "label": str(line.get("label") or "")[:120], "tool": line.get("tool"), "state": line.get("state")}
                    for line in (mine.get("steps") if mine else [])
                ],
            }
        )
    return walks


#: object type -> (table, name column). What a step reached, said in the reader's words.
_LABEL_SOURCES: dict[str, tuple[str, str]] = {
    "semantic-view": ("app.semantic_views", "name"),
    "semantic-concept": ("app.semantic_concepts", "name"),
    "canonical-field": ("app.mdm_canonical_fields", "canonical_name"),
    "datastream": ("app.datastreams", "name"),
    "query-spec": ("app.query_specs", "name"),
    "visualization": ("app.visualizations", "name"),
    "procedure": ("app.procedures", "name"),
    "context-topic": ("app.context_topics", "title"),
}
#: id prefix -> (table, id column to match, name column). The ids a step CHOSE.
_NAME_SOURCES: dict[str, tuple[str, str, str]] = {
    "mdm_": ("app.mdm_canonical_fields", "id", "canonical_name"),
    "ds_": ("app.datastreams", "id", "name"),
    "sv_": ("app.semantic_views", "id", "name"),
    "svv_": ("app.semantic_view_versions", "id", "label"),
    "sc_": ("app.semantic_concepts", "id", "name"),
    "qs_": ("app.query_specs", "id", "name"),
    "vis_": ("app.visualizations", "id", "name"),
    "proc_": ("app.procedures", "id", "name"),
    "top_": ("app.context_topics", "id", "title"),
    "mck_": ("app.mdm_common_keys", "id", "name"),
}
NAMES_MAX_IDS = 80
LABEL_MAX_CHARS = 120


def owner_labels(conn, steps: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """(object type, object id) -> the object's own name, for the owners the steps reached.

    One query per object type present, ids batched. An owner whose name cannot
    be read keeps its id: a missing label is an absence, never an invented word
    (rendering iteration, 2026-09-05).
    """
    wanted: dict[str, set[str]] = {}
    for step in steps:
        object_type = str(step.get("owner_object_type") or "")
        object_id = str(step.get("owner_object_id") or "")
        if object_type in _LABEL_SOURCES and object_id:
            wanted.setdefault(object_type, set()).add(object_id)
    labels: dict[tuple[str, str], str] = {}
    for object_type, ids in wanted.items():
        table, column = _LABEL_SOURCES[object_type]
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, {column} FROM {table} WHERE id = ANY(%s)", (sorted(ids)[:NAMES_MAX_IDS],))  # noqa: S608 - constants of this module
                for row in cur.fetchall():
                    label = _safe_label(row[1]) if row else None
                    if row and label:
                        labels[(object_type, str(row[0]))] = label
        except Exception as exc:  # noqa: BLE001 -- a label never breaks the read
            logger.debug("ai_paths: owner labels unreadable for %s: %s", object_type, type(exc).__name__)
    return labels


def _ids_in_choices(detail: Any, out: set[str]) -> None:
    if isinstance(detail, Mapping):
        for value in detail.values():
            _ids_in_choices(value, out)
    elif isinstance(detail, (list, tuple)):
        for value in detail:
            _ids_in_choices(value, out)
    elif isinstance(detail, str) and "_" in detail and len(detail) <= 64:
        prefix = detail.split("_", 1)[0] + "_"
        if prefix in _NAME_SOURCES:
            out.add(detail)


def names_for(conn, steps: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """id -> name for the governed ids the steps chose (`detail`), bounded and batched by prefix."""
    ids: set[str] = set()
    for step in steps:
        _ids_in_choices(step.get("detail"), ids)
    names: dict[str, str] = {}
    by_prefix: dict[str, list[str]] = {}
    for value in sorted(ids)[:NAMES_MAX_IDS]:
        by_prefix.setdefault(value.split("_", 1)[0] + "_", []).append(value)
    for prefix, values in by_prefix.items():
        table, id_column, name_column = _NAME_SOURCES[prefix]
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {id_column}, {name_column} FROM {table} WHERE {id_column} = ANY(%s)", (values,))  # noqa: S608 - constants of this module
                for row in cur.fetchall():
                    if row and row[1]:
                        name = _safe_label(row[1])
                        if name:
                            names[str(row[0])] = name
        except Exception as exc:  # noqa: BLE001
            logger.debug("ai_paths: names unreadable for %s: %s", prefix, type(exc).__name__)
    return names


SKILL_WALKS_MAX = 20


def skill_walk_stats(conn, *, project_id: str, procedure_id: str, limit: int = SKILL_WALKS_MAX) -> dict[str, Any]:
    """What the last walks of a Skill say about it -- per step, per walk, never stored.

    Iteration 3 of the Fable ↔ Opus loop (tracking). The Skill's author reads,
    on the Skill's own page: how many interactions took it, how their verdicts
    split, and step by step how often each prescribed step was crossed, skipped
    or unobservable -- a step skipped nine times in ten is either unrealistic
    or a habit of the model to correct, and only the ranking tells which.
    Bounded to `SKILL_WALKS_MAX` walks, read through `previous_walks`' own
    query so there is one way to find a Skill's walks.
    """
    walks = previous_walks(conn, project_id=project_id, procedure_id=procedure_id, limit=max(1, min(int(limit), SKILL_WALKS_MAX)))
    verdicts: dict[str, int] = {}
    steps: dict[str, dict[str, Any]] = {}
    versions: dict[str, int] = {}
    for walk in walks:
        verdict = str(walk.get("verdict") or "unverifiable")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        version = walk.get("skill_version")
        if version:
            versions[str(version)] = versions.get(str(version), 0) + 1
        for line in walk.get("steps") or []:
            entry = steps.setdefault(
                str(line.get("step")),
                {"step": str(line.get("step")), "label": line.get("label"), "tool": line.get("tool"), "crossed": 0, "skipped": 0, "unobservable": 0},
            )
            if not entry.get("label") and line.get("label"):
                entry["label"] = line.get("label")
            state = line.get("state")
            if state in ("crossed", "skipped", "unobservable"):
                entry[state] += 1
    ranked = sorted(steps.values(), key=lambda e: (-e["skipped"], int(e["step"]) if str(e["step"]).isdigit() else 0))
    return {
        "procedure_id": procedure_id,
        "walks": len(walks),
        "window": f"last {len(walks)} walk(s)" if walks else "no walk recorded yet",
        "verdicts": verdicts,
        "versions": versions,
        "steps": sorted(steps.values(), key=lambda e: int(e["step"]) if str(e["step"]).isdigit() else 0),
        "most_skipped": [e for e in ranked if e["skipped"] > 0][:3],
        "recent": [
            {k: walk.get(k) for k in ("path_id", "started_at", "verdict", "skill_version", "crossed", "skipped")}
            for walk in walks[:5]
        ],
    }
