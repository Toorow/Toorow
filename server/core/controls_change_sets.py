"""The one Controls & Quality command family (Story 49.4, AC7).

Create intent, prepare a SERVER-derived diff and impact, confirm with a
single-use expiring token. The same shape as ``app.semantic_change_sets`` (Story
49.3) and ``app.project_change_sets`` (Story 48.1), and the sameness is
deliberate: three change-set families that behaved differently would be three
things to learn and three to get wrong.

Three properties are recomputed at confirm rather than trusted from prepare, and
each has a failure it prevents:

* **The dependency fingerprint.** A prepare that was correct ten minutes ago is
  not evidence that it is correct now. If the Rule Set head, the monitor version
  or the case status moved in between, the confirmation is refused rather than
  applied to a world that no longer matches the review.
* **The token.** Single-use and expiring, hashed at rest, never returned twice.
  A replayed confirmation returns the FIRST result instead of minting a second
  version.
* **Project scope.** Every object id is re-resolved inside the authorized
  Project. A cross-Project reference is indistinguishable from a missing one.

What this module does NOT do: edit a physical mapping, advance a Data
publication pointer, or run an extraction. When a decision must affect Data, the
owner command is CALLED and its exact result is recorded on the decision --
including a failure, which leaves the case open.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

OBJECT_TYPES = ("control-case", "dq-monitor", "rule-set")

#: How long a prepared change set stays confirmable. Long enough to read the
#: impact, short enough that the world has not moved underneath it.
CONFIRMATION_TTL = timedelta(minutes=30)


class ControlsChangeSetError(ValueError):
    """The request was well formed and is refused."""

    code = "invalid_controls_change_set"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


class ControlsChangeSetNotFound(LookupError):
    """The change set does not exist in this Project."""


class ControlsChangeSetStale(ControlsChangeSetError):
    """The world moved between prepare and confirm."""

    code = "controls_change_set_stale"


def _mint(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlsChangeSetError(f"{label} is required")
    return value.strip()


# ---------------------------------------------------------------------------
# Create.
# ---------------------------------------------------------------------------


def create_change_set(
    conn,
    *,
    project_id: str,
    object_type: str,
    intent: Mapping[str, Any],
    actor: str,
    idempotency_key: str,
    object_id: str | None = None,
    base_version_id: str | None = None,
) -> dict[str, Any]:
    """Open a change set. Accepts an allowlisted intent and nothing else.

    Idempotent by key: the same key returns the same change set rather than
    opening a second one for the same request.
    """

    if object_type not in OBJECT_TYPES:
        raise ControlsChangeSetError(f"object_type must be one of {list(OBJECT_TYPES)}")
    if not isinstance(intent, Mapping) or not intent:
        raise ControlsChangeSetError("intent must be a non-empty object")
    key_hash = _hash(_require(idempotency_key, "idempotency_key"))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.controls_change_sets "
            "WHERE project_id = %s AND idempotency_key_hash = %s",
            (project_id, key_hash),
        )
        existing = cur.fetchone()
        if existing:
            return read_change_set(conn, project_id=project_id, change_set_id=str(existing[0]))
        change_set_id = _mint("ccs")
        cur.execute(
            """
            INSERT INTO app.controls_change_sets
                (id, project_id, object_type, object_id, base_version_id, intent,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                change_set_id,
                project_id,
                object_type,
                object_id,
                base_version_id,
                canonical_json(dict(intent)),
                key_hash,
                _require(actor, "actor"),
            ),
        )
    return read_change_set(conn, project_id=project_id, change_set_id=change_set_id)


_COLUMNS = (
    "id",
    "project_id",
    "object_type",
    "object_id",
    "base_version_id",
    "intent",
    "diff",
    "impact",
    "dependency_fingerprint",
    "state",
    "confirmation_used_at",
    "expires_at",
    "result_version_id",
    "created_by",
    "created_at",
    "updated_at",
)


