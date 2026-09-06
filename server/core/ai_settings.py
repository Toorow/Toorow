"""AI settings: ONE object per scope, ONE cascade, served to agents.

Story 75-4 (`epic-75-couche-semantique-amelioree-par-usage.md`), the code half of
the amendment ratified 2026-09-05 in `docs/product-architecture/context-hub.md`
-- *"AI settings: one object per scope, one cascade, served in the agent
envelope"*.

WHAT WAS MISSING, MEASURED BEFORE THIS FILE. The guidance an agent is meant to
obey exists and none of it is an object: `ai_context` per metric on
`app.metric_definitions`, procedure in the Skills, reach in the Answerable
Topics, facts in the context events. Nothing carried behaviour rules, a fiscal
calendar, a query scope or a narrative language for an organization or a
project -- so two agents reading the same Project answer in two registers and
count the year from two different Januaries, and no surface can correct either.

THE CASCADE IS `metric_semantics._SCOPE_RANK`, NOT A SECOND ONE. PLATFORM 0, ORG
1, PROJECT 2; the most specific scope that STATES a field wins that field. It is
resolved FIELD BY FIELD and not object by object, which is the whole difference
between "the project set its language" and "the project silently dropped the
organization's rules": a Project that states only `narrative_language` keeps the
organization's rules and the platform's calendar, and `sources` says so per
field.

CLEARING IS A VERSION, AND THERE IS NO DELETE HERE. `clear_settings` appends a
version marked `cleared`; that scope becomes TRANSPARENT and the resolution
falls through to its parent. The history then reads "the project overrode the
language on the 3rd and gave it back on the 5th", which is the sentence an audit
needs and a deletion destroys. Migration 346 makes the two halves inseparable in
the schema, so a convention cannot drift away from them.

THE SHIPPED DEFAULTS ARE CODE. `PLATFORM_DEFAULTS` below is a catalogue
delivered with the product, so it is not read from a table (CLAUDE.md, *"ce qui
est du code reste du code"*). A PLATFORM row exists only when a deployment
overrides them, and it overrides field by field like every other scope.

BCP 47 IS PARSED BY THE ONE PARSER. `core.language_dimensions.parse_language_tag`
already owns "is this a language tag, and what is its canonical form"; a second
list of language codes here would be a second answer free to disagree with the
first.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from ulid import ULID

from core.audit import declare_action, insert_audit_row

logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# The scopes. Same words, same rank, same rule as `metric_semantics._SCOPE_RANK`.
# ---------------------------------------------------------------------------
SCOPE_PLATFORM = "PLATFORM"
SCOPE_ORG = "ORG"
SCOPE_PROJECT = "PROJECT"
SCOPES = (SCOPE_PLATFORM, SCOPE_ORG, SCOPE_PROJECT)
_SCOPE_RANK = {SCOPE_PLATFORM: 0, SCOPE_ORG: 1, SCOPE_PROJECT: 2}

#: How far a model may reach for data -- the equivalent of Omni's
#: `query_all_views_and_fields`, narrowest first. The names are flagged
#: "to arbitrate (Jean)" in the amendment; the CHECK of migration 346 mirrors
#: this tuple so an unknown fourth cannot be stored while that is open.
QUERY_SCOPES = ("governed_views_only", "any_published_view", "any_field")

#: How the answer is written. Also to arbitrate (Jean).
NARRATIVE_REGISTERS = ("plain", "executive", "technical")

WEEK_START_DAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

#: The six fields the cascade resolves, in the order a panel reads them.
FIELDS = (
    "rules_always",
    "rules_never",
    "query_scope",
    "fiscal_calendar",
    "narrative_language",
    "narrative_register",
)

#: Bounds. A behaviour rule is a SENTENCE -- one imperative line -- and not a
#: paragraph smuggled into a list; the caps refuse the pathological payload by
#: name instead of letting it reach a prompt.
MAX_RULES = 40
MAX_RULE_CHARS = 280
MAX_NOTE_CHARS = 500

#: THE BLOCK RIDES EVERY AGENT ENVELOPE, SO IT IS CHARGED EVERY TIME.
#: `envelope_block` is written into `meta.ai_settings` by `core.main._envelope`,
#: and `model_channel.partition_envelope` never routes a `meta` honesty field to
#: the app channel -- the block is counted against
#: `model_channel.MODEL_CHANNEL_MAX_BYTES` (4096) on every single agent call and
#: nothing can move it out of the way. Measured 2026-09-06 with the count/length
#: caps above and nothing else: 40 rules of 280 characters in each of the two
#: lists serialize to 23029 bytes -- five times the WHOLE model channel. The
#: caps bounded one rule and one list's LENGTH; they never bounded the block.
#: This is that missing bound, and it is the block's declared share of the
#: channel. Measured at this value: 977 bytes for the worst payload the write
#: path accepts, asserted by `test_ai_settings_pg.py`
#: (`test_the_envelope_block_stays_inside_its_share_of_the_model_channel`).
ENVELOPE_BLOCK_MAX_BYTES = 1024

#: The serialized ceiling on ONE rule list, refused ON WRITE and by name. Two
#: lists, because ORG may state `rules_always` while PROJECT states
#: `rules_never` and the resolution then carries both: bounding the pair would
#: bound a payload nobody ever sends, and bounding each list is the only bound
#: that composes across the cascade. 300 bytes is one long rule, three of a
#: hundred characters, or six short imperatives -- and 2 * 300 plus the four
#: scalar fields and the `sources` map is the 977 above.
MAX_RULES_BYTES = 300

#: Delivered with the product. Never a row, never a per-deployment constant that
#: a project cannot override.
PLATFORM_DEFAULTS: dict[str, Any] = {
    "rules_always": [],
    "rules_never": [],
    "query_scope": "governed_views_only",
    "fiscal_calendar": {"year_start_month": 1, "week_start_day": "monday"},
    "narrative_language": "en",
    "narrative_register": "plain",
}



def platform_defaults() -> dict[str, Any]:
    """A caller's OWN copy of the shipped defaults, nested values included.

    `dict(PLATFORM_DEFAULTS)` copies the top level and shares the list and the
    calendar underneath: a reader that resolved a project, then appended a rule
    to `values["rules_always"]`, was appending to the constant every other
    request reads. The doors hand the same object to `json` and a handler that
    mutated it would poison the process. One deep copy, one place.
    """
    return copy.deepcopy(PLATFORM_DEFAULTS)


ACTION_AI_SETTINGS_SET = declare_action("ai_settings.set")
ACTION_AI_SETTINGS_CLEARED = declare_action("ai_settings.cleared")


class AiSettingsNotFound(LookupError):
    """The scope named by the caller does not exist (or is not theirs to see)."""


class AiSettingsRefused(ValueError):
    """A refusal that NAMES the field and the gesture that repairs it."""

    def __init__(self, code: str, message: str, *, field: str | None = None,
                 remedy: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.remedy = remedy

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "field": self.field,
            "remedy": self.remedy,
        }


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


# ---------------------------------------------------------------------------
# Validation. Every refusal names its field, so a panel can point at the row that
# is wrong instead of colouring the whole form red.
# ---------------------------------------------------------------------------


def _validated_rules(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise AiSettingsRefused(
            "rules_are_a_list",
            f"`{field}` is a list of short sentences, one rule per line",
            field=field,
            remedy="Write one imperative sentence per rule.",
        )
    if len(value) > MAX_RULES:
        raise AiSettingsRefused(
            "too_many_rules",
            f"`{field}` carries at most {MAX_RULES} rules ({len(value)} sent)",
            field=field,
            remedy="Keep the rules that change an answer; drop the rest.",
        )
    rules: list[str] = []
    for index, rule in enumerate(value):
        text = str(rule or "").strip()
        if not text or len(text) > MAX_RULE_CHARS:
            raise AiSettingsRefused(
                "rule_out_of_bounds",
                f"`{field}[{index}]` is 1..{MAX_RULE_CHARS} characters",
                field=field,
                remedy="One sentence per rule -- a paragraph belongs in the note.",
            )
        rules.append(text)
    size = len(json.dumps(rules, separators=(",", ":")).encode("utf-8"))
    if size > MAX_RULES_BYTES:
        raise AiSettingsRefused(
            "rules_over_envelope_budget",
            f"`{field}` serializes to {size} bytes, over the {MAX_RULES_BYTES} bytes "
            f"this list may take from the model's context on every agent call",
            field=field,
            remedy="Keep the rules that change an answer, and shorten them.",
        )
    return rules


def _validated_query_scope(value: Any) -> str:
    text = str(value or "").strip()
    if text not in QUERY_SCOPES:
        raise AiSettingsRefused(
            "unknown_query_scope",
            f"`query_scope` is one of {', '.join(QUERY_SCOPES)} (got `{text or 'nothing'}`)",
            field="query_scope",
            remedy="Choose how far a model may reach: governed views only is the narrowest.",
        )
    return text


def _validated_fiscal_calendar(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AiSettingsRefused(
            "fiscal_calendar_shape",
            "`fiscal_calendar` states a year start month and a week start day",
            field="fiscal_calendar",
            remedy="Send {year_start_month, week_start_day}.",
        )
    unknown = sorted(set(value) - {"year_start_month", "week_start_day"})
    if unknown:
        raise AiSettingsRefused(
            "unknown_fiscal_calendar_key",
            f"`fiscal_calendar` does not carry {', '.join(unknown)}",
            field="fiscal_calendar",
            remedy="Only year_start_month and week_start_day are settings of a calendar here.",
        )
    calendar: dict[str, Any] = {}
    if "year_start_month" in value:
        raw = value["year_start_month"]
        # `bool` is an `int` in Python, and `True` would silently become month 1.
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 12:
            raise AiSettingsRefused(
                "fiscal_month_out_of_range",
                f"`fiscal_calendar.year_start_month` is a month 1..12 (got `{raw}`)",
                field="fiscal_calendar",
                remedy="Name the month the fiscal year opens on, 1 for January.",
            )
        calendar["year_start_month"] = raw
    if "week_start_day" in value:
        day = str(value["week_start_day"] or "").strip().lower()
        if day not in WEEK_START_DAYS:
            raise AiSettingsRefused(
                "unknown_week_start_day",
                f"`fiscal_calendar.week_start_day` is a day of the week (got `{day}`)",
                field="fiscal_calendar",
                remedy="Use a day name in English, monday..sunday.",
            )
        calendar["week_start_day"] = day
    if not calendar:
        raise AiSettingsRefused(
            "empty_fiscal_calendar",
            "`fiscal_calendar` states at least a year start month or a week start day",
            field="fiscal_calendar",
            remedy="Set the month the fiscal year opens on, or clear this scope.",
        )
    # A CALENDAR IS STATED WHOLE, and a partial one is refused BY NAME.
    #
    # The cascade resolves FIELD by field, and `fiscal_calendar` is one field:
    # a scope that stated only `week_start_day` used to replace the parent's
    # calendar entire, so the fiscal year silently fell back to January while
    # the panel said "Set here". The other repair -- merging key by key -- would
    # give one field two sources, which no `sources` map and no badge can say.
    # So the object is atomic: both keys, or a sentence naming the missing one.
    missing = sorted({"year_start_month", "week_start_day"} - set(calendar))
    if missing:
        raise AiSettingsRefused(
            "partial_fiscal_calendar",
            "`fiscal_calendar` is stated whole: the month the fiscal year opens on "
            f"AND the day the week starts on (missing {', '.join(missing)})",
            field="fiscal_calendar",
            remedy="Send both year_start_month and week_start_day, or return this "
                   "scope's calendar to its parent.",
        )
    return calendar


def _validated_language(value: Any) -> str:
    """The ONE parser, never a second list of language codes."""
    from core.language_dimensions import parse_language_tag  # noqa: PLC0415

    tag = parse_language_tag(value)
    if tag is None:
        raise AiSettingsRefused(
            "unknown_language",
            f"`narrative_language` is a BCP 47 tag such as fr-FR (got `{value}`)",
            field="narrative_language",
            remedy="Write the language, and the region when it matters: en, en-GB, fr-FR.",
        )
    return tag.canonical


def _validated_register(value: Any) -> str:
    text = str(value or "").strip()
    if text not in NARRATIVE_REGISTERS:
        raise AiSettingsRefused(
            "unknown_register",
            f"`narrative_register` is one of {', '.join(NARRATIVE_REGISTERS)}"
            f" (got `{text or 'nothing'}`)",
            field="narrative_register",
            remedy="Choose how an answer is written for this scope.",
        )
    return text


def _validated_payload(payload: Any) -> dict[str, Any]:
    """The fields THIS scope overrides -- and nothing else.

    A PUT states the whole override: a field the payload does not carry (or
    carries as null) is NOT set at this scope, and the cascade falls through for
    it. That is the same act as clearing that one field, and it is why there is
    no per-field delete: the override IS the payload.
    """
    if not isinstance(payload, dict):
        raise AiSettingsRefused(
            "payload_shape", "AI settings are sent as an object", field=None,
            remedy="Send a JSON object naming the fields this scope sets.",
        )
    unknown = sorted(set(payload) - set(FIELDS) - {"note"})
    if unknown:
        raise AiSettingsRefused(
            "unknown_field",
            f"unknown AI setting: {', '.join(unknown)}",
            field=unknown[0],
            remedy=f"The settings are {', '.join(FIELDS)}.",
        )
    validators = {
        "rules_always": lambda v: _validated_rules(v, "rules_always"),
        "rules_never": lambda v: _validated_rules(v, "rules_never"),
        "query_scope": _validated_query_scope,
        "fiscal_calendar": _validated_fiscal_calendar,
        "narrative_language": _validated_language,
        "narrative_register": _validated_register,
    }
    stated: dict[str, Any] = {}
    for field in FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        stated[field] = validators[field](value)
    if not stated:
        raise AiSettingsRefused(
            "states_nothing",
            "this would set nothing at this scope",
            field=None,
            remedy="Set at least one field, or return this scope to its parent.",
        )
    return stated


def _validated_note(note: Any) -> str | None:
    text = str(note or "").strip()
    if not text:
        return None
    if len(text) > MAX_NOTE_CHARS:
        raise AiSettingsRefused(
            "note_too_long",
            f"the note is at most {MAX_NOTE_CHARS} characters ({len(text)} sent)",
            field="note",
            remedy="Say why in one or two sentences.",
        )
    return text


def _validated_scope(scope: Any, scope_id: Any, org_id: Any) -> tuple[str, str | None, str | None]:
    """(scope_level, org_id, project_id) -- the triplet migration 346 stores.

    `scope_id` is the caller's vocabulary (the org for ORG, the project for
    PROJECT); the triplet is the schema's, because a bare id can carry no
    foreign key and the erasure walk follows foreign keys.
    """
    level = str(scope or "").strip().upper()
    if level not in SCOPES:
        raise AiSettingsRefused(
            "unknown_scope",
            f"a scope is one of {', '.join(SCOPES)} (got `{scope}`)",
            field="scope",
        )
    if level == SCOPE_PLATFORM:
        return level, None, None
    if level == SCOPE_ORG:
        target = str(scope_id or org_id or "").strip()
        if not target:
            raise AiSettingsRefused(
                "missing_scope_id", "an ORG scope names its organization", field="scope_id"
            )
        return level, target, None
    project_id = str(scope_id or "").strip()
    owner = str(org_id or "").strip()
    if not project_id or not owner:
        raise AiSettingsRefused(
            "missing_scope_id",
            "a PROJECT scope names its project and the organization that owns it",
            field="scope_id",
        )
    return level, owner, project_id


# ---------------------------------------------------------------------------
# Reading: the cascade.
# ---------------------------------------------------------------------------

#: The columns of one version, in the order `_version_row` reads them. Named once
#: so the four readers below cannot drift into four different SELECT lists.
_VERSION_COLUMNS = (
    "id", "ai_settings_id", "version_number", "cleared", "rules_always", "rules_never",
    "query_scope", "fiscal_calendar", "narrative_language", "narrative_register", "note",
    "created_by", "created_at",
)


def _columns(prefix: str = "") -> str:
    return ", ".join(f"{prefix}{name}" for name in _VERSION_COLUMNS)


def _version_row(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "ai_settings_id": row[1],
        "version_number": row[2],
        "cleared": bool(row[3]),
        "rules_always": row[4],
        "rules_never": row[5],
        "query_scope": row[6],
        "fiscal_calendar": row[7],
        "narrative_language": row[8],
        "narrative_register": row[9],
        "note": row[10],
        "created_by": row[11],
        "created_at": row[12].isoformat() if row[12] else None,
    }


def _scope_overrides(conn, *, org_id: str | None, project_id: str | None) -> dict[str, dict]:
    """{scope_level -> its current version}, for the three scopes in play.

    ONE query, not three: a cascade read three times is a cascade that can be
    read three different ways when a write lands between two of them.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.scope_level, {_columns("v.")}
            FROM app.ai_settings s
            JOIN app.ai_setting_versions v ON v.id = s.current_version_id
            WHERE s.scope_level = 'PLATFORM'
               OR (s.scope_level = 'ORG' AND s.org_id = %s)
               OR (s.scope_level = 'PROJECT' AND s.project_id = %s)
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall()
    return {row[0]: _version_row(row[1:]) for row in rows}


