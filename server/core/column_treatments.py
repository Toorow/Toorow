"""Story 60.6 -- what ONE source column becomes, said in one word.

WHY THIS MODULE EXISTS. The repository has known how to NOT load a column since
Epic 12: three compiler gates, the file import and the fail-closed MDM all read
``binding.status == "excluded"`` (``datastream_projection.py``,
``csv_excel_import.py``, ``datastream_field_mapping.py``). What nobody could do
was SAY it, or say the two other things a person does to a spreadsheet: joining
several columns into one concept (year + month + day -> a date) and splitting one
column into several (a placement code -> country + format + theme).

Those two verbs existed in the repository exactly twice, both times FROZEN:
``file_source_producer.ReshapeSpec`` joins a start/end pair in one hardcoded
shape, and the mart concatenates four hardcoded pairs. Neither is declarable.

THE VOCABULARY IS CLOSED, AND IT HAS A SIXTH WORD THE PLAN DID NOT NAME.
``epic-60:129-131`` names five treatments: ``Direct``, ``Joined``,
``Split into N``, ``Resolved by a list``, ``Excluded``. A sixth is required and
is not an invention: a column nobody has bound and nobody has excluded is neither
direct nor excluded, and calling it ``Direct`` would claim a binding that does
not exist. The epic itself names the state -- "35 colonnes sur 46 ne servent a
rien et restent listees : leur absence serait l'inventaire perdu de ce qui reste
a decider". ``Not decided`` is that inventory, and it is the honest word.

WHERE THE DECLARATION LIVES, AND WHY NO MIGRATION.
In the mapping version payload (``datastream-field-mapping.schema.json``,
``column_treatments``), which is already versioned, content-hashed and immutable
-- publishing a change appends a version and rewrites no history
(``datastream-workbench-and-wizard.md:987``). No table is added: the only store
that could have claimed one, ``app.datastream_derived_columns``, is deprecated by
this same story (AI-252) and carries zero rows.

A SPLIT NAMES ITS N TARGETS IN ONE ENTRY, NEVER N ENTRIES CARRYING THE SAME
PATTERN. Two copies of one pattern are free to diverge, and that is the defect
story 59.5 spent a day repairing. One entry, one pattern, N named targets.

STORY 70.1 -- THE SEVENTH WORD, AND WHY ONE PATTERN WAS NOT ENOUGH. ``Split into
N`` carries ONE shape per column. The reference case of
``docs/product-architecture/capabilities/analytics-alignment.md`` carries three
in the same column -- positional layouts of 21, 19 and 12 dimensions -- and which
one applies is decided ROW BY ROW, by where an anchor token sits in the token
list. Declaring that as three ``Split into N`` entries is the 59.5 defect again
at a larger scale: three copies of one grammar, free to diverge, and no column
able to say which of the three produced a value.

``Split by declared schema`` is one entry carrying a SET of layouts, one
detector, one separator and one declared ``remainder``. It emits its own
provenance on every row -- the layout it chose, the token count it saw, and
whether that count is the one the layout describes -- because a derivation that
cannot be contradicted by the row it derived is the FX defect this repository
already refuses. A row no layout detects stays UNSPLIT and keeps its token
count: it is countable, and a default layout would be a wrong answer that looks
right.

A JOIN KEEPS ITS SOURCE COLUMNS AND MARKS THEM. Hiding the sources of a join is
the same defect as hiding an excluded column under another name, and the story's
"Refuse" line already forbids the second: "leur absence serait l'inventaire
perdu". They stay listed, carrying ``Joined``.

Pure module: no I/O, no database, no dialect. Deterministic on its inputs.
"""

from __future__ import annotations

import re
from typing import Any

#: The treatment vocabulary. Closed set; a writer that invents a word is a bug.
DIRECT = "Direct"
JOINED = "Joined"
RESOLVED_BY_LIST = "Resolved by a list"
EXCLUDED = "Excluded"
NOT_DECIDED = "Not decided"
SPLIT_BY_DECLARED_SCHEMA = "Split by declared schema"

#: The two absences, and they are NOT the same sentence. `NO_SAMPLE_VALUE` means
#: the sample was read and this column carried nothing in it; `UNKNOWN_SAMPLE`
#: means no profile was ever taken. Never an empty string, and never a `0` --
#: a `0` READ from the file is a real value and is rendered as itself.
NO_SAMPLE_VALUE = "no sample value"
UNKNOWN_SAMPLE = "unknown"

#: The binding status that decides exclusion, and the ONLY authority for it
#: (story 60.6, arbitrage 2). The `included` boolean the review bundle used to
#: carry is derived from this one and governs nothing.
EXCLUDED_BINDING_STATUS = "excluded"

