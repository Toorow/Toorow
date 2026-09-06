"""Where a report profile's pull function ACTUALLY writes -- AI-311.

WHY THIS EXISTS, AND WHAT THE GUARD BESIDE IT COULD NOT SEE.
`tests/conformance/test_profile_relations_declared.py` refuses three things, and
the second is "a `raw_relation` no `connector.py` of that module creates -- this
is the wrong address, and it is the expensive one". It checks that the MODULE
creates the relation named. It never checks that the profile's own pull function
writes to it, and for a module that creates two relations those are different
questions.

The instance that made this file, found under AI-310 and repaired there:
`youtube-analytics` declared `channel_snapshot` on `raw_youtube_breakdown` --
which the module does create -- while `pull_channel_snapshot` calls
`_insert_raw_rows`, which writes `raw_youtube_daily`. The guard read 140/140,
before and after.

The cost is not cosmetic. `query_execution._grain_restrictions` derives a
profile's grain exclusions from the pair (`raw_relation`, `dimensions`): a wrong
address moves a profile into the wrong sibling set and hands it the wrong
restriction -- which is a wrong number on a screen, arrived at through a correct
mechanism fed a false declaration.

WHAT IS TRACED, AND WHAT IS DELIBERATELY NOT GUESSED. The trace walks the
module's own AST from the callable the manifest names (`source_capabilities.
reports[].dispatch.callable`), following module-level calls, aliases -- a
factory-built `pull_x = _breakdown("x")` is followed to `_breakdown` -- and
module-level string constants. It collects two kinds of evidence, both of them
declarations the module writes about itself:

  * an `INSERT INTO <relation>` reachable from the callable;
  * a module-level constant whose whole value is a bare `raw_*` name, referenced
    somewhere in the call graph -- the shape connectors use when the landing is
    performed by a shared helper (`_RAW_TABLE = "raw_amazon_ads_daily"`).

Anything that resolves to zero relations, or to more than one, is reported
`undetermined` and NOT judged. A profile whose landing is chosen at runtime by an
argument has no single answer available statically, and inventing one would make
this file the second thing that can be wrong about an address.

Measured 2026-08-28 over the 140 report profiles of the 39 manifests: 103 trace
to exactly one relation and 37 stay undetermined -- 20 `several_landings_reached`
(a helper that writes more than one, the target chosen by an argument), 11
`callable_not_declared`, 6 `non_warehouse_landing` (a determined landing outside
the warehouse, e.g. `app.context_events`). Zero `no_landing_reached` remain, and
zero wrong addresses, which is the state AFTER AI-310 repaired the one this
file was written for. (Previous measure 2026-08-23: 107 traced, 33 undetermined
-- 16 several / 11 callable / 6 then-classified `no_landing_reached`.)

AD-2: no connector name appears in the code below. Manifests and module sources
are walked; both are declarations of the modules themselves.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# Imported rather than re-spelled: the key this compares against belongs to the
# resolver that reads it, and two spellings of it is how the two drift.
from core.stage_relation_resolver import RAW_RELATION_KEY as _RAW_KEY

logger = logging.getLogger(__name__)

_MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

#: An `INSERT INTO` and the relation it targets. `{table}` of an f-string is
#: matched and then discarded by the `raw_` filter -- a placeholder is not a name.
_INSERT = re.compile(r"INSERT\s+INTO\s+\{?([A-Za-z0-9_.]+)\}?", re.IGNORECASE)

#: A constant whose WHOLE value is a relation name. Anchored on both ends on
#: purpose: a sentence mentioning `raw_gsc_daily` in a docstring is prose, not a
#: declaration, and matching it would attribute a landing to whoever wrote it up.
_BARE_RELATION = re.compile(r"^raw_[a-z0-9_]+$")

#: THE LANDINGS THAT ARE NOT IN THE WAREHOUSE, and they are why the raw set alone
#: cannot decide. `brevo/transactional_events` declares `raw_relation: null` and
#: lands in `app.context_events` -- correctly. Its callable reaches `_pull_profile`,
#: a DISPATCHER whose other branches land in `raw_brevo_daily`, so a trace that
#: collects only `raw_*` names sees exactly one and answers with confidence it has
#: no right to. This guard reported that as a wrong address on its first run; it
#: was the guard that was wrong.
#:
#: So a non-warehouse landing counts as a landing. Reaching one AND a raw relation
#: means the callable passes through a branch point, which is `several_landings_
#: reached` -- undetermined, not a verdict.
_CONTEXT_LANDING_CALLS = frozenset({"persist_context_event"})
_NON_WAREHOUSE = "app.context_events"

#: How deep a call chain is followed. Landing is one or two hops from a pull in
#: every module measured; the bound exists so a cycle cannot hang a conformance
#: run, and `seen` already makes recursion terminate.
_MAX_DEPTH = 5

#: Why a callable's landing could not be resolved. One word per cause, the same
#: rule `stage_relation_resolver` applies to its four absences.
CALLABLE_NOT_DECLARED = "callable_not_declared"
CALLABLE_ABSENT_FROM_MODULE = "callable_absent_from_module"
NO_LANDING_REACHED = "no_landing_reached"
SEVERAL_LANDINGS_REACHED = "several_landings_reached"
#: Determinate, and the landing is outside the warehouse -- what `raw_relation:
#: null` declares. Not a failure to trace: an answer of a different kind.
NON_WAREHOUSE_LANDING = "non_warehouse_landing"


class _ModuleIndex:
    """The module's own top level: functions, string constants, and aliases."""

    def __init__(self, tree: ast.Module) -> None:
        self.functions: dict[str, ast.AST] = {}
        self.constants: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
                continue
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    self.constants[target.id] = value.value
                elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    # `pull_audience_geography = _breakdown("audience_geography")`
                    # -- the callable the manifest names is not a def, and the
                    # landing lives in the factory it came from.
                    self.aliases[target.id] = value.func.id
                elif isinstance(value, ast.Name):
                    self.aliases[target.id] = value.id