def resolve(conn, *, org_id: str | None, project_id: str | None) -> dict[str, Any]:
    """The resolved settings and, per field, the scope the value came from.

    Returns ``{"values": {field: value}, "sources": {field: "PLATFORM"|"ORG"|"PROJECT"}}``.

    FIELD BY FIELD, and the walk goes from the least specific to the most: each
    scope that STATES a field overwrites it, a scope that does not leaves it
    alone, and a `cleared` version states nothing at all -- which is exactly what
    makes it transparent. `PLATFORM` is the source of a field nobody overrode,
    whether the value is the shipped default or a platform row's override; a
    reader is told "the platform decided this", which is true either way.
    """
    overrides = _scope_overrides(conn, org_id=org_id, project_id=project_id)
    values: dict[str, Any] = platform_defaults()
    sources: dict[str, str] = dict.fromkeys(FIELDS, SCOPE_PLATFORM)
    for scope in sorted(overrides, key=lambda s: _SCOPE_RANK.get(s, -1)):
        version = overrides[scope]
        if version["cleared"]:
            continue
        for field in FIELDS:
            stated = version[field]
            if stated is None:
                continue
            values[field] = stated
            sources[field] = scope
    return {"values": values, "sources": sources}


def read_scope(conn, *, scope: str, scope_id: str | None,
               org_id: str | None = None) -> dict[str, Any] | None:
    """The override a single scope states today, or None when it states nothing.

    `None` covers both "no object here" and "its current version is a clearing":
    a panel asking "is anything set here" gets one answer, not two shapes of
    nothing.
    """
    level, owner, project = _validated_scope(scope, scope_id, org_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_columns("v.")}
            FROM app.ai_settings s
            JOIN app.ai_setting_versions v ON v.id = s.current_version_id
            WHERE s.scope_level = %s
              AND s.org_id IS NOT DISTINCT FROM %s
              AND s.project_id IS NOT DISTINCT FROM %s
            """,
            (level, owner, project),
        )
        row = cur.fetchone()
    if row is None:
        return None
    version = _version_row(row)
    if version["cleared"]:
        return None
    return version


def history(conn, *, scope: str, scope_id: str | None,
            org_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Every version of one scope, newest first -- clearings included.

    A clearing is in the list because it IS the history: "gave the language back
    to the organization on the 5th" is the line a deletion would have erased.
    """
    level, owner, project = _validated_scope(scope, scope_id, org_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_columns("v.")}
            FROM app.ai_setting_versions v
            JOIN app.ai_settings s ON s.id = v.ai_settings_id
            WHERE s.scope_level = %s
              AND s.org_id IS NOT DISTINCT FROM %s
              AND s.project_id IS NOT DISTINCT FROM %s
            ORDER BY v.version_number DESC
            LIMIT %s
            """,
            (level, owner, project, int(limit)),
        )
        return [_version_row(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Writing: a version is appended and the head's pointer moves, in one act.
# ---------------------------------------------------------------------------


def project_org_id(conn, project_id: str) -> str | None:
    """The organization a Project belongs to, or None when the Project is unknown.

    IT LIVES HERE AND NOT IN THE DOOR. `module-boundaries.md` criterion 4: a
    `*_api.py` module parses, authorizes and calls -- it issues no SQL of its
    own (`scripts/api_sql_census.py --gate` enforces it). The door needs the
    owning organization to name the scope, so the service answers it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


