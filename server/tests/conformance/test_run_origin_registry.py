"""One place knows WHY a run exists -- and no site mints a run nothing finishes.

Story 63.7. Two guards live here, and they are two halves of one rule.

THE FIRST is the pattern of `test_execution_state_registry.py` and
`test_pull_job_state_registry.py`, applied to a third vocabulary: the ORIGIN of
an execution. No Python reader re-lists the origins, the console mirror matches
entry for entry, and every path that mints an execution stamps one instead of
writing a literal into a projection plan.

THE SECOND IS A SWEEP OF THE MINTING SITES, and it is the reason this file could
not have been written before the refusal that ships with it. It finds every
module that calls `create_execution` and demands, for each, a DECLARED path that
carries the execution to a terminal state -- because an execution nothing
advances is not an orphan row. `uq_datastream_executions_active` allows at most
one non-terminal execution per Datastream: a run stuck in `created` answers 409
to every later publication and makes `open_collection_run` return `None` every
following night. The flux loses its progression, permanently.

Measured 2026-08-06, two sites did exactly that -- `bounded_recovery` for
`Synchronize` / `Reload` / `Reprocess`, and `operations_mcp` for its `retry` /
`refetch` verbs -- and both gestures were offered on screen. Until they refused,
this sweep would have been red on every run, and a suite that is always red is
not a report.

WHAT THE SWEEP PROVES AND WHAT IT DOES NOT. It does not prove reachability by
symbolic execution; it proves that every minting module DECLARES the mechanism
that finishes its run, that the mechanism still exists in the module named for
it, and that a NEW minting site -- or one whose consumer was deleted or renamed
-- turns this test red rather than shipping a Datastream that locks itself.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from core import run_origins

ROOT = Path(__file__).resolve().parents[3]
SERVER_CORE = ROOT / "server" / "core"
CONSOLE_MIRROR = (
    ROOT / "ui" / "admin" / "src" / "datastreams" / "workbench" / "runOrigins.ts"
)


#: `server/core/*.py` on 2026-08-31. A FLOOR, not an equality. Criterion 13 of
#: `docs/product-architecture/module-boundaries.md`: three tests here walk this
#: glob and end on `assert not offenders`, which is what a glob of nothing says.
_CORE_MODULES_AT_2026_08_31 = 524


def test_the_registry_walk_still_reaches_core_and_the_console() -> None:
    """A moved `core/` or a moved mirror must be red HERE, not silently clean."""
    modules = sorted(SERVER_CORE.glob("*.py"))
    assert len(modules) >= _CORE_MODULES_AT_2026_08_31, (
        f"{len(modules)} modules globbed under {SERVER_CORE}, "
        f"{_CORE_MODULES_AT_2026_08_31} on 2026-08-31 -- every 'no offender' "
        "verdict below is about a tree that moved."
    )
    assert CONSOLE_MIRROR.is_file() and CONSOLE_MIRROR.read_text(encoding="utf-8").strip(), (
        f"{CONSOLE_MIRROR} is gone or empty. The mirror test compares two lists "
        "entry for entry, and an absent mirror compares nothing."
    )


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The registry is total: every origin answers every question.
# ---------------------------------------------------------------------------


def test_every_origin_is_classified_on_every_axis() -> None:
    assert run_origins.RUN_ORIGINS, "the registry is empty"
    for origin in run_origins.RUN_ORIGINS:
        assert re.fullmatch(r"[a-z_]+", origin.key), origin.key
        # The label is what a person reads: English words, never the wire key.
        assert origin.label and origin.label[0].isupper(), origin.key
        assert origin.label != origin.key, origin.key
        assert isinstance(origin.reads_provider_windows, bool), origin.key
        assert isinstance(origin.has_engine, bool), origin.key
    keys = [o.key for o in run_origins.RUN_ORIGINS]
    assert len(keys) == len(set(keys)), "a key is declared twice"
    labels = [o.label for o in run_origins.RUN_ORIGINS]
    assert len(labels) == len(set(labels)), "two origins would read the same"


def test_the_derived_sets_partition_the_registry() -> None:
    keys = set(run_origins.ORIGIN_KEYS)
    assert set(run_origins.MINTABLE_ORIGINS) | set(run_origins.REFUSED_ORIGINS) == keys
    assert not set(run_origins.MINTABLE_ORIGINS) & set(run_origins.REFUSED_ORIGINS)
    assert set(run_origins.COLLECTION_ORIGINS) <= keys


def test_an_unknown_origin_is_never_folded_onto_a_neighbour() -> None:
    """A build older than its server shows what it received, and claims nothing."""
    assert run_origins.label_for("an_origin_no_build_knows") is None
    assert run_origins.label_for(None) is None
    # And it never claims the run reads provider windows, which is what decides
    # whether a missing progress bar gets an explanation.
    assert run_origins.reads_provider_windows("an_origin_no_build_knows") is False
    assert run_origins.has_engine("an_origin_no_build_knows") is False


def test_stamping_refuses_an_invented_key_and_a_verb_with_no_engine() -> None:
    stamped = run_origins.stamp_origin({"executable": True}, run_origins.REFETCH)
    assert stamped == {"executable": True, "origin": "refetch"}
    # The source plan is not mutated: a caller's dict is its own.
    assert run_origins.origin_of({"executable": True}) is None

    with pytest.raises(run_origins.RunOriginError) as invented:
        run_origins.stamp_origin({}, "an_origin_no_build_knows")
    assert invented.value.code == "unknown_origin"

    for key in run_origins.REFUSED_ORIGINS:
        with pytest.raises(run_origins.RunOriginError) as refused:
            run_origins.stamp_origin({}, key)
        assert refused.value.code == run_origins.NO_ENGINE
        # The refusal says what was NOT done, and what would have broken.
        assert "Nothing was created." in refused.value.message
        assert "publication lock" in refused.value.message


def test_the_refusal_names_the_verb_a_person_pressed() -> None:
    for key in run_origins.REFUSED_ORIGINS:
        assert run_origins.refusal_message(key).startswith(run_origins.label_for(key))
    # And an unnamed verb still gets a sentence rather than a bare code.
    assert run_origins.refusal_message(None).startswith("This recovery")


# ---------------------------------------------------------------------------
# Layer 1 -- the Python writers. One stamp, no literals.
# ---------------------------------------------------------------------------

#: The five paths of story 63.7 section 1 that DO mint, and the module each
#: lives in. `datastream_activation` mints the first candidate of a Datastream
#: confirmed through setup, which is the same origin as `first_candidate`.
_MINTING_PATHS = (
    "server/core/execution_progress.py",
    "server/core/datastream_change.py",
    "server/core/datastream_first_candidate.py",
    "server/core/datastream_activation.py",
    "server/core/country_activation.py",
)


@pytest.mark.parametrize("relative", _MINTING_PATHS)
def test_every_minting_path_stamps_an_origin(relative: str) -> None:
    """A projection plan reaches `create_execution` through `stamp_origin`.

    Not "mentions an origin somewhere": the stamp is the only function that
    validates the key against this registry AND refuses a verb with no engine,
    so a path that writes `plan["origin"] = ...` by hand would be a sixth
    vocabulary with none of the guarantees.
    """
    source = _source(relative)
    assert "stamp_origin" in source, f"{relative} mints without stamping an origin"
    assert "run_origins" in source, f"{relative} does not read the registry"


def test_the_projection_schema_admits_the_stamp_it_receives() -> None:
    """The compiled plan is `additionalProperties: false`, and four paths stamp it.

    `datastream_change`, `datastream_first_candidate`, `datastream_activation`
    and `country_activation` all hand `create_execution` a plan compiled by
    `datastream_projection`, which validates against a CLOSED schema. Nothing
    re-validates a STORED plan today -- but `inbound_ingest` reuses one verbatim
    to drive an import, and the day something does, an undeclared `origin` would
    break those four paths at once. So the schema declares it, and this test is
    what refuses a schema edit that takes it back out.
    """
    import jsonschema
    from core.datastream_projection import _PROJECTION_SCHEMA_PATH

    schema = json.loads(_PROJECTION_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema.get("additionalProperties") is False, (
        "this test exists because the schema is closed; if it opened, say so here"
    )
    assert "origin" in schema["properties"]
    # And the vocabulary is NOT repeated in JSON: a third copy of the ten keys is
    # a third thing to keep in step, and the registry is the one that decides.
    declared = json.dumps(schema["properties"]["origin"])
    assert not [key for key in run_origins.ORIGIN_KEYS if f'"{key}"' in declared]

    validator = jsonschema.Draft202012Validator(schema)
    stamped = run_origins.stamp_origin({}, run_origins.MAPPING_CHANGE)
    assert not [
        error for error in validator.iter_errors(stamped)
        if "origin" in error.message or "additional" in error.message.lower()
    ]


def test_no_python_reader_keeps_its_own_copy_of_the_origin_list() -> None:
    """Three or more origin keys in one literal is a second vocabulary.

    Read from the AST, so naming the origins in a comment or a docstring --
    where it is exactly right -- never trips it. `run_origins.py` itself is the
    one place allowed to hold the list.
    """
    known = set(run_origins.ORIGIN_KEYS)
    offenders: list[str] = []
    for path in sorted(SERVER_CORE.glob("*.py")):
        if path.name == "run_origins.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                names = [
                    e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = re.findall(r"'([a-z_]+)'", node.value)
            hits = [n for n in names if n in known]
            if len(hits) >= 3:
                offenders.append(f"{path.name}: {sorted(set(hits))}")
    assert not offenders, (
        "these modules re-declare the run origins instead of importing "
        f"core.run_origins: {offenders}"
    )


# ---------------------------------------------------------------------------
# Layer 2 -- THE SWEEP. Every mint has a path to a terminal state.
# ---------------------------------------------------------------------------

#: Every module that mints an execution, and what finishes the run it minted.
#:
#: Each entry names the mechanism in words, then the evidence: `(module, symbol)`
#: pairs that must ALL still exist. The pairs are what make this a guard rather
#: than a comment -- delete `close_collection_run_if_complete`, rename
#: `enqueue_activation_work`, and this test names the mint that has just been
#: orphaned.
_TERMINAL_PATHS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "execution_progress.py": (
        "opens the run, enqueues its windows as pull jobs, and closes it when "
        "the last window is terminal",
        (
            ("execution_progress.py", "def close_collection_run_if_complete"),
            ("queue.py", "def _record_window_outcome"),
        ),
    ),
    "datastream_change.py": (
        "enqueues one candidate_materialization activation job, which the "
        "activation worker advances",
        (
            ("datastream_change.py", "enqueue_activation_work"),
            ("datastream_activation.py", "advance_state("),
        ),
    ),
    "datastream_first_candidate.py": (
        "enqueues one candidate_materialization activation job",
        (
            ("datastream_first_candidate.py", "enqueue_activation_work"),
            ("datastream_activation.py", "advance_state("),
        ),
    ),
    "datastream_activation.py": (
        "advances the candidate itself, in the same module",
        (("datastream_activation.py", "advance_state("),),
    ),
    "country_activation.py": (
        "enqueues one candidate_materialization activation job",
        (
            ("country_activation.py", "enqueue_activation_work"),
            ("datastream_activation.py", "advance_state("),
        ),
    ),
    "datastream_publication.py": (
        "advances the execution it mints for an inbound precondition failure "
        "straight to `failed`, in the same call",
        (("datastream_publication.py", "advance_state("),),
    ),
    "recent_first_publication.py": (
        "drives the candidate it mints through to publication, in the same "
        "module",
        (("recent_first_publication.py", "advance_state("),),
    ),
    "managed_feed_ledger.py": (
        "mints the candidate an import owns; the import driver advances it",
        (
            ("import_runner.py", "open_import"),
            ("import_runner.py", "advance_state("),
        ),
    ),
    "external_bq_registration.py": (
        "returns the minted candidate to its caller, which runs the 12.5 gates "
        "and advances it through the execution-state route",
        (
            ("external_bq_registration.py", "observe_and_register"),
            # AD-40 (2026-08-12): the state route left `admin_api.py` with the
            # rest of the `/api/datastreams/…` family. The declaration follows
            # the SYMBOL, which is the whole point of naming one -- a path was
            # never what finished a run.
            ("datastream_executions_api.py", "def _advance_datastream_execution_state"),
        ),
    ),
    "datastream_executions_api.py": (
        "is the execution route family itself: what it mints, the state route "
        "advances -- both in this module since AD-40",
        ((
            "datastream_executions_api.py",
            "def _advance_datastream_execution_state",
        ),),
    ),
}


def _minting_modules() -> set[str]:
    """Every module of `server/core` that calls `create_execution`, from the AST."""
    minting: set[str] = set()
    for path in sorted(SERVER_CORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "create_execution":
                minting.add(path.name)
    # The module that DEFINES it mints on its own account too; the AST above
    # already catches its one internal call site.
    return minting


def test_every_site_that_mints_an_execution_declares_what_finishes_it() -> None:
    """A mint with no path to a terminal state locks the Datastream forever.

    This is the guard that could only be placed with the refusal of story 63.7:
    `bounded_recovery` and `operations_mcp` minted runs that no `enqueue` and no
    `advance_state` would ever touch, and the gestures were on screen.
    """
    swept = _minting_modules()
    declared = set(_TERMINAL_PATHS)
    orphaned = sorted(swept - declared)
    assert not orphaned, (
        "these modules mint an execution and NOTHING here declares what carries "
        "it to a terminal state -- a run left non-terminal holds "
        "uq_datastream_executions_active and answers 409 to every later "
        f"publication and every following night's collection: {orphaned}"
    )
    stale = sorted(declared - swept)
    assert not stale, (
        f"these modules no longer mint an execution; drop their entry: {stale}"
    )


@pytest.mark.parametrize("module", sorted(_TERMINAL_PATHS))
def test_the_declared_terminal_path_still_exists(module: str) -> None:
    """The mechanism named for a mint is still in the code that was named."""
    _reason, evidence = _TERMINAL_PATHS[module]
    for evidence_module, symbol in evidence:
        source = _source(f"server/core/{evidence_module}")
        assert symbol in source, (
            f"{module}'s run is finished by `{symbol}` in {evidence_module}, "
            "and that symbol is gone -- the mint is now an orphan"
        )


def test_a_module_that_refuses_for_want_of_an_engine_mints_nothing() -> None:
    """Refusing and minting are contradictory: one of the two is the defect.

    Read from the sweep rather than from a list of module names, so the rule
    holds for the next verb somebody offers before building its engine.
    """
    minting = _minting_modules()
    offenders = []
    for path in sorted(SERVER_CORE.glob("*.py")):
        if path.name == "run_origins.py":
            continue
        source = path.read_text(encoding="utf-8")
        refuses = "NO_ENGINE" in source or "refusal_message" in source
        if refuses and path.name in minting:
            offenders.append(path.name)
    assert not offenders, (
        "these modules answer 'no engine' AND still mint an execution: "
        f"{offenders}"
    )


def test_the_three_bounded_verbs_refuse_through_the_registry() -> None:
    """The gesture is offered, so the refusal has to be the server's own word.

    `bounded_recovery` is where a person presses Synchronize / Reload /
    Reprocess, and `operations_mcp` is the same three verbs through the tools.
    Both answer the ONE sentence `run_origins.refusal_message` writes -- a
    console that had to compose its own would be a second explanation for the
    same refusal.
    """
    minting = _minting_modules()
    for module in ("bounded_recovery.py", "operations_mcp.py"):
        source = _source(f"server/core/{module}")
        assert "run_origins" in source, f"{module} does not read the registry"
        assert "NO_ENGINE" in source, f"{module} does not answer the one code"
        assert module not in minting, (
            f"{module} still mints an execution nothing would advance"
        )


# ---------------------------------------------------------------------------
# Layer 3 -- the console.
# ---------------------------------------------------------------------------


def _console_registry() -> list[dict[str, object]]:
    """Parse the TS mirror's table into plain data, without running a bundler."""
    source = CONSOLE_MIRROR.read_text(encoding="utf-8")
    body = source.split("RUN_ORIGINS: readonly RunOriginEntry[] = [", 1)[1]
    body = body.split("\n];", 1)[0]
    rows: list[dict[str, object]] = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        as_json = re.sub(r"(\w+):", r'"\1":', line)
        rows.append(json.loads(as_json))
    return rows


