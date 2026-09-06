"""toorow -- bounded previews: what the bytes say, WITHOUT any publish.

Two previews, one rule: a preview is evidence, never authority.

  * ``build_preview`` -- the CSV/Excel preview: parse, bound the rows, report the
    columns and the rejections honestly (``row_count=0`` stays 0);
  * ``build_file_source_preview`` -- the Epic 22 file-source preview: the same
    parse plus the reports a human confirms against (vocabulary, row validation,
    coercion, classification, units) and the drift verdict.

Neither function writes. Neither opens a ledger.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from core.file_source_resolution import (
    _confirmed_mapping,
    resolve_file_source_producer,
)
from core.import_contract import (
    validate_import_contract,
)
from core.tabular_parsing import (
    detect_format,
    parse_csv,
    validate_upload_meta,
)
from core.tabular_types import (
    FORMAT_CSV,
    FORMAT_EXCEL,
    MAX_COLUMNS,
    MAX_FIELD_CHARS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    PREVIEW_ROW_LIMIT,
    SUPPORTED_FORMATS,
    ImportPreview,
    InvalidImportContract,
)
from core.xlsx_parsing import (
    parse_excel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preview builder (pure, no publish).
# ---------------------------------------------------------------------------


def build_preview(
    data: bytes,
    *,
    filename: str | None = None,
    contract: dict[str, Any] | None = None,
    format_hint: str | None = None,
) -> ImportPreview:
    """Produce a bounded preview from upload bytes WITHOUT publishing or writing data.

    This is the first half of the 2-step import flow:
      1. ``build_preview`` -- validate + parse + return bounded preview (no publish).
      2. ``run_import``    -- confirm + open_import + record_rows + publication gate.

    The contract (if provided) is validated first; contract settings override
    auto-detection (delimiter, encoding, sheet_name, header_row, date_format).

    Raises: UnsupportedFileType, EmptyFile, FileTooLarge, EncodingError,
            NoHeaderRow, DuplicateColumns, FormulaInCells, InvalidImportContract.
    """
    validate_upload_meta(data)

    # Resolve format.
    if format_hint and format_hint in SUPPORTED_FORMATS:
        fmt = format_hint
    else:
        fmt = detect_format(filename, data)

    # Validate and normalise the contract if provided.
    norm_contract: dict[str, Any] = {}
    if contract is not None:
        norm_contract = validate_import_contract(contract)
        if norm_contract.get("format") and norm_contract["format"] != fmt:
            raise InvalidImportContract(
                f"Contract format '{norm_contract['format']}' does not match "
                f"detected file format '{fmt}'."
            )

    # Parse.
    if fmt == FORMAT_CSV:
        result = parse_csv(
            data,
            delimiter=norm_contract.get("delimiter"),
            encoding=norm_contract.get("encoding"),
            date_format=norm_contract.get("date_format"),
            locale=norm_contract.get("locale"),
            header_row=norm_contract.get("header_row", 1),
            max_rows=norm_contract.get("max_rows", MAX_ROWS),
            max_columns=norm_contract.get("max_columns", MAX_COLUMNS),
            max_field_chars=norm_contract.get("max_field_chars", MAX_FIELD_CHARS),
            column_types=norm_contract.get("column_types"),
        )
    elif fmt == FORMAT_EXCEL:
        result = parse_excel(
            data,
            sheet_name=norm_contract.get("sheet_name"),
            cell_range=norm_contract.get("cell_range"),
            header_row=norm_contract.get("header_row", 1),
            date_format=norm_contract.get("date_format"),
            locale=norm_contract.get("locale"),
            formulas_as_values=norm_contract.get("formulas_as_values", True),
            max_rows=norm_contract.get("max_rows", MAX_SHEET_ROWS),
            max_columns=norm_contract.get("max_columns", MAX_SHEET_COLUMNS),
            max_field_chars=norm_contract.get("max_field_chars", MAX_FIELD_CHARS),
            column_types=norm_contract.get("column_types"),
        )
    else:
        from core.inbound_sav import parse_sav

        result = parse_sav(
            data,
            max_bytes=norm_contract.get("max_bytes", MAX_FILE_BYTES),
            max_rows=norm_contract.get("max_rows", MAX_ROWS),
            max_columns=norm_contract.get("max_columns", MAX_COLUMNS),
            max_cells=norm_contract.get("max_cells", 5_000_000),
            max_string_chars=norm_contract.get("max_string_chars", MAX_FIELD_CHARS),
            max_decompressed_bytes=norm_contract.get("max_decompressed_bytes", 256 * 1024 * 1024),
            max_memory_bytes=norm_contract.get("max_memory_bytes", 256 * 1024 * 1024),
            max_seconds=norm_contract.get("max_seconds", 10.0),
            apply_user_missing=norm_contract.get("apply_user_missing", True),
            column_types=norm_contract.get("column_types"),
        )

    return ImportPreview(
        format=fmt,
        encoding=result.encoding,
        delimiter=result.delimiter,
        sheet_name=result.sheet_name,
        columns=result.columns,
        row_count=len(result.rows),
        rejected_count=len(result.rejected),
        preview_rows=[
            {
                key: (value.isoformat() if isinstance(value, (date, datetime)) else value)
                for key, value in row.items()
            }
            for row in result.rows[:PREVIEW_ROW_LIMIT]
        ],
        content_hash=result.content_hash,
        contract_version_id=None,  # preview does not version the contract (no publish)
        schema_fingerprint=result.schema_fingerprint,
        envelope_version=result.envelope_version,
        warnings=list(result.warnings),
        issues=list(result.issues),
        producer_format=result.source_format,
    )


def _vocabulary_report(
    source_columns: list[str], sample_rows: list[Any]
) -> list[dict[str, Any]]:
    """What each source column reads as against the four international standards.

    Story 38.16 AC3 names "date/timezone" and "currency/unit" among the readings a
    preview must REPORT. The verb is `reports`: nothing here is a choice. Country,
    language, currency and reporting timezone all start from one predefined
    international standard, identical for every client, and the governed alias
    lists exist so the conventions external tools write -- `Andorra`, `afr`,
    `dirham`, `europe/paris` -- resolve to the same code for everybody.

    All FOUR axes, not the two the AC happens to name: they are one mechanism
    over four registries (`core.vocabulary_normalization`), and reporting two
    would leave a client's country and language columns unread for the next
    screen, for the same reason.

    Only columns where at least one value resolved are returned -- an inventory of
    every column against every axis would bury the two that matter. The counts are
    raw and unthresholded, so `1/50` reads as the accident it is.
    """
    from core.vocabulary_normalization import profile_column  # noqa: PLC0415

    if not source_columns or not sample_rows:
        return []
    report: list[dict[str, Any]] = []
    for column in source_columns:
        values = [
            row.get(column) for row in sample_rows if isinstance(row, dict) and column in row
        ]
        profiles = profile_column(values)
        if not profiles:
            continue
        report.append(
            {
                "source_column": column,
                "readings": [
                    {
                        "vocabulary": profile.kind,
                        "resolved": profile.resolved,
                        "sampled": profile.total,
                        "canonical_values": list(profile.canonical_values),
                        "unresolved_reasons": profile.unresolved_reasons,
                    }
                    for profile in profiles
                ],
            }
        )
    return report


def _row_validation_report(result: Any) -> dict[str, Any]:
    """What the parse REJECTED, summarised by rule -- 38.16 AC3.

    << Row-level validation summaries >> is the last of the seven readings AC3
    names, and the material was already in hand: every parse returns a
    `ParseResult` carrying `rejected` (one `RejectedRow` per failure, with its
    row number, its column, its stable rule code) and `detected_row_count`.
    `build_file_source_preview` computed it and threw it away, so a person
    learned that a fifth of their file would not land by IMPORTING it.

    A SUMMARY, not the rows. The rejected values are bounded source text, and
    the preview's whole discipline is that provider values stay non-instructional
    and minimally disclosed (AC5). What an operator needs to decide is the SHAPE
    of the damage: which rule, how many, and where the first one is -- a row
    number they can open in their own file.

    The rate is `None` when nothing was counted. Zero rejected out of an unknown
    total is not 0 %, and a clean rate on a parse that read nothing reassures
    about exactly the wrong thing.
    """
    rejected = list(getattr(result, "rejected", None) or [])
    detected = int(getattr(result, "detected_row_count", 0) or 0)
    accepted = len(getattr(result, "rows", None) or [])

    by_rule: dict[str, dict[str, Any]] = {}
    for row in rejected:
        rule = str(getattr(row, "rule", "") or "unknown")
        entry = by_rule.setdefault(
            rule,
            {"rule": rule, "count": 0, "first_row_number": None, "fields": set()},
        )
        entry["count"] += 1
        number = getattr(row, "row_number", None)
        if isinstance(number, int) and (
            entry["first_row_number"] is None or number < entry["first_row_number"]
        ):
            entry["first_row_number"] = number
        field_name = str(getattr(row, "field_name", "") or "")
        if field_name:
            entry["fields"].add(field_name)

    return {
        "detected_row_count": detected,
        "accepted_row_count": accepted,
        "rejected_row_count": len(rejected),
        "rejected_row_pct": (
            round(100.0 * len(rejected) / detected, 3) if detected else None
        ),
        "by_rule": sorted(
            (
                {**entry, "fields": sorted(entry["fields"])}
                for entry in by_rule.values()
            ),
            key=lambda entry: (-entry["count"], entry["rule"]),
        ),
    }


def _coercion_report(template: dict[str, Any], result: Any) -> list[dict[str, Any]]:
    """How each column will be READ, not merely which target it hits -- AC3.

    The recognizer answers << which canonical field is this column? >>. AC3 asks
    a second question the screen never showed: << and what will happen to the
    values in it? >> A date column read under `%d.%m.%Y` and the same column read
    under the default both report `matched`, and only one of them is right.

    The coercions are DECLARED by the template's reshape spec, not inferred from
    the values: `date_format` is a declaration, and reading it back is reporting.
    Guessing a format from a sample would be the preview inventing the very thing
    it exists to let a person check.
    """
    contract = template.get("contract") if "contract" in template else template
    contract = contract if isinstance(contract, dict) else {}
    reshape = contract.get("reshape") or {}
    if not isinstance(reshape, dict):
        reshape = {}

    date_format = reshape.get("date_format")
    date_fields = {
        str(reshape.get(name) or "")
        for name in ("start_field", "end_field", "date_field")
        if reshape.get(name)
    }
    amount_field = str(reshape.get("amount_field") or "")

    out: list[dict[str, Any]] = []
    for spec in getattr(result, "columns", None) or []:
        name = getattr(spec, "name", None)
        if not name:
            continue
        if name in date_fields:
            out.append(
                {
                    "column": name,
                    "coercion": "date",
                    # `None` says << the parser's default >>, and it is not the
                    # same statement as a declared format. A screen that printed
                    # a default here would hide the declaration that is missing.
                    "declared_format": date_format,
                }
            )
        elif name == amount_field:
            out.append({"column": name, "coercion": "amount", "declared_format": None})
    return out


def _classification_report(
    conn, *, project_id: str, template: dict[str, Any]
) -> dict[str, Any]:
    """The sensitive classification of the fields this file lands on -- AC3.

    JE NE SAIS PAS, ET JE LE DIS. The vocabulary is ratified and in use --
    `none / pii / financial / credentials / internal / unknown`
    (`datastream_field_mapping.py`) -- and the sensitivity gate of the Epic-36
    mapping path already refuses a bound `credentials` field. But that value
    arrives with an API source's own schema metadata, and a FILE carries no such
    metadata: the classification of a canonical field belongs to the field, and
    `app.mdm_canonical_fields` (migration 032) declares `unit`, `currency_scope`,
    `aggregation` and `non_additive` -- and no classification column.

    So every target field here reads `unknown`, and the report NAMES why rather
    than leaving a screen to render six reassuring blanks. Deriving a
    classification from the sample values would be exactly the invention this
    repository forbids: a column of numbers is not evidence that it is not
    financial, and a screen that said `none` on that basis would be worse than
    one that said nothing.

    The arbitrage -- should `mdm_canonical_fields` carry a classification? --
    belongs to the alignment register, not to this function.
    """
    contract = template.get("contract") if "contract" in template else template
    contract = contract if isinstance(contract, dict) else {}
    fields = sorted(
        {
            *(contract.get("required_fields") or []),
            *(contract.get("optional_fields") or []),
        }
    )
    return {
        "vocabulary": ["none", "pii", "financial", "credentials", "internal", "unknown"],
        "declared_by": None,
        "undeclared_reason": "canonical_field_registry_declares_no_classification",
        "fields": [{"field_id": field_id, "classification": "unknown"} for field_id in fields],
    }


def _unit_report(conn, *, project_id: str, template: dict[str, Any]) -> list[dict[str, Any]]:
    """What each target field is MEASURED IN -- the other half of AC3's currency/unit.

    `_vocabulary_report` reads the currency codes present in the FILE. This reads
    what the canonical field the file lands on declares it holds: its unit, its
    currency scope, how it aggregates, and whether it aggregates at all.

    The two are different questions and a preview needs both. A column of values
    labelled `EUR` landing on a field whose `currency_scope` is something else is
    a mistake neither reading catches alone.
    """
    contract = template.get("contract") if "contract" in template else template
    contract = contract if isinstance(contract, dict) else {}
    field_ids = sorted(
        {
            *(contract.get("required_fields") or []),
            *(contract.get("optional_fields") or []),
        }
    )
    if not field_ids:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, canonical_name, concept_kind, unit, currency_scope, "
                "aggregation, non_additive "
                "FROM app.mdm_canonical_fields "
                "WHERE id = ANY(%s) AND status = 'active' "
                "  AND (project_id = %s OR project_id IS NULL)",
                (field_ids, project_id),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- a preview never fails on its extras
        logger.warning("file_source: unit report unavailable: %s", type(exc).__name__)
        return []
    return [
        {
            "field_id": field_id,
            # THE WORD, RESOLVED WHERE THE VOCABULARY LIVES. The review screen
            # read "mdm_01KZ... is measured in EUR" because this report carried
            # the id alone -- the same defect as a Builder rail printing
            # `mdm_01KZ...`. `canonical_name` is NOT NULL on the registry
            # (032_datastream_field_mappings.sql:114), so the name is always
            # there to serve and the browser composes nothing.
            "canonical_name": canonical_name,
            "concept_kind": concept_kind,
            "unit": unit,
            "currency_scope": currency_scope,
            "aggregation": aggregation,
            "non_additive": bool(non_additive),
        }
        for (
            field_id,
            canonical_name,
            concept_kind,
            unit,
            currency_scope,
            aggregation,
            non_additive,
        ) in rows
    ]


def build_file_source_preview(
    conn,
    data: bytes,
    *,
    project_id: str,
    datastream_id: str,
    mapping_version_id: str | None,
    filename: str | None = None,
    mapping_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """What the onboarding review screen shows, BEFORE anything is imported.

    Story 22.19. Returns ``None`` when the Datastream carries no ``fst_``
    binding -- the ordinary CSV/Excel preview path is untouched, and an empty
    file-source object is never fabricated for a Datastream that has none.

    WHY THIS EXISTS. `evaluate_landing_gate` had exactly one caller outside its
    own module: ``run_import``. A person could therefore only discover that a
    required field was unmapped by ATTEMPTING the import -- after the fact, which
    is what the ratified step 5 refuses ("bounded masked sample, parse errors,
    schema/profile, import coverage, DQ gates" come before publication).

    It COMPOSES three bricks that were already delivered and re-implements none:
      * ``recognize_columns``     (22.17) -- per-column target + confidence;
      * ``stamp_placement``       (22.14) -- the matrix class the rows carry;
      * ``evaluate_landing_gate`` (22.15) -- the same verdict the import uses.

    That last point is the load-bearing one: preview and import must return the
    SAME verdict on the same inputs. A second scorer here would let a person see
    green and then hit red, which is worse than no preview at all -- and it is
    the defect 22.20 already carries.

    A refusal is REPORTED, never raised: ``run_import`` is right to refuse a
    template with no declared class (AD-6) because it is about to write, but the
    preview exists precisely to say why the import will refuse. Raising would
    turn an explanation into a 500.

    Writes nothing, opens no ledger row, and commits nothing.
    """
    producer = resolve_file_source_producer(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        mapping_version_id=mapping_version_id,
    )
    if producer is None:
        return None
    if mapping_override is not None:
        producer.mapping = _confirmed_mapping(mapping_override)
        producer.mapping_payload = mapping_override

    from core.column_treatments import describe_recognized_columns  # noqa: PLC0415
    from core.file_source_gate import evaluate_landing_gate  # noqa: PLC0415
    from core.file_source_producer import (  # noqa: PLC0415
        ReshapeProducerError,
        detect_source_drift,
        stamp_placement,
    )
    from core.file_source_recognizer import recognize_columns  # noqa: PLC0415

    template = producer.template
    contract = template.get("contract") if "contract" in template else template
    contract = contract if isinstance(contract, dict) else {}

    blocked: dict[str, Any] = {
        "fields": [],
        "placement": None,
        "gate": None,
        "drift": None,
        "ambiguities": [],
        # Present on the BLOCKED returns too, and deliberately: those branches
        # exist to hand a person what they need to lift the block, and "your date
        # column carries a timezone nothing recognises" is often the block.
        "vocabularies": [],
        # Presentes sur les retours BLOQUES aussi, pour la raison qui y met deja
        # `vocabularies` : ces branches existent pour donner de quoi lever le
        # blocage, et << ce fichier ne declare pas de format de date >> ou
        # << la moitie des lignes sont des sous-totaux >> en fait souvent partie.
        "coercions": [],
        "row_validation": None,
        "units": [],
        "classification": None,
        "blocked": True,
        "reason": None,
    }

    # THE RECOGNIZER RUNS FIRST, AND IT RUNS EVEN WHEN THE PRODUCER REFUSES.
    #
    # It scores the SOURCE headers against the Template, and needs nothing else:
    # not a mapping, not produced rows. `producer(data)` used to be called
    # first, so a tabular catalog Template with no confirmed mapping yet raised
    # `mapping_required` and this function returned with `fields: []` -- the
    # recognition never ran.
    #
    # That is a circle a person cannot get out of: the mapping is built FROM the
    # recognition, and the recognition was withheld until a mapping existed. A
    # catalog-Template file could never be onboarded at all. Measured on the
    # deployed walk (G9, 2026-08-08): `{"fields": [], "blocked": true, "reason":
    # "mapping_required"}` -- and this docstring already said what step 5
    # refuses, "a person could only discover that a required field was unmapped
    # by ATTEMPTING the import".
    #
    # `producer(data)` renames the source headers to their canonical targets, so
    # the raw parse is read separately either way -- running the recognizer on
    # produced columns would score `day` against `day` and rob the screen of the
    # one thing it exists to show.
    source_columns: list[str] = []
    sample_rows: list[Any] = []
    try:
        raw = build_preview(data, filename=filename)
        source_columns = [spec.name for spec in raw.columns]
        sample_rows = raw.preview_rows
    except Exception as exc:  # noqa: BLE001 -- a preview never fails on its own extras
        logger.warning("file_source: preview raw parse unavailable ds=%s: %s", datastream_id, exc)
    recognized = recognize_columns(template, source_columns, sample_rows) if source_columns else {}
    # Story 60.6: the two readings the review screen was missing.
    #
    # `recognize_columns` RECEIVES the sample rows already and never renders a
    # value from them, so a person looking at 46 unfamiliar headers could not
    # recognise their own file. The treatment word comes from the recognizer's
    # own three statuses plus the mapping's declared joins/splits -- no second
    # scorer, and no fabricated example: a column the sample leaves empty says
    # `no sample value`, never an empty cell and never a `0`.
    if recognized:
        recognized = {
            **recognized,
            "fields": describe_recognized_columns(
                recognized.get("fields", []),
                sample_rows=sample_rows,
                mapping_payload=producer.mapping_payload
                if isinstance(getattr(producer, "mapping_payload", None), dict)
                else None,
            ),
        }

    try:
        result = producer(data)
    except ReshapeProducerError as exc:
        logger.warning(
            "file_source: preview parse refused ds=%s template=%s code=%s",
            datastream_id,
            getattr(producer, "template_id", None),
            exc.code,
        )
        # Blocked, and SAYING WHY -- with the recognition the person needs to
        # lift the block. Placement, gate and drift stay null: those genuinely
        # need the produced rows, and inventing them would be the fabrication
        # this file refuses everywhere else.
        #
        # `fields` is a LIST of columns here as it is on every other return of
        # this function. It used to be handed the whole recognizer result -- an
        # object carrying `fields`, `mapping` and `ambiguities` -- so the one
        # branch that exists to unblock a person served a shape the screen
        # iterates over and finds nothing in.
        return {
            **blocked,
            "reason": exc.code,
            "fields": recognized.get("fields", []),
            "ambiguities": recognized.get("ambiguities", []),
            "vocabularies": _vocabulary_report(source_columns, sample_rows),
        }

    try:
        result = stamp_placement(result, template, filename=filename)
    except ReshapeProducerError as exc:
        # AD-6 and the variant discriminator both land here. The screen needs the
        # reason, not a stack trace: this is the whole point of a preview.
        logger.info(
            "file_source: preview placement refused ds=%s template=%s code=%s",
            datastream_id,
            getattr(producer, "template_id", None),
            exc.code,
        )
        return {
            **blocked,
            "reason": exc.code,
            "fields": recognized.get("fields", []),
            "ambiguities": recognized.get("ambiguities", []),
            "vocabularies": _vocabulary_report(source_columns, sample_rows),
        }

    gate = evaluate_landing_gate(
        template,
        producer.mapping or None,
        columns=[spec.name for spec in result.columns],
        rows=result.rows,
    )
    # Story 22.16 / AD-8: disappeared required sources re-enter the gate.
    # Preview and replay use the same fail-closed drift policy.
    drift = detect_source_drift(template, producer.mapping or None, source_columns)
    placement = contract.get("placement") or {}
    return {
        "fields": recognized.get("fields", []),
        # Shaped for the screen: the class sits beside its coordinates rather
        # than one level up, because that is how a person reads the matrix.
        "placement": {
            "class": contract.get("class"),
            "metric": placement.get("metric"),
            "period": placement.get("period"),
            "dimension": list(placement.get("dimension") or []),
        },
        "gate": gate,
        # `drift` is the SOURCE drift (columns that disappeared). The Template
        # can move too, and until now that was indistinguishable from "never
        # confirmed" -- AC4's rebase requirement had no way to be seen.
        "drift": drift,
        "confirmation": producer.confirmation_state(mapping_version_id or ""),
        # `ambiguities` is the WARNING level of the ratified vocabulary; the
        # gate's `missing_required` and `flagged` are both BLOCKING (flagged only
        # ever carries REQUIRED fields whose binding is blocking --
        # file_source_gate.py:123). Kept distinct here so the screen cannot
        # conflate the two the way the current component does.
        "ambiguities": recognized.get("ambiguities", []),
        # AC3: the four international readings of this file, reported not chosen.
        "vocabularies": _vocabulary_report(source_columns, sample_rows),
        # AC3, les trois lectures restantes. Toutes trois etaient calculees ou a
        # une jointure de distance, et jetees : un operateur apprenait qu'un
        # cinquieme de son fichier n'atterrirait pas EN L'IMPORTANT.
        "coercions": _coercion_report(template, result),
        "row_validation": _row_validation_report(result),
        "units": _unit_report(conn, project_id=project_id, template=template),
        "classification": _classification_report(
            conn, project_id=project_id, template=template
        ),
        "blocked": drift.get("status") == "needs_revalidation",
        "reason": ("source_drift" if drift.get("status") == "needs_revalidation" else None),
    }
