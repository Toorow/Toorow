"""Every CHECK of the Analyze write tables, translated into a named refusal.

WHY THIS FILE EXISTS, measured 2026-09-01 (AI-357). `POST /renders` answered a
mute 500: `create_render` wrote `renderer_adapter = ''` when the caller had not
supplied one, `ck_renders_pins_are_exact` refused the row, and the resulting
`psycopg.errors.CheckViolation` travelled all the way out of the handler. A caller
was told nothing -- not which pin, not which gesture, not even that it was their
request rather than the server that was wrong. That is one instance of a class:
NO write of this domain translated a constraint name into anything a person could
read, so every one of the fifty CHECKs below was one un-anticipated payload away
from the same mute 500.

THE TRANSLATION IS CODE, NEVER A ROW. A table of "constraint name -> sentence" in
the database would be executable presentation metadata (AD-2) and a second
authority that drifts the day a migration renames a constraint. It lives here, and
`untranslated_constraints` reads the catalogue of a real database and returns
every CHECK this map does not translate -- so a migration that adds one turns a
test red instead of shipping a mute 500.

WHAT A TRANSLATION OWES. `code` is stable and machine-readable; `message` states
the fact in the caller's vocabulary; `subject` names the field the caller can
point at; `remedy` names the GESTURE that repairs it. A message that quoted the
constraint expression would name the cause and not the move, which is the failure
this file exists to stop repeating.

THIS MAP IS A LAST RESORT, not the refusal path. A service that can say WHY before
the database does, does -- `create_render` refuses pin by pin, `create_notebook`
refuses block by block. What lands here is the payload nobody anticipated, and its
refusal is still named, still 422, and still carries a gesture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The tables `core.analyze_artifacts` writes. The enumeration test reads the
#: catalogue for exactly these, so a table added to the module without a
#: translation for its CHECKs cannot pass unnoticed.
ANALYZE_DOMAIN_TABLES: tuple[str, ...] = (
    "analysis_reports",
    "analysis_report_versions",
    "analysis_report_runs",
    "analysis_notebooks",
    "analysis_notebook_versions",
    "analysis_notebook_version_blocks",
    "analysis_notebook_schedules",
    "analysis_notebook_runs",
    "analysis_notebook_run_blocks",
    "renders",
)

ANALYZE_DOMAIN_SCHEMA = "app"


@dataclass(frozen=True)
class CheckTranslation:
    """One constraint, said in words a person can act on."""

    code: str
    message: str
    subject: str
    remedy: str


def _t(code: str, message: str, subject: str, remedy: str) -> CheckTranslation:
    return CheckTranslation(code=code, message=message, subject=subject, remedy=remedy)


#: The exact words `app.is_exact_pin` refuses, repeated in prose so a remedy can
#: name them without a reader opening migration 154.
_PLACEHOLDER_WORDS = "`legacy`, `current`, `deferred`, `latest`, `unknown` or `none`"

CHECK_TRANSLATIONS: dict[str, CheckTranslation] = {
    # -- app.analysis_reports -------------------------------------------------
    "ck_analysis_reports_archived_pair": _t(
        "inconsistent_archive",
        "a Report is either archived with the person who archived it, or not archived at all",
        "archived_at",
        "archive the Report through the archive action rather than by setting a date on it",
    ),
    "ck_analysis_reports_label_bounded": _t(
        "invalid_label",
        "a Report name must be between 1 and 200 characters",
        "label",
        "give the Report a name of at most 200 characters",
    ),
    "ck_analysis_reports_seed_origin": _t(
        "invalid_origin",
        "a Report comes from this Project, from a connector seed, or from Explore",
        "seed_origin",
        "create the Report from the Reports screen or from Explore rather than naming its origin",
    ),
    "ck_analysis_reports_seed_pair": _t(
        "incomplete_seed_origin",
        "a Report created from a connector seed names the connector and the seed it came from",
        "seed_report_id",
        "pick the seed from the available list so the Report carries which connector offered it",
    ),
    # -- app.analysis_report_versions -----------------------------------------
    "ck_analysis_report_versions_hash": _t(
        "invalid_content_hash",
        "the content hash of a Report version is a 64-character hexadecimal digest",
        "content_hash",
        "let the server compute the version hash; it is never supplied with the request",
    ),
    "ck_analysis_report_versions_lineage": _t(
        "broken_version_lineage",
        "version 1 of a Report has no predecessor, and every later version names one",
        "predecessor_version_id",
        "save the edit as a new version of the Report you opened, not as a version of nothing",
    ),
    "ck_analysis_report_versions_number_positive": _t(
        "invalid_version_number",
        "a Report version is numbered from 1 upwards",
        "version_number",
        "let the server number the version; the first one is 1",
    ),
    "ck_analysis_report_versions_presentation_is_honest": _t(
        "dishonest_presentation_pin",
        "a Report version either pins one exact presentation version, or says in words that "
        "no accepted presentation contract exists -- never both and never neither",
        "presentation_version_id",
        "choose a presentation version from the offered list, or leave the presentation unset",
    ),
    "ck_analysis_report_versions_seed_is_object": _t(
        "invalid_seed_provenance",
        "the seed provenance of a Report version is an object",
        "seed_provenance",
        "send the seed provenance as a JSON object, or omit it",
    ),
    # -- app.analysis_report_runs ---------------------------------------------
    "ck_analysis_report_runs_ends_after_start": _t(
        "impossible_run_window",
        "a Report run cannot end before it started",
        "ended_at",
        "re-run the Report; its timings are recorded by the server and never supplied",
    ),
    "ck_analysis_report_runs_outcome": _t(
        "invalid_outcome",
        "a Report run ends as success, empty, degraded, refused or unavailable",
        "outcome",
        "re-run the Report; the outcome is decided by the run and never supplied",
    ),
    # -- app.analysis_notebooks -----------------------------------------------
    "ck_analysis_notebooks_archived_pair": _t(
        "inconsistent_archive",
        "a Notebook is either archived with the person who archived it, or not archived at all",
        "archived_at",
        "archive the Notebook through the archive action rather than by setting a date on it",
    ),
    "ck_analysis_notebooks_label_bounded": _t(
        "invalid_label",
        "a Notebook name must be between 1 and 200 characters",
        "label",
        "give the Notebook a name of at most 200 characters",
    ),
    # -- app.analysis_notebook_versions ---------------------------------------
    "ck_analysis_notebook_versions_hash": _t(
        "invalid_content_hash",
        "the content hash of a Notebook version is a 64-character hexadecimal digest",
        "content_hash",
        "let the server compute the version hash; it is never supplied with the request",
    ),
    "ck_analysis_notebook_versions_lineage": _t(
        "broken_version_lineage",
        "version 1 of a Notebook has no predecessor, and every later version names one",
        "predecessor_version_id",
        "save the edit as a new version of the Notebook you opened, not as a version of nothing",
    ),
    "ck_analysis_notebook_versions_number_positive": _t(
        "invalid_version_number",
        "a Notebook version is numbered from 1 upwards",
        "version_number",
        "let the server number the version; the first one is 1",
    ),
    # -- app.analysis_notebook_version_blocks ---------------------------------
    "ck_analysis_notebook_blocks_as_of_rule": _t(
        "invalid_as_of_rule",
        "a block reads the data as it is now, or as of one stated moment",
        "as_of_rule",
        "set the block to `current` or to `as_of` with the moment it reads",
    ),
    "ck_analysis_notebook_blocks_hash": _t(
        "invalid_content_hash",
        "the content hash of a Notebook block is a 64-character hexadecimal digest",
        "content_hash",
        "let the server compute the block hash; it is never supplied with the request",
    ),
    "ck_analysis_notebook_blocks_input": _t(
        "block_input_mismatch",
        "a query block pins one Query Spec version, a report block pins one Report version, "
        "and a narrative block pins neither",
        "block_type",
        "give the block the one input its type takes, and remove the other",
    ),
    "ck_analysis_notebook_blocks_key_bounded": _t(
        "invalid_block_key",
        "a block key is 1 to 63 characters of lowercase letters, digits, hyphen or underscore, "
        "starting with a letter or a digit",
        "block_key",
        "rename the block with lowercase letters, digits, `-` or `_`",
    ),
    "ck_analysis_notebook_blocks_narrative_does_not_render": _t(
        "narrative_cannot_render",
        "a narrative block carries words, so it produces no Render",
        "renders",
        "turn rendering off on the narrative block, or make it a query block",
    ),
    "ck_analysis_notebook_blocks_narrative_is_object": _t(
        "invalid_narrative",
        "the narrative of a block is an object",
        "narrative",
        "send the narrative as a JSON object, or omit it",
    ),
    "ck_analysis_notebook_blocks_position": _t(
        "invalid_position",
        "blocks are ordered from position 1 upwards",
        "position",
        "reorder the blocks; their positions are assigned by the order you send them in",
    ),
    "ck_analysis_notebook_blocks_presentation_is_honest": _t(
        "dishonest_presentation_pin",
        "a block either pins one exact presentation version, or says in words that no "
        "accepted presentation contract exists -- never both and never neither",
        "presentation_version_id",
        "choose a presentation version from the offered list, or leave the presentation unset",
    ),
    "ck_analysis_notebook_blocks_type": _t(
        "invalid_block_type",
        "a Notebook block is a query, a report or a narrative",
        "block_type",
        "set the block type to `query`, `report` or `narrative`",
    ),
    # -- app.analysis_notebook_schedules --------------------------------------
    "ck_analysis_notebook_schedules_dispatch_is_paired": _t(
        "inconsistent_last_dispatch",
        "a schedule records the last run it dispatched together with when it dispatched it",
        "last_run_id",
        "re-save the schedule; its dispatch history is written by the scheduler, never supplied",
    ),
    "ck_analysis_notebook_schedules_recurrence": _t(
        "invalid_recurrence",
        "a Notebook runs daily, weekly or monthly",
        "recurrence",
        "choose daily, weekly or monthly",
    ),
    # -- app.analysis_notebook_runs -------------------------------------------
    "ck_analysis_notebook_runs_dispatch": _t(
        "invalid_dispatch_source",
        "a Notebook Run is started by a person or by its schedule",
        "dispatch_source",
        "start the Run from the Notebook, or let its schedule start it",
    ),
    "ck_analysis_notebook_runs_ends_after_start": _t(
        "impossible_run_window",
        "a Notebook Run cannot finish before it was accepted",
        "terminal_at",
        "re-run the Notebook; its timings are recorded by the server and never supplied",
    ),
    "ck_analysis_notebook_runs_state": _t(
        "invalid_run_state",
        "a Notebook Run is accepted, running or terminal",
        "state",
        "re-run the Notebook; the state is owned by the run and never supplied",
    ),
    "ck_analysis_notebook_runs_terminal_is_complete": _t(
        "incomplete_terminal_run",
        "a finished Notebook Run carries when it finished and how it ended; an unfinished "
        "one carries neither",
        "outcome",
        "re-run the Notebook; a run is closed by the server, never by the request",
    ),
    # -- app.analysis_notebook_run_blocks -------------------------------------
    "ck_analysis_notebook_run_blocks_ends_after_start": _t(
        "impossible_run_window",
        "a block cannot end before it started",
        "ended_at",
        "re-run the Notebook; block timings are recorded by the server and never supplied",
    ),
    "ck_analysis_notebook_run_blocks_narrative_has_no_result": _t(
        "narrative_has_no_result",
        "a narrative block carries words, so it produces no Result",
        "result_id",
        "make the block a query block if it is meant to return data",
    ),
    "ck_analysis_notebook_run_blocks_position": _t(
        "invalid_position",
        "blocks are ordered from position 1 upwards",
        "position",
        "reorder the blocks; their positions follow the order of the Notebook version",
    ),
    "ck_analysis_notebook_run_blocks_render_is_honest": _t(
        "dishonest_render_record",
        "a block names the Render it produced, or says in the exact words of this product "
        "why it produced none",
        "render_id",
        "re-run the Notebook; whether a block rendered is recorded by the run, never supplied",
    ),
    "ck_analysis_notebook_run_blocks_result_matches_status": _t(
        "result_contradicts_status",
        "a block that succeeded, is empty, degraded, refused or unavailable names its Result; "
        "a failed block names no Result and says what limited it",
        "status",
        "re-run the Notebook; the block outcome is written by the run, never supplied",
    ),
    "ck_analysis_notebook_run_blocks_status": _t(
        "invalid_block_status",
        "a block ends as succeeded, empty, degraded, refused, unavailable or failed",
        "status",
        "re-run the Notebook; the block status is written by the run, never supplied",
    ),
    "ck_analysis_notebook_run_blocks_type": _t(
        "invalid_block_type",
        "a Notebook block is a query, a report or a narrative",
        "block_type",
        "set the block type to `query`, `report` or `narrative`",
    ),
    # -- app.renders ----------------------------------------------------------
    "ck_renders_creation_surface": _t(
        "invalid_creation_surface",
        "a Render is frozen from Explore, from a Report, from a Notebook or from an assistant",
        "creation_surface",
        "freeze the Render from one of those surfaces rather than naming a fifth",
    ),
    "ck_renders_datum_keys_is_object": _t(
        "invalid_datum_evidence_keys",
        "the map from a drawn value to its evidence key is an object",
        "datum_evidence_keys",
        "send the datum-to-evidence map as a JSON object, or omit it",
    ),
    "ck_renders_display_state_bounded": _t(
        "display_state_too_large",
        "the local display state of a Render is bounded to 64 KB",
        "display_state",
        "keep only the sort, selection and collapse state in display state; data belongs to "
        "the Result",
    ),
    "ck_renders_display_state_carries_no_query": _t(
        "display_state_carries_a_query",
        "a Render carries no live query instruction and grants no rerun authority, so its "
        "display state may not hold a query, a spec, SQL, a rerun, a refresh or tool arguments",
        "display_state",
        "remove that key from display state; to ask a different question, run the query and "
        "freeze the new Result",
    ),
    "ck_renders_display_state_is_object": _t(
        "invalid_display_state",
        "the local display state of a Render is an object",
        "display_state",
        "send display state as a JSON object, or omit it",
    ),
    "ck_renders_evidence_manifest_is_object": _t(
        "invalid_evidence_manifest",
        "the evidence manifest of a Render is an object",
        "evidence_manifest",
        "send the evidence manifest as a JSON object",
    ),
    "ck_renders_evidence_manifest_not_empty": _t(
        "empty_evidence_manifest",
        "an empty evidence manifest is not a manifest: every tooltip and drill-through would "
        "resolve to nothing while the Render looked complete",
        "evidence_manifest",
        "freeze the Render from a Result that carries its evidence, so the manifest is filled",
    ),
    "ck_renders_hash": _t(
        "invalid_content_hash",
        "the content hash of a Render is a 64-character hexadecimal digest",
        "content_hash",
        "let the server compute the Render hash; it is never supplied with the request",
    ),
    "ck_renders_origin_kind": _t(
        "invalid_origin_kind",
        "a Render originates in Explore, in a Report run or in a Notebook run",
        "origin_kind",
        "set the origin to `explore`, `report_run` or `notebook_run`",
    ),
    "ck_renders_origin_reference": _t(
        "origin_reference_mismatch",
        "a Render from a Report run names that run, a Render from a Notebook run names that "
        "run, and a Render from Explore names neither",
        "origin_kind",
        "name the run the Render came from, or set the origin to `explore`",
    ),
    "ck_renders_pins_are_exact": _t(
        "placeholder_pin",
        "every replay pin of a Render names an exact identity: the Visualization Spec version, "
        "the renderer adapter and build, the runtime build, the theme, the formatter and the "
        f"responsive profile -- never empty and never {_PLACEHOLDER_WORDS}",
        "renderer_build_id",
        "freeze the Render from a visual the deployment ships, so each pin carries the exact "
        "build that drew it",
    ),
    "ck_renders_result_hash": _t(
        "invalid_result_content_hash",
        "the retained-data hash of the Result a Render pins is a 64-character hexadecimal "
        "digest",
        "result_content_hash",
        "pin the Result by its identifier and let the server read back its hash",
    ),
}


def translate(constraint_name: str | None) -> CheckTranslation | None:
    """The named refusal for one constraint, or `None` when it has no translation.

    `None` is a fact and not a default: the caller says "an unnamed constraint
    refused this write" rather than inventing a sentence about a rule it cannot
    read.
    """
    if not constraint_name:
        return None
    return CHECK_TRANSLATIONS.get(constraint_name)


class CatalogueNotVisible(RuntimeError):
    """The role reading the catalogue cannot see the constraints of these tables.

    `information_schema.check_constraints` shows only constraints on tables owned
    by a currently enabled role, so the application role -- which owns nothing --
    reads an EMPTY list from a schema that declares fifty rules. An enumeration
    that accepted that answer would be an instrument measuring its own blindness
    and would pass forever. Raised instead, naming the gesture.
    """


def visible_check_constraints(conn) -> list[tuple[str, str]]:
    """Every CHECK of `ANALYZE_DOMAIN_TABLES` this connection can see.

    Read from `information_schema`, so it measures the database in front of it
    rather than a list somebody kept in step by hand.

    NOT-NULL IS EXCLUDED, AND ON PURPOSE. `information_schema.check_constraints`
    re-lists every `NOT NULL` column as a CHECK named `<oid>_<n>_not_null` with
    the clause `<column> IS NOT NULL`. Counting those would make this instrument
    measure column nullability -- which the driver already reports by name -- and
    would bury the rules this domain actually states. The exclusion matches that
    generated clause exactly, so a hand-written constraint can never be dropped
    by it.

    Returns `(table, constraint_name)` pairs, sorted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.table_name, tc.constraint_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.check_constraints cc
                ON cc.constraint_schema = tc.constraint_schema
               AND cc.constraint_name = tc.constraint_name
             WHERE tc.constraint_type = 'CHECK'
               AND tc.table_schema = %s
               AND tc.table_name = ANY(%s)
               AND cc.check_clause !~ '^\\(?[A-Za-z_][A-Za-z0-9_]* IS NOT NULL\\)?$'
             ORDER BY tc.table_name, tc.constraint_name
            """,
            (ANALYZE_DOMAIN_SCHEMA, list(ANALYZE_DOMAIN_TABLES)),
        )
        rows = cur.fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def untranslated_constraints(
    conn, *, translations: dict[str, CheckTranslation] | None = None
) -> list[tuple[str, str]]:
    """Every visible CHECK of the domain this map does not translate.

    `translations` exists so a test can prove the enumeration BITES: hand it the
    map minus one entry and the missing constraint comes back.
    """
    table = translations if translations is not None else CHECK_TRANSLATIONS
    visible = visible_check_constraints(conn)
    if not visible:
        raise CatalogueNotVisible(
            "no CHECK of the Analyze tables is visible to this connection: "
            "`information_schema` hides constraints on tables another role owns. "
            "Read the catalogue with the connection that owns the schema."
        )
    return [(table_name, name) for table_name, name in visible if name not in table]


def constraint_name_of(exc: Any) -> str | None:
    """The constraint a psycopg error names, or `None` when it names none."""
    diag = getattr(exc, "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None
