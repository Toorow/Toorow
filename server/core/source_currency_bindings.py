"""Declared source currency: the Currency & FX family that ends the second store.

WHAT THIS REPLACES, AND WHY IT IS NOT A NEW STORE.

``app.fx_conflict_resolutions`` (migration 053, Story 13.2) held one row per
``(project, target_field, source_module)`` saying "read the cost of this module as
USD". Migration 145 listed it among the seven stores that "stop being
AUTHORITIES" once Controls & Quality landed, and left the DROP to "the commit that
removes the last reader". Until then two things stayed true and were both wrong:

* the console still WROTE it, through an ``ON CONFLICT DO UPDATE`` that overwrote
  ``decided_by`` and ``decided_at`` in place. A governed decision that leaves no
  version is not a decision anyone can audit -- exactly the defect Story 60.5
  repaired for the two rule families, and the one this module repairs for the
  third;
* nine dbt staging models still READ it, so the dead store was the runtime
  authority for every converted cost in the product.

The living store is the one migration 144 built and 145 adopted:
``app.governance_rule_sets`` / ``app.governance_rule_set_versions``, family-typed,
with immutable published versions, three version pointers and a frozen content
hash. Money, FX ingestion, Timezone (48.3) and Tax & Fees (48.4) are already
mounted on it. A declared source currency is the same kind of statement, so it
becomes a fifth family on the SAME substrate rather than a table of its own.

THE SHAPE, AND WHY THE CONTENT IS ``ordered_rules`` RATHER THAN ``payload``.

There is exactly one head per Project (``FAMILY_SOURCE_CURRENCY`` /
``BINDING_NAME``): a second named set of declarations would be a second authority
over the same field, which is the defect being removed, not a setting. The
declarations themselves are a LIST, one entry per
``(target_field, source_module)``, and a list is what ``ordered_rules`` is for --
the same choice ``core.tax_fee_rule_set`` makes for its ladder.

Every declaration carries its OWN ``declared_by`` / ``declared_at``, carried
forward untouched when another declaration in the same set changes. That is the
attribution the upsert destroyed: without it, editing the Meta binding would
re-stamp the TikTok one with today's date and today's actor.

WHAT dbt READS. Not this module: ``app.fx_source_currency_bindings_v`` (migration
281) projects the PUBLISHED version of every such head back into the flat
``(project_id, target_field, source_module, resolved_source_currency)`` shape the
staging models already join on, exactly as ``app.project_money_policy_v``
(migration 148) projects the Money Policy. No Python converts a currency here
(AD-6 still holds); this module only decides what the source currency IS.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import (
    RuleSetError,
    RuleSetProfile,
    active_version,
    draft_version,
    ensure_rule_set,
    publish_version,
    register_profile,
)

logger = logging.getLogger(__name__)

FAMILY_SOURCE_CURRENCY = "source_currency"
PROFILE_SOURCE_CURRENCY = "source_currency_binding_v1"

#: One head per Project. See the module docstring: a second named set would be a
#: second authority over the same (field, module) pair.
BINDING_NAME = "project_bindings"

RULE_SET_LABEL = "Declared Source Currencies"

#: Exactly the keys one declaration may carry. A superset is REFUSED rather than
#: dropped, so a caller sending ``currency`` instead of ``source_currency`` is
#: told, not silently published with no currency at all.
_BINDING_KEYS = frozenset(
    {
        "target_field",
        "source_module",
        "source_currency",
        "note",
        "declared_by",
        "declared_at",
    }
)

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,126}$")
#: Module names carry hyphens ('meta-ads'), which the rule-set name grammar does
#: not. This is content, not an identifier of the substrate, so it has its own.
_MODULE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,126}$")

_NOTE_MAX = 600


def _text(value: Any) -> str:
    return str(value or "").strip()


def _validate_currency(code: str) -> str:
    """The governed ISO 4217 vocabulary, never a second hand-written list."""
    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    if not code:
        raise RuleSetError(
            "source_currency is required; pick an ISO 4217 code offered by "
            "/api/reference/currencies"
        )
    resolved = resolve_currency(code)
    if resolved is None or not resolved.is_tender:
        raise RuleSetError(
            f"{code!r} is not a selectable source currency; pick an ISO 4217 code "
            "offered by /api/reference/currencies"
        )
    return resolved.code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_binding_rules(
    rules: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize and order the declarations of one version.

    Ordered by ``(target_field, source_module)`` because order here is CONTENT --
    two logically identical sets must hash identically, or "has this Project's FX
    reading changed?" stops being answerable. A duplicate pair is refused rather
    than resolved by write order: the substrate's own contract forbids breaking a
    tie that way, and silently keeping the last one is how the old upsert lost
    decisions.
    """

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rules:
        unknown = sorted(set(raw) - _BINDING_KEYS)
        if unknown:
            raise RuleSetError(
                f"a source currency declaration cannot carry {unknown}; "
                f"the accepted keys are {sorted(_BINDING_KEYS)}"
            )
        target_field = _text(raw.get("target_field"))
        if not _FIELD_RE.fullmatch(target_field):
            raise RuleSetError(
                "target_field must be a canonical field name in lowercase "
                f"snake_case; got {target_field!r}"
            )
        source_module = _text(raw.get("source_module"))
        if not _MODULE_RE.fullmatch(source_module):
            raise RuleSetError(
                f"source_module must be a module name; got {source_module!r}"
            )
        key = (target_field, source_module)
        if key in seen:
            raise RuleSetError(
                f"{target_field}/{source_module} is declared twice in the same "
                "version; one pair has exactly one source currency"
            )
        seen.add(key)

        note = _text(raw.get("note")) or None
        if note is not None and len(note) > _NOTE_MAX:
            raise RuleSetError(f"a declaration note cannot exceed {_NOTE_MAX} characters")
        declared_by = _text(raw.get("declared_by"))
        if not declared_by:
            raise RuleSetError(
                f"{target_field}/{source_module} has no declared_by; a governed "
                "declaration names who made it"
            )
        declared_at = _text(raw.get("declared_at")) or _now_iso()

        normalized.append(
            {
                "target_field": target_field,
                "source_module": source_module,
                "source_currency": _validate_currency(_text(raw.get("source_currency")).upper()),
                "note": note,
                "declared_by": declared_by,
                "declared_at": declared_at,
            }
        )

    normalized.sort(key=lambda item: (item["target_field"], item["source_module"]))
    return normalized