#: Refusal codes. Each one names a shape that must never reach a mapping version,
#: because every one of them would produce a column whose provenance cannot be
#: told -- or two rules that disagree about the same target.
REFUSE_JOIN_NEEDS_TWO_SOURCES = "treatment_join_needs_two_sources"
REFUSE_SPLIT_NEEDS_TWO_TARGETS = "treatment_split_needs_two_targets"
REFUSE_SOURCE_UNKNOWN = "treatment_source_unknown"
REFUSE_SOURCE_EXCLUDED = "treatment_source_excluded"
REFUSE_SOURCE_CLAIMED_TWICE = "treatment_source_claimed_twice"
REFUSE_TARGET_CLAIMED_TWICE = "treatment_target_claimed_twice"
REFUSE_SPLIT_PATTERN_INVALID = "treatment_split_pattern_invalid"
REFUSE_SPLIT_GROUP_MISSING = "treatment_split_group_missing"

#: Story 70.1. The refusals of a declared schema set. Each one names a shape that
#: would make a row's provenance untellable, or make two layouts answer one row.
REFUSE_SCHEMA_NAME_INVALID = "treatment_schema_name_invalid"
REFUSE_SCHEMA_SEPARATOR_INVALID = "treatment_schema_separator_invalid"
REFUSE_SCHEMA_REMAINDER_MISSING = "treatment_schema_remainder_missing"
REFUSE_SCHEMA_NEEDS_A_LAYOUT = "treatment_schema_needs_a_layout"
REFUSE_SCHEMA_LAYOUT_EMPTY = "treatment_schema_layout_empty"
REFUSE_SCHEMA_LAYOUT_NAME_CLAIMED_TWICE = "treatment_schema_layout_name_claimed_twice"
REFUSE_SCHEMA_DETECTION_UNKNOWN = "treatment_schema_detection_unknown"
REFUSE_SCHEMA_ANCHOR_TOKEN_INVALID = "treatment_schema_anchor_token_invalid"
REFUSE_SCHEMA_ANCHOR_AMBIGUOUS = "treatment_schema_anchor_ambiguous"
REFUSE_SCHEMA_ANCHOR_OFFSET_INVALID = "treatment_schema_anchor_offset_invalid"
REFUSE_SCHEMA_COLUMN_NAME_INVALID = "treatment_schema_column_name_invalid"

#: A split pattern is a Python regular expression, bounded the way story 60.3
#: bounds a cleanup rule: a length cap, and nothing else -- no network, no file
#: system, no evaluation. It is compiled here to refuse a bad one BEFORE a
#: version is appended, never executed here.
MAX_PATTERN_LENGTH = 500

#: Story 70.1. The bounds of a declared schema set. They are stated here AND in
#: the JSON schema, because a bound that lives in only one of the two is a bound
#: the other half forgets.
MAX_SEPARATOR_LENGTH = 8
MAX_LAYOUT_POSITIONS = 64
MAX_LAYOUT_NAME_LENGTH = 64
#: The detector emits one bound value per anchor token per scanned offset, so
#: these two caps together bound the compiled statement -- 16 x 32 = 512 values
#: at the very worst. The reference case needs 3 tokens and 3 offsets.
MAX_ANCHOR_TOKENS = 16
MAX_ANCHOR_OFFSET = 31

#: The only detection method there is. It is declared as a word rather than
#: implied, so a second method arrives as a second word and not as a silent
#: change of meaning for rows already derived under the first.
ANCHOR_MIN_OFFSET = "anchor_min_offset"
DETECTION_METHODS = (ANCHOR_MIN_OFFSET,)

#: A schema set names itself, and that name prefixes the three provenance
#: columns it emits. Lower-case identifier: the name becomes a warehouse column
#: in both dialects, so it must be one that neither has to quote creatively.
_SCHEMA_SET_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")

#: A target concept or the remainder becomes a warehouse column NAME, emitted by
#: `schema_split_compiler` as a quoted alias. Quoting is not escaping: a name
#: carrying a double quote or a backtick would close its own quoting and let the
#: rest of the string execute at read (`schema_split_compiler` formats these
#: through `quote_identifier`, exactly as `source_column` is, and `source_column`
#: is validated by `cleanup_rules.validate_field` for this same reason). A stored
#: mapping version is immutable, so this MUST be refused at the declaration --
#: after it lands, the version can neither run nor be edited. Same shape as
#: `cleanup_rules._IDENTIFIER`.
_EMITTED_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

EMPTY_TREATMENTS: dict[str, list[Any]] = {"joins": [], "splits": [], "schema_splits": []}