def _refuse_foreign_project(conn, *, owner: str | None, project: str | None) -> None:
    """A PROJECT scope names a project of THAT organization, or it is refused BY NAME.

    The composite foreign key of migration 346 already makes the row impossible;
    this reads the same fact first so a caller gets a sentence naming the
    organization instead of a psycopg traceback about a constraint. Belt and
    floor: the check is the message, the constraint is the guarantee.
    """
    if not project:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.projects WHERE id = %s AND org_id = %s", (project, owner)
        )
        if cur.fetchone() is None:
            raise AiSettingsRefused(
                "project_not_in_organization",
                "this project does not belong to that organization",
                field="scope_id",
                remedy="Set the AI settings from the organization that owns the project.",
            )


def _head(conn, *, level: str, owner: str | None, project: str | None,
          actor: str, create: bool) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, current_version_id FROM app.ai_settings
            WHERE scope_level = %s
              AND org_id IS NOT DISTINCT FROM %s
              AND project_id IS NOT DISTINCT FROM %s
            FOR UPDATE
            """,
            (level, owner, project),
        )
        row = cur.fetchone()
        if row is not None:
            return {"id": row[0], "current_version_id": row[1]}
        if not create:
            return None
        head_id = _uid("aiset")
        cur.execute(
            """
            INSERT INTO app.ai_settings
                (id, scope_level, org_id, project_id, created_by)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (head_id, level, owner, project, actor),
        )
    return {"id": head_id, "current_version_id": None}


