"""Story 50.3 -- Reports, Notebooks and Renders as three distinct lifecycle objects.

WHAT THIS OWNS. Three objects the current code conflates, and nothing else:

  * a **Report** versions reusable analytical intent -- one exact Query Spec
    version plus one default presentation intent. Running it is EXECUTION, never
    mutation: the Report is not a cached answer (`analyze-and-test.md:50`).
  * a **Notebook** versions a rerunnable composition of ordered, version-pinned
    blocks. Each **Notebook Run** pins the exact Notebook version and, per block,
    the exact Result and the exact Render -- or the spelled-out literal that says
    the block was intentionally not rendered.
  * a **Render** is an immutable presentation snapshot over one exact Result with
    the complete replay contract.

WHAT IT REFUSES TO BE. `app.project_reports` is connector-seed ENABLEMENT and is
read here only as availability. `app.notebooks` / `app.notebook_runs` are the
legacy mutable model and are read here only as classified legacy evidence.
`app.render_snapshots` is a frozen envelope with a widget URI and is never
promoted into `app.renders` -- it pins none of the ten replay inputs, and
labelling it canonical would be the fabrication AC12 forbids.

THE CONTRACT GATE, and what it actually refuses. Stories 50.4 (Visualization
Spec) and 50.5 (renderer registry and shared runtime) own six of the pins AC8
requires, and BOTH have landed: `app.visualization_spec_versions` (migration
156) and `app.renderer_runtime_builds` (migration 160). Re-measured 2026-08-17
on a migrated database (278 migrations applied): `render_contract_state` returns
`available: true` with `missing: []`, and Renders are written for real -- see the
INSERT in `create_render`, and `test_multi_source_visualization_pg.py` which
freezes one and reads every replay pin back.

This paragraph said the opposite until 2026-08-17 ("no Render is ever written; an
empty `app.renders` is a true statement about this repository"). It had been
false since migrations 156/160, and a reader who trusted it would conclude the
Render surface does not exist and re-plan delivered work.

What still refuses, and it is not the registries: `create_render` demands the ten
replay pins one by one and rejects placeholder words. It does not write a Render
with `visualization_spec_version_id = 'current'`, it does not omit the column, and
it does not invent a build id. The registry branch is KEPT because an unmigrated
deployment is a real state -- but it no longer fires here. The gate that binds in
practice is `registered_families`: a family nothing has registered cannot name its
own build, so its Render is refused pin by pin instead. That list is answered with
a STATE (`read` / `contract_unavailable` / `unreadable`), because a ledger the
service failed to read is not a ledger holding nothing, and flattening the two
into `[]` asserted an absence the database never stated.

NON-DISCLOSURE IS THE DEFAULT. Foreign, denied and absent identities raise the
same `ArtifactNotFound`, exactly as Story 50.1 does, so a caller cannot use this
service as an existence oracle for another Project's objects.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ulid import ULID

from core.analyze_check_refusals import constraint_name_of, translate

logger = logging.getLogger(__name__)

#: Bumped when a canonical serialization changes shape. A stored `content_hash`
#: is only comparable to another produced by the same contract version, so the
#: version travels inside the hashed document -- the same rule Story 50.1 applies.
REPORT_CONTRACT_VERSION = "analysis-report.v1"
NOTEBOOK_CONTRACT_VERSION = "analysis-notebook.v1"
RENDER_CONTRACT_VERSION = "render.v1"

#: The exact literal a Report version or block carries when no accepted
#: presentation contract exists. Spelled here and in migration 154's CHECK, so
#: the two cannot drift and no caller can invent a softer wording.
NO_PRESENTATION_CONTRACT = "No accepted presentation contract"

#: The four exact literals a Notebook Run block may carry instead of a Render.
#: Spelled in migration 154's CHECK (made NULL-proof by 155, extended by 158), so
#: no caller can invent a softer wording and no branch can leave the pair empty.
NO_RENDER = "No Render"
NO_RENDER_BLOCK_FAILED = "No Render: block failed"
NO_RENDER_NO_PRESENTATION = "No Render: no accepted presentation contract"
#: The fourth case, added by migration 158. The block pins an ACCEPTED
#: presentation and both downstream registries exist, so a Render is genuinely
#: owed -- and a server-side Notebook Run does not mint one. Saying `No Render`
#: here would claim the block was intentionally non-rendered; saying "no accepted
#: presentation contract" would deny a contract that is present.
NO_RENDER_NOT_DISPATCHED = "No Render: rendering is not dispatched by a Notebook Run"

#: Why that literal is true, recorded on the block so a reader does not have to
#: guess which of the two "no Render" worlds they are in.
RENDER_NOT_DISPATCHED_LIMITATION = (
    "this block pins an accepted presentation, so a Render is owed; minting one "
    "from a server-side Run needs the renderer/runtime build identities and the "
    "render dispatch owned by Story 50.5"
)

#: The registries Stories 50.4 and 50.5 own. Presence is probed rather than
#: assumed, so the day either lands this service starts accepting Renders without
#: an edit here -- and until then it names exactly what is missing.
_VISUALIZATION_SPEC_TABLE = "app.visualization_spec_versions"
_RENDERER_REGISTRY_TABLE = "app.renderer_runtime_builds"

#: THE THIRD REGISTRY, AND THE ONLY ONE THAT DOES NOT EXIST -- probed on exactly
#: the same terms as the two above, and for the same reason: the day story 72.1
#: creates it, this module starts accepting the kind without an edit here.
#:
#: Until 2026-08-31 the kind `visualization_template_version` was ACCEPTED by
#: `resolve_presentation` while nothing anywhere could resolve it: migrations 154
#: and 155 carry the word in a CHECK, no table was ever created for it, and
#: `analysis_report_versions.presentation_version_id` has no foreign key. So a
#: Report version could be written pinning a Chart Template version that no read
#: will ever find -- the fabricated pin this function's own docstring says it
#: refuses. It refused it for the Spec and never for the Template.
_CHART_TEMPLATE_TABLE = "app.visualization_template_versions"

#: Which registry answers which presentation kind. One mapping, so the probe, the
#: refusal and the state a screen renders cannot disagree about what is missing.
#: The wire token stays `visualization_template_version` -- it is written into
#: migration 154's CHECK, and a stored token is not a product word; the product
#: noun is **Chart Template** (glossary, ratified 2026-08-31).
_PRESENTATION_KIND_REGISTRY: dict[str, tuple[str, str, str, str]] = {
    # kind -> (contract name, owning story, the table that resolves it, product noun)
    "visualization_spec_version": (
        "visualization_spec_version",
        "50.4",
        _VISUALIZATION_SPEC_TABLE,
        "Visualization Spec version",
    ),
    "visualization_template_version": (
        "chart_template_version",
        "72.1",
        _CHART_TEMPLATE_TABLE,
        # The product noun, never the wire token. `visualization_template_version`
        # is what migration 154 stores; `Chart Template` is what a person reads
        # (glossary, ratified 2026-08-31).
        "Chart Template version",
    ),
}

#: The one sentence a person reads when they aim a pin at the Chart Template. It
#: is a CONSTANT and carries no identifier: what is missing is an object of this
#: deployment, never the value the caller typed.
CHART_TEMPLATE_PIN_UNAVAILABLE = (
    "a Chart Template version cannot be pinned in this deployment yet, so pin a "
    "Visualization Spec version instead"
)

#: WHAT `registered_families` IS WHEN IT IS NOT A LIST OF FAMILIES. A read that
#: could not answer is not an answer of "nothing"
#: (`visualization-and-rendering.md:209`, `analyze-and-test.md:1648`): three
#: distinguishable states, so a caller can never conclude "this deployment
#: registers no renderer" from a ledger it failed to read.
FAMILIES_READ = "read"
FAMILIES_CONTRACT_UNAVAILABLE = "contract_unavailable"
FAMILIES_UNREADABLE = "unreadable"

#: What a caller is told when the ledger could not be read. A refusal names the
#: gesture that repairs it, and never the driver's words
#: (`first-figure-path.md:141-151`).
LEDGER_UNREADABLE_REASON = (
    "Which renderer builds this deployment registers could not be read just now, "
    "so it is unknown rather than none. Re-run this request in a moment, and if "
    "it keeps failing ask an administrator to read the server log for this "
    "deployment."
)

#: The ten replay pins of `visualization-and-rendering.md:318-322`, in that order,
#: mapped to the payload field that must supply each. Used by `create_render` to
#: refuse an incomplete request pin by pin rather than with one banner.
RENDER_REPLAY_PINS: tuple[tuple[str, str], ...] = (
    ("result_identity", "result_id"),
    ("retained_result_data", "result_content_hash"),
    ("visualization_spec_version", "visualization_spec_version_id"),
    ("renderer_build", "renderer_build_id"),
    ("runtime_build", "runtime_build_id"),
    ("theme_version", "theme_version"),
    ("formatter_version", "formatter_version"),
    ("responsive_profile", "responsive_profile"),
    ("local_display_state", "display_state"),
    ("evidence_manifest", "evidence_manifest"),
)

#: Words migration 154 refuses in any pin, repeated here so the service can say
#: WHY before the database does. The database remains the authority.
_FORBIDDEN_PIN_VALUES = frozenset({"legacy", "current", "deferred", "latest", "unknown", "none"})

_BLOCK_TYPES = frozenset({"query", "report", "narrative"})
_AS_OF_RULES = frozenset({"current", "as_of"})
_MAX_BLOCKS = 100


class ArtifactNotFound(LookupError):
    """The object is not in the authorized Project -- foreign, denied or absent.

    One exception for all three. Which of the three it was belongs in audit, not
    in a response body (AC13).
    """


class LedgerUnreadable(RuntimeError):
    """A read of the build ledger failed, and its result is therefore unknown.

    Raised rather than swallowed into an empty list. The one caller catches it and
    turns it into a NAMED state on the contract -- so the fact travels instead of
    being flattened into an assertion the ledger never made.
    """


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    subject: str | None = None
    #: One action the reader can take. `message` states the fact; this states the
    #: move. Added 2026-08-31 (story 72.1 AC3), which asks for the code, the
    #: subject AND the remedy -- the shape `VisualizationRefusal`
    #: (`visualization_specs.py:136-157`) has carried since Story 50.4. Optional,
    #: so every refusal already written here keeps its exact meaning; a refusal
    #: about a pin a person must repair carries one.
    remedy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "subject": self.subject,
            "remedy": self.remedy,
        }


class ArtifactRefused(ValueError):
    """A structured refusal carrying every reason, not just the first."""

    def __init__(self, code: str, message: str, refusals: list[Refusal] | None = None):
        super().__init__(message)
        self.code = code
        self.refusals = list(refusals or [])

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "refusals": [r.as_dict() for r in self.refusals],
        }


# ---------------------------------------------------------------------------
# A CHECK is a rule of this product, so its violation is a REFUSAL and never a
# mute 500 (AI-357, measured 2026-09-01 on `POST /renders`).
#
# The class, not the instance: every write of this module is wrapped, so no
# constraint of `analyze_check_refusals.ANALYZE_DOMAIN_TABLES` can reach a caller
# as a `psycopg` traceback -- and a test enumerates the catalogue and fails the
# day a migration adds a CHECK nobody translated.
# ---------------------------------------------------------------------------

#: PostgreSQL's class 23 code for a CHECK violation. Compared rather than caught
#: by type, so this module does not import the driver to name one exception.
_CHECK_VIOLATION_SQLSTATE = "23514"

#: What a caller is told when a CHECK with no translation refused the write. It
#: names a gesture and never the constraint: the constraint name is a fact about
#: the database, it goes to the log, and a person cannot act on it.
UNNAMED_RULE_REASON = (
    "This was refused by a rule of this deployment the server could not put into "
    "words. Send the request again unchanged, and if it is refused a second time "
    "ask an administrator to read the server log for this deployment."
)


def _as_named_refusal(exc: BaseException) -> ArtifactRefused | None:
    """Turn a CHECK violation into a named refusal, or return `None`.

    `None` means "this is not a CHECK violation" -- the caller re-raises, because
    swallowing an unrelated database error into a refusal would tell a person to
    repair a request that was never the problem.
    """
    if getattr(exc, "sqlstate", None) != _CHECK_VIOLATION_SQLSTATE:
        return None
    constraint = constraint_name_of(exc)
    known = translate(constraint)
    if known is None:
        logger.error(
            "analyze_artifacts: untranslated check violation on %s -- add it to "
            "core.analyze_check_refusals.CHECK_TRANSLATIONS",
            constraint,
        )
        return ArtifactRefused(
            "refused_by_an_unnamed_rule",
            UNNAMED_RULE_REASON,
            [Refusal("unnamed_rule", UNNAMED_RULE_REASON, None, remedy=UNNAMED_RULE_REASON)],
        )
    return ArtifactRefused(
        known.code,
        known.message,
        [Refusal(known.code, known.message, known.subject, remedy=known.remedy)],
    )


def _names_its_check_violations(fn):
    """Every public write of this module wears this. One line, one whole class."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ArtifactRefused:
            raise
        except Exception as exc:
            refused = _as_named_refusal(exc)
            if refused is None:
                raise
            raise refused from exc

    return wrapper