class ColumnTreatmentError(ValueError):
    """A declaration that must not become an immutable mapping version."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": dict(self.detail)}


def split_into(count: int) -> str:
    """The `Split into N` word, with its N. One writer for this string."""
    return f"Split into {int(count)}"


def split_by_declared_schema() -> str:
    """The seventh word. One writer for this string, as `split_into` is for its own.

    It carries no N: a schema set has several layouts of DIFFERENT widths, and a
    number in the word would have to pick one of them and be wrong for the rows
    that took another. The width a row actually saw is `token_count`, emitted per
    row, which is the honest place for it.
    """
    return SPLIT_BY_DECLARED_SCHEMA


def is_excluded(field: Any) -> bool:
    """The one reading of exclusion, shared by every caller.

    ``binding.status == "excluded"`` and nothing else. The `included` boolean is
    not consulted: measured on 2026-08-08, its only writer set it to ``True`` in
    hard for every field (``datastream_preconfiguration.py``), so it could never
    say anything, while five live readers already read this status.
    """
    if not isinstance(field, dict):
        return False
    binding = field.get("binding")
    if not isinstance(binding, dict):
        return False
    return str(binding.get("status") or "") == EXCLUDED_BINDING_STATUS


def is_reviewed(field: Any) -> bool:
    """A field is reviewed when a binding decision has been recorded on it.

    Not "has a canonical target": an intentionally ungoverned column is a
    decision. What is NOT a decision is a field entry carrying no binding at
    all -- nothing was ever said about it.
    """
    if not isinstance(field, dict):
        return False
    binding = field.get("binding")
    return isinstance(binding, dict) and bool(str(binding.get("status") or ""))


def sample_value(profile: Any) -> str:
    """The example value a person recognises their own file by.

    Read from the sample that was ALREADY parsed -- ``profile.sample_values`` is
    filled by ``datastream_field_mapping.profile_fields`` from the sample rows,
    and nothing here re-reads a file or invents a value.

    Three answers, three meanings:
      * the first sample value, as it was read (``"0"`` included -- a zero in the
        file is data, not an absence);
      * ``no sample value`` when the sample was read and this column was empty;
      * ``unknown`` when no profile was ever taken, which is a different fact.
    """
    if not isinstance(profile, dict) or "sample_values" not in profile:
        return UNKNOWN_SAMPLE
    values = profile.get("sample_values")
    if not isinstance(values, list):
        return UNKNOWN_SAMPLE
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return NO_SAMPLE_VALUE


# ---------------------------------------------------------------------------
# Story 70.1 -- the declared schema set: read it once, and refuse it once.
#
# Everything below is PURE: it reads a declaration and answers about ONE string.
# The warehouse emission of the same declaration lives in
# `core/schema_split_compiler.py`, because this module carries no dialect -- and
# `tests/conformance/test_schema_split_dialects.py` executes both halves on the
# same rows so the two can never answer differently.
# ---------------------------------------------------------------------------


def _refuse_unsafe_emitted_column(value: str, *, schema: str, role: str) -> None:
    """A target or remainder is a column NAME, quoted and never escaped.

    Refused here, at the declaration, and not at compile time: the compiler runs
    against an immutable stored version, so a name that breaks its quoting there
    is a version that can neither run nor be re-edited. See `_EMITTED_COLUMN_NAME`.
    """
    if not _EMITTED_COLUMN_NAME.fullmatch(value):
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_COLUMN_NAME_INVALID,
            f"A {role} becomes a warehouse column name and is quoted, not escaped: "
            "it must be a letter or underscore, then letters, digits or "
            "underscores. A name carrying a quote, a space or a parenthesis would "
            "break out of its quoting when the split is read.",
            detail={"name": schema, role: value},
        )


def read_schema_split(entry: Any) -> dict[str, Any]:
    """Parse ONE `Split by declared schema` entry, or refuse it.

    One reader for the shape, used by the validator, by the row-level split and
    by the two-dialect compiler. A second reader is a second opinion about what
    a stored declaration means, and a stored declaration is immutable: the two
    would disagree about rows nobody can re-derive.
    """
    if not isinstance(entry, dict):
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_NAME_INVALID, "A declared schema set must be an object."
        )

    name = str(entry.get("name") or "")
    if not _SCHEMA_SET_NAME.fullmatch(name):
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_NAME_INVALID,
            "A declared schema set needs a lower-case name: a letter, then letters, "
            "digits or underscores. It becomes a warehouse column prefix.",
            detail={"name": name},
        )

    source = str(entry.get("source") or "")
    separator = entry.get("separator")
    if not isinstance(separator, str) or not separator or len(separator) > MAX_SEPARATOR_LENGTH:
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_SEPARATOR_INVALID,
            "A declared schema set needs a separator of 1 to "
            f"{MAX_SEPARATOR_LENGTH} characters. An empty one would make every "
            "value a single token.",
            detail={"name": name, "separator": separator},
        )

    remainder = str(entry.get("remainder") or "")
    if not remainder:
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_REMAINDER_MISSING,
            "A declared schema set must name the concept that receives what "
            "exceeds the last position of the chosen layout. Truncating it in "
            "silence is how a value disappears without anybody being told.",
            detail={"name": name},
        )
    _refuse_unsafe_emitted_column(remainder, schema=name, role="remainder")

    detection = entry.get("detection")
    detection = detection if isinstance(detection, dict) else {}
    method = str(detection.get("method") or "")
    if method not in DETECTION_METHODS:
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_DETECTION_UNKNOWN,
            f"Unknown detection method {method!r}. One of {DETECTION_METHODS}.",
            detail={"name": name, "method": method},
        )

    tokens = [value for value in (detection.get("anchor_tokens") or [])]
    anchors = tuple(str(value) for value in tokens)
    if not anchors or len(anchors) > MAX_ANCHOR_TOKENS:
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_ANCHOR_TOKEN_INVALID,
            f"A detector needs 1 to {MAX_ANCHOR_TOKENS} anchor tokens.",
            detail={"name": name, "anchor_tokens": list(anchors)},
        )
    if len(set(anchors)) != len(anchors):
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_ANCHOR_TOKEN_INVALID,
            "One anchor token is declared twice.",
            detail={"name": name, "anchor_tokens": list(anchors)},
        )
    for token in anchors:
        if not token or separator in token:
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_ANCHOR_TOKEN_INVALID,
                "An anchor token must be non-empty and must not contain the "
                "separator: a value carrying the separator is never ONE token, "
                "so it could never be found at an offset.",
                detail={"name": name, "anchor_token": token, "separator": separator},
            )

    declared_layouts = list(entry.get("layouts") or [])
    if not declared_layouts:
        raise ColumnTreatmentError(
            REFUSE_SCHEMA_NEEDS_A_LAYOUT,
            "A declared schema set must carry at least one positional layout.",
            detail={"name": name},
        )

    layouts: list[dict[str, Any]] = []
    layout_names: dict[str, int] = {}
    layout_by_offset: dict[int, str] = {}
    for index, declared in enumerate(declared_layouts):
        declared = declared if isinstance(declared, dict) else {}
        layout_name = str(declared.get("name") or "")
        if not layout_name or len(layout_name) > MAX_LAYOUT_NAME_LENGTH:
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_LAYOUT_NAME_CLAIMED_TWICE,
                "Every layout needs a name of 1 to "
                f"{MAX_LAYOUT_NAME_LENGTH} characters: it is the value the "
                "`layout` column carries on every row that took it.",
                detail={"name": name, "position": index},
            )
        if layout_name in layout_names:
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_LAYOUT_NAME_CLAIMED_TWICE,
                "Two layouts carry the same name, so no row could say which one "
                "produced its values.",
                detail={"name": name, "layout": layout_name},
            )
        layout_names[layout_name] = index

        targets = [str(value) for value in (declared.get("targets") or [])]
        if not targets or len(targets) > MAX_LAYOUT_POSITIONS:
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_LAYOUT_EMPTY,
                "A layout must name 1 to "
                f"{MAX_LAYOUT_POSITIONS} ordered target concepts. An empty layout "
                "describes nothing and would still be detectable.",
                detail={"name": name, "layout": layout_name, "targets": targets},
            )
        seen: set[str] = set()
        for target in targets:
            if not target:
                raise ColumnTreatmentError(
                    REFUSE_SCHEMA_LAYOUT_EMPTY,
                    "A layout position must name the concept it feeds.",
                    detail={"name": name, "layout": layout_name},
                )
            _refuse_unsafe_emitted_column(target, schema=name, role="target")
            if target in seen:
                raise ColumnTreatmentError(
                    REFUSE_TARGET_CLAIMED_TWICE,
                    "One layout feeds the same concept from two positions, and "
                    "neither position can win in silence.",
                    detail={"name": name, "layout": layout_name, "target": target},
                )
            seen.add(target)

        offset = declared.get("anchor_offset")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_ANCHOR_OFFSET_INVALID,
                "A layout is chosen by the offset its anchor token sits at, and "
                "that offset must be declared as a whole number.",
                detail={"name": name, "layout": layout_name, "anchor_offset": offset},
            )
        if not 0 <= offset <= MAX_ANCHOR_OFFSET or offset >= len(targets):
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_ANCHOR_OFFSET_INVALID,
                "The anchor offset must be one of the layout's own positions, "
                f"between 0 and min({MAX_ANCHOR_OFFSET}, positions - 1). An anchor "
                "outside the layout keys the detection on something the layout "
                "does not describe.",
                detail={
                    "name": name,
                    "layout": layout_name,
                    "anchor_offset": offset,
                    "positions": len(targets),
                },
            )
        if offset in layout_by_offset:
            raise ColumnTreatmentError(
                REFUSE_SCHEMA_ANCHOR_AMBIGUOUS,
                "Two layouts claim the same anchor offset, so the minimum offset "
                "designates both and the row has no layout it can name.",
                detail={
                    "name": name,
                    "anchor_offset": offset,
                    "layouts": [layout_by_offset[offset], layout_name],
                },
            )
        layout_by_offset[offset] = layout_name
        layouts.append(
            {"name": layout_name, "anchor_offset": offset, "targets": tuple(targets)}
        )

    return {
        "name": name,
        "source": source,
        "separator": separator,
        "remainder": remainder,
        "method": method,
        "anchor_tokens": anchors,
        "layouts": tuple(layouts),
        "layout_by_offset": dict(layout_by_offset),
        # How far the detector has to look to find the SMALLEST anchor offset:
        # past the largest declared one, no offset could designate a layout.
        "scan_limit": max(layout_by_offset),
    }


def provenance_columns(parsed: dict[str, Any]) -> dict[str, str]:
    """The three columns every schema split emits, named from the set's own name.

    Derived, never declared: a name that can be computed from the declaration is
    a second place for the declaration to be contradicted.
    """
    name = str(parsed.get("name") or "")
    return {
        "layout": f"{name}_layout",
        "token_count": f"{name}_token_count",
        "token_count_matches_schema": f"{name}_token_count_matches_schema",
    }


def schema_split_targets(parsed: dict[str, Any]) -> list[str]:
    """The union of every layout's targets, in declaration order, each once.

    Two layouts naming one concept at two positions is the NORMAL case -- they
    are alternatives for the same row, never rivals -- so the union is one
    column whose value depends on the layout the row took.
    """
    union: list[str] = []
    for layout in parsed.get("layouts") or ():
        for target in layout.get("targets") or ():
            if target not in union:
                union.append(target)
    return union


def schema_split_columns(parsed: dict[str, Any]) -> list[dict[str, str]]:
    """Every column ONE schema set produces, provenance first.

    The order is the order the compiler emits them in, so a person reading the
    warehouse and a person reading the declaration see the same list.
    """
    provenance = provenance_columns(parsed)
    produced = [
        {"target": provenance["layout"], "kind": "schema_split_provenance"},
        {"target": provenance["token_count"], "kind": "schema_split_provenance"},
        {
            "target": provenance["token_count_matches_schema"],
            "kind": "schema_split_provenance",
        },
        {"target": str(parsed.get("remainder") or ""), "kind": "schema_split"},
    ]
    produced.extend(
        {"target": target, "kind": "schema_split"} for target in schema_split_targets(parsed)
    )
    return produced


def split_row_by_declared_schema(parsed: dict[str, Any], value: Any) -> dict[str, Any]:
    """Split ONE value, from the LEFT, under the layout its own tokens designate.

    The answer always carries `token_count`, detected or not: a row no layout
    claims is the inventory of what the schema set does not describe yet, and a
    row without a count could not be listed against the ones it does describe.

    `layout` is `None` when no anchor token sits at an offset a layout declares.
    Nothing is guessed there -- `values` stays empty and the value is returned
    whole in `undetected_value`, so the column is still readable as it was
    collected.
    """
    separator = str(parsed["separator"])
    if not isinstance(value, str):
        return {
            "layout": None,
            "token_count": None,
            "token_count_matches_schema": None,
            "values": {},
            "remainder": None,
            "undetected_value": value,
        }

    tokens = value.split(separator)
    anchors = set(parsed["anchor_tokens"])
    layout_by_offset: dict[int, str] = parsed["layout_by_offset"]

    # MIN(offset) selects the layout -- and the minimum is taken over EVERY
    # offset an anchor sits at, not only over the declared ones. An anchor
    # closer to the left than any layout declares means the value is shaped
    # like nothing that was declared, and that is an undetected row, not the
    # next-best layout.
    first_anchor = next(
        (index for index, token in enumerate(tokens) if token in anchors), None
    )
    layout_name = layout_by_offset.get(first_anchor) if first_anchor is not None else None
    if layout_name is None:
        return {
            "layout": None,
            "token_count": len(tokens),
            "token_count_matches_schema": None,
            "values": {},
            "remainder": None,
            "undetected_value": value,
        }

    layout = next(
        entry for entry in parsed["layouts"] if entry["name"] == layout_name
    )
    targets: tuple[str, ...] = layout["targets"]
    values = {
        target: (tokens[position] if position < len(tokens) else None)
        for position, target in enumerate(targets)
    }
    overflow = tokens[len(targets):]
    return {
        "layout": layout_name,
        "token_count": len(tokens),
        "token_count_matches_schema": len(tokens) == len(targets),
        "values": values,
        "remainder": separator.join(overflow) if overflow else None,
        "undetected_value": None,
    }


def _field_ids(mapping_payload: dict[str, Any]) -> list[str]:
    return [
        str(field.get("field_id"))
        for field in (mapping_payload.get("fields") or [])
        if isinstance(field, dict) and field.get("field_id")
    ]


def _declared_targets(mapping_payload: dict[str, Any]) -> dict[str, str]:
    """Canonical targets already claimed by a binding: target -> claiming field."""
    claimed: dict[str, str] = {}
    for field in mapping_payload.get("fields") or []:
        if not isinstance(field, dict) or is_excluded(field):
            continue
        binding = field.get("binding")
        target = (
            str((binding or {}).get("canonical_target") or "")
            if isinstance(binding, dict)
            else ""
        )
        if target and target not in claimed:
            claimed[target] = str(field.get("field_id") or "")
    return claimed


def normalize_treatments(mapping_payload: dict[str, Any]) -> dict[str, list[Any]]:
    """Validate the join/split declaration of one mapping payload, or refuse it.

    Called before a mapping version is appended, which is what makes a target
    claimed twice a refusal BEFORE the import rather than a
    ``dispatch_mapping_collision`` discovered while landing rows
    (``csv_excel_import.py``, ``dispatch_mapping_collision``).

    Returns the declaration unchanged when it holds. Raises
    ``ColumnTreatmentError`` otherwise -- it never repairs, never drops an entry
    and never picks a winner between two claims.
    """
    declaration = mapping_payload.get("column_treatments")
    if declaration is None:
        return {"joins": [], "splits": [], "schema_splits": []}
    if not isinstance(declaration, dict):
        raise ColumnTreatmentError(
            REFUSE_SOURCE_UNKNOWN, "The column treatment declaration must be an object."
        )

    known = set(_field_ids(mapping_payload))
    excluded = {
        str(field.get("field_id"))
        for field in (mapping_payload.get("fields") or [])
        if isinstance(field, dict) and is_excluded(field)
    }
    claimed_targets = _declared_targets(mapping_payload)
    claimed_sources: dict[str, str] = {}

    joins = list(declaration.get("joins") or [])
    splits = list(declaration.get("splits") or [])
    schema_splits = list(declaration.get("schema_splits") or [])

    for join in joins:
        target = str((join or {}).get("target") or "")
        sources = [str(value) for value in ((join or {}).get("sources") or [])]
        if len(set(sources)) < 2:
            raise ColumnTreatmentError(
                REFUSE_JOIN_NEEDS_TWO_SOURCES,
                "A join must name at least two distinct source columns.",
                detail={"target": target, "sources": sources},
            )
        _claim_target(claimed_targets, target, f"join:{target}")
        for source in sources:
            _check_source(source, known, excluded)
            _claim_source(claimed_sources, source, f"join:{target}")

    for split in splits:
        source = str((split or {}).get("source") or "")
        pattern = str((split or {}).get("pattern") or "")
        targets = list((split or {}).get("targets") or [])
        if len({str((entry or {}).get("target") or "") for entry in targets}) < 2:
            raise ColumnTreatmentError(
                REFUSE_SPLIT_NEEDS_TWO_TARGETS,
                "A split must name at least two distinct target concepts in ONE entry.",
                detail={"source": source, "targets": targets},
            )
        _check_source(source, known, excluded)
        _claim_source(claimed_sources, source, f"split:{source}")
        compiled = _compile_pattern(pattern, source)
        for entry in targets:
            group = str((entry or {}).get("group") or "")
            target = str((entry or {}).get("target") or "")
            if group not in compiled.groupindex:
                raise ColumnTreatmentError(
                    REFUSE_SPLIT_GROUP_MISSING,
                    "A split target must name a group the pattern actually captures.",
                    detail={"source": source, "group": group,
                            "captured": sorted(compiled.groupindex)},
                )
            _claim_target(claimed_targets, target, f"split:{source}")

    # Story 70.1. A schema set claims its source once, and claims every concept
    # it can produce -- the union of its layouts, its declared remainder and its
    # three provenance columns. The provenance columns are claimed like any
    # other: a binding that also wrote `<set>_layout` would leave two writers on
    # one column, and the row could no longer say which one answered.
    for schema_split in schema_splits:
        parsed = read_schema_split(schema_split)
        source = parsed["source"]
        _check_source(source, known, excluded)
        _claim_source(claimed_sources, source, f"schema_split:{parsed['name']}")
        for column in schema_split_columns(parsed):
            _claim_target(
                claimed_targets, column["target"], f"schema_split:{parsed['name']}"
            )

    return {"joins": joins, "splits": splits, "schema_splits": schema_splits}


def _compile_pattern(pattern: str, source: str) -> re.Pattern[str]:
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        raise ColumnTreatmentError(
            REFUSE_SPLIT_PATTERN_INVALID,
            f"A split pattern must be non-empty and at most {MAX_PATTERN_LENGTH} characters.",
            detail={"source": source, "length": len(pattern)},
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ColumnTreatmentError(
            REFUSE_SPLIT_PATTERN_INVALID,
            "The split pattern is not a valid regular expression.",
            detail={"source": source, "reason": str(exc)},
        ) from exc


def _check_source(source: str, known: set[str], excluded: set[str]) -> None:
    if source not in known:
        raise ColumnTreatmentError(
            REFUSE_SOURCE_UNKNOWN,
            "A treatment names a source column this mapping does not carry.",
            detail={"source": source},
        )
    if source in excluded:
        raise ColumnTreatmentError(
            REFUSE_SOURCE_EXCLUDED,
            "An excluded column cannot feed a join or a split: it never lands.",
            detail={"source": source},
        )


def _claim_source(claims: dict[str, str], source: str, claimer: str) -> None:
    if source in claims:
        raise ColumnTreatmentError(
            REFUSE_SOURCE_CLAIMED_TWICE,
            "One source column is claimed by two treatments.",
            detail={"source": source, "claimed_by": [claims[source], claimer]},
        )
    claims[source] = claimer


def _claim_target(claims: dict[str, str], target: str, claimer: str) -> None:
    if not target:
        raise ColumnTreatmentError(
            REFUSE_TARGET_CLAIMED_TWICE,
            "A treatment must name the concept it produces.",
            detail={"claimed_by": claimer},
        )
    if target in claims:
        raise ColumnTreatmentError(
            REFUSE_TARGET_CLAIMED_TWICE,
            "Two rules claim the same concept, and neither can win in silence.",
            detail={"target": target, "claimed_by": [claims[target], claimer]},
        )
    claims[target] = claimer


def produced_columns(mapping_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every column the treatments PRODUCE, with the sources each one comes from.

    This is what makes a joined column accountable downstream: the projection
    carries it, so a reader of the warehouse can say a value came from three
    named columns instead of guessing. A split produces N columns, all from the
    same one source, and each names its capture group.
    """
    declaration = mapping_payload.get("column_treatments") or {}
    produced: list[dict[str, Any]] = []
    for join in declaration.get("joins") or []:
        produced.append(
            {
                "target": str((join or {}).get("target") or ""),
                "kind": "join",
                "sources": [str(value) for value in ((join or {}).get("sources") or [])],
            }
        )
    for split in declaration.get("splits") or []:
        source = str((split or {}).get("source") or "")
        for entry in (split or {}).get("targets") or []:
            produced.append(
                {
                    "target": str((entry or {}).get("target") or ""),
                    "kind": "split",
                    "sources": [source],
                }
            )
    # Story 70.1. A schema set produces its targets AND its provenance, and the
    # provenance is listed here for the same reason the rest is: a reader of the
    # warehouse must be able to say `<set>_layout` came from one named column
    # rather than from somewhere.
    for schema_split in declaration.get("schema_splits") or []:
        parsed = read_schema_split(schema_split)
        produced.extend(
            {**column, "sources": [parsed["source"]]}
            for column in schema_split_columns(parsed)
        )
    produced.sort(key=lambda item: (item["target"], item["kind"]))
    return produced