def _validate_binding_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload of this family is empty on purpose.

    Every family so far carries a policy object; this one's entire content is the
    LIST of declarations, which lives in ``ordered_rules``. Inventing a payload key
    to look like the others would put a second place to write the same fact.
    """

    unknown = sorted(payload)
    if unknown:
        raise RuleSetError(
            "a source currency binding version carries no payload keys; the "
            f"declarations are its ordered rules (got {unknown})"
        )
    return {}


register_profile(
    RuleSetProfile(
        key=PROFILE_SOURCE_CURRENCY,
        family=FAMILY_SOURCE_CURRENCY,
        label="Declared Source Currencies",
        validate=_validate_binding_payload,
        validate_rules=validate_binding_rules,
        # No form, and the reason is the one `_validate_binding_payload` states:
        # this family's whole content is its list of declarations, and one
        # governed gesture already writes it. A second editor on the Rule Set
        # workbench would be a second place to write one fact.
        composed_by=(
            "A source currency is declared where the ambiguity is seen: on the mapping "
            "screen, on the field whose currency two modules disagree about. Declaring one "
            "there — or withdrawing one — republishes this whole set as a new version, and "
            "the withdrawn declaration stays readable in the superseded version so a figure "
            "published while it was in force can still be explained."
        ),
    )
)


# ---------------------------------------------------------------------------
# Reads. Every one names the version it came from.
# ---------------------------------------------------------------------------


def list_bindings(conn, *, project_id: str) -> list[dict[str, Any]]:
    """The declarations in force for this Project, each naming its version.

    An unpublished head is an empty list, not a gap: "nobody has declared a source
    currency" is a legitimate and common state -- every module then reads its own
    native currency, which is what the staging ``COALESCE`` already does.
    """

    found = active_version(
        conn, project_id=project_id, family=FAMILY_SOURCE_CURRENCY, name=BINDING_NAME
    )
    if found is None:
        return []
    head, version = found
    rows: list[dict[str, Any]] = []
    for rule in version.get("ordered_rules") or []:
        rows.append(
            {
                "project_id": project_id,
                "target_field": rule["target_field"],
                "source_module": rule["source_module"],
                "resolved_source_currency": rule["source_currency"],
                "decided_by": rule["declared_by"],
                "decided_at": rule["declared_at"],
                "note": rule.get("note"),
                "rule_set_id": str(head["id"]),
                "rule_set_version_id": str(version["id"]),
                "content_hash": str(version["content_hash"]),
            }
        )
    return rows


def get_binding(
    conn, *, project_id: str, target_field: str, source_module: str
) -> dict[str, Any] | None:
    """One declaration, or ``None``."""

    for row in list_bindings(conn, project_id=project_id):
        if row["target_field"] == target_field and row["source_module"] == source_module:
            return row
    return None


# ---------------------------------------------------------------------------
# Writes. Each one publishes a version; nothing is ever overwritten in place.
# ---------------------------------------------------------------------------


def _org_id(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None or not row[0]:
        raise RuleSetError(f"project {project_id!r} has no organization")
    return str(row[0])


def _publish(
    conn,
    *,
    project_id: str,
    rules: Sequence[Mapping[str, Any]],
    actor: str,
    label: str,
    description: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    head = ensure_rule_set(
        conn,
        org_id=_org_id(conn, project_id),
        project_id=project_id,
        family=FAMILY_SOURCE_CURRENCY,
        name=BINDING_NAME,
        label=RULE_SET_LABEL,
        actor=actor,
    )
    version = draft_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        profile=PROFILE_SOURCE_CURRENCY,
        label=label,
        description=description,
        payload={},
        ordered_rules=list(rules),
        actor=actor,
    )
    published = publish_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        version_id=str(version["id"]),
        actor=actor,
    )
    return head, published


def declare_binding(
    conn,
    *,
    project_id: str,
    target_field: str,
    source_module: str,
    source_currency: str,
    actor: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Declare (or re-declare) the source currency of one field of one module.

    Publishes a NEW version containing the whole set. The previous version is
    superseded, never edited: that is what makes "what did this Project read as
    the source currency on the day this figure was published?" answerable, and it
    is precisely what the old ``ON CONFLICT DO UPDATE`` made unanswerable.

    Idempotent by content: re-declaring the same currency with the same note
    returns the version already in force instead of minting an identical one.
    """

    if not _text(project_id):
        raise RuleSetError("project_id is required")
    actor = _text(actor) or "anonymous"

    current = list_bindings(conn, project_id=project_id)
    kept = [
        {
            "target_field": row["target_field"],
            "source_module": row["source_module"],
            "source_currency": row["resolved_source_currency"],
            "note": row["note"],
            "declared_by": row["decided_by"],
            "declared_at": row["decided_at"],
        }
        for row in current
        if not (
            row["target_field"] == _text(target_field)
            and row["source_module"] == _text(source_module)
        )
    ]
    kept.append(
        {
            "target_field": _text(target_field),
            "source_module": _text(source_module),
            "source_currency": _text(source_currency).upper(),
            "note": _text(note) or None,
            "declared_by": actor,
            "declared_at": _now_iso(),
        }
    )

    _publish(
        conn,
        project_id=project_id,
        rules=kept,
        actor=actor,
        label=f"{_text(target_field)} / {_text(source_module)} declared",
        description=(
            f"Declared source currency for {_text(target_field)} on "
            f"{_text(source_module)}."
        ),
    )
    result = get_binding(
        conn,
        project_id=project_id,
        target_field=_text(target_field),
        source_module=_text(source_module),
    )
    if result is None:  # pragma: no cover -- only under a concurrent republish
        raise RuleSetError("the declaration disappeared immediately after publishing")

    logger.info(
        "source_currency_bindings: declared project=%s field=%s module=%s "
        "currency=%s version=%s by=%s",
        project_id,
        result["target_field"],
        result["source_module"],
        result["resolved_source_currency"],
        result["rule_set_version_id"],
        actor,
    )
    return result