# ---------------------------------------------------------------------------
# Canonical serialization. Identical to Story 50.1's, deliberately: two content
# hashes produced by two modules that disagree on key order would make "the old
# evidence is byte-stable" impossible to assert across objects.
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ArtifactRefused("invalid_body", "the document must be JSON serializable") from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


# ---------------------------------------------------------------------------
# The presentation contract, and the render contract.
#
# Both answer the same question -- "does an accepted downstream identity exist?"
# -- by probing the catalogue rather than by trusting a constant. A constant here
# would be a second source of truth about whether Story 50.4 has landed.
# ---------------------------------------------------------------------------


def _table_exists(conn, qualified: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,))
        return bool(cur.fetchone()[0])


def render_contract_state(conn) -> dict[str, Any]:
    """Report which downstream identity registries exist, and which do not.

    This is the whole gate of AC8 in one function. It is a READ: it never creates
    a placeholder registry so the rest of the story can pretend to work.

    Returns `available`, `missing`, `registered_families`, and
    `registered_families_state` -- one of `read`, `contract_unavailable`,
    `unreadable`. The state is what makes an empty list legible: only under `read`
    does `[]` assert that nothing is registered. Under `unreadable` the answer also
    carries `registered_families_unavailable_reason`, a sentence naming the gesture.

    AND `unpinnable_kinds`, ADDED 2026-08-31 AS A SEPARATE FACT ON PURPOSE.
    `available` answers "can this deployment carry a preserved artifact at all",
    and the Spec and the renderer ledger are what decide that -- both shipped. The
    Chart Template is a THIRD registry that does not exist, and folding it into
    `missing` would flip `available` to false and start refusing the Spec pin the
    deployment serves perfectly well. So it is its own list: every presentation
    kind this deployment cannot resolve, with the story that owns it and the table
    that would. It is what lets a screen name the object with no table instead of
    offering it and letting the server refuse afterwards.
    """
    missing: list[dict[str, str]] = []
    if not _table_exists(conn, _VISUALIZATION_SPEC_TABLE):
        missing.append(
            {
                "contract": "visualization_spec_version",
                "owner_story": "50.4",
                "missing_link": _VISUALIZATION_SPEC_TABLE,
            }
        )
    if not _table_exists(conn, _RENDERER_REGISTRY_TABLE):
        missing.append(
            {
                "contract": "renderer_and_runtime_build",
                "owner_story": "50.5",
                "missing_link": _RENDERER_REGISTRY_TABLE,
            }
        )
    #  WHICH FAMILIES CAN ACTUALLY BE PINNED, and not merely whether the ledger
    #  exists. `available` answered "the table is there" and stayed true while the
    #  table held ONE row for eight shipped families -- so a caller was told the
    #  contract was available and then refused, pin by pin, for a family nothing
    #  had registered. The two facts are different, and only the second tells a
    #  client whether the Render it is about to freeze can name its own build.
    #
    #  AND WHICH OF THE THREE ANSWERS THIS IS. `registered_families: []` used to
    #  mean three different things at once -- the ledger holds nothing, the ledger
    #  is not there yet, the ledger could not be read -- and a caller that reads it
    #  as "this build registers no family" would refuse a Render the deployment can
    #  perfectly well pin. The state says which one it is, in a field of its own.
    if missing:
        families: list[dict[str, str]] = []
        families_state = FAMILIES_CONTRACT_UNAVAILABLE
    else:
        try:
            families = _registered_families(conn)
            families_state = FAMILIES_READ
        except LedgerUnreadable as exc:
            logger.error("analyze_artifacts: renderer_build_ledger_unreadable: %s", exc)
            families = []
            families_state = FAMILIES_UNREADABLE
    unpinnable, pinnable = _presentation_kind_state(conn)
    state: dict[str, Any] = {
        "available": not missing,
        "missing": missing,
        "registered_families": families,
        "registered_families_state": families_state,
        "pinnable_kinds": pinnable,
        "unpinnable_kinds": unpinnable,
    }
    if families_state == FAMILIES_UNREADABLE:
        state["registered_families_unavailable_reason"] = LEDGER_UNREADABLE_REASON
    return state


def _presentation_kind_state(conn) -> tuple[list[dict[str, str]], list[str]]:
    """Which presentation kinds can be pinned here, and which cannot -- and why.

    Derived from the registry map by PROBING, never from a constant: a hard-coded
    "the Template is not built" would be a second authority about whether story
    72.1 has landed, and it would keep saying so the day after it lands.
    """
    unpinnable: list[dict[str, str]] = []
    pinnable: list[str] = []
    for kind, (contract, owner_story, table, _noun) in _PRESENTATION_KIND_REGISTRY.items():
        if _table_exists(conn, table):
            pinnable.append(kind)
            continue
        unpinnable.append(
            {
                "kind": kind,
                "contract": contract,
                "owner_story": owner_story,
                "missing_link": table,
            }
        )
    return unpinnable, pinnable


#: How each pinnable kind is LISTED, so the picker offers a version that resolves
#: instead of asking a person to type one. One row per candidate: the identifier
#: the pin stores, and the words a person recognises it by.
#:
#: WHY THIS EXISTS (story 72.5, AC21). Until 2026-09-01 the Presentation tab of a
#: Report asked for an "Exact version identifier" in a free-text box with a
#: `vsv_...` placeholder, and the commit that left it there said why: *"aucun
#: endpoint de liste n'existe, un picker aurait invente une route"* (`5576ae60`).
#: The route exists now, so the box does not. A pin typed by hand is a pin nobody
#: verified, and `is_exact_pin` (154:70-75) refuses only six literal words.
_PRESENTATION_KIND_LISTING: dict[str, str] = {
    "visualization_spec_version": """
        SELECT sv.id, COALESCE(v.name, sv.family), sv.version_number, sv.family,
               sv.content_hash, sv.created_at
        FROM app.visualization_spec_versions sv
        JOIN app.visualizations v
          ON v.id = sv.visualization_id AND v.org_id = sv.org_id
         AND v.project_id = sv.project_id
        WHERE sv.org_id = %s AND sv.project_id = %s
        ORDER BY sv.created_at DESC
        LIMIT %s
    """,
    "visualization_template_version": """
        SELECT tv.id, t.label, tv.version_number, tv.family,
               tv.content_hash, tv.created_at
        FROM app.visualization_template_versions tv
        JOIN app.visualization_templates t
          ON t.id = tv.template_id AND t.org_id = tv.org_id
         AND t.project_id = tv.project_id
        WHERE tv.org_id = %s AND tv.project_id = %s AND t.archived_at IS NULL
        ORDER BY tv.created_at DESC
        LIMIT %s
    """,
}


def list_pinnable_presentation_versions(
    conn, *, org_id: str, project_id: str, limit: int = 100
) -> dict[str, list[dict[str, Any]]]:
    """Every presentation version of this Project a pin could actually resolve.

    Keyed by the WIRE token the pin stores, because that is what the caller will
    write back; the label beside it is what a person reads. A kind whose registry
    is not in this deployment is absent from the map entirely -- not present with
    an empty list, which would read as "this Project has none" instead of "this
    deployment cannot resolve that kind at all". `render_contract_state` already
    says which of the two it is, in its own key.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for kind, (_contract, _story, table, noun) in _PRESENTATION_KIND_REGISTRY.items():
        if not _table_exists(conn, table):
            continue
        with conn.cursor() as cur:
            cur.execute(
                _PRESENTATION_KIND_LISTING[kind],
                (org_id, project_id, max(1, min(int(limit), 200))),
            )
            rows = cur.fetchall()
        out[kind] = [
            {
                "version_id": row[0],
                "label": row[1],
                "version_number": row[2],
                "family": row[3],
                "content_hash": row[4],
                "created_at": row[5].isoformat() if row[5] else None,
                #: The product noun of the kind, so the option a person reads
                #: never carries the stored token.
                "kind_noun": noun,
            }
            for row in rows
        ]
    return out


#: The one kind whose head can be retired while its versions stay pinnable.
#: A Visualization Spec version has no archivable head on this path, so asking
#: the question of that kind would invent a state nothing carries.
_ARCHIVABLE_PIN_KIND = "visualization_template_version"


def archived_template_pins(
    conn, *, org_id: str, project_id: str, version_ids: Sequence[str]
) -> set[str]:
    """Of these pinned Chart Template versions, which belong to an ARCHIVED head.

    WHY A PIN OUTLIVES THE ARCHIVE, AND WHY IT MUST SAY SO. Archiving a Chart
    Template retires the head; it deletes nothing, so
    `_presentation_version_resolves` goes on answering yes and the Report goes on
    resolving its presentation exactly as it did the day it was pinned. That is
    the product's archive pattern -- a version and a date, never a DELETE -- and
    it is the whole reason `list_pinnable_presentation_versions` can exclude an
    archived head from NEW pins without breaking an old one.

    But "still resolves" and "still offered" are two facts, and a screen that
    knows only the first shows a live-looking pin to a template nobody can add a
    version to any more. So the state travels WITH the pin. It is not a second
    authority on liveness: `archived_at` is read here, on the same row the list
    route filters on.

    Empty in, empty out -- and no query at all: a Report with no Chart Template
    pin asks the database nothing.
    """
    wanted = [v for v in dict.fromkeys(version_ids) if v]
    if not wanted:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tv.id
            FROM app.visualization_template_versions tv
            JOIN app.visualization_templates t
              ON t.id = tv.template_id AND t.org_id = tv.org_id
             AND t.project_id = tv.project_id
            WHERE tv.org_id = %s AND tv.project_id = %s
              AND tv.id = ANY(%s) AND t.archived_at IS NOT NULL
            """,
            (org_id, project_id, wanted),
        )
        return {row[0] for row in cur.fetchall()}


def _presentation_payload(
    kind: str | None, version_id: str | None, absent_literal: str | None, archived: set[str]
) -> dict[str, Any]:
    """The three stored columns plus the ONE fact none of them carries.

    `archived` is `None` for every pin that is not a Chart Template version --
    never `False`, which would assert a liveness this function did not measure.
    """
    return {
        "kind": kind,
        "version_id": version_id,
        "absent_literal": absent_literal,
        "archived": (
            (version_id in archived) if kind == _ARCHIVABLE_PIN_KIND and version_id else None
        ),
    }


def _registered_families(conn) -> list[dict[str, str]]:
    """The build identities this deployment ships, one entry per family.

    A projection of `ui/cards/shell/src/viz/renderers/index.ts`, carried into the
    ledger by `scripts/register_renderer_builds.py`. Read here rather than
    restated: a second list would drift the day a renderer ships.

    Raises `LedgerUnreadable` when the read fails. It does NOT return an empty
    list: "nothing is registered" and "I could not find out" are two facts, and
    only the first is an answer.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT family, id, runtime_build, theme_version, formatter_version, "
                f"renderer_id FROM {_RENDERER_REGISTRY_TABLE} ORDER BY family, id"  # noqa: S608
            )
            rows = cur.fetchall()
    except Exception as exc:
        #  AN UNREADABLE LEDGER IS NOT AN EMPTY ONE -- and until 2026-08-25 this
        #  comment stood over a `return []` that said exactly the opposite. The
        #  read fails loud; `render_contract_state` is the single place that turns
        #  the failure into a state a caller can render honestly.
        raise LedgerUnreadable(
            f"{_RENDERER_REGISTRY_TABLE} could not be read: {type(exc).__name__}"
        ) from exc
    #  All FIVE "drawn by" pins, not two: a client that must fill
    #  `theme_version` and `formatter_version` from somewhere would either
    #  hard-code them -- a second authority that drifts the day the theme moves --
    #  or guess. They are properties of the build, and the build is here.
    #
    #  `renderer_adapter` joined them on 2026-09-01 (AI-357). The column
    #  `app.renders.renderer_adapter` is under `ck_renders_pins_are_exact` and the
    #  ledger has ALWAYS known it -- `renderer_build` is `<family>/<renderer_id>
    #  @<semver>` and `renderer_id` is that adapter -- but this read did not hand
    #  it out. So a caller that filled every pin the registry offered still wrote
    #  an empty adapter, and `POST /renders` answered a mute 500.
    families = [
        {
            "family": str(row[0]),
            "renderer_build_id": str(row[1]),
            "runtime_build_id": str(row[2]),
            "theme_version": str(row[3]),
            "formatter_version": str(row[4]),
            "renderer_adapter": str(row[5]),
        }
        for row in rows
    ]
    #  THE PINS A CALLER IS OFFERED ARE THE SERVED RUNTIME'S, NOT THE LEDGER ROW'S
    #  (2026-09-04, deployment window; `visualization-and-rendering.md`, note of
    #  that day). The ledger is keyed by the renderer build id alone and is
    #  insert-once, so its row for `table/toorow-table@1.0.0` names the runtime
    #  build of the FIRST deployment that registered it (`+c2969870d2d3`,
    #  2026-08-13) for ever -- and G14-T01 was red on the day of a deployment
    #  serving `+c12d45ac065a` because the gate froze the pin this list handed it
    #  and the running runtime refused to replay it, correctly. The ratified page
    #  already says which way this must go: "registered_families must stop
    #  returning builds the deployment no longer ships". The adapter and the
    #  family stay the ledger's (the identity of WHICH renderer draws); the three
    #  build properties come from the manifest beside the bundle this deployment
    #  serves, for exactly the renderer builds that manifest declares. When the
    #  manifest is unreadable, the ledger row is what we know, and it is served
    #  unchanged rather than guessed.
    served = _served_runtime_pins()
    for entry in families:
        pins = served.get(entry["renderer_build_id"])
        if pins is not None:
            entry.update(pins)
    return families


def _served_runtime_pins() -> dict[str, dict[str, str]]:
    """`{renderer_build_id: {runtime_build_id, theme_version, formatter_version}}` of the
    runtime this deployment serves, or `{}` when no manifest can be read."""
    from core import render_app_payload  # noqa: PLC0415

    try:
        manifest = render_app_payload.load_runtime_manifest()
    except render_app_payload.RenderAppPayloadRefused:
        return {}
    renderers = manifest.get("renderers") or {}
    return {
        str(declaration["renderer_build"]): {
            "runtime_build_id": str(manifest["runtime_build"]),
            "theme_version": str(manifest["theme_version"]),
            "formatter_version": str(manifest["formatter_version"]),
        }
        for declaration in renderers.values()
        if isinstance(declaration, dict) and declaration.get("renderer_build")
    }


def _presentation_version_resolves(
    conn, *, table: str, version_id: str, org_id: str, project_id: str
) -> bool:
    """Does this exact version exist, IN THIS PROJECT?

    The Project is part of the LOOKUP, never checked afterwards: a version of
    another Project simply does not resolve and answers exactly like a missing
    one -- the rule `_require_query_spec_version` states three functions above,
    and the reason is the same. A caller able to tell "denied" from "absent" has
    a tenant-enumeration oracle.

    `table` is a module constant of `_PRESENTATION_KIND_REGISTRY`, never a value
    that reached this process from a request.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {table} WHERE id = %s AND org_id = %s AND project_id = %s",  # noqa: S608
            (version_id, org_id, project_id),
        )
        return cur.fetchone() is not None