def describe_columns(
    mapping_payload: dict[str, Any],
    *,
    resolved_by_list: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """One row per source column of the mapping, the excluded ones included.

    The order is the payload's own order -- the order of the file's headers --
    because a person reads their spreadsheet left to right, and re-sorting would
    make the screen and the file disagree about which column is which.

    ``resolved_by_list`` is the set of source fields a value mapping table is
    assigned to (``app.value_mapping_assignments.source_field``, story 60.1). It
    is passed in rather than read here so this module stays pure; an empty set
    means no table is assigned, never that the store was unreadable.
    """
    declaration = mapping_payload.get("column_treatments") or {}
    join_of: dict[str, str] = {}
    join_partners: dict[str, list[str]] = {}
    for join in declaration.get("joins") or []:
        target = str((join or {}).get("target") or "")
        sources = [str(value) for value in ((join or {}).get("sources") or [])]
        for source in sources:
            join_of[source] = target
            join_partners[source] = sources
    split_of: dict[str, list[str]] = {}
    for split in declaration.get("splits") or []:
        source = str((split or {}).get("source") or "")
        split_of[source] = [
            str((entry or {}).get("target") or "")
            for entry in ((split or {}).get("targets") or [])
        ]
    schema_split_of = _schema_split_of(declaration)

    rows: list[dict[str, Any]] = []
    for field in mapping_payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("field_id") or "")
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        canonical = binding.get("canonical_target")
        rows.append(
            {
                "field_id": field_id,
                "treatment": _treatment_word(
                    field, field_id, join_of, split_of, schema_split_of, resolved_by_list
                ),
                "canonical_target": canonical if isinstance(canonical, str) else None,
                "mdm_target": binding.get("mdm_target"),
                "binding_status": str(binding.get("status") or "") or None,
                "confidence": _confidence(field),
                "sample_value": sample_value(field.get("profile")),
                # What this column contributes to, and who it contributes WITH.
                # A joined source keeps its own row and names its partners: the
                # inventory of 46 columns stays 46 rows.
                "contributes_to": (
                    [join_of[field_id]] if field_id in join_of
                    else list(
                        split_of.get(field_id) or schema_split_of.get(field_id) or []
                    )
                ),
                "joined_with": [
                    partner
                    for partner in join_partners.get(field_id, [])
                    if partner != field_id
                ],
            }
        )
    return rows