def find_by_idempotency_key(
    conn, *, project_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    """The change set this key already opened, if any.

    :func:`create_change_set` is idempotent by key, but a caller that must COMPOSE
    an intent before it can call create cannot use that: composing may refuse on
    exactly the state the first run produced. `rule_set_adoption` met this --
    replaying an adoption refused with `rule_key_already_published`, because the
    rule the first run published was already in the ladder. A replay is the same
    gesture, and it returns its result rather than a refusal that reads as a
    rewrite.
    """

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.controls_change_sets "
            "WHERE project_id = %s AND idempotency_key_hash = %s",
            (project_id, _hash(_require(idempotency_key, "idempotency_key"))),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return read_change_set(conn, project_id=project_id, change_set_id=str(row[0]))


def read_change_set(conn, *, project_id: str, change_set_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.controls_change_sets "
            "WHERE id = %s AND project_id = %s",
            (change_set_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ControlsChangeSetNotFound(change_set_id)
    record = dict(zip(_COLUMNS, row))
    # The token hash is never selected, so it cannot leak through a read.
    return record


# ---------------------------------------------------------------------------
# Prepare.
# ---------------------------------------------------------------------------


def dependency_fingerprint(
    conn, *, project_id: str, object_type: str, object_id: str | None
) -> str:
    """A digest of everything the confirmation depends on.

    Recomputed at confirm and compared. It deliberately includes the POINTERS
    rather than the content: a Rule Set whose current version moved is a
    different world even if the new version happens to say the same thing, and
    the reviewer approved a diff against the old one.
    """

    state: dict[str, Any] = {"object_type": object_type, "object_id": object_id}
    if object_id:
        with conn.cursor() as cur:
            if object_type == "rule-set":
                cur.execute(
                    "SELECT lifecycle_status, current_version_id, pending_version_id "
                    "FROM app.governance_rule_sets WHERE id = %s AND project_id = %s",
                    (object_id, project_id),
                )
            elif object_type == "dq-monitor":
                cur.execute(
                    "SELECT lifecycle_status, current_version_id, pending_version_id "
                    "FROM app.dq_monitors WHERE id = %s AND project_id = %s",
                    (object_id, project_id),
                )
            else:
                cur.execute(
                    "SELECT status, last_observed_at::text, "
                    "(SELECT COUNT(*) FROM app.control_case_decisions WHERE case_id = %s)::text "
                    "FROM app.control_cases WHERE id = %s AND project_id = %s",
                    (object_id, object_id, project_id),
                )
            row = cur.fetchone()
        # A missing object is a REAL state, hashed as such: preparing against
        # something that has since been archived must not confirm.
        state["owner_state"] = list(row) if row else None
    return content_hash(state)


def prepare_change_set(
    conn, *, project_id: str, change_set_id: str, actor: str
) -> tuple[dict[str, Any], str]:
    """Derive the diff, impact and fingerprint, and mint a single-use token.

    Returns ``(change_set, confirmation_token)``. The token is returned ONCE and
    stored only as a hash; a caller that loses it prepares again.
    """

    record = read_change_set(conn, project_id=project_id, change_set_id=change_set_id)
    if record["state"] == "confirmed":
        raise ControlsChangeSetError("this change set is already confirmed")
    if record["state"] in {"rejected", "expired"}:
        raise ControlsChangeSetError(f"a {record['state']} change set cannot be prepared")

    fingerprint = dependency_fingerprint(
        conn,
        project_id=project_id,
        object_type=str(record["object_type"]),
        object_id=record["object_id"],
    )
    diff, impact = _derive(conn, project_id=project_id, record=record)

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + CONFIRMATION_TTL
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.controls_change_sets
            SET state = 'prepared', diff = %s::jsonb, impact = %s::jsonb,
                dependency_fingerprint = %s, confirmation_token_hash = %s,
                expires_at = %s, updated_at = NOW()
            WHERE id = %s AND project_id = %s
            """,
            (
                canonical_json(diff),
                canonical_json(impact),
                fingerprint,
                _hash(token),
                expires,
                change_set_id,
                project_id,
            ),
        )
    _ = actor
    return read_change_set(conn, project_id=project_id, change_set_id=change_set_id), token


def _derive(conn, *, project_id: str, record: Mapping[str, Any]) -> tuple[dict, dict]:
    """SERVER-derived diff and impact. Never accepted from a caller.

    An impact a client supplied describes what the client claimed -- the same
    class of defect Story 48.3 removed when it deleted the money calculators that
    took caller-authored amounts.
    """

    object_type = str(record["object_type"])
    intent = record["intent"] if isinstance(record["intent"], dict) else {}
    diff = {"object_type": object_type, "intent_keys": sorted(intent)}
    impact: dict[str, Any] = {"object_type": object_type}

    if object_type == "rule-set" and record["object_id"]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT family, name, current_version_id FROM app.governance_rule_sets "
                "WHERE id = %s AND project_id = %s",
                (record["object_id"], project_id),
            )
            head = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM app.governance_rule_set_exceptions "
                "WHERE rule_set_id = %s AND (expires_at IS NULL OR expires_at >= CURRENT_DATE)",
                (record["object_id"],),
            )
            exceptions = cur.fetchone()[0]
        impact.update(
            {
                "family": head[0] if head else None,
                "replaces_version_id": head[2] if head else None,
                "active_exceptions": int(exceptions),
            }
        )
    elif object_type == "dq-monitor" and record["object_id"]:
        with conn.cursor() as cur:
            # THE LAST dq_issues READER OFF THE PREDICATE, routed onto it
            # (AI-235, closed 2026-08-21): spelled by hand this count omitted
            # the suppression half, so a live-suppressed anomaly weighed on a
            # change-set's impact while `dq_governance.open_issues` said the
            # anomaly was not open. Two readers, one word, opposite answers.
            from core.dq_governance import open_issue_predicate  # noqa: PLC0415

            cur.execute(
                "SELECT COUNT(*) FROM app.dq_issues WHERE monitor_id = %s "
                f"AND {open_issue_predicate()}",  # noqa: S608
                (record["object_id"],),
            )
            open_issues = cur.fetchone()[0]
        impact["open_issues"] = int(open_issues)
    elif object_type == "control-case" and record["object_id"]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.control_case_candidates WHERE case_id = %s",
                (record["object_id"],),
            )
            candidates = cur.fetchone()[0]
        impact["candidates"] = int(candidates)
    return diff, impact


# ---------------------------------------------------------------------------
# Confirm.
# ---------------------------------------------------------------------------


def confirm_change_set(
    conn,
    *,
    project_id: str,
    change_set_id: str,
    confirmation_token: str,
    actor: str,
    apply: Any = None,
) -> dict[str, Any]:
    """Consume the token once and commit, or refuse with the exact reason.

    Everything is recomputed rather than trusted. ``apply`` is the owner command
    callable -- it receives ``(conn, record)`` and returns the result version id.
    Nothing here edits a mapping or advances a Data pointer itself: when a
    decision must affect Data, the owner is CALLED and its exact outcome is what
    gets recorded.
    """

    record = read_change_set(conn, project_id=project_id, change_set_id=change_set_id)

    if record["state"] == "confirmed":
        # Idempotent replay: the same change set confirmed twice returns the
        # first result rather than minting a second version.
        return {**record, "replayed": True}
    if record["state"] != "prepared":
        raise ControlsChangeSetError("only a prepared change set can be confirmed")

    expires = record["expires_at"]
    if expires and expires < datetime.now(timezone.utc):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.controls_change_sets SET state = 'expired' WHERE id = %s",
                (change_set_id,),
            )
        raise ControlsChangeSetStale("the confirmation window has closed; prepare again")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT confirmation_token_hash FROM app.controls_change_sets "
            "WHERE id = %s AND project_id = %s",
            (change_set_id, project_id),
        )
        stored = cur.fetchone()
    if not stored or not stored[0]:
        raise ControlsChangeSetError("this change set has no confirmation to consume")
    if not secrets.compare_digest(str(stored[0]), _hash(confirmation_token or "")):
        # Deliberately the same refusal as a missing token: distinguishing them
        # tells a caller whether a token EXISTS, which is information.
        raise ControlsChangeSetError("this change set has no confirmation to consume")

    live = dependency_fingerprint(
        conn,
        project_id=project_id,
        object_type=str(record["object_type"]),
        object_id=record["object_id"],
    )
    if live != record["dependency_fingerprint"]:
        raise ControlsChangeSetStale(
            "the object changed since this change was prepared; review it again"
        )

    result_version_id = apply(conn, record) if apply is not None else None

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.controls_change_sets
            SET state = 'confirmed', confirmation_used_at = NOW(),
                confirmation_token_hash = NULL, result_version_id = %s, updated_at = NOW()
            WHERE id = %s AND project_id = %s
            """,
            (result_version_id, change_set_id, project_id),
        )
    _ = actor
    return {
        **read_change_set(conn, project_id=project_id, change_set_id=change_set_id),
        "replayed": False,
    }
