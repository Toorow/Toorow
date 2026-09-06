"""What changes between two contracts, in the values a person can read.

WHY THIS MODULE EXISTS — measured 2026-08-18. `datastream_change._top_level_diff`
composes the only diff the confirmation dialog has ever shown, and it carries
`before_hash` / `after_hash` per TOP-LEVEL key. Excluding one column out of
forty-six therefore reached the person as a single row:

    $.fields    9f2c…a1    4b70…de

Two hex strings. The one moment the product asks for an exact confirmation was
the one moment it showed nothing about what is being confirmed. The hashes are
not wrong — they are what `MutationResult` compares — they are simply not a
reading, and a screen that renders them as one is asking for approval of an
arithmetic nobody has been shown.

IT IS NOT `mapping_diff.diff_mappings`, AND THE TWO ARE NOT MERGED. That one
(Story 36.17) reads two VERSION ROWS for the governed publication review and
answers in counted categories — added/removed fields, type changes, target
rebindings — under E36-NFR01, which forbids a provider value ever appearing in
it. This one reads two CONTRACT PAYLOADS for the tab where the person authors
them, and its whole subject is the before and after VALUE of each reading. Same
noun, two questions: fusing them would make one of the two lie.

WHY IT IS COMPOSED ON THE SERVER AND NOT IN THE CONSOLE. Three reasons, each of
them a rule this product already applies elsewhere:

  * the BASE is the server's. `prepare_change` resolves it through
    `datastream_change_base.resolve_base` — the pointer when there is one, the
    head of the ledger when there is not — and it is not always the version the
    screen happens to have selected. A diff composed in the console would show a
    comparison against a document the confirmation never looked at;
  * the CONCEPT IS NAMED, NEVER THE IDENTITY. `binding.mdm_target` holds a
    registry id (`mdm_6D13WZ…`); the name lives in `app.mdm_canonical_fields`,
    and `datastream-workbench-and-wizard.md:4299` states in as many words that
    the resolution happens where the vocabulary lives — "le serveur propose, le
    client n'invente pas";
  * ONE writer of the vocabulary. The same entries answer the confirmation
    dialog and the version-to-version comparison of the ledger
    (`datastream_mapping_api._compare_datastream_mapping_versions`), so the two
    cannot drift into two ways of saying "this column stopped landing".

WHAT IT NEVER DOES. It writes nothing, decides nothing and refuses nothing: a
value diff is a reading beside the hashes, never in place of them. Absent, it
costs the dialog its values and nothing else.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["MAX_VALUE_CHARS", "value_diff"]

# How much of one value reaches the screen before it is cut, and the cut is
# ANNOUNCED. A silent truncation is a value that reads as complete and is not.
MAX_VALUE_CHARS = 400

# The three readings the Mapping tab's own controls write, in the words that tab
# uses. A fourth exists and is deliberate: `Other readings` catches everything a
# raw-contract edit can reach, so this diff never says "nothing else changed"
# about a field it did not look at.
READING_LANDING = "Landing"
READING_BINDING = "Binding state"
READING_TARGET = "Governed target"
READING_OTHER = "Other readings of this column"
READING_PRESENCE = "Presence in the contract"
READING_PATH = "Contract path"

# The words this file puts on the wire for a landing state. They are the Mapping
# tab's, not the store's: `binding.status == "excluded"` is the sole authority of
# exclusion (`datastream_change._landing_field_ids`), and "Excluded" is what the
# row says.
LANDS = "Lands"
EXCLUDED = "Does not land (excluded)"
ABSENT = "Absent"
NO_TARGET = "No governed target"

# The keys of a field record this module reads explicitly. Everything else is
# folded into `READING_OTHER` rather than dropped.
_EXPLICIT_FIELD_KEYS = {"field_id", "binding"}
_EXPLICIT_BINDING_KEYS = {"status", "mdm_target", "canonical_target"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _same(left: Any, right: Any) -> bool:
    return _canonical(left) == _canonical(right)


def _capped(value: str) -> tuple[str, bool]:
    """The value, and whether the reader is looking at all of it."""
    if len(value) <= MAX_VALUE_CHARS:
        return value, False
    return f"{value[:MAX_VALUE_CHARS]}… (+{len(value) - MAX_VALUE_CHARS} characters)", True


def _entry(
    subject: str, subject_kind: str, reading: str, before: str, after: str
) -> dict[str, Any]:
    before_text, before_cut = _capped(before)
    after_text, after_cut = _capped(after)
    return {
        "subject": subject,
        "subject_kind": subject_kind,
        "reading": reading,
        "before": before_text,
        "after": after_text,
        "truncated": before_cut or after_cut,
    }


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _fields_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for field in _records(payload.get("fields")):
        field_id = field.get("field_id") or field.get("name")
        if isinstance(field_id, str) and field_id:
            fields[field_id] = field
    return fields


def _binding(field: dict[str, Any]) -> dict[str, Any]:
    binding = field.get("binding")
    return binding if isinstance(binding, dict) else {}


def _landing(field: dict[str, Any]) -> str:
    return EXCLUDED if _binding(field).get("status") == "excluded" else LANDS


def _binding_state(field: dict[str, Any]) -> str:
    status = _binding(field).get("status")
    return str(status) if isinstance(status, str) and status else "unknown"


def _target(field: dict[str, Any], names: dict[str, str]) -> str:
    """The concept the column names, by NAME whenever the vocabulary answered.

    BOTH KEYS ARE READ, and that is measured rather than defensive: 105 of 708
    bindings leave `mdm_target` null and put the same registry identity under
    `canonical_target` (`mappingModel.governedTarget`). Reading one alone reports
    a bound column as unbound — in a confirmation dialog that would be a false
    "this column stops naming its concept".
    """
    binding = _binding(field)
    identity = binding.get("mdm_target") or binding.get("canonical_target") or ""
    identity = str(identity or "")
    if not identity:
        return NO_TARGET
    # The id travels beside the name only when they differ: a vocabulary that
    # could not be read leaves the identity alone rather than pretending a name.
    name = names.get(identity)
    return f"{name} ({identity})" if name and name != identity else identity


def _rest_of_field(field: dict[str, Any]) -> dict[str, Any]:
    rest = {key: value for key, value in field.items() if key not in _EXPLICIT_FIELD_KEYS}
    binding_rest = {
        key: value for key, value in _binding(field).items() if key not in _EXPLICIT_BINDING_KEYS
    }
    if binding_rest:
        rest["binding"] = binding_rest
    return rest


def _join_sentence(join: dict[str, Any]) -> str:
    sources = [str(one) for one in (join.get("sources") or [])]
    separator = join.get("separator")
    said = " + ".join(sources) if sources else "nothing"
    target = str(join.get("target") or "")
    tail = f" joined by “{separator}”" if isinstance(separator, str) and separator else ""
    return f"{said} → {target}{tail}"


def _split_sentence(split: dict[str, Any]) -> str:
    targets = [
        str(entry.get("target") or "")
        for entry in _records(split.get("targets"))
        if entry.get("target")
    ]
    pattern = str(split.get("pattern") or "")
    said = ", ".join(targets) if targets else "nothing"
    return f"{split.get('source')} → {said} (pattern {pattern})"


def _treatments(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    treatments = payload.get("column_treatments")
    treatments = treatments if isinstance(treatments, dict) else {}
    joins = {
        str(join.get("target") or ""): join
        for join in _records(treatments.get("joins"))
        if join.get("target")
    }
    splits = {
        str(split.get("source") or ""): split
        for split in _records(treatments.get("splits"))
        if split.get("source")
    }
    return joins, splits


def _field_entries(
    before: dict[str, Any], after: dict[str, Any], names: dict[str, str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    before_fields, after_fields = _fields_by_id(before), _fields_by_id(after)
    for field_id in sorted(set(before_fields) | set(after_fields)):
        was, now = before_fields.get(field_id), after_fields.get(field_id)
        if was is None or now is None:
            entries.append(
                _entry(
                    field_id,
                    "field",
                    READING_PRESENCE,
                    ABSENT if was is None else "Declared",
                    ABSENT if now is None else "Declared",
                )
            )
            # A column that appears or disappears still says what it becomes:
            # "Declared" alone would leave the reader to open the raw contract.
            present = now if was is None else was
            entries.append(
                _entry(
                    field_id,
                    "field",
                    READING_TARGET,
                    NO_TARGET if was is None else _target(present, names),
                    NO_TARGET if now is None else _target(present, names),
                )
            )
            continue
        if _landing(was) != _landing(now):
            entries.append(_entry(field_id, "field", READING_LANDING, _landing(was), _landing(now)))
        if _binding_state(was) != _binding_state(now):
            entries.append(
                _entry(field_id, "field", READING_BINDING, _binding_state(was), _binding_state(now))
            )
        if _target(was, names) != _target(now, names):
            entries.append(
                _entry(field_id, "field", READING_TARGET, _target(was, names), _target(now, names))
            )
        rest_was, rest_now = _rest_of_field(was), _rest_of_field(now)
        if not _same(rest_was, rest_now):
            entries.append(
                _entry(field_id, "field", READING_OTHER, _canonical(rest_was), _canonical(rest_now))
            )
    return entries


def _treatment_entries(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    joins_was, splits_was = _treatments(before)
    joins_now, splits_now = _treatments(after)
    for target in sorted(set(joins_was) | set(joins_now)):
        was, now = joins_was.get(target), joins_now.get(target)
        if _same(was, now):
            continue
        entries.append(
            _entry(
                target,
                "treatment",
                "Declared join",
                _join_sentence(was) if was else "Not declared",
                _join_sentence(now) if now else "Withdrawn",
            )
        )
    for source in sorted(set(splits_was) | set(splits_now)):
        was, now = splits_was.get(source), splits_now.get(source)
        if _same(was, now):
            continue
        entries.append(
            _entry(
                source,
                "treatment",
                "Declared split",
                _split_sentence(was) if was else "Not declared",
                _split_sentence(now) if now else "Withdrawn",
            )
        )
    return entries


def _path_entries(
    before: dict[str, Any], after: dict[str, Any], *, skip: set[str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        if key in skip:
            continue
        was, now = before.get(key), after.get(key)
        if _same(was, now):
            continue
        entries.append(
            _entry(
                f"$.{key}",
                "path",
                READING_PATH,
                ABSENT if key not in before else _canonical(was),
                ABSENT if key not in after else _canonical(now),
            )
        )
    return entries


def value_diff(
    kind: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    canonical_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The readable difference between two contracts, beside the hashed one.

    `state` is always one of two words, because "there is nothing to read" and
    "this could not be composed" are two sentences and an empty list says the
    first while meaning either:

      composed     the entries are next to it, and an empty list means the two
                   contracts agree on every reading this module knows how to name.
      unavailable  the composition failed, with the reason. The hashes stay.

    A mapping change is read FIELD BY FIELD; every other kind is read path by
    path, which is still values rather than hashes. `column_treatments` is a
    mapping path with a sentence of its own — "date_part + hour → event_date" —
    because its canonical JSON is exactly the shape nobody could confirm.
    """
    names = canonical_names or {}
    if kind == "mapping":
        entries = _field_entries(before, after, names)
        entries += _treatment_entries(before, after)
        entries += _path_entries(before, after, skip={"fields", "column_treatments"})
    else:
        entries = _path_entries(before, after, skip=set())
    return {"state": "composed", "entries": entries}