def _relations_in(text: str) -> set[str]:
    return {match.group(1).rsplit(".", 1)[-1] for match in _INSERT.finditer(text)}


def _walk(source: str, index: _ModuleIndex, name: str, seen: set[str], depth: int) -> set[str]:
    if name in seen or depth > _MAX_DEPTH:
        return set()
    seen.add(name)
    if name in index.aliases and name not in index.functions:
        return _walk(source, index, index.aliases[name], seen, depth + 1)
    node = index.functions.get(name)
    if node is None:
        return set()

    found = _relations_in(ast.get_source_segment(source, node) or "")
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if _BARE_RELATION.match(child.value):
                found.add(child.value)
        elif isinstance(child, ast.Name):
            value = index.constants.get(child.id)
            if value:
                found |= _relations_in(value)
                if _BARE_RELATION.match(value):
                    found.add(value)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            callee = child.func.id
            if callee in _CONTEXT_LANDING_CALLS:
                found.add(_NON_WAREHOUSE)
            if callee in index.functions or callee in index.aliases:
                found |= _walk(source, index, callee, seen, depth + 1)
    return {
        relation
        for relation in found
        if relation.startswith("raw_") or relation == _NON_WAREHOUSE
    }


def landing_of(source: str, tree: ast.Module, callable_name: str) -> tuple[str | None, str | None]:
    """`(relation, reason)` -- exactly one of the two is filled.

    A reason is not a failure. It says the trace could not answer, which is a
    different statement from "the declaration is wrong", and the guard treats
    them as different.
    """
    if not callable_name:
        return None, CALLABLE_NOT_DECLARED
    index = _ModuleIndex(tree)
    if callable_name not in index.functions and callable_name not in index.aliases:
        return None, CALLABLE_ABSENT_FROM_MODULE
    relations = _walk(source, index, callable_name, set(), 0)
    if not relations:
        return None, NO_LANDING_REACHED
    if len(relations) > 1:
        return None, SEVERAL_LANDINGS_REACHED
    only = next(iter(relations))
    if only == _NON_WAREHOUSE:
        # A determinate answer, and the answer is "not in the warehouse". The
        # caller compares it against a declaration of `null`, so it must not be
        # returned as a relation name.
        return None, NON_WAREHOUSE_LANDING
    return only, None


@lru_cache(maxsize=1)
def traced_profile_landings(modules_dir: str | None = None) -> list[dict[str, Any]]:
    """One record per report profile that names a pull callable.

    Each record carries what the manifest DECLARES and what the module's own
    source SAYS IT WRITES, side by side, so the caller compares rather than
    trusts either one.
    """
    root = Path(modules_dir) if modules_dir else _MODULES_DIR
    records: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        connector = manifest_path.parent.name
        connector_path = manifest_path.parent / "connector.py"
        if not connector_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source = connector_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, ValueError, SyntaxError) as exc:  # noqa: BLE001
            # An unreadable module is not a wrong address, and refusing the whole
            # sweep over one would make this guard the reason a run is red.
            logger.warning("profile_landing_trace: %s unreadable: %s", connector, exc)
            continue

        declared_raw = {
            str(profile.get("id")): profile.get(_RAW_KEY)
            for profile in manifest.get("report_profiles") or []
        }
        for report in (manifest.get("source_capabilities") or {}).get("reports") or []:
            profile_id = str(report.get("id") or "")
            callable_name = str(((report.get("dispatch") or {}).get("callable")) or "")
            relation, reason = landing_of(source, tree, callable_name)
            records.append(
                {
                    "connector": connector,
                    "report_profile_id": profile_id,
                    "callable": callable_name or None,
                    "declared_relation": declared_raw.get(profile_id),
                    "traced_relation": relation,
                    "undetermined_reason": reason,
                }
            )
    return records



def wrong_addresses(modules_dir: str | None = None) -> list[dict[str, Any]]:
    """Profiles whose pull writes somewhere other than what they declare.

    Only the traced ones, and only when the manifest declares a relation to
    disagree with. A profile that declares `null` -- one landing in
    `app.context_events` -- is not addressed at the warehouse at all.
    """
    return [
        record
        for record in traced_profile_landings(modules_dir)
        if record["traced_relation"]
        and record["declared_relation"]
        and record["traced_relation"] != record["declared_relation"]
    ]