def test_the_console_mirror_matches_the_registry_entry_for_entry() -> None:
    """The screen may not hold a second opinion about why a run exists."""
    assert _console_registry() == run_origins.as_registry_rows(), (
        "ui/admin/src/datastreams/workbench/runOrigins.ts has drifted from "
        "server/core/run_origins.py -- update both, in the same change"
    )


def test_the_ratified_surface_says_what_this_build_does() -> None:
    """A behaviour that changed and a document that did not is half a story.

    Two things had to reach `datastream-workbench-and-wizard.md`: that a
    treatment now says WHY it is running, and that three verbs the surface still
    OFFERS refuse on this build. The second matters more -- a reader who finds
    `Synchronize` listed and presses it deserves to have been told.
    """
    surface = ROOT / "docs" / "product-architecture" / "datastream-workbench-and-wizard.md"
    text = surface.read_text(encoding="utf-8")

    assert "server/core/run_origins.py" in text
    assert "runOrigins.ts" in text
    # The refusal, and the damage it stops, in the document that lists the verbs.
    assert "refused on this build" in text
    assert "publication lock" in text
    # And the sweep, named where somebody adding a sixth minting path will read it.
    assert "test_run_origin_registry.py" in text
    # The two decisions a later session would otherwise re-litigate.
    assert "This update reads no provider window" in text
    assert "at most one" in text


def test_the_progress_route_carries_the_origin_and_not_the_plan() -> None:
    """One derived field on the wire, never the whole projection plan.

    `projection_plan_ref` carries the compiled projection, the retained
    execution reference and the recovery scope: none of that is a screen's
    business, and story 63.7 refuses to widen a polled payload to ship it.
    """
    from core.datastream_progress_api import PROGRESS_FIELDS, PROGRESS_SQL

    assert "origin" in PROGRESS_FIELDS
    assert "projection_plan_ref" not in PROGRESS_FIELDS
    # The plan travels on the row the poll was ALREADY reading -- no second
    # statement and no second connection on the one route of this product called
    # in a loop -- and the origin is derived from it in Python, through the
    # registry, so the wire value is resolved in exactly one place.
    assert "projection_plan_ref" in PROGRESS_SQL
    assert "origin_of(" in _source("server/core/datastream_progress_api.py")