def _append_version(conn, *, head_id: str, owner: str | None, project: str | None,
                    stated: dict[str, Any], cleared: bool, note: str | None,
                    actor: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.ai_setting_versions "
            "WHERE ai_settings_id = %s",
            (head_id,),
        )
        version_number = int(cur.fetchone()[0])
        version_id = _uid("aisetv")
        cur.execute(
            """
            INSERT INTO app.ai_setting_versions
                (id, ai_settings_id, org_id, project_id, version_number, cleared,
                 rules_always, rules_never, query_scope, fiscal_calendar,
                 narrative_language, narrative_register, note, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version_id, head_id, owner, project, version_number, cleared,
                _as_json(stated.get("rules_always")),
                _as_json(stated.get("rules_never")),
                stated.get("query_scope"),
                _as_json(stated.get("fiscal_calendar")),
                stated.get("narrative_language"),
                stated.get("narrative_register"),
                note, actor,
            ),
        )
        cur.execute(
            "UPDATE app.ai_settings SET current_version_id = %s, updated_at = NOW() "
            "WHERE id = %s",
            (version_id, head_id),
        )
        cur.execute(
            f"SELECT {_columns()} FROM app.ai_setting_versions WHERE id = %s",
            (version_id,),
        )
        return _version_row(cur.fetchone())


def _as_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def set_settings(conn, *, scope: str, scope_id: str | None, org_id: str | None,
                 payload: Any, actor: str, note: Any = None) -> dict[str, Any]:
    """Append a version stating this scope's override, and move the head to it.

    ONE transaction: the version and the pointer commit together or neither
    does -- a head pointing at nothing would make the scope silently transparent,
    which is the one failure this object must not have.
    """
    level, owner, project = _validated_scope(scope, scope_id, org_id)
    stated = _validated_payload(payload)
    validated_note = _validated_note(note if note is not None else
                                     (payload.get("note") if isinstance(payload, dict) else None))
    _refuse_foreign_project(conn, owner=owner, project=project)
    head = _head(conn, level=level, owner=owner, project=project, actor=actor, create=True)
    version = _append_version(
        conn, head_id=head["id"], owner=owner, project=project,
        stated=stated, cleared=False, note=validated_note, actor=actor,
    )
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_AI_SETTINGS_SET,
        provider_account="",
        connection_ref="",
        metadata={
            "scope": level,
            "org_id": owner,
            "project_id": project,
            "ai_settings_id": head["id"],
            "version_id": version["id"],
            "version_number": version["version_number"],
            "fields": sorted(stated),
        },
    )
    return version


def clear_settings(conn, *, scope: str, scope_id: str | None, org_id: str | None,
                   actor: str, note: Any = None) -> dict[str, Any]:
    """Return this scope to its parent -- by APPENDING a version, never a DELETE.

    Refused by name when there is nothing to give back: a scope that states
    nothing is already inherited, and an action that undoes nothing must not
    pretend it did something.
    """
    level, owner, project = _validated_scope(scope, scope_id, org_id)
    if level == SCOPE_PLATFORM:
        raise AiSettingsRefused(
            "platform_has_no_parent",
            "the platform scope has no parent to return to",
            field="scope",
            remedy="Set the platform values, or override them at the organization.",
        )
    validated_note = _validated_note(note)
    _refuse_foreign_project(conn, owner=owner, project=project)
    head = _head(conn, level=level, owner=owner, project=project, actor=actor, create=False)
    if head is None or head["current_version_id"] is None:
        raise AiSettingsRefused(
            "nothing_to_clear",
            "nothing is set at this scope, so there is nothing to give back",
            field="scope",
            remedy="This scope already inherits every value from its parent.",
        )
    current = read_scope(conn, scope=level, scope_id=scope_id, org_id=org_id)
    if current is None:
        raise AiSettingsRefused(
            "nothing_to_clear",
            "nothing is set at this scope, so there is nothing to give back",
            field="scope",
            remedy="This scope already inherits every value from its parent.",
        )
    version = _append_version(
        conn, head_id=head["id"], owner=owner, project=project,
        stated={}, cleared=True, note=validated_note, actor=actor,
    )
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_AI_SETTINGS_CLEARED,
        provider_account="",
        connection_ref="",
        metadata={
            "scope": level,
            "org_id": owner,
            "project_id": project,
            "ai_settings_id": head["id"],
            "version_id": version["id"],
            "version_number": version["version_number"],
        },
    )
    return version


# ---------------------------------------------------------------------------
# The agent envelope. ADDITIVE, and honest when it cannot read.
# ---------------------------------------------------------------------------


def envelope_block(conn, org_id: str | None, project_id: str | None) -> dict[str, Any] | None:
    """The `meta.ai_settings` block: resolved values AND the scope each came from.

    A model is told the rule and told who set it, so "why did you answer in
    English" has an answer that names a scope instead of a guess.

    RETURNS None RATHER THAN THE DEFAULTS when the store cannot be read. Serving
    `PLATFORM_DEFAULTS` under a `sources` map would tell a caller that someone
    chose those values; nobody did, and a block that cannot be read is honestly
    absent. `_envelope` stays additive either way (AD-1).
    """
    try:
        resolved = resolve(conn, org_id=org_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- an envelope never breaks its payload
        logger.warning("ai_settings: envelope block unavailable: %s", exc)
        return None
    return {"values": resolved["values"], "sources": resolved["sources"]}


def envelope_block_for_project(conn, project_id: str | None) -> dict[str, Any] | None:
    """`envelope_block` for a caller that knows the project and not its org.

    The agent surface resolves a project from a token; the ORG layer of the
    cascade would be invisible if the org were not derived first, and an
    envelope that silently skipped a scope would misname the source of a value.
    """
    if not project_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- an envelope never breaks its payload
        logger.warning("ai_settings: org of project unavailable: %s", exc)
        return None
    if not row or not row[0]:
        # THE PROJECT DOES NOT EXIST -- so nobody set anything for it, and the
        # block is absent rather than the shipped defaults dressed as PLATFORM.
        # Serving them would tell the caller "the platform decided this for your
        # project" about a project that is not there.
        logger.warning("ai_settings: no project %s, envelope block omitted", project_id)
        return None
    return envelope_block(conn, str(row[0]), project_id)