def withdraw_binding(
    conn,
    *,
    project_id: str,
    target_field: str,
    source_module: str,
    actor: str,
) -> dict[str, Any]:
    """Withdraw one declaration by publishing the set without it.

    Not a DELETE. The withdrawn declaration stays readable in the superseded
    version, so a figure published while it was in force can still be explained.
    """

    target_field = _text(target_field)
    source_module = _text(source_module)
    actor = _text(actor) or "anonymous"

    current = list_bindings(conn, project_id=project_id)
    remaining = [
        {
            "target_field": row["target_field"],
            "source_module": row["source_module"],
            "source_currency": row["resolved_source_currency"],
            "note": row["note"],
            "declared_by": row["decided_by"],
            "declared_at": row["decided_at"],
        }
        for row in current
        if not (
            row["target_field"] == target_field and row["source_module"] == source_module
        )
    ]
    if len(remaining) == len(current):
        raise RuleSetError(
            f"no source currency is declared for {target_field}/{source_module} "
            "in this Project"
        )

    _, published = _publish(
        conn,
        project_id=project_id,
        rules=remaining,
        actor=actor,
        label=f"{target_field} / {source_module} withdrawn",
        description=(
            f"Withdrew the declared source currency for {target_field} on "
            f"{source_module}."
        ),
    )

    logger.info(
        "source_currency_bindings: withdrawn project=%s field=%s module=%s "
        "version=%s by=%s",
        project_id,
        target_field,
        source_module,
        published["id"],
        actor,
    )
    return {
        "project_id": project_id,
        "target_field": target_field,
        "source_module": source_module,
        "rule_set_version_id": str(published["id"]),
    }