def resolve_presentation(
    conn,
    payload: dict[str, Any] | None,
    *,
    org_id: str,
    project_id: str,
) -> dict[str, Any | None]:
    """Return the three presentation columns, complete or honestly absent.

    A caller that supplies a presentation reference while Story 50.4 has not
    landed is REFUSED rather than accepted-and-ignored: accepting it would store
    a version id nothing can resolve, which is the fabricated pin AC12 forbids.

    AND THAT REFUSAL NOW COVERS BOTH KINDS. It was written for the Spec and left
    the Chart Template through: `visualization_template_version` passed the kind
    check, `render_contract_state` probed two tables that are not the Template's,
    and the row was stored. A Chart Template version is refused BY NAME until the
    registry story 72.1 owns exists -- and the moment it does, the probe answers
    yes and this function opens, with no edit here.

    AND THE KIND CHECK IS NOT THE PIN CHECK -- story 72.1 AC3. Until 2026-08-31
    the two were confused: once a kind was pinnable, ANY string passed. The kind
    says which registry answers; the pin says whether that registry holds this
    version, in THIS Project. Both are asked here, in that order, because they
    fail for different reasons and a caller repairs them differently. This is
    also what keeps the service and the database saying the same thing: migration
    333 keys both carriers of the pin onto `app.presentation_version_registry`,
    so a pin this function accepted and the key then refused would be AI-338
    reintroduced on another table.
    """
    payload = payload or {}
    kind = _text(payload.get("kind"), limit=64)
    version_id = _text(payload.get("version_id"), limit=200)
    if not kind and not version_id:
        return {
            "presentation_kind": None,
            "presentation_version_id": None,
            "presentation_absent_literal": NO_PRESENTATION_CONTRACT,
        }
    if kind not in _PRESENTATION_KIND_REGISTRY:
        raise ArtifactRefused(
            "invalid_presentation",
            "a presentation reference names a Chart Template version or a "
            "Visualization Spec version",
            [Refusal("invalid_presentation", f"`{kind}` is not a presentation kind", "kind")],
        )
    if version_id.lower() in _FORBIDDEN_PIN_VALUES:
        raise ArtifactRefused(
            "placeholder_pin",
            f"`{version_id}` is a placeholder, not a version identity",
            [Refusal("placeholder_pin", "name the exact version", "version_id")],
        )
    state = render_contract_state(conn)
    #  THE KIND-SPECIFIC REFUSAL COMES FIRST, because it names the real blocker.
    #  A caller aiming at the Chart Template while the Spec tables are also absent
    #  would otherwise read "Story 50.4 has not landed" and go build a Spec pin
    #  that this deployment would refuse for a different reason.
    for unpinnable in state["unpinnable_kinds"]:
        if unpinnable["kind"] != kind:
            continue
        raise ArtifactRefused(
            "presentation_kind_unavailable",
            CHART_TEMPLATE_PIN_UNAVAILABLE
            if kind == "visualization_template_version"
            else "this presentation kind cannot be pinned in this deployment yet",
            [
                Refusal(
                    "missing_contract",
                    f"{unpinnable['contract']} is owned by Story "
                    f"{unpinnable['owner_story']} and has not landed "
                    f"({unpinnable['missing_link']})",
                    unpinnable["contract"],
                )
            ],
        )
    if not state["available"]:
        raise ArtifactRefused(
            "presentation_contract_unavailable",
            "no accepted presentation contract exists in this deployment yet",
            [
                Refusal(
                    "missing_contract",
                    f"{m['contract']} is owned by Story {m['owner_story']} and has not landed "
                    f"({m['missing_link']})",
                    m["contract"],
                )
                for m in state["missing"]
            ],
        )
    #  THE PIN ITSELF. The kind is pinnable, so the registry that answers it
    #  exists; whether that registry holds THIS version, in THIS Project, is the
    #  other question -- and it is the one nothing asked.
    _contract, _owner_story, table, noun = _PRESENTATION_KIND_REGISTRY[kind]
    if not _presentation_version_resolves(
        conn, table=table, version_id=version_id, org_id=org_id, project_id=project_id
    ):
        #  Non-disclosing: absent here, held by another Project, or unreachable all
        #  answer identically. Which of the three it was belongs in audit, never in
        #  a response body -- the rule `ArtifactNotFound` states at the top of this
        #  module, and `VisualizationNotFound` states in the other one.
        sentence = f"this {noun} does not resolve in this Project"
        raise ArtifactRefused(
            "unresolvable_pin",
            sentence,
            [
                Refusal(
                    "unresolvable_pin",
                    sentence,
                    "version_id",
                    f"Choose a {noun} of this Project, or save with no presentation "
                    f"and pin one once it exists.",
                )
            ],
        )
    return {
        "presentation_kind": kind,
        "presentation_version_id": version_id,
        "presentation_absent_literal": None,
    }


# ---------------------------------------------------------------------------
# Reports.
# ---------------------------------------------------------------------------


def _require_query_spec_version(conn, *, org_id: str, project_id: str, version_id: str) -> str:
    """Return the Query Spec head id for an exact version, or refuse to resolve.

    Scope is part of the LOOKUP, never checked afterwards: a foreign version
    simply does not resolve, and answers exactly like a missing one (AC13).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT query_spec_id FROM app.query_spec_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ArtifactNotFound("query spec version not found in this Project")
    return str(row[0])


@_names_its_check_violations
def create_report(
    conn,
    *,
    org_id: str,
    project_id: str,
    label: str,
    actor: str,
    query_spec_version_id: str,
    description: str | None = None,
    presentation: dict[str, Any] | None = None,
    seed_origin: str = "project",
    seed_module_name: str | None = None,
    seed_report_id: str | None = None,
) -> dict[str, Any]:
    """Create a Report head and its version 1, in the caller's transaction.

    A connector seed SEEDS this Report (AC4). `seed_module_name`/`seed_report_id`
    are provenance recorded on the head, never a foreign key into connector JSON
    and never the Report's identity -- editing here cannot reach the pack.
    """
    label = _text(label)
    if not label:
        raise ArtifactRefused(
            "missing_field", "a Report needs a label", [Refusal("missing_field", "label", "label")]
        )
    if seed_origin not in {"project", "connector_seed", "explore"}:
        raise ArtifactRefused(
            "invalid_seed_origin", f"`{seed_origin}` is not a Report origin", []
        )
    query_spec_id = _require_query_spec_version(
        conn, org_id=org_id, project_id=project_id, version_id=query_spec_version_id
    )
    presentation_columns = resolve_presentation(
        conn, presentation, org_id=org_id, project_id=project_id
    )

    report_id = f"rep_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_reports
                (id, org_id, project_id, label, description, seed_origin,
                 seed_module_name, seed_report_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                report_id,
                org_id,
                project_id,
                label,
                description,
                seed_origin,
                seed_module_name if seed_origin == "connector_seed" else None,
                seed_report_id if seed_origin == "connector_seed" else None,
                actor,
            ),
        )
    return _append_report_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        report_id=report_id,
        label=label,
        description=description,
        query_spec_id=query_spec_id,
        query_spec_version_id=query_spec_version_id,
        presentation_columns=presentation_columns,
        seed_provenance=(
            {"module_name": seed_module_name, "report_id": seed_report_id}
            if seed_origin == "connector_seed"
            else {}
        ),
        actor=actor,
        version_number=1,
        predecessor=None,
    )


@_names_its_check_violations
def create_report_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    report_id: str,
    actor: str,
    query_spec_version_id: str,
    label: str | None = None,
    description: str | None = None,
    presentation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append the next immutable Report version. Editing never mutates (AC2, AC11)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, description, current_version_id, archived_at
            FROM app.analysis_reports
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (report_id, org_id, project_id),
        )
        head = cur.fetchone()
    if head is None:
        raise ArtifactNotFound("report not found in this Project")
    if head[3] is not None:
        raise ArtifactRefused(
            "archived", "an archived Report does not accept new versions", []
        )
    if head[2] is None:
        raise ArtifactRefused("head_without_version", "this Report has no version to revise", [])

    query_spec_id = _require_query_spec_version(
        conn, org_id=org_id, project_id=project_id, version_id=query_spec_version_id
    )
    presentation_columns = resolve_presentation(
        conn, presentation, org_id=org_id, project_id=project_id
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.analysis_report_versions
            WHERE report_id = %s AND project_id = %s
            """,
            (report_id, project_id),
        )
        version_number = int(cur.fetchone()[0])

    return _append_report_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        report_id=report_id,
        label=_text(label) or str(head[0]),
        description=description if description is not None else head[1],
        query_spec_id=query_spec_id,
        query_spec_version_id=query_spec_version_id,
        presentation_columns=presentation_columns,
        seed_provenance={},
        actor=actor,
        version_number=version_number,
        predecessor=str(head[2]),
    )


def _append_report_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    report_id: str,
    label: str,
    description: str | None,
    query_spec_id: str,
    query_spec_version_id: str,
    presentation_columns: dict[str, Any],
    seed_provenance: dict[str, Any],
    actor: str,
    version_number: int,
    predecessor: str | None,
) -> dict[str, Any]:
    document = {
        "contract_version": REPORT_CONTRACT_VERSION,
        "label": label,
        "description": description,
        "query_spec_version_id": query_spec_version_id,
        "presentation": presentation_columns,
        "seed_provenance": seed_provenance,
    }
    content_hash = canonical_hash(document)
    version_id = f"repv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, description,
                 query_spec_id, query_spec_version_id, presentation_kind,
                 presentation_version_id, presentation_absent_literal, seed_provenance,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING created_at
            """,
            (
                version_id,
                report_id,
                org_id,
                project_id,
                version_number,
                label,
                description,
                query_spec_id,
                query_spec_version_id,
                presentation_columns["presentation_kind"],
                presentation_columns["presentation_version_id"],
                presentation_columns["presentation_absent_literal"],
                _canonical_json(seed_provenance),
                content_hash,
                predecessor,
                actor,
            ),
        )
        created_at = cur.fetchone()[0]
        cur.execute(
            """
            UPDATE app.analysis_reports
            SET current_version_id = %s, label = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, label, report_id, org_id, project_id),
        )
    return {
        "report_id": report_id,
        "id": version_id,
        "version_number": version_number,
        "label": label,
        "content_hash": content_hash,
        "query_spec_version_id": query_spec_version_id,
        "presentation": presentation_columns,
        "predecessor_version_id": predecessor,
        "created_at": created_at.isoformat() if created_at else None,
    }