def _schema_split_of(declaration: dict[str, Any]) -> dict[str, list[str]]:
    """Source column -> every column its declared schema set produces.

    Read through :func:`read_schema_split` rather than by digging into the dict,
    so a screen and the store never disagree about what a set produces.
    """
    produced: dict[str, list[str]] = {}
    for schema_split in declaration.get("schema_splits") or []:
        try:
            parsed = read_schema_split(schema_split)
        except ColumnTreatmentError:
            # A declaration that would be refused at the append is not rendered
            # as a treatment here either. It keeps its honest `Not decided`
            # rather than a word claiming a derivation nobody could run.
            continue
        produced[parsed["source"]] = [
            column["target"] for column in schema_split_columns(parsed)
        ]
    return produced


def _treatment_word(
    field: dict[str, Any],
    field_id: str,
    join_of: dict[str, str],
    split_of: dict[str, list[str]],
    schema_split_of: dict[str, list[str]],
    resolved_by_list: frozenset[str] | set[str],
) -> str:
    """The one word, decided in the order a person would decide it.

    Exclusion first: an excluded column is excluded whatever else was said about
    it. Then the two declared verbs. Then the value mapping table, which
    translates a column's VALUES without changing which concept it feeds. Then
    the plain binding -- and, last, the honest absence.
    """
    if is_excluded(field):
        return EXCLUDED
    if field_id in join_of:
        return JOINED
    if field_id in split_of:
        return split_into(len(split_of[field_id]))
    if field_id in schema_split_of:
        return split_by_declared_schema()
    if field_id in resolved_by_list:
        return RESOLVED_BY_LIST
    binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
    if binding.get("canonical_target") or binding.get("mdm_target"):
        return DIRECT
    return NOT_DECIDED


