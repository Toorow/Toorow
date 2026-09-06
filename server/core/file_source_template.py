"""toorow -- File-source template artifact: create + version (Story 22.11, AD-2).

The ONE immutable, content-hashed, versioned template artifact for the
file-source ingestion framework. A file-source template declares the canonical
TARGET a later import maps onto -- required canonical fields (ids from
``app.mdm_canonical_fields``), grain, matrix class (planned/actual/extrapolated),
and placement coordinates -- independent of any file's shifting layout.

Two kinds share ONE artifact + ONE producer interface (AD-2):
  * ``catalog``    -- a declarative JSONB contract (this story's focus);
  * ``adaptation`` -- a locked ``.py`` + its content-hash (Phase B-3).

Every create is written through ``operations.execute_operation`` (audit + outbox
+ idempotent replay -- no parallel audit path). Idempotency is two-layered and
consistent:
  * identical contract for the same (project, template_code) -> the SAME version
    is returned (operation replay + the DB UNIQUE(content_hash) dedup);
  * a different contract -> a NEW version row is appended (version = MAX+1);
  * UPDATE/DELETE of identity columns is rejected by the 097 immutability trigger.

Layering (mirrors csv_excel_import): a PURE validation/hash layer (no I/O,
offline-testable) + a DB layer (registry check + create/read, pg-gated). The
API layer maps the typed errors to 4xx.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any

from ulid import ULID

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_FILE_SOURCE_TEMPLATE_CREATED = declare_action("file_source.template.created")


# Closed vocabularies (mirror the 097 CHECK constraints).
TEMPLATE_KINDS = frozenset({"catalog", "adaptation"})
PLACEMENT_CLASSES = frozenset({"planned", "actual", "extrapolated"})

# Variant discriminator sources -- exactly what ``file_source_producer.
# _resolve_discriminator`` knows how to evaluate (Story 22.18).
DISCRIMINATOR_SOURCES = frozenset({"cell", "filename"})

# WHERE a template's rows land (chantier 67-25b, the convergence of the mediaplan
# path onto the one engine). Declared by the template and routed by
# ``import_runner.run_import``; it is NOT a second landing implementation.
#
#   'warehouse_relation' -- the project-scoped raw relation (the default, and
#       what every template built before the convergence keeps).
#   'plan_store'         -- the versioned media-plan object in Postgres
#       (``app.media_plan_versions`` / ``media_plan_lines``) through
#       ``mediaplan_store.create_version_with_lines``. The plan store IS the
#       plan's data model; landing there is what lets the mediaplan profile run
#       on this engine without changing what every plan reader reads.
LANDING_WAREHOUSE_RELATION = "warehouse_relation"
LANDING_PLAN_STORE = "plan_store"
LANDING_TARGETS = frozenset({LANDING_WAREHOUSE_RELATION, LANDING_PLAN_STORE})

# The field vocabulary of a template landing in the PLAN STORE: the attributes of
# a plan line, in the order the store carries them.
#
# It is a closed list that ships WITH the product, so it is code and not a table:
# a plan line is not a warehouse row and its fields are not project-authored
# canonical fields. That is why a plan-store template is validated against this
# tuple instead of `app.mdm_canonical_fields` -- asking the MDM registry to
# vouch for `line_key` would be asking the warehouse vocabulary to describe an
# object the warehouse does not hold.
#
# `import_landing.land_plan_store_rows` maps exactly these onto
# `mediaplan_store.create_version_with_lines`, and imports them FROM HERE so the
# contract that validates a template and the code that lands it cannot drift.
PLAN_LINE_FIELDS = (
    "line_key",
    "label",
    "channel",
    "start_date",
    "end_date",
    "budget",
    "buy_mode",
    "is_plan_only",
)

# The two grains the producer emits (mirrors ``file_source_producer.GRAINS``;
# duplicated as a literal here to keep this pure module import-free of it).
GRAIN_DAILY = "daily"
GRAIN_LINE = "line"

# An A1 cell range, the key a merged-amount explode binds to ("E2:E4").
_A1_RANGE = re.compile(r"^[A-Za-z]{1,3}[0-9]{1,7}:[A-Za-z]{1,3}[0-9]{1,7}$")

# Layout keys the PRODUCER reads back off the persisted contract (Story 22.12):
# ``produce`` / ``read_source_columns`` consult format / sheet_name / date_format
# (and header_row, an int, handled separately) on the simple tabular path.
_LAYOUT_STRING_KEYS = ("format", "sheet_name", "date_format")

ACTION_FILE_SOURCE_TEMPLATE_CREATED = "file_source.template.created"

_TEMPLATE_COLS = [
    "id",
    "project_id",
    "template_code",
    "version",
    "kind",
    "content_hash",
    "idempotency_key_hash",
    "contract",
    "placement_class",
    "grain",
    "created_by",
    "created_at",
    "label",
    "is_active",
]


# ---------------------------------------------------------------------------
# Typed errors (stable code -> HTTP status at the API layer).
# ---------------------------------------------------------------------------


class FileSourceTemplateError(ValueError):
    """Base for file-source template errors (carries a stable code)."""

    code = "file_source_template_error"


class FileSourceTemplateValidationError(FileSourceTemplateError):
    """A caller-supplied value is invalid (maps to 422)."""

    code = "invalid_param"


class UnknownCanonicalField(FileSourceTemplateValidationError):
    """A declared required/optional field id is not an active mdm_canonical_field."""

    code = "unknown_canonical_field"


# ---------------------------------------------------------------------------
# Pure layer: contract validation + content hash (no I/O).
# ---------------------------------------------------------------------------


def _require_str_list(value: Any, *, name: str, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise FileSourceTemplateValidationError(f"'{name}' must be a list of field ids.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise FileSourceTemplateValidationError(
                f"'{name}' must contain non-empty field id strings."
            )
        out.append(item.strip())
    if not allow_empty and not out:
        raise FileSourceTemplateValidationError(f"'{name}' must not be empty.")
    if len(set(out)) != len(out):
        raise FileSourceTemplateValidationError(f"'{name}' contains duplicate ids.")
    return out


def validate_template_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a file-source template contract (pure, no DB).

    Required shape:
      kind            : 'catalog' | 'adaptation'
      required_fields : non-empty list of canonical field ids
      optional_fields : list of canonical field ids (default [])
      grain           : non-empty string (e.g. 'daily', 'broadcast_spot')
      class            : 'planned' | 'actual' | 'extrapolated'  (AD-6: mandatory)
      placement       : { metric: str, period: str, dimension?: [str...] }
    Optional:
      aliases, field_types, reshape (an Excel ReshapeSpec-like dict);
      discriminator (the Story 22.18 variant discriminator);
      format / header_row / sheet_name / date_format (the tabular layout keys);
      and for an 'adaptation' kind, py_content_hash (the locked .py's SHA-256).

    Every key kept here is a key the CONSUMERS read back off the persisted
    contract: ``create_file_source_template`` stores ``json.dumps(normalised)``, so
    a key dropped here is UNREACHABLE on every persisted template. That is how the
    declared discriminator became unreachable in ``stamp_placement`` -- the
    normalised doc is the contract, not a summary of it.

    Normalisation is IDEMPOTENT: re-validating an already-normalised contract
    returns it unchanged, so a persisted contract can always be re-hashed to check
    its own seal (``file_source_producer._assert_template_locked``).

    Returns a normalised copy (sorted lists, canonical placement) so the content
    hash is stable. Raises FileSourceTemplateValidationError (422) on any issue.
    """
    if not isinstance(contract, dict):
        raise FileSourceTemplateValidationError("Contract must be an object.")

    kind = contract.get("kind")
    if kind not in TEMPLATE_KINDS:
        raise FileSourceTemplateValidationError(
            f"'kind' must be one of {sorted(TEMPLATE_KINDS)}; got {kind!r}."
        )

    required_fields = _require_str_list(
        contract.get("required_fields"), name="required_fields", allow_empty=False
    )
    optional_fields = _require_str_list(
        contract.get("optional_fields", []), name="optional_fields", allow_empty=True
    )
    overlap = set(required_fields) & set(optional_fields)
    if overlap:
        raise FileSourceTemplateValidationError(
            f"fields cannot be both required and optional: {sorted(overlap)}."
        )

    grain = contract.get("grain")
    if not isinstance(grain, str) or not grain.strip():
        raise FileSourceTemplateValidationError("'grain' must be a non-empty string.")

    placement_class = contract.get("class")
    if placement_class not in PLACEMENT_CLASSES:
        raise FileSourceTemplateValidationError(
            f"'class' must be one of {sorted(PLACEMENT_CLASSES)} (a template with no "
            f"declared class is refused, AD-6); got {placement_class!r}."
        )

    placement = contract.get("placement")
    if not isinstance(placement, dict):
        raise FileSourceTemplateValidationError(
            "'placement' must be an object with metric + period coordinates."
        )
    metric = placement.get("metric")
    period = placement.get("period")
    if not isinstance(metric, str) or not metric.strip():
        raise FileSourceTemplateValidationError("'placement.metric' must be a non-empty string.")
    if not isinstance(period, str) or not period.strip():
        raise FileSourceTemplateValidationError("'placement.period' must be a non-empty string.")
    dimension = placement.get("dimension", [])
    dimension = _require_str_list(dimension, name="placement.dimension", allow_empty=True)

    normalised: dict[str, Any] = {
        "kind": kind,
        "required_fields": sorted(required_fields),
        "optional_fields": sorted(optional_fields),
        "grain": grain.strip(),
        "class": placement_class,
        "placement": {
            "metric": metric.strip(),
            "period": period.strip(),
            "dimension": sorted(dimension),
        },
    }

    # WHERE the rows land. Declared, never inferred: a template that lands in the
    # plan store and one that lands in the warehouse are two different contracts,
    # and the difference must survive into the content hash.
    landing_target = contract.get("landing_target", LANDING_WAREHOUSE_RELATION)
    if landing_target not in LANDING_TARGETS:
        raise FileSourceTemplateValidationError(
            f"'landing_target' must be one of {sorted(LANDING_TARGETS)}; "
            f"got {landing_target!r}."
        )
    normalised["landing_target"] = landing_target

    # Optional passthrough (kept in the hash so a change versions a new row).
    for opt_key in ("aliases", "field_types", "reshape"):
        if contract.get(opt_key) is not None:
            if not isinstance(contract[opt_key], dict):
                raise FileSourceTemplateValidationError(f"'{opt_key}' must be an object.")
            normalised[opt_key] = contract[opt_key]

    if "reshape" in normalised:
        _validate_reshape_capabilities(normalised["reshape"])

    # A plan-store landing carries PLAN LINES. Landing daily rows into a store
    # whose model is the line would silently change the plan's data model, and
    # the plan spreads to daily itself at publish -- so the two would disagree.
    if landing_target == LANDING_PLAN_STORE:
        reshape_grain = (normalised.get("reshape") or {}).get("grain")
        if normalised["grain"] != GRAIN_LINE or reshape_grain != GRAIN_LINE:
            raise FileSourceTemplateValidationError(
                f"a '{LANDING_PLAN_STORE}' landing target requires "
                f"grain '{GRAIN_LINE}' (declared both on the contract and on its "
                "'reshape' sub-document): the plan store's model is the plan LINE, "
                "and it spreads to daily itself at publish."
            )

    # Tabular layout keys. Dropping them made every persisted template fall back to
    # header row 1 + format sniffing, whatever it declared.
    for layout_key in _LAYOUT_STRING_KEYS:
        value = contract.get(layout_key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise FileSourceTemplateValidationError(
                    f"'{layout_key}' must be a non-empty string."
                )
            normalised[layout_key] = value.strip()

    header_row = contract.get("header_row")
    if header_row is not None:
        if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 1:
            raise FileSourceTemplateValidationError(
                "'header_row' must be a 1-based positive integer."
            )
        normalised["header_row"] = header_row

    # The variant discriminator (Story 22.18). ``stamp_placement`` and
    # ``evaluate_variant_discriminator`` read it off the PERSISTED contract, so it
    # must survive normalisation -- and be one the resolver can actually evaluate.
    discriminator = contract.get("discriminator")
    if discriminator is not None:
        normalised["discriminator"] = _validate_discriminator(discriminator)

    if kind == "adaptation":
        py_hash = contract.get("py_content_hash")
        if not isinstance(py_hash, str) or not _is_sha256(py_hash):
            raise FileSourceTemplateValidationError(
                "an 'adaptation' template requires 'py_content_hash' "
                "(the locked .py SHA-256)."
            )
        normalised["py_content_hash"] = py_hash
        # The locked .py source is stored so the adaptation replays deterministically
        # (Story 22.24). When present, its SHA-256 MUST equal py_content_hash.
        py_source = contract.get("py_source")
        if py_source is not None:
            if not isinstance(py_source, str) or not py_source.strip():
                raise FileSourceTemplateValidationError("'py_source' must be a non-empty string.")
            if hashlib.sha256(py_source.encode("utf-8")).hexdigest() != py_hash:
                raise FileSourceTemplateValidationError(
                    "'py_content_hash' does not match the SHA-256 of 'py_source'."
                )
            normalised["py_source"] = py_source

    return normalised


def _validate_reshape_capabilities(reshape: Any) -> None:
    """Validate the plan-line capabilities declared on a ``reshape`` sub-document.

    Checked against what ``file_source_plan_lines`` can actually execute. These
    were named in `file-source-ingestion.md` as the capabilities the engine had to
    declare before the mediaplan path could converge onto it; a key the executor
    cannot read is a key nobody can rely on, which is how the discriminator became
    unreachable once already.
    """
    if not isinstance(reshape, dict):
        return

    grain = reshape.get("grain")
    if grain is not None and grain not in (GRAIN_DAILY, GRAIN_LINE):
        raise FileSourceTemplateValidationError(
            f"'reshape.grain' must be '{GRAIN_DAILY}' or '{GRAIN_LINE}'; got {grain!r}."
        )

    sheets = reshape.get("sheets")
    if sheets is not None:
        if not isinstance(sheets, list) or not sheets:
            raise FileSourceTemplateValidationError(
                "'reshape.sheets' must be a non-empty list of sheet names."
            )
        seen: set[str] = set()
        for name in sheets:
            if not isinstance(name, str) or not name.strip():
                raise FileSourceTemplateValidationError(
                    "'reshape.sheets' must contain non-empty sheet names."
                )
            if name in seen:
                raise FileSourceTemplateValidationError(
                    f"'reshape.sheets' names sheet {name!r} twice; one import "
                    "walks each sheet once."
                )
            seen.add(name)
        if grain != GRAIN_LINE:
            raise FileSourceTemplateValidationError(
                f"'reshape.sheets' requires grain '{GRAIN_LINE}': the daily grain "
                "reshapes a single sheet, so several sheets would need several "
                "imports and no single one would see the whole workbook."
            )

    line_key = reshape.get("line_key")
    if line_key is not None and (not isinstance(line_key, str) or not line_key.strip()):
        raise FileSourceTemplateValidationError(
            "'reshape.line_key' must be 'auto' or a source column name."
        )

    merged = reshape.get("merged_amount")
    if merged is None:
        return
    if not isinstance(merged, dict):
        raise FileSourceTemplateValidationError(
            "'reshape.merged_amount' must be an object "
            "({'explode': {<A1 range>: [weights]}})."
        )
    explode = merged.get("explode")
    if explode is None:
        return
    if not isinstance(explode, dict):
        raise FileSourceTemplateValidationError(
            "'reshape.merged_amount.explode' must be an object "
            "{<A1 range>: [weights]}, keyed by the merge's PHYSICAL range."
        )
    for range_id, weights in explode.items():
        if not isinstance(range_id, str) or not _A1_RANGE.match(range_id.strip()):
            raise FileSourceTemplateValidationError(
                f"'reshape.merged_amount.explode' key {range_id!r} is not an A1 "
                "range (e.g. 'E2:E4'); the split binds to the merge's PHYSICAL "
                "coordinates so a shifted merge stops applying."
            )
        if not isinstance(weights, list) or not weights:
            raise FileSourceTemplateValidationError(
                f"'reshape.merged_amount.explode[{range_id}]' must be a non-empty "
                "list of weights."
            )
        for w in weights:
            if isinstance(w, bool) or not isinstance(w, (int, float, str)):
                raise FileSourceTemplateValidationError(
                    f"'reshape.merged_amount.explode[{range_id}]': weight {w!r} is "
                    "not a number."
                )
            try:
                weight = Decimal(str(w))
            except (ArithmeticError, ValueError):
                raise FileSourceTemplateValidationError(
                    f"'reshape.merged_amount.explode[{range_id}]': weight {w!r} is "
                    "not a number."
                ) from None
            if not weight.is_finite() or weight <= 0:
                raise FileSourceTemplateValidationError(
                    f"'reshape.merged_amount.explode[{range_id}]': invalid "
                    f"distribution (negative or zero weight {w!r})."
                )


def _validate_discriminator(discriminator: Any) -> dict[str, Any]:
    """Validate + normalise a variant discriminator (Story 22.18).

    Checked against what ``file_source_producer._resolve_discriminator`` can
    actually evaluate: a discriminator with an unknown ``source``, a missing
    ``cell``, or an uncompilable ``pattern`` resolves to None at landing time --
    i.e. a NULL or mislabeled market dimension on every row, silently. Fail closed
    at creation instead (same spirit as the AD-6 no-class refusal).
    """
    if not isinstance(discriminator, dict):
        raise FileSourceTemplateValidationError("'discriminator' must be an object.")

    dimension = discriminator.get("dimension")
    if not isinstance(dimension, str) or not dimension.strip():
        raise FileSourceTemplateValidationError(
            "'discriminator.dimension' must be a non-empty canonical dimension id."
        )
    source = discriminator.get("source")
    if source not in DISCRIMINATOR_SOURCES:
        raise FileSourceTemplateValidationError(
            f"'discriminator.source' must be one of {sorted(DISCRIMINATOR_SOURCES)}; "
            f"got {source!r}."
        )

    out: dict[str, Any] = {"dimension": dimension.strip(), "source": source}
    if source == "cell":
        cell = discriminator.get("cell")
        if not isinstance(cell, str) or not cell.strip():
            raise FileSourceTemplateValidationError(
                "a 'cell' discriminator requires 'cell' (the metadata block label to read)."
            )
        out["cell"] = cell.strip()
    else:
        pattern = discriminator.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise FileSourceTemplateValidationError(
                "a 'filename' discriminator requires 'pattern' (a regular expression)."
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise FileSourceTemplateValidationError(
                f"'discriminator.pattern' is not a valid regular expression: {exc}."
            ) from exc
        out["pattern"] = pattern
    return out


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def compute_content_hash(normalised_contract: dict[str, Any]) -> str:
    """SHA-256 of the canonical (sorted-key) normalised contract JSON (64 hex)."""
    canonical = json.dumps(normalised_contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_template_code(template_code: str) -> str:
    code = (template_code or "").strip()
    if not code or len(code) > 128 or not code[0].isalnum():
        raise FileSourceTemplateValidationError(
            "'template_code' must be 1..128 chars, start alphanumeric "
            "(letters/digits/_/- allowed)."
        )
    if not all(c.isalnum() or c in "_-" for c in code):
        raise FileSourceTemplateValidationError(
            "'template_code' may only contain letters, digits, '_' and '-'."
        )
    return code


def default_idempotency_key(project_id: str, template_code: str, content_hash: str) -> str:
    """Deterministic idempotency key: identical (scope + content) replays cleanly."""
    return f"file_source_template:{project_id}:{template_code}:{content_hash}"


# ---------------------------------------------------------------------------
# DB layer: registry check + read helpers.
# ---------------------------------------------------------------------------


def _assert_plan_line_fields(field_ids: list[str]) -> None:
    """A plan-store template declares PLAN LINE attributes, and only those.

    The vocabulary ships with the product (see ``PLAN_LINE_FIELDS``), so this is
    a pure check with no registry to consult: a plan line is not a warehouse row.
    Fails closed, naming both the offending ids and the vocabulary that was
    available -- a refusal that does not say what WAS allowed sends the author
    guessing.
    """
    unknown = [fid for fid in field_ids if fid not in PLAN_LINE_FIELDS]
    if unknown:
        raise UnknownCanonicalField(
            "a template landing in the plan store may only declare plan line "
            "fields; these are not plan line fields: "
            + ", ".join(sorted(unknown))
            + ". Available: "
            + ", ".join(PLAN_LINE_FIELDS)
            + "."
        )


def _assert_required_fields_registered(
    conn, *, project_id: str, field_ids: list[str]
) -> None:
    """Every declared field id must be an ACTIVE mdm_canonical_field in scope.

    Scope = the project's own canonical fields OR the platform-canonical rows
    (project_id IS NULL), per AD-5. Fails closed with the offending ids listed.
    """
    if not field_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM app.mdm_canonical_fields
            WHERE id = ANY(%s) AND status = 'active'
              AND (project_id = %s OR project_id IS NULL)
            """,
            (list(field_ids), project_id),
        )
        found = {row[0] for row in cur.fetchall()}
    unknown = [fid for fid in field_ids if fid not in found]
    if unknown:
        raise UnknownCanonicalField(
            "these field ids are not active canonical fields in scope: "
            + ", ".join(sorted(unknown))
        )


def list_canonical_fields(conn, *, project_id: str) -> list[dict[str, Any]]:
    """The canonical fields a template may declare, for this project.

    THE EXACT MIRROR of `_assert_required_fields_registered` above -- same table,
    same status filter, same AD-5 scope (the project's own rows OR the
    platform-canonical rows where `project_id IS NULL`). Written as its mirror on
    purpose: a catalog that offered a field the validator then refuses would send
    a person to author a template that cannot be created, and a catalog that
    hides a field the validator accepts makes part of the vocabulary invisible.
    Both are the same bug, in opposite directions.

    WHY IT DID NOT EXIST. Measured 2026-08-01: `app.mdm_canonical_fields` was
    only ever QUERIED TO VALIDATE ids -- `_assert_required_fields_registered`
    here and one lookup in `datastream_field_mapping.py`. Nothing anywhere listed
    it. So a person could be told an id was wrong, and had no way to discover
    which ids were right.

    Ordered by concept kind then id so a metric and a dimension never interleave
    in a picker: choosing the period field out of a list of metrics is the
    mistake this ordering removes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, concept_kind, canonical_name, unit, aggregation, description
            FROM app.mdm_canonical_fields
            WHERE status = 'active' AND (project_id = %s OR project_id IS NULL)
            ORDER BY concept_kind, id
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "concept_kind": row[1],
            "canonical_name": row[2],
            "unit": row[3],
            "aggregation": row[4],
            "description": row[5],
        }
        for row in rows
    ]


def _row_to_template(row: tuple[Any, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, val in zip(_TEMPLATE_COLS, row):
        if col == "created_at" and val is not None:
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


def _select_by_content(conn, *, project_id: str, template_code: str, content_hash: str):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TEMPLATE_COLS)}
            FROM app.file_source_templates
            WHERE project_id = %s AND template_code = %s AND content_hash = %s
            """,
            (project_id, template_code, content_hash),
        )
        row = cur.fetchone()
    return _row_to_template(row) if row else None


def get_file_source_template(
    conn, *, project_id: str, template_code: str, version: int
) -> dict[str, Any] | None:
    """Return one template version by (project, code, version), or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TEMPLATE_COLS)}
            FROM app.file_source_templates
            WHERE project_id = %s AND template_code = %s AND version = %s
            """,
            (project_id, template_code, version),
        )
        row = cur.fetchone()
    return _row_to_template(row) if row else None


def list_file_source_template_versions(
    conn, *, project_id: str, template_code: str
) -> list[dict[str, Any]]:
    """List every version of a template, newest version first."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TEMPLATE_COLS)}
            FROM app.file_source_templates
            WHERE project_id = %s AND template_code = %s
            ORDER BY version DESC
            """,
            (project_id, template_code),
        )
        rows = cur.fetchall()
    return [_row_to_template(r) for r in rows]


def _mint_template_id() -> str:
    return f"fst_{ULID()}"


# ---------------------------------------------------------------------------
# Write path: create a versioned template through execute_operation (AD-2).
# ---------------------------------------------------------------------------


def create_file_source_template(
    conn,
    *,
    project_id: str,
    org_id: str,
    template_code: str,
    contract: dict[str, Any],
    created_by: str,
    label: str | None = None,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Create one immutable, content-hashed template VERSION (idempotent).

    Validates the contract (pure) + that every required/optional field id is an
    active canonical field in scope, then writes ONE version row through
    ``operations.execute_operation``:
      * identical contract for (project, template_code) -> the existing version
        (no new row);
      * a different contract -> version = MAX(version)+1;
      * the caller owns the transaction (this never commits).

    Returns the created (or replayed) template row dict.
    """
    code = _validate_template_code(template_code)
    normalised = validate_template_contract(contract)
    declared_fields = normalised["required_fields"] + normalised["optional_fields"]
    # WHERE it lands decides WHICH vocabulary describes it. A plan-store template
    # declares plan line attributes; a warehouse template declares the project's
    # canonical fields.
    if normalised["landing_target"] == LANDING_PLAN_STORE:
        _assert_plan_line_fields(declared_fields)
    else:
        _assert_required_fields_registered(
            conn,
            project_id=project_id,
            field_ids=declared_fields,
        )

    content_hash = compute_content_hash(normalised)
    idem = idempotency_key or default_idempotency_key(project_id, code, content_hash)
    placement_class = normalised["class"]
    grain = normalised["grain"]
    kind = normalised["kind"]

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    spec = OperationSpec(
        command_type=ACTION_FILE_SOURCE_TEMPLATE_CREATED,
        actor=created_by,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"file_source_template:{code}",
        ),
        idempotency_key=idem,
        host_context=host_context or {},
        versions={"policy": "file-source-template-v1"},
        request_payload={
            "project_id": project_id,
            "template_code": code,
            "content_hash": content_hash,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"file-source-template:{project_id}:{code}:{content_hash}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        # Defensive dedup (the operation replay usually short-circuits identical
        # re-creates before the mutation even runs; this guards a concurrent race).
        existing = _select_by_content(
            operation_conn, project_id=project_id, template_code=code,
            content_hash=content_hash,
        )
        if existing is not None:
            row = existing
        else:
            idem_hash = hashlib.sha256(idem.encode("utf-8")).hexdigest()
            with operation_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM app.file_source_templates
                    WHERE project_id = %s AND template_code = %s
                    """,
                    (project_id, code),
                )
                next_version = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO app.file_source_templates
                        (id, project_id, template_code, version, kind, content_hash,
                         idempotency_key_hash, contract, placement_class, grain,
                         created_by, label)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (project_id, template_code, content_hash) DO NOTHING
                    """,
                    (
                        _mint_template_id(), project_id, code, next_version, kind,
                        content_hash, idem_hash, json.dumps(normalised),
                        placement_class, grain, created_by, label,
                    ),
                )
            # Re-read: our insert may have been a no-op if a concurrent writer won
            # the content race -> return whichever row is now persisted.
            row = _select_by_content(
                operation_conn, project_id=project_id, template_code=code,
                content_hash=content_hash,
            )
            if row is None:  # pragma: no cover - the insert above just ran
                raise FileSourceTemplateError("template row not found after insert")

        result = {
            "template_id": row["id"],
            "template_code": row["template_code"],
            "version": row["version"],
            "kind": row["kind"],
            "content_hash": row["content_hash"],
            "class": row["placement_class"],
            "grain": row["grain"],
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "template_id": row["id"],
                "template_code": row["template_code"],
                "version": row["version"],
                "kind": row["kind"],
            },
        )

    execute_operation(conn, spec, mutation=mutation)
    # Return the authoritative persisted row (create OR idempotent replay).
    row = _select_by_content(
        conn, project_id=project_id, template_code=code, content_hash=content_hash
    )
    if row is None:  # pragma: no cover
        raise FileSourceTemplateError("template row not found after operation")
    return row