def list_reports(
    conn, *, org_id: str, project_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Configured Reports only. Connector seeds are a SEPARATE list (AC4).

    ARCHIVED ROWS ARE OUT BY DEFAULT since 2026-08-17, which is the half of the
    archive gesture that makes it worth having: an archive that does not empty
    the list is a flag, not a retirement. Migration 154 had already anticipated
    it -- `idx_analysis_reports_live` is a partial index `WHERE archived_at IS
    NULL`, waiting for this predicate.

    `include_archived` exists so the archived ones remain REACHABLE rather than
    deleted: their versions and runs are evidence, which is exactly why the
    schema refuses to delete a head at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.label, r.description, r.seed_origin, r.seed_module_name,
                   r.seed_report_id, r.current_version_id, r.archived_at, r.created_by,
                   r.created_at, r.updated_at,
                   v.version_number, v.query_spec_version_id, v.presentation_absent_literal,
                   (SELECT COUNT(*) FROM app.analysis_report_runs run
                     WHERE run.report_id = r.id) AS run_count,
                   r.archived_by
            FROM app.analysis_reports r
            LEFT JOIN app.analysis_report_versions v ON v.id = r.current_version_id
            WHERE r.org_id = %s AND r.project_id = %s
              AND (%s OR r.archived_at IS NULL)
            ORDER BY r.updated_at DESC
            """,
            (org_id, project_id, include_archived),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "label": r[1],
            "description": r[2],
            "seed_origin": r[3],
            "seed": (
                {"module_name": r[4], "report_id": r[5]} if r[3] == "connector_seed" else None
            ),
            "current_version_id": r[6],
            "archived": r[7] is not None,
            "archived_at": r[7].isoformat() if r[7] else None,
            "archived_by": r[15],
            "created_by": r[8],
            "created_at": r[9].isoformat() if r[9] else None,
            "updated_at": r[10].isoformat() if r[10] else None,
            "current_version_number": r[11],
            "query_spec_version_id": r[12],
            "presentation_absent": r[13],
            "run_count": int(r[14] or 0),
        }
        for r in rows
    ]


def _archive_head(
    conn,
    *,
    table: str,
    noun: str,
    org_id: str,
    project_id: str,
    artifact_id: str,
    actor: str,
) -> dict[str, Any]:
    """Retire ONE stable head, timestamped and attributed. The shared writer.

    ONE FUNCTION FOR BOTH ARTIFACTS on purpose. A Report and a Notebook are the
    same object here -- a stable head whose versions are evidence -- and the
    defect being repaired was a CLASS defect: `archived_at` existed on both,
    was selected by both, refused new versions on both, and was written by
    nothing on either. Two copies of this would be two places for the next rule
    to be applied to only one.

    WHY ARCHIVE AND NOT DELETE. The database already answered that: migration
    154's `reject_stable_head_rebind` trigger refuses DELETE outright with
    "archive % rather than deleting it: its versions and runs are evidence". The
    gesture the console owes is therefore archive, and only archive.

    IDEMPOTENT, and it matters for a confirmation dialog: a double-submit, or a
    second person clicking the same button, must not answer an error for a state
    that is already what they asked for. The UPDATE is guarded on `archived_at
    IS NULL`, and a zero-row result is re-read to tell "already archived" apart
    from "not yours / not here" -- the two answers a bare rowcount would merge.

    NON-DISCLOSING on the second of those: an artifact of another Project is
    `ArtifactNotFound`, exactly as every read on this surface answers.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.{table}
               SET archived_at = NOW(), archived_by = %s, updated_at = NOW()
             WHERE id = %s AND org_id = %s AND project_id = %s
               AND archived_at IS NULL
            RETURNING id, archived_at, archived_by
            """,  # noqa: S608 -- `table` is chosen by the two callers below
            (actor, artifact_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is not None:
            return {
                "id": row[0],
                "archived": True,
                "archived_at": row[1].isoformat() if row[1] else None,
                "archived_by": row[2],
                "already_archived": False,
            }

        # Nothing moved. Which of the two reasons is it?
        cur.execute(
            f"""
            SELECT archived_at, archived_by FROM app.{table}
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,  # noqa: S608 -- same literal
            (artifact_id, org_id, project_id),
        )
        existing = cur.fetchone()
    if existing is None:
        raise ArtifactNotFound(f"{noun} not found in this Project")
    return {
        "id": artifact_id,
        "archived": True,
        "archived_at": existing[0].isoformat() if existing[0] else None,
        "archived_by": existing[1],
        # Said rather than hidden: the caller asked for a state change that had
        # already happened, and a screen may want to say so quietly.
        "already_archived": True,
    }


@_names_its_check_violations
def archive_report(
    conn, *, org_id: str, project_id: str, report_id: str, actor: str
) -> dict[str, Any]:
    """Retire a Report. The writer `analyze_artifacts` never had.

    Until 2026-08-17 `archived_at` was selected here (`list_reports`,
    `get_report`) and enforced here ("an archived Report does not accept new
    versions") while **no code in the repository ever wrote it** -- so the
    refusal was unreachable and the column was decoration.
    """
    return _archive_head(
        conn,
        table="analysis_reports",
        noun="report",
        org_id=org_id,
        project_id=project_id,
        artifact_id=report_id,
        actor=actor,
    )


@_names_its_check_violations
def archive_notebook(
    conn, *, org_id: str, project_id: str, notebook_id: str, actor: str
) -> dict[str, Any]:
    """Retire a Notebook. Same defect, same repair, same writer.

    Note what archiving a Notebook already does elsewhere and did not do before:
    `dispatch_due_notebook_schedules` filters `n.archived_at IS NULL`, so a
    schedule stops with the archive rather than needing a second gesture. That
    filter was written against a column nothing set.
    """
    return _archive_head(
        conn,
        table="analysis_notebooks",
        noun="notebook",
        org_id=org_id,
        project_id=project_id,
        artifact_id=notebook_id,
        actor=actor,
    )


def list_report_seeds(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Connector seed AVAILABILITY, read from `app.project_reports` (migration 014).

    This is not a Report list and is never displayed as one. It answers "which
    expert packs may this Project draw from", which is precisely what the
    enablement toggle has always meant (AC4).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT module_name, report_id, enabled, display_order
            FROM app.project_reports WHERE project_id = %s
            ORDER BY display_order, module_name, report_id
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "module_name": r[0],
            "report_id": r[1],
            "enabled": bool(r[2]),
            "display_order": int(r[3] or 0),
            "kind": "connector_seed",
        }
        for r in rows
    ]