def _confidence(field: dict[str, Any]) -> float | None:
    profile = field.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("confidence"), (int, float)):
        return float(profile["confidence"])
    return None


def describe_recognized_columns(
    recognized_fields: list[dict[str, Any]],
    *,
    sample_rows: list[Any] | None = None,
    mapping_payload: dict[str, Any] | None = None,
    resolved_by_list: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The same two readings for the FILE preview, from the sample already parsed.

    ``recognize_columns`` returns one entry per source header with its status
    (``matched`` / ``ambiguous`` / ``unmatched``) and its confidence, and those
    three stay the source of the treatment word -- no second scorer. What it does
    not return is the example value, although it RECEIVES the sample rows
    (``csv_excel_import.build_file_source_preview``): this reads them, and reads
    nothing else.
    """
    declaration = (mapping_payload or {}).get("column_treatments") or {}
    join_of = {
        str(source): str((join or {}).get("target") or "")
        for join in (declaration.get("joins") or [])
        for source in ((join or {}).get("sources") or [])
    }
    split_of = {
        str((split or {}).get("source") or ""): [
            str((entry or {}).get("target") or "")
            for entry in ((split or {}).get("targets") or [])
        ]
        for split in (declaration.get("splits") or [])
    }
    schema_split_of = _schema_split_of(declaration)
    excluded = {
        str(field.get("field_id"))
        for field in ((mapping_payload or {}).get("fields") or [])
        if isinstance(field, dict) and is_excluded(field)
    }

    described: list[dict[str, Any]] = []
    for entry in recognized_fields:
        if not isinstance(entry, dict):
            continue
        column = str(entry.get("source_column") or "")
        described.append(
            {
                **entry,
                "sample_value": _sample_from_rows(column, sample_rows),
                "treatment": _recognized_treatment(
                    column,
                    entry,
                    join_of,
                    split_of,
                    schema_split_of,
                    excluded,
                    resolved_by_list,
                ),
            }
        )
    return described


def _recognized_treatment(
    column: str,
    entry: dict[str, Any],
    join_of: dict[str, str],
    split_of: dict[str, list[str]],
    schema_split_of: dict[str, list[str]],
    excluded: set[str],
    resolved_by_list: frozenset[str] | set[str],
) -> str:
    if column in excluded:
        return EXCLUDED
    if column in join_of:
        return JOINED
    if column in split_of:
        return split_into(len(split_of[column]))
    if column in schema_split_of:
        return split_by_declared_schema()
    if column in resolved_by_list:
        return RESOLVED_BY_LIST
    # `unmatched` is an EXTRA column the recognizer refuses to force-map
    # (`file_source_recognizer.py`, "never force-mapped"), and `ambiguous` is a
    # choice nobody has made. Neither is a treatment; both are the inventory of
    # what is left to decide, and calling either one `Direct` would claim a
    # binding that does not exist.
    if str(entry.get("status") or "") == "matched" and entry.get("canonical_target"):
        return DIRECT
    return NOT_DECIDED


def _sample_from_rows(column: str, sample_rows: list[Any] | None) -> str:
    if sample_rows is None:
        return UNKNOWN_SAMPLE
    for row in sample_rows:
        if not isinstance(row, dict) or column not in row:
            continue
        value = row.get(column)
        if value is None:
            continue
        rendered = value if isinstance(value, str) else str(value)
        if rendered.strip():
            return rendered
    return NO_SAMPLE_VALUE