def get_report(conn, *, org_id: str, project_id: str, report_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label, description, seed_origin, seed_module_name, seed_report_id,
                   current_version_id, archived_at, created_by, created_at, updated_at
            FROM app.analysis_reports WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (report_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise ArtifactNotFound("report not found in this Project")
        cur.execute(
            """
            SELECT id, version_number, label, query_spec_id, query_spec_version_id,
                   presentation_kind, presentation_version_id, presentation_absent_literal,
                   content_hash, predecessor_version_id, created_by, created_at
            FROM app.analysis_report_versions
            WHERE report_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (report_id, project_id),
        )
        versions = cur.fetchall()
        cur.execute(
            """
            SELECT id, report_version_id, query_spec_version_id, result_id, render_id,
                   outcome, requested_as_of, resolved_as_of, actor, started_at, ended_at
            FROM app.analysis_report_runs
            WHERE report_id = %s AND project_id = %s
            ORDER BY created_at DESC LIMIT 200
            """,
            (report_id, project_id),
        )
        runs = cur.fetchall()
        #  ONE query for every version at once, and only over the pins that could
        #  carry the state. A per-version read would be N reads to say a thing the
        #  same row already knows.
        archived_pins = archived_template_pins(
            conn,
            org_id=org_id,
            project_id=project_id,
            version_ids=[v[6] for v in versions if v[5] == _ARCHIVABLE_PIN_KIND],
        )
    return {
        "id": head[0],
        "label": head[1],
        "description": head[2],
        "seed_origin": head[3],
        "seed": (
            {"module_name": head[4], "report_id": head[5]}
            if head[3] == "connector_seed"
            else None
        ),
        "current_version_id": head[6],
        "archived": head[7] is not None,
        "created_by": head[8],
        "created_at": head[9].isoformat() if head[9] else None,
        "updated_at": head[10].isoformat() if head[10] else None,
        #  THE PANEL THAT OFFERS THE PIN HAS TO KNOW WHAT CAN BE PINNED. The
        #  collection route already sent this; the workbench did not, and the
        #  workbench is where the picker lives -- so it listed a presentation kind
        #  with no registry behind it and let the server refuse afterwards. The
        #  gesture this screen exists for is decided here, not one screen away.
        "presentation_contract": render_contract_state(conn),
        "versions": [
            {
                "id": v[0],
                "version_number": v[1],
                "label": v[2],
                "query_spec_id": v[3],
                "query_spec_version_id": v[4],
                "presentation": _presentation_payload(v[5], v[6], v[7], archived_pins),
                "content_hash": v[8],
                "predecessor_version_id": v[9],
                "created_by": v[10],
                "created_at": v[11].isoformat() if v[11] else None,
            }
            for v in versions
        ],
        "runs": [
            {
                "id": r[0],
                "report_version_id": r[1],
                "query_spec_version_id": r[2],
                "result_id": r[3],
                "render_id": r[4],
                "outcome": r[5],
                "requested_as_of": r[6],
                "resolved_as_of": r[7],
                "actor": r[8],
                "started_at": r[9].isoformat() if r[9] else None,
                "ended_at": r[10].isoformat() if r[10] else None,
            }
            for r in runs
        ],
    }


@_names_its_check_violations
def run_report_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    report_version_id: str,
    actor: str,
    requested_as_of: str | None = None,
) -> dict[str, Any]:
    """Execute the exact Query Spec version this Report version pins (AC3).

    Every call creates a NEW Result through Story 50.1's execution service and a
    NEW immutable run reference. Nothing about the Report, the Query Spec version,
    or any previous Result or Render is touched -- which is why this function has
    no branch that could reuse an earlier Result.
    """
    from core.query_execution import accept_execution, run_execution  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.report_id, v.query_spec_version_id, q.spec, q.semantic_view_version_id
            FROM app.analysis_report_versions v
            JOIN app.query_spec_versions q ON q.id = v.query_spec_version_id
            WHERE v.id = %s AND v.org_id = %s AND v.project_id = %s
            """,
            (report_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ArtifactNotFound("report version not found in this Project")
    report_id, query_spec_version_id, spec, semantic_view_version_id = row

    attempt = accept_execution(
        conn,
        org_id=org_id,
        project_id=project_id,
        query_spec_version_id=str(query_spec_version_id),
        actor=actor,
    )
    result = run_execution(
        conn,
        attempt=attempt,
        org_id=org_id,
        project_id=project_id,
        spec=spec or {},
        semantic_view_version_id=str(semantic_view_version_id),
    )

    run_id = f"reprun_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT started_at, ended_at FROM app.query_results
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (result["result_id"], org_id, project_id),
        )
        timing = cur.fetchone()
        cur.execute(
            """
            INSERT INTO app.analysis_report_runs
                (id, report_id, report_version_id, org_id, project_id, query_spec_version_id,
                 result_id, render_id, outcome, requested_as_of, resolved_as_of, actor,
                 started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                report_id,
                report_version_id,
                org_id,
                project_id,
                query_spec_version_id,
                result["result_id"],
                result["outcome"],
                requested_as_of,
                # The as-of the SERVER resolved. Echoing the request here would
                # make an unresolved window look like an honoured one.
                (spec or {}).get("time", {}).get("as_of") if isinstance(spec, dict) else None,
                actor,
                timing[0],
                timing[1],
            ),
        )
    return {
        "run_id": run_id,
        "report_id": report_id,
        "report_version_id": report_version_id,
        "attempt_id": attempt["attempt_id"],
        # A Render is created only when a presentation is MATERIALIZED, and that
        # needs Stories 50.4/50.5. The state is reported, never guessed at.
        "render": render_contract_state(conn),
        **result,
    }


# ---------------------------------------------------------------------------
# Notebooks.
# ---------------------------------------------------------------------------


def _validate_blocks(conn, *, org_id: str, project_id: str, blocks: Any) -> list[dict[str, Any]]:
    """Normalize and prove every block's version pins, collecting all refusals."""
    if not isinstance(blocks, list) or not blocks:
        raise ArtifactRefused(
            "no_blocks",
            "a Notebook composition needs at least one block",
            [Refusal("no_blocks", "blocks", "blocks")],
        )
    if len(blocks) > _MAX_BLOCKS:
        raise ArtifactRefused(
            "limit_exceeded", f"a Notebook version may hold at most {_MAX_BLOCKS} blocks", []
        )

    refusals: list[Refusal] = []
    normalized: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for index, raw in enumerate(blocks):
        subject = f"blocks[{index}]"
        if not isinstance(raw, dict):
            refusals.append(Refusal("invalid_shape", "a block must be an object", subject))
            continue
        block_key = _text(raw.get("block_key"), limit=63).lower()
        block_type = _text(raw.get("block_type"), limit=32)
        if not block_key:
            refusals.append(Refusal("missing_field", "a block needs a stable key", subject))
            continue
        if block_key in seen_keys:
            refusals.append(
                Refusal("duplicate_block_key", f"`{block_key}` appears twice", subject)
            )
            continue
        seen_keys.add(block_key)
        if block_type not in _BLOCK_TYPES:
            refusals.append(
                Refusal("invalid_block_type", f"`{block_type}` is not a block type", subject)
            )
            continue
        as_of_rule = _text(raw.get("as_of_rule"), limit=16) or "current"
        if as_of_rule not in _AS_OF_RULES:
            refusals.append(
                Refusal("invalid_as_of_rule", f"`{as_of_rule}` is not an as-of rule", subject)
            )
            continue

        query_spec_version_id: str | None = None
        report_version_id: str | None = None
        if block_type == "query":
            query_spec_version_id = _text(raw.get("query_spec_version_id"))
            if not query_spec_version_id:
                refusals.append(
                    Refusal("missing_field", "a query block pins a Query Spec version", subject)
                )
                continue
            try:
                _require_query_spec_version(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    version_id=query_spec_version_id,
                )
            except ArtifactNotFound:
                # Non-disclosing: the block names something this Project cannot
                # reach. Saying whether it exists elsewhere would be the oracle.
                refusals.append(
                    Refusal("unresolvable_pin", "this Query Spec version does not resolve", subject)
                )
                continue
        elif block_type == "report":
            report_version_id = _text(raw.get("report_version_id"))
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM app.analysis_report_versions
                    WHERE id = %s AND org_id = %s AND project_id = %s
                    """,
                    (report_version_id, org_id, project_id),
                )
                if cur.fetchone() is None:
                    refusals.append(
                        Refusal(
                            "unresolvable_pin", "this Report version does not resolve", subject
                        )
                    )
                    continue

        try:
            presentation = resolve_presentation(
                conn, raw.get("presentation"), org_id=org_id, project_id=project_id
            )
        except ArtifactRefused as exc:
            refusals.extend(exc.refusals or [Refusal(exc.code, str(exc), subject)])
            continue

        # A narrative block never renders. An analytical block renders only when a
        # presentation contract actually exists -- otherwise its Run would owe a
        # Render that cannot be produced, and would record a gap instead of a fact.
        renders = block_type != "narrative" and presentation["presentation_absent_literal"] is None
        narrative = raw.get("narrative") if isinstance(raw.get("narrative"), dict) else {}

        block = {
            "block_key": block_key,
            "position": len(normalized) + 1,
            "block_type": block_type,
            "query_spec_version_id": query_spec_version_id or None,
            "report_version_id": report_version_id or None,
            "renders": renders,
            "as_of_rule": as_of_rule,
            "narrative": narrative,
            **presentation,
        }
        block["content_hash"] = canonical_hash(block)
        normalized.append(block)

    if refusals:
        raise ArtifactRefused(
            "invalid_composition",
            f"the Notebook composition was refused on {len(refusals)} point(s)",
            refusals,
        )
    return normalized


@_names_its_check_violations
def create_notebook(
    conn,
    *,
    org_id: str,
    project_id: str,
    label: str,
    actor: str,
    blocks: Any,
    description: str | None = None,
    legacy_notebook_id: str | None = None,
) -> dict[str, Any]:
    label = _text(label)
    if not label:
        raise ArtifactRefused("missing_field", "a Notebook needs a label", [])
    normalized = _validate_blocks(conn, org_id=org_id, project_id=project_id, blocks=blocks)
    notebook_id = f"nbk_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_notebooks
                (id, org_id, project_id, label, description, legacy_notebook_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (notebook_id, org_id, project_id, label, description, legacy_notebook_id, actor),
        )
    return _append_notebook_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        notebook_id=notebook_id,
        label=label,
        blocks=normalized,
        actor=actor,
        version_number=1,
        predecessor=None,
    )


@_names_its_check_violations
def create_notebook_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    notebook_id: str,
    actor: str,
    blocks: Any,
    label: str | None = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, current_version_id, archived_at FROM app.analysis_notebooks
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (notebook_id, org_id, project_id),
        )
        head = cur.fetchone()
    if head is None:
        raise ArtifactNotFound("notebook not found in this Project")
    if head[2] is not None:
        raise ArtifactRefused("archived", "an archived Notebook does not accept new versions", [])
    normalized = _validate_blocks(conn, org_id=org_id, project_id=project_id, blocks=blocks)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.analysis_notebook_versions
            WHERE notebook_id = %s AND project_id = %s
            """,
            (notebook_id, project_id),
        )
        version_number = int(cur.fetchone()[0])
    return _append_notebook_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        notebook_id=notebook_id,
        label=_text(label) or str(head[0]),
        blocks=normalized,
        actor=actor,
        version_number=version_number,
        predecessor=str(head[1]),
    )


def _append_notebook_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    notebook_id: str,
    label: str,
    blocks: list[dict[str, Any]],
    actor: str,
    version_number: int,
    predecessor: str | None,
) -> dict[str, Any]:
    document = {
        "contract_version": NOTEBOOK_CONTRACT_VERSION,
        "label": label,
        "blocks": blocks,
    }
    content_hash = canonical_hash(document)
    version_id = f"nbkv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_versions
                (id, notebook_id, org_id, project_id, version_number, label,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at
            """,
            (
                version_id,
                notebook_id,
                org_id,
                project_id,
                version_number,
                label,
                content_hash,
                predecessor,
                actor,
            ),
        )
        created_at = cur.fetchone()[0]
        for block in blocks:
            cur.execute(
                """
                INSERT INTO app.analysis_notebook_version_blocks
                    (id, notebook_version_id, notebook_id, org_id, project_id, block_key,
                     position, block_type, query_spec_version_id, report_version_id,
                     presentation_kind, presentation_version_id, presentation_absent_literal,
                     renders, as_of_rule, narrative, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    f"nbkb_{ULID()}",
                    version_id,
                    notebook_id,
                    org_id,
                    project_id,
                    block["block_key"],
                    block["position"],
                    block["block_type"],
                    block["query_spec_version_id"],
                    block["report_version_id"],
                    block["presentation_kind"],
                    block["presentation_version_id"],
                    block["presentation_absent_literal"],
                    block["renders"],
                    block["as_of_rule"],
                    _canonical_json(block["narrative"]),
                    block["content_hash"],
                ),
            )
        cur.execute(
            """
            UPDATE app.analysis_notebooks
            SET current_version_id = %s, label = %s, updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, label, notebook_id, org_id, project_id),
        )
    return {
        "notebook_id": notebook_id,
        "id": version_id,
        "version_number": version_number,
        "label": label,
        "content_hash": content_hash,
        "predecessor_version_id": predecessor,
        "created_at": created_at.isoformat() if created_at else None,
        "blocks": blocks,
    }


def list_notebooks(
    conn, *, org_id: str, project_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Archived Notebooks are out by default -- the sibling of `list_reports`.

    Same rule, same reason: an archive that leaves the row in the list is a flag,
    not a retirement. Migration 285 adds the partial index this predicate wants,
    which 154 had given Reports and not Notebooks.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.id, n.label, n.description, n.current_version_id, n.archived_at,
                   n.legacy_notebook_id, n.created_by, n.created_at, n.updated_at,
                   v.version_number,
                   (SELECT COUNT(*) FROM app.analysis_notebook_runs r
                     WHERE r.notebook_id = n.id),
                   (SELECT r2.accepted_at FROM app.analysis_notebook_runs r2
                     WHERE r2.notebook_id = n.id ORDER BY r2.accepted_at DESC LIMIT 1),
                   (SELECT r3.outcome FROM app.analysis_notebook_runs r3
                     WHERE r3.notebook_id = n.id ORDER BY r3.accepted_at DESC LIMIT 1),
                   s.recurrence, s.enabled, s.next_due_at, n.archived_by
            FROM app.analysis_notebooks n
            LEFT JOIN app.analysis_notebook_versions v ON v.id = n.current_version_id
            LEFT JOIN app.analysis_notebook_schedules s ON s.notebook_id = n.id
            WHERE n.org_id = %s AND n.project_id = %s
              AND (%s OR n.archived_at IS NULL)
            ORDER BY n.updated_at DESC
            """,
            (org_id, project_id, include_archived),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "label": r[1],
            "description": r[2],
            "current_version_id": r[3],
            "archived": r[4] is not None,
            "archived_at": r[4].isoformat() if r[4] else None,
            "archived_by": r[16],
            "legacy_notebook_id": r[5],
            "created_by": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "updated_at": r[8].isoformat() if r[8] else None,
            "current_version_number": r[9],
            "run_count": int(r[10] or 0),
            # The LAST run, named as such. Never a `latest` pointer anything
            # follows: it is display state on a collection row, and no share,
            # export or schedule path reads it (AC15).
            "last_run_at": r[11].isoformat() if r[11] else None,
            "last_run_outcome": r[12],
            "schedule": (
                {"recurrence": r[13], "enabled": bool(r[14]),
                 "next_due_at": r[15].isoformat() if r[15] else None}
                if r[13] else None
            ),
        }
        for r in rows
    ]


def get_notebook(conn, *, org_id: str, project_id: str, notebook_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label, description, current_version_id, archived_at,
                   legacy_notebook_id, created_by, created_at, updated_at
            FROM app.analysis_notebooks WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (notebook_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise ArtifactNotFound("notebook not found in this Project")
        cur.execute(
            """
            SELECT id, version_number, label, content_hash, predecessor_version_id,
                   created_by, created_at
            FROM app.analysis_notebook_versions
            WHERE notebook_id = %s AND project_id = %s ORDER BY version_number DESC
            """,
            (notebook_id, project_id),
        )
        versions = cur.fetchall()
        cur.execute(
            """
            SELECT b.notebook_version_id, b.block_key, b.position, b.block_type,
                   b.query_spec_version_id, b.report_version_id, b.presentation_kind,
                   b.presentation_version_id, b.presentation_absent_literal, b.renders,
                   b.as_of_rule, b.narrative, b.content_hash
            FROM app.analysis_notebook_version_blocks b
            WHERE b.notebook_id = %s AND b.project_id = %s
            ORDER BY b.notebook_version_id, b.position
            """,
            (notebook_id, project_id),
        )
        block_rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, notebook_version_id, idempotency_key, dispatch_source, state, outcome,
                   requested_as_of, resolved_as_of, actor, accepted_at, terminal_at
            FROM app.analysis_notebook_runs
            WHERE notebook_id = %s AND project_id = %s
            ORDER BY accepted_at DESC LIMIT 200
            """,
            (notebook_id, project_id),
        )
        runs = cur.fetchall()
        cur.execute(
            """
            SELECT recurrence, timezone, enabled, next_due_at, updated_by, updated_at,
                   last_dispatched_at, last_run_id, last_dispatch_note
            FROM app.analysis_notebook_schedules WHERE notebook_id = %s AND project_id = %s
            """,
            (notebook_id, project_id),
        )
        schedule = cur.fetchone()

        #  THE SAME FACT ON THE SAME PIN. A Notebook block carries the identical
        #  three presentation columns as a Report version (migration 154), so an
        #  archived Chart Template is invisible on exactly one of the two screens
        #  unless it is answered in both -- the class, not the instance.
        archived_pins = archived_template_pins(
            conn,
            org_id=org_id,
            project_id=project_id,
            version_ids=[b[7] for b in block_rows if b[6] == _ARCHIVABLE_PIN_KIND],
        )

    blocks_by_version: dict[str, list[dict[str, Any]]] = {}
    for b in block_rows:
        blocks_by_version.setdefault(str(b[0]), []).append(
            {
                "block_key": b[1],
                "position": b[2],
                "block_type": b[3],
                "query_spec_version_id": b[4],
                "report_version_id": b[5],
                "presentation": _presentation_payload(b[6], b[7], b[8], archived_pins),
                "renders": bool(b[9]),
                "as_of_rule": b[10],
                "narrative": b[11],
                "content_hash": b[12],
            }
        )

    return {
        "id": head[0],
        "label": head[1],
        "description": head[2],
        "current_version_id": head[3],
        "archived": head[4] is not None,
        "legacy_notebook_id": head[5],
        "created_by": head[6],
        "created_at": head[7].isoformat() if head[7] else None,
        "updated_at": head[8].isoformat() if head[8] else None,
        "versions": [
            {
                "id": v[0],
                "version_number": v[1],
                "label": v[2],
                "content_hash": v[3],
                "predecessor_version_id": v[4],
                "created_by": v[5],
                "created_at": v[6].isoformat() if v[6] else None,
                "blocks": blocks_by_version.get(str(v[0]), []),
            }
            for v in versions
        ],
        "runs": [
            {
                "id": r[0],
                "notebook_version_id": r[1],
                "idempotency_key": r[2],
                "dispatch_source": r[3],
                "state": r[4],
                "outcome": r[5],
                "requested_as_of": r[6],
                "resolved_as_of": r[7],
                "actor": r[8],
                "accepted_at": r[9].isoformat() if r[9] else None,
                "terminal_at": r[10].isoformat() if r[10] else None,
            }
            for r in runs
        ],
        "schedule": (
            {
                "recurrence": schedule[0],
                "timezone": schedule[1],
                "enabled": bool(schedule[2]),
                "next_due_at": schedule[3].isoformat() if schedule[3] else None,
                "updated_by": schedule[4],
                "updated_at": schedule[5].isoformat() if schedule[5] else None,
                # What dispatch actually did, so the panel can stop claiming a
                # recurrence it has no evidence ever fired.
                "last_dispatched_at": schedule[6].isoformat() if schedule[6] else None,
                "last_run_id": schedule[7],
                "last_dispatch_note": schedule[8],
                # The exact call site that turns this policy into Runs. A named
                # dispatcher can be checked; a boolean "connected: true" cannot,
                # and this panel spent its first version claiming a recurrence
                # nothing in the repository read.
                "dispatcher": NOTEBOOK_DISPATCHER,
            }
            if schedule
            else None
        ),
    }


_RECURRENCES = ("daily", "weekly", "monthly")

#: The one call site that turns a schedule into Runs, named so the Notebook
#: workbench can show it and a test can assert it really calls this service.
#: Before this repair the schedule table had exactly three references in the whole
#: repository, all three inside this file, while the panel showed a green
#: "Enabled: Yes" badge -- a control nothing read.
NOTEBOOK_DISPATCHER = "server/core/scheduler.py::_run_due_notebooks"


def _zone(name: str | None) -> tzinfo:
    """The schedule's own timezone, or UTC when it names one this host lacks.

    A schedule is a calendar statement -- "daily" means one Run per local day --
    so the period it belongs to is computed where the user lives. An unknown zone
    falls back to UTC rather than raising: a bad string in operational config must
    not stop every other Notebook from being dispatched.
    """
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def schedule_period_key(recurrence: str, moment: datetime, timezone_name: str) -> str:
    """The calendar period `moment` falls in, in the schedule's own timezone.

    This IS the idempotency contract of a scheduled Run (AC7): two dispatches in
    the same period build the same key, and `app.analysis_notebook_runs`'
    `UNIQUE (notebook_id, idempotency_key)` turns the second into a replay of the
    first rather than a duplicate Run over the same evidence.
    """
    local = moment.astimezone(_zone(timezone_name))
    if recurrence == "daily":
        return local.strftime("%Y-%m-%d")
    if recurrence == "weekly":
        year, week, _weekday = local.isocalendar()
        return f"{year}-W{week:02d}"
    return local.strftime("%Y-%m")


def next_due_after(recurrence: str, moment: datetime, timezone_name: str) -> datetime:
    """The start of the NEXT period, in the schedule's timezone, as UTC.

    Deliberately the next period boundary rather than `moment + 24h`: a nightly
    step that runs at 23:58 must not make tomorrow's Run due at 23:58 tomorrow and
    drift a minute a day until it crosses midnight and skips a day entirely.
    """
    zone = _zone(timezone_name)
    local = moment.astimezone(zone)
    if recurrence == "daily":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif recurrence == "weekly":
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight + timedelta(days=7 - local.isoweekday() + 1)
    else:
        year = local.year + (1 if local.month == 12 else 0)
        month = 1 if local.month == 12 else local.month + 1
        start = local.replace(
            year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return start.astimezone(UTC)


@_names_its_check_violations
def set_notebook_schedule(
    conn,
    *,
    org_id: str,
    project_id: str,
    notebook_id: str,
    recurrence: str,
    enabled: bool,
    actor: str,
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Operational policy, not content (AC7). Editing it creates no version.

    Enabling a schedule computes `next_due_at`. It used to stay NULL forever,
    which meant the panel could say "Next due: --" while claiming the schedule was
    enabled -- a green badge over a control nothing read.
    """
    if recurrence not in _RECURRENCES:
        raise ArtifactRefused("invalid_recurrence", f"`{recurrence}` is not a recurrence", [])
    now = datetime.now(UTC)
    # Due immediately when it is switched on: the operator asked for this Notebook
    # to run on a cadence, and making them wait a whole period for the first Run is
    # the kind of silence that reads as "the schedule is broken".
    next_due = now if enabled else None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM app.analysis_notebooks
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (notebook_id, org_id, project_id),
        )
        if cur.fetchone() is None:
            raise ArtifactNotFound("notebook not found in this Project")
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_schedules
                (notebook_id, org_id, project_id, recurrence, timezone, enabled,
                 next_due_at, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (notebook_id) DO UPDATE
              SET recurrence = EXCLUDED.recurrence, timezone = EXCLUDED.timezone,
                  enabled = EXCLUDED.enabled, next_due_at = EXCLUDED.next_due_at,
                  updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            (
                notebook_id, org_id, project_id, recurrence, timezone, enabled,
                next_due, actor,
            ),
        )
    return {
        "notebook_id": notebook_id,
        "recurrence": recurrence,
        "enabled": enabled,
        "timezone": timezone,
        "next_due_at": next_due.isoformat() if next_due else None,
    }


def dispatch_due_notebook_schedules(
    conn, *, now: datetime | None = None, actor: str = "scheduler", limit: int = 200
) -> dict[str, Any]:
    """Run every due canonical Notebook through the SAME service a person uses.

    This is the second half of AC7, and it is the half that was missing: the
    schedule table existed, the panel rendered it, and nothing in the repository
    ever read it. `server/core/scheduler.py` calls this, and only this -- there is
    no second execution path, so a scheduled Run cannot produce weaker evidence
    than a manual one.

    Three properties, each for a reason this codebase has already paid for:

      * **idempotent per period.** The key is the calendar period, so a nightly
        step that fires twice completes the Run it already accepted rather than
        running the composition twice over the same evidence.
      * **isolated per Notebook.** Each dispatch runs inside its own SAVEPOINT, so
        one Notebook whose pinned Query Spec version no longer resolves cannot
        abort the transaction and take every later Notebook down with it.
      * **it advances the clock even when a dispatch fails.** A schedule that
        refuses forever would otherwise be retried on every nightly step until
        someone reads the log.
    """
    now = now or datetime.now(UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.notebook_id, s.org_id, s.project_id, s.recurrence, s.timezone
            FROM app.analysis_notebook_schedules s
            JOIN app.analysis_notebooks n
              ON n.id = s.notebook_id AND n.org_id = s.org_id AND n.project_id = s.project_id
            WHERE s.enabled
              AND n.archived_at IS NULL
              AND n.current_version_id IS NOT NULL
              AND (s.next_due_at IS NULL OR s.next_due_at <= %s)
            ORDER BY s.next_due_at NULLS FIRST, s.notebook_id
            LIMIT %s
            """,
            (now, limit),
        )
        due = cur.fetchall()

    dispatched: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for notebook_id, nb_org, nb_project, recurrence, zone_name in due:
        key = f"scheduled:{recurrence}:{schedule_period_key(recurrence, now, zone_name)}"
        savepoint = "analyze_dispatch"
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {savepoint}")
        try:
            run = run_notebook(
                conn,
                org_id=str(nb_org),
                project_id=str(nb_project),
                notebook_id=str(notebook_id),
                actor=actor,
                idempotency_key=key,
                dispatch_source="scheduled",
            )
        except Exception as exc:  # noqa: BLE001 -- isolation is the whole point
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cur.execute(
                    """
                    UPDATE app.analysis_notebook_schedules
                    SET next_due_at = %s, last_dispatch_note = %s
                    WHERE notebook_id = %s AND project_id = %s
                    """,
                    (
                        next_due_after(recurrence, now, zone_name),
                        _text(f"{type(exc).__name__}: {exc}", limit=500),
                        notebook_id,
                        nb_project,
                    ),
                )
            failed.append({"notebook_id": notebook_id, "reason": type(exc).__name__})
            continue
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
            cur.execute(
                """
                UPDATE app.analysis_notebook_schedules
                SET next_due_at = %s, last_dispatched_at = %s, last_run_id = %s,
                    last_dispatch_note = NULL
                WHERE notebook_id = %s AND project_id = %s
                """,
                (
                    next_due_after(recurrence, now, zone_name),
                    now,
                    run["run_id"],
                    notebook_id,
                    nb_project,
                ),
            )
        dispatched.append(
            {
                "notebook_id": notebook_id,
                "run_id": run["run_id"],
                "notebook_version_id": run["notebook_version_id"],
                "outcome": run["outcome"],
                "idempotent_replay": bool(run.get("idempotent_replay")),
                "idempotency_key": key,
            }
        )
    return {"considered": len(due), "dispatched": dispatched, "failed": failed}


@_names_its_check_violations
def run_notebook(
    conn,
    *,
    org_id: str,
    project_id: str,
    notebook_id: str,
    actor: str,
    idempotency_key: str,
    dispatch_source: str = "manual",
    requested_as_of: str | None = None,
) -> dict[str, Any]:
    """Accept one idempotent Run over the exact current Notebook version (AC6, AC7).

    Manual and scheduled dispatch call THIS. There is no second execution path, so
    a scheduled Run cannot produce weaker evidence than a manual one -- which is
    exactly what happens today, where `scheduler.py` and the API each build their
    own envelope.

    Retry with the same key returns the ORIGINAL Run rather than duplicating it.
    """
    from core.query_execution import accept_execution, run_execution  # noqa: PLC0415

    idempotency_key = _text(idempotency_key, limit=200)
    if not idempotency_key:
        raise ArtifactRefused(
            "missing_field",
            "a Notebook Run needs an idempotency key so a retry cannot duplicate it",
            [Refusal("missing_field", "idempotency_key", "idempotency_key")],
        )
    if dispatch_source not in {"manual", "scheduled"}:
        raise ArtifactRefused("invalid_dispatch", f"`{dispatch_source}` is not a dispatch", [])

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_version_id, archived_at FROM app.analysis_notebooks
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (notebook_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise ArtifactNotFound("notebook not found in this Project")
        # `archived_at` was SELECTed here and never tested, so the scheduler
        # refused an archived Notebook (`dispatch_due_notebook_schedules` filters
        # `archived_at IS NULL`) while a manual run accepted it. Two doors, two
        # answers, on a column nothing wrote -- invisible until the archive
        # gesture landed. The two doors agree now.
        if head[1] is not None:
            raise ArtifactRefused(
                "archived",
                "an archived Notebook does not run. Restore it before running it.",
                [],
            )
        if head[0] is None:
            raise ArtifactRefused("head_without_version", "this Notebook has no version to run", [])
        notebook_version_id = str(head[0])

        # Idempotency first, before any work: a retry must not re-execute.
        cur.execute(
            """
            SELECT id, notebook_version_id, state, outcome FROM app.analysis_notebook_runs
            WHERE notebook_id = %s AND idempotency_key = %s
            """,
            (notebook_id, idempotency_key),
        )
        existing = cur.fetchone()
    if existing is not None:
        return {
            "run_id": existing[0],
            "notebook_version_id": existing[1],
            "state": existing[2],
            "outcome": existing[3],
            "idempotent_replay": True,
            "blocks": get_notebook_run(
                conn, org_id=org_id, project_id=project_id, run_id=str(existing[0])
            )["blocks"],
        }

    run_id = f"nbkrun_{ULID()}"
    with conn.cursor() as cur:
        # AC7: the Run pins the exact version BEFORE any block executes. Resolving
        # it afterwards would let an edit mid-run change what the Run says it ran.
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_runs
                (id, notebook_id, notebook_version_id, org_id, project_id, idempotency_key,
                 dispatch_source, state, requested_as_of, actor)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted', %s, %s)
            """,
            (
                run_id,
                notebook_id,
                notebook_version_id,
                org_id,
                project_id,
                idempotency_key,
                dispatch_source,
                requested_as_of,
                actor,
            ),
        )
        cur.execute(
            """
            SELECT b.block_key, b.position, b.block_type, b.query_spec_version_id,
                   b.report_version_id, b.renders, b.presentation_absent_literal
            FROM app.analysis_notebook_version_blocks b
            WHERE b.notebook_version_id = %s AND b.project_id = %s
            ORDER BY b.position
            """,
            (notebook_version_id, project_id),
        )
        blocks = cur.fetchall()

    contract = render_contract_state(conn)
    statuses: list[str] = []
    for block_key, position, block_type, qsv_id, report_version_id, renders, absent in blocks:
        block_id = f"nbkrb_{ULID()}"
        if block_type == "narrative":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.analysis_notebook_run_blocks
                        (id, run_id, org_id, project_id, block_key, position, block_type,
                         render_absent_literal, status, started_at, ended_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'narrative', %s, 'succeeded', NOW(), NOW())
                    """,
                    (block_id, run_id, org_id, project_id, block_key, position, NO_RENDER),
                )
            statuses.append("succeeded")
            continue

        # Resolve the exact analytical input for this block. A report block runs
        # the Query Spec version ITS Report version pins -- not the Report head,
        # which could have moved since the composition was saved.
        resolved_qsv = qsv_id
        if block_type == "report":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT query_spec_version_id FROM app.analysis_report_versions
                    WHERE id = %s AND org_id = %s AND project_id = %s
                    """,
                    (report_version_id, org_id, project_id),
                )
                found = cur.fetchone()
            if found is None:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.analysis_notebook_run_blocks
                            (id, run_id, org_id, project_id, block_key, position, block_type,
                             report_version_id, render_absent_literal, status, limitation,
                             started_at, ended_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'failed', %s, NOW(), NOW())
                        """,
                        (
                            block_id, run_id, org_id, project_id, block_key, position,
                            block_type, report_version_id, NO_RENDER_BLOCK_FAILED,
                            "the pinned Report version no longer resolves in this Project",
                        ),
                    )
                statuses.append("failed")
                continue
            resolved_qsv = str(found[0])

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT spec, semantic_view_version_id FROM app.query_spec_versions
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (resolved_qsv, org_id, project_id),
            )
            spec_row = cur.fetchone()

        attempt = accept_execution(
            conn,
            org_id=org_id,
            project_id=project_id,
            query_spec_version_id=str(resolved_qsv),
            actor=actor,
        )
        result = run_execution(
            conn,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            spec=spec_row[0] or {},
            semantic_view_version_id=str(spec_row[1]),
        )

        # AC6: a rendered block records an EXACT Render, or an exact literal saying
        # why there is none. There is no third shape -- migration 154's CHECK
        # refuses a row that carries neither, and a branch that produced one would
        # raise a CheckViolation and lose the WHOLE Run, every other block with it.
        #
        # Three worlds, three literals, and which one it is depends on the
        # deployment rather than on the reader's interpretation:
        render_id = None
        block_limitation: str | None = None
        if not renders:
            # The composition itself says this block does not render.
            render_literal = NO_RENDER if absent is None else NO_RENDER_NO_PRESENTATION
        elif contract["available"]:
            # A Render is genuinely owed. Story 50.5 owns the dispatch that mints
            # it; this Run records the debt instead of pretending it does not exist.
            render_literal = NO_RENDER_NOT_DISPATCHED
            block_limitation = RENDER_NOT_DISPATCHED_LIMITATION
        else:
            # The presentation was accepted when the composition was saved, and the
            # registry it needs is no longer present in this deployment.
            render_literal = NO_RENDER_NO_PRESENTATION
            block_limitation = "; ".join(
                f"{m['contract']} (Story {m['owner_story']}) is missing: {m['missing_link']}"
                for m in contract["missing"]
            )

        status = "succeeded" if result["outcome"] == "success" else result["outcome"]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT started_at, ended_at FROM app.query_results
                WHERE id = %s AND org_id = %s AND project_id = %s
                """,
                (result["result_id"], org_id, project_id),
            )
            timing = cur.fetchone()
            cur.execute(
                """
                INSERT INTO app.analysis_notebook_run_blocks
                    (id, run_id, org_id, project_id, block_key, position, block_type,
                     query_spec_version_id, report_version_id, result_id, render_id,
                     render_absent_literal, status, limitation, started_at, ended_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    block_id, run_id, org_id, project_id, block_key, position, block_type,
                    resolved_qsv if block_type == "query" else None,
                    report_version_id, result["result_id"], render_id, render_literal,
                    status, block_limitation, timing[0], timing[1],
                ),
            )
        statuses.append(status)

    # AC6: the Run is inspectable even when one block fails. `partial` is a real
    # terminal state, not an error that hides the blocks that did succeed.
    if not statuses or all(s == "succeeded" for s in statuses):
        outcome = "succeeded"
    elif all(s in {"failed", "refused", "unavailable"} for s in statuses):
        outcome = "failed"
    else:
        outcome = "partial"

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.analysis_notebook_runs
            SET state = 'terminal', outcome = %s, terminal_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (outcome, run_id, org_id, project_id),
        )
    return {
        "run_id": run_id,
        "notebook_id": notebook_id,
        "notebook_version_id": notebook_version_id,
        "state": "terminal",
        "outcome": outcome,
        "idempotent_replay": False,
        "render": contract,
        "blocks": get_notebook_run(conn, org_id=org_id, project_id=project_id, run_id=run_id)[
            "blocks"
        ],
    }


def get_notebook_run(conn, *, org_id: str, project_id: str, run_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, notebook_id, notebook_version_id, idempotency_key, dispatch_source,
                   state, outcome, requested_as_of, resolved_as_of, actor, accepted_at,
                   terminal_at
            FROM app.analysis_notebook_runs
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (run_id, org_id, project_id),
        )
        run = cur.fetchone()
        if run is None:
            raise ArtifactNotFound("notebook run not found in this Project")
        cur.execute(
            """
            SELECT block_key, position, block_type, query_spec_version_id, report_version_id,
                   result_id, render_id, render_absent_literal, status, limitation,
                   resolved_as_of, started_at, ended_at
            FROM app.analysis_notebook_run_blocks
            WHERE run_id = %s AND project_id = %s ORDER BY position
            """,
            (run_id, project_id),
        )
        blocks = cur.fetchall()
    return {
        "id": run[0],
        "notebook_id": run[1],
        "notebook_version_id": run[2],
        "idempotency_key": run[3],
        "dispatch_source": run[4],
        "state": run[5],
        "outcome": run[6],
        "requested_as_of": run[7],
        "resolved_as_of": run[8],
        "actor": run[9],
        "accepted_at": run[10].isoformat() if run[10] else None,
        "terminal_at": run[11].isoformat() if run[11] else None,
        "blocks": [
            {
                "block_key": b[0],
                "position": b[1],
                "block_type": b[2],
                "query_spec_version_id": b[3],
                "report_version_id": b[4],
                "result_id": b[5],
                "render_id": b[6],
                "render_absent_literal": b[7],
                "status": b[8],
                "limitation": b[9],
                "resolved_as_of": b[10],
                "started_at": b[11].isoformat() if b[11] else None,
                "ended_at": b[12].isoformat() if b[12] else None,
            }
            for b in blocks
        ],
    }


# ---------------------------------------------------------------------------
# Renders.
# ---------------------------------------------------------------------------


def _ledger_adapter_for_build(conn, renderer_build_id: str) -> str | None:
    """The adapter the build ledger records for this renderer build, if it has it.

    Raises `LedgerUnreadable` rather than answering `None` on a failed read, for
    the same reason `_registered_families` does: "this build is not registered"
    and "I could not find out" are two facts, and only the first is an answer.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT renderer_id FROM {_RENDERER_REGISTRY_TABLE} WHERE id = %s",  # noqa: S608
                (renderer_build_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        raise LedgerUnreadable(
            f"{_RENDERER_REGISTRY_TABLE} could not be read: {type(exc).__name__}"
        ) from exc
    return str(row[0]) if row and row[0] else None


def _renderer_adapter_pin(conn, payload: dict[str, Any]) -> str:
    """The adapter this Render freezes: DERIVED from the ledger, never trusted.

    WHY IT IS NOT AN ELEVENTH PIN. The ratified replay contract
    (`visualization-and-rendering.md:446-450`) names "renderer and runtime build"
    and no adapter, and the adapter is a property OF that build: `renderer_build`
    is `<family>/<renderer_id>@<semver>` and the ledger stores `renderer_id` in a
    column of its own. A derivable value is not asked of a caller.

    But `ck_renders_pins_are_exact` requires the adapter STORED, exactly, on the
    row -- so the service derives it from the ledger and pins it. Deriving it from
    what the caller sent would make the client a second authority on which
    renderer drew the chart, which is the thing a replay pin exists to prevent.

    THE THREE ANSWERS, in order:

      * the ledger knows this build -> its `renderer_id` is the pin. A caller that
        also sent one and DISAGREES is refused with both identities shown, never
        redrawn silently (`visualization-and-rendering.md:857`);
      * the ledger does not know this build -> nothing can be derived, so the
        adapter is required as a named pin. Sent: it is used. Absent: `missing_pin`
        on `renderer_adapter`, naming the gesture;
      * the ledger could not be READ -> that is not "not registered". The caller's
        own adapter is used when they sent one, and the read failure is logged.
    """
    supplied = payload.get("renderer_adapter")
    supplied = supplied.strip() if isinstance(supplied, str) else ""
    try:
        registered = _ledger_adapter_for_build(conn, str(payload["renderer_build_id"]).strip())
    except LedgerUnreadable as exc:
        logger.error("analyze_artifacts: renderer_build_ledger_unreadable: %s", exc)
        registered = None
    if registered:
        if supplied and supplied != registered:
            raise ArtifactRefused(
                "renderer_pin_disagrees",
                "the renderer named for this Render is not the one that build draws with",
                [
                    Refusal(
                        "renderer_pin_disagrees",
                        f"this deployment draws `{payload['renderer_build_id']}` with "
                        f"`{registered}`, and the request names `{supplied}`",
                        "renderer_adapter",
                        remedy="leave the renderer out of the request: the build that drew "
                        "the visual is what names it",
                    )
                ],
            )
        return registered
    if supplied:
        if supplied.lower() in _FORBIDDEN_PIN_VALUES:
            raise ArtifactRefused(
                "incomplete_render",
                "the Render was refused on 1 pin(s)",
                [
                    Refusal(
                        "placeholder_pin",
                        f"replay pin `renderer_adapter` may not be `{supplied}` -- "
                        "name the exact identity",
                        "renderer_adapter",
                        remedy="freeze the Render from a visual this deployment ships, so the "
                        "renderer that drew it names itself",
                    )
                ],
            )
        return supplied
    raise ArtifactRefused(
        "incomplete_render",
        "the Render was refused on 1 pin(s)",
        [
            Refusal(
                "missing_pin",
                "this deployment does not register the renderer build "
                f"`{payload['renderer_build_id']}`, so which renderer drew this visual cannot "
                "be derived and must be named",
                "renderer_adapter",
                remedy="freeze the Render from a visual this deployment ships -- its renderer "
                "then names itself -- or name the renderer that drew this one",
            )
        ],
    )


@_names_its_check_violations
def create_render(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one immutable Render, or refuse and name every pin that is missing.

    This function is where AC8 either holds or does not. It performs three checks
    in order, and each of them can only ever REFUSE -- there is no branch that
    substitutes a default, and no argument that can relax one:

      1. the downstream identity registries (Stories 50.4/50.5) exist;
      2. every one of the ten replay pins is present;
      3. none of them is a placeholder word.

    Check 1 PASSES on a migrated deployment of this repository -- migrations 156
    and 160 created both registries, and `render_contract_state` was measured
    returning `available: true` on 2026-08-17. Renders are written; the INSERT
    below is reached. Check 1 is kept for the unmigrated case, where it reports
    `unavailable` with the exact missing links rather than raising an error.

    So the refusal a caller actually meets here is check 2 or 3: a missing pin, or
    a placeholder word where an exact identity belongs. A family absent from
    `registered_families` fails that way too -- it has no build id to name.

    AND THE ADAPTER, WHICH IS DERIVED RATHER THAN ASKED FOR (AI-357, 2026-09-01).
    `app.renders.renderer_adapter` is under `ck_renders_pins_are_exact` and was
    written as `payload.get("renderer_adapter") or ""` -- so a caller that filled
    every pin the ratified contract names got a mute 500 from the CHECK, and the
    registry of families never offered the value that would have avoided it.
    `_renderer_adapter_pin` reads it off the build ledger and pins it; a caller who
    names a different one is refused with both identities; and a CHECK that still
    refuses this write now arrives as a named refusal, never as a traceback.
    """
    contract = render_contract_state(conn)
    if not contract["available"]:
        raise ArtifactRefused(
            "render_contract_unavailable",
            "a canonical Render cannot be created until its replay contract exists",
            [
                Refusal(
                    "missing_contract",
                    f"{m['contract']} is owned by Story {m['owner_story']} and has not landed "
                    f"({m['missing_link']})",
                    m["contract"],
                )
                for m in contract["missing"]
            ],
        )

    refusals: list[Refusal] = []
    for pin_name, field in RENDER_REPLAY_PINS:
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            refusals.append(Refusal("missing_pin", f"replay pin `{pin_name}` is required", field))
            continue
        if isinstance(value, str) and value.strip().lower() in _FORBIDDEN_PIN_VALUES:
            refusals.append(
                Refusal(
                    "placeholder_pin",
                    f"replay pin `{pin_name}` may not be `{value}` -- name the exact identity",
                    field,
                )
            )
    if not isinstance(payload.get("evidence_manifest"), dict) or not payload["evidence_manifest"]:
        refusals.append(
            Refusal("missing_pin", "the evidence manifest may not be empty", "evidence_manifest")
        )
    if refusals:
        raise ArtifactRefused(
            "incomplete_render", f"the Render was refused on {len(refusals)} pin(s)", refusals
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.content_hash FROM app.query_results r
            WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
            """,
            (payload["result_id"], org_id, project_id),
        )
        result = cur.fetchone()
    if result is None:
        raise ArtifactNotFound("result not found in this Project")

    #  Derived from the ledger, then pinned on the row. It is deliberately NOT
    #  added to the hashed document: the adapter is a property of
    #  `renderer_build_id`, which the document already carries, so hashing it too
    #  would change what `render.v1` means without adding one fact.
    renderer_adapter = _renderer_adapter_pin(conn, payload)
    document = {
        "contract_version": RENDER_CONTRACT_VERSION,
        **{field: payload.get(field) for _, field in RENDER_REPLAY_PINS},
    }
    render_id = f"rnd_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.renders
                (id, org_id, project_id, result_id, result_content_hash,
                 visualization_spec_version_id, renderer_adapter, renderer_build_id,
                 runtime_build_id, theme_version, formatter_version, responsive_profile,
                 display_state, evidence_manifest, datum_evidence_keys, creation_surface,
                 origin_kind, origin_report_run_id, origin_notebook_run_id, content_hash,
                 created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)
            RETURNING created_at
            """,
            (
                render_id, org_id, project_id, payload["result_id"], str(result[0]),
                payload["visualization_spec_version_id"],
                renderer_adapter,
                payload["renderer_build_id"], payload["runtime_build_id"],
                payload["theme_version"], payload["formatter_version"],
                payload["responsive_profile"],
                _canonical_json(payload.get("display_state") or {}),
                _canonical_json(payload["evidence_manifest"]),
                _canonical_json(payload.get("datum_evidence_keys") or {}),
                payload.get("creation_surface") or "explore",
                payload.get("origin_kind") or "explore",
                payload.get("origin_report_run_id"),
                payload.get("origin_notebook_run_id"),
                canonical_hash(document),
                actor,
            ),
        )
        created_at = cur.fetchone()[0]
    return {
        "id": render_id,
        "result_id": payload["result_id"],
        "content_hash": canonical_hash(document),
        "created_at": created_at.isoformat() if created_at else None,
    }


def list_renders(
    conn, *, org_id: str, project_id: str, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    """Server-paginated canonical Renders (AC9). Legacy snapshots are elsewhere."""
    limit = max(1, min(int(limit or 50), 200))
    params: list[Any] = [org_id, project_id]
    where = "r.org_id = %s AND r.project_id = %s"
    if cursor:
        where += " AND r.id < %s"
        params.append(cursor)
    params.append(limit + 1)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT r.id, r.result_id, r.visualization_spec_version_id, r.renderer_adapter,
                   r.renderer_build_id, r.runtime_build_id, r.theme_version,
                   r.formatter_version, r.responsive_profile, r.origin_kind,
                   r.creation_surface, r.created_by, r.created_at,
                   (SELECT COUNT(*) FROM app.render_retention_actions a
                     WHERE a.render_id = r.id) AS retention_actions
            FROM app.renders r WHERE {where}
            ORDER BY r.id DESC LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "renders": [
            {
                "id": r[0],
                "result_id": r[1],
                "visualization_spec_version_id": r[2],
                "renderer_adapter": r[3],
                "renderer_build_id": r[4],
                "runtime_build_id": r[5],
                "theme_version": r[6],
                "formatter_version": r[7],
                "responsive_profile": r[8],
                "origin_kind": r[9],
                "creation_surface": r[10],
                "created_by": r[11],
                "created_at": r[12].isoformat() if r[12] else None,
                # AC9: a Render whose runtime material has been retired is
                # distinguishable from one that never had it.
                "replayable": int(r[13] or 0) == 0,
            }
            for r in rows
        ],
        "next_cursor": rows[-1][0] if has_more and rows else None,
        "contract": render_contract_state(conn),
    }


def get_render(conn, *, org_id: str, project_id: str, render_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_id, result_content_hash, result_payload_retained,
                   visualization_spec_version_id, renderer_adapter, renderer_build_id,
                   runtime_build_id, theme_version, formatter_version, responsive_profile,
                   display_state, evidence_manifest, datum_evidence_keys, creation_surface,
                   origin_kind, origin_report_run_id, origin_notebook_run_id,
                   predecessor_render_id, content_hash, created_by, created_at
            FROM app.renders WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (render_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ArtifactNotFound("render not found in this Project")
        cur.execute(
            """
            SELECT action, reason, policy_ref, actor, created_at
            FROM app.render_retention_actions WHERE render_id = %s AND project_id = %s
            ORDER BY created_at DESC
            """,
            (render_id, project_id),
        )
        retention = cur.fetchall()
    return {
        "id": row[0],
        "result_id": row[1],
        "result_content_hash": row[2],
        "result_payload_retained": bool(row[3]),
        "visualization_spec_version_id": row[4],
        "renderer_adapter": row[5],
        "renderer_build_id": row[6],
        "runtime_build_id": row[7],
        "theme_version": row[8],
        "formatter_version": row[9],
        "responsive_profile": row[10],
        "display_state": row[11],
        "evidence_manifest": row[12],
        "datum_evidence_keys": row[13],
        "creation_surface": row[14],
        "origin_kind": row[15],
        "origin_report_run_id": row[16],
        "origin_notebook_run_id": row[17],
        "predecessor_render_id": row[18],
        "content_hash": row[19],
        "created_by": row[20],
        "created_at": row[21].isoformat() if row[21] else None,
        # AC9, same rule as `list_renders`: a Render whose runtime material has
        # been retired is distinguishable from one that never had it. The detail
        # route did NOT carry this key while `RenderDetail extends RenderSummary`
        # declared it, so every reader of `render.replayable` on a detail page
        # read `undefined` — falsy, and indistinguishable from "retired".
        "replayable": len(retention) == 0,
        "retention_actions": [
            {
                "action": a[0], "reason": a[1], "policy_ref": a[2], "actor": a[3],
                "created_at": a[4].isoformat() if a[4] else None,
            }
            for a in retention
        ],
        # AC15: this story does not create a public Share. The Sharing tab reads
        # this, and shows a read-only migration state.
        "sharing": {
            "canonical_share_available": False,
            "reason": "AD-30 fragment exchange for one Render is owned by Story 50.7",
        },
    }


# ---------------------------------------------------------------------------
# Legacy classification. A READ, and only a read.
# ---------------------------------------------------------------------------


def classify_legacy_artifacts(conn, *, project_id: str) -> dict[str, Any]:
    """Count and classify the legacy rows, without changing one of them (AC12).

    Restartable by construction: it writes nothing, so running it twice gives the
    same answer, and it can never make a count "look complete" by deleting a row.
    Each classification names the exact pin that is missing, because "legacy" on
    its own tells a reader nothing about what would have to be true to promote it.
    """
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE enabled) FROM app.project_reports "
            "WHERE project_id = %s",
            (project_id,),
        )
        total, enabled = cur.fetchone()
        out["project_reports"] = {
            "total": int(total),
            "enabled": int(enabled),
            "classification": "seed_availability",
            "reason": "an enablement toggle carries no Query Spec version and no history; "
            "it is seed availability, never a configured Report",
        }

        cur.execute("SELECT COUNT(*) FROM app.notebooks WHERE project_id = %s", (project_id,))
        out["notebooks"] = {
            "total": int(cur.fetchone()[0]),
            "classification": "legacy_mutable_definition",
            "reason": "one mutable report_ref/window_rule/prompt row with no version history",
        }

        cur.execute(
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE r.envelope_ref = 'deferred'),
                   COUNT(*) FILTER (WHERE r.envelope_inline IS NULL AND r.envelope_ref IS NULL),
                   COUNT(*) FILTER (WHERE r.status = 'error')
            FROM app.notebook_runs r
            JOIN app.notebooks n ON n.id = r.notebook_id
            WHERE n.project_id = %s
            """,
            (project_id,),
        )
        total, deferred, no_envelope, errored = cur.fetchone()
        out["notebook_runs"] = {
            "total": int(total),
            "deferred_envelope": int(deferred),
            "no_evidence_at_all": int(no_envelope),
            "errored": int(errored),
            "classification": "legacy_incomplete_evidence",
            "reason": "no per-block Result or Render pin exists; `deferred` is shown as "
            "incomplete and is never reconstructed from current state",
        }

        cur.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE widget_uri IS NOT NULL) "
            "FROM app.render_snapshots WHERE project_id = %s",
            (project_id,),
        )
        total, with_widget = cur.fetchone()
        out["render_snapshots"] = {
            "total": int(total),
            "with_widget_uri": int(with_widget),
            "promotable_to_canonical_render": 0,
            "classification": "legacy_unverifiable",
            "missing_pins": [name for name, _ in RENDER_REPLAY_PINS],
            "reason": "a frozen envelope pins none of the ten replay inputs, so none of these "
            "rows can become a canonical Render without fabricating every one",
        }

        cur.execute(
            """
            SELECT COUNT(*), COUNT(*) FILTER (WHERE s.revoked_at IS NULL)
            FROM app.render_snapshot_shares s
            JOIN app.render_snapshots r ON r.id = s.snapshot_id
            WHERE r.project_id = %s
            """,
            (project_id,),
        )
        total, live = cur.fetchone()
        out["render_snapshot_shares"] = {
            "total": int(total),
            "live": int(live),
            "classification": "legacy_raw_token_share",
            "reason": "a path bearer token; preserved for revocation evidence only. Story 50.7 "
            "owns the AD-30 exchange that replaces it",
        }

        cur.execute(
            "SELECT COUNT(*) FROM app.analysis_reports WHERE project_id = %s", (project_id,)
        )
        canonical_reports = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM app.analysis_notebooks WHERE project_id = %s", (project_id,)
        )
        canonical_notebooks = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM app.renders WHERE project_id = %s", (project_id,))
        canonical_renders = int(cur.fetchone()[0])

    out["canonical"] = {
        "reports": canonical_reports,
        "notebooks": canonical_notebooks,
        "renders": canonical_renders,
    }
    out["render_contract"] = render_contract_state(conn)
    return out


def list_legacy_notebooks(
    conn, *, project_id: str, limit: int = 50, runs_per_notebook: int = 20
) -> list[dict[str, Any]]:
    """The legacy Notebook definitions and their Runs, readable, with what they hold.

    AC12 requires legacy definitions and Runs to "remain readable with their actual
    evidence and limitations". They were readable through `NotebooksPanel.tsx`
    until the canonical screens took over the Analyze sections and that panel
    stopped being mounted anywhere -- which made the requirement false without one
    line of it being repealed. This route is what makes it true again, and it is a
    READ: nothing here writes, promotes, backfills or reconstructs.

    What it deliberately does NOT return:

      * the share token on `app.notebooks`. It is a plaintext path bearer, it is
        preserved for revocation evidence (Story 50.7 owns the replacement), and a
        list response is exactly where the old code leaked one. Only whether a
        share exists is reported.
      * the stored envelope body. The legacy envelope is untrusted stored text and
        the browser has no honest way to replay it; shipping it here is what let
        the old gallery open a widget window and retry `postMessage`.

    Every limitation is stated per row rather than summarized once, because a
    reader looking at ONE Run needs to know what THAT Run lacks.
    """
    limit = max(1, min(int(limit or 50), 200))
    runs_per_notebook = max(1, min(int(runs_per_notebook or 20), 100))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, report_ref, window_rule, narrative_prompt, scheduled,
                   schedule_rule, (share_token IS NOT NULL) AS is_shared, shared_at,
                   created_by, created_at, updated_at
            FROM app.notebooks WHERE project_id = %s
            ORDER BY updated_at DESC LIMIT %s
            """,
            (project_id, limit),
        )
        rows = cur.fetchall()
        notebooks: list[dict[str, Any]] = []
        for r in rows:
            cur.execute(
                """
                SELECT id, executed_at, as_of, status, error_message, envelope_ref,
                       (envelope_inline IS NOT NULL) AS has_inline,
                       COALESCE(array_length(pull_ids, 1), 0) AS pull_id_count,
                       LEFT(summary_text, 280) AS summary_snippet
                FROM app.notebook_runs WHERE notebook_id = %s
                ORDER BY executed_at DESC LIMIT %s
                """,
                (r[0], runs_per_notebook),
            )
            run_rows = cur.fetchall()
            runs = []
            for run in run_rows:
                envelope_ref, has_inline = run[5], bool(run[6])
                if envelope_ref == "deferred":
                    evidence = "deferred"
                    limitation = (
                        "the envelope was too large to store inline and no blob was ever "
                        "written; `deferred` is shown as incomplete and is never "
                        "reconstructed from current state"
                    )
                elif has_inline:
                    evidence = "inline_envelope"
                    limitation = (
                        "one whole-notebook envelope, with no per-block Result and no "
                        "Render pin; it cannot be promoted to a canonical Run"
                    )
                else:
                    evidence = "absent"
                    limitation = "this Run recorded no envelope at all"
                runs.append(
                    {
                        "id": run[0],
                        "executed_at": run[1].isoformat() if run[1] else None,
                        "as_of": run[2],
                        "status": run[3],
                        "error_message": run[4],
                        "evidence": evidence,
                        "pull_id_count": int(run[7] or 0),
                        "summary_snippet": run[8],
                        "result_id": None,
                        "render_id": None,
                        "limitation": limitation,
                        "classification": "legacy_incomplete_evidence",
                    }
                )
            notebooks.append(
                {
                    "id": r[0],
                    "title": r[1],
                    "report_ref": r[2],
                    "window_rule": r[3],
                    "narrative_prompt": r[4],
                    "scheduled": bool(r[5]),
                    "schedule_rule": r[6],
                    "is_shared": bool(r[7]),
                    "shared_at": r[8].isoformat() if r[8] else None,
                    "created_by": r[9],
                    "created_at": r[10].isoformat() if r[10] else None,
                    "updated_at": r[11].isoformat() if r[11] else None,
                    "classification": "legacy_mutable_definition",
                    "limitation": (
                        "one mutable report_ref/window_rule/prompt row with no version "
                        "history: every Run below refers to a definition that may since "
                        "have been edited in place"
                    ),
                    "runs": runs,
                }
            )
    return notebooks


def list_legacy_snapshots(
    conn, *, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The legacy snapshot browser, labelled as such (AC9, AC12).

    It returns no envelope body and no widget URI: the collection's job is to say
    what exists and why it is not replayable, and shipping the envelope here is
    what let the old gallery open a widget window and retry `postMessage`.
    """
    limit = max(1, min(int(limit or 50), 200))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tool_name, summary_snippet, question, identity, trace_id, created_at,
                   (widget_uri IS NOT NULL) AS had_widget
            FROM app.render_snapshots WHERE project_id = %s
            ORDER BY created_at DESC LIMIT %s
            """,
            (project_id, limit),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "tool_name": r[1],
            "summary_snippet": r[2],
            "question": r[3],
            "identity": r[4],
            "trace_id": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "had_widget_uri": bool(r[7]),
            "classification": "legacy_unverifiable",
            "replayable": False,
            "missing_pins": [name for name, _ in RENDER_REPLAY_PINS],
        }
        for r in rows
    ]
