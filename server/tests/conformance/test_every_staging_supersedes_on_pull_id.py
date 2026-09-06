"""Every module staging model supersedes on `pull_id`. No exception, and none reserved.

WHAT THIS CLOSES, AND WHY IT IS A CLASS AND NOT AN INSTANCE. Measured 2026-08-30:
of the 54 module staging models, 53 superseded on `pull_id` and ONE did not --
`stg_taboola_history` was a bare `SELECT *` over an append-only raw table
(AD-7), so the second pull of any overlapping window returned the same immutable
record twice and its `unique` test went red on real data. Nothing anywhere in
the repo could see that. The connector-coverage ratchet
(`test_fact_daily_kpi_connector_coverage.py`) counts which models REACH the
fact, not what they do to a re-pull, and taboola counted "reached" through its
other model; the dbt tests only run where a fixture exists. The defect was found
by reading 54 files, which is exactly the kind of reading a guard replaces.

WHY IT MATTERS MORE THAN ONE RED TEST. `docs/product-architecture/execution-substrate.md`
makes the whole retry design rest on this line -- "Retry is the repair mechanism,
not a tolerated accident. The raw zone is append-only (AD-7) and every staging
model supersedes on `pull_id`" -- and its `Incomplete if` #5 says the surface is
unfinished while "a retried unit is not idempotent". Cloud Tasks retries, a
backfill re-runs a window, an operator re-pulls a day: each of those lands the
rows AGAIN by design. A staging model without supersede turns every one of those
into duplicated facts, and it is the LANDING path that decides, not the caller.

THREE THINGS THIS GUARD IS BUILT NOT TO DO:

  * Measure its own copy. The count of models it found travels with the
    assertion: a glob that breaks and returns zero files fails HERE rather than
    reporting a serene 100%.
  * Cover one package and hide the rest. Its scope is every
    `server/modules/*/dbt/staging/stg_*.sql`, which is the whole population --
    not the connectors someone remembered to list.
  * Pass on prose. Comments are stripped before matching, so a model that only
    MENTIONS `pull_id` in a header does not satisfy it.

NO EXCEPTION SET. Not "empty for now": there is no reason a landing may
duplicate on retry, so a model that cannot supersede is a model whose grain is
not established -- which is a design answer to give, not a name to add here.
"""

from __future__ import annotations

import re
from pathlib import Path

_MODULES = Path(__file__).resolve().parents[2] / "modules"

#: The population measured on 2026-08-30. A FLOOR, not an equality: adding a
#: connector must not make this file red, but losing half the population to a
#: renamed folder must.
_KNOWN_STAGING_MODELS = 54

_SQL_COMMENT = re.compile(r"--[^\n]*")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _staging_models() -> list[Path]:
    return sorted(_MODULES.glob("*/dbt/staging/stg_*.sql"))


def _sql_without_comments(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return _SQL_COMMENT.sub("", _JINJA_COMMENT.sub("", text))


#: THE RANKING FUNCTIONS A SUPERSEDE MAY USE, and why there are two (AI-342).
#:
#: This guard asked for `ROW_NUMBER()` by name, which is the SPELLING of the
#: repair rather than the property. The property is *the latest pull per natural
#: key wins*, and `DENSE_RANK() OVER (PARTITION BY ... ORDER BY pull_id DESC) = 1`
#: holds it exactly: the rank is on the PULL, so rank 1 is the whole of the latest
#: pull. `ROW_NUMBER` keeps ONE ROW of it, which is right for a landing whose
#: declared grain is unique within a pull and WRONG for one whose grain legitimately
#: holds several cells -- `stg_youtube_breakdown`, where a report declaring two
#: dimensions lands the same marginal once per cell of its cross, and where the
#: `ROW_NUMBER` form silently dropped rows.
#:
#: Both remain SUPERSEDES: neither can leave rows of an older pull behind, which is
#: what `Incomplete if` #5 is about. A model with no ranking window at all still
#: fails, and so does one whose partition drops `project_id` or whose order is not
#: on `pull_id`.
_RANKERS = r"(?:ROW_NUMBER|DENSE_RANK)"


def _window_bodies(sql: str) -> list[str]:
    """The inside of every ranking `OVER ( ... )`, parens balanced.

    A regex cannot do this: a partition key like `json_extract_string(x, '$.k')`
    carries its own parentheses, and `[^)]*` would stop at the first of them and
    declare the window malformed.
    """
    bodies: list[str] = []
    for match in re.finditer(rf"{_RANKERS}\s*\(\s*\)\s*OVER\s*\(", sql, re.IGNORECASE):
        depth = 1
        index = match.end()
        while index < len(sql) and depth:
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
            index += 1
        if depth == 0:
            bodies.append(sql[match.end() : index - 1])
    return bodies


def _supersede_failure(path: Path) -> str | None:
    """The reason this model does not supersede, or None."""
    sql = _sql_without_comments(path)
    if not re.search(r"\bQUALIFY\b", sql, re.IGNORECASE):
        return "no QUALIFY: nothing keeps one row per grain"
    for body in _window_bodies(sql):
        parts = re.split(r"\bORDER\s+BY\b", body, flags=re.IGNORECASE)
        if len(parts) < 2:
            continue
        if not re.search(r"\bPARTITION\s+BY\b", parts[0], re.IGNORECASE):
            continue
        if "project_id" not in parts[0]:
            return "the supersede grain does not carry project_id: it dedups across tenants"
        if "pull_id" not in parts[-1]:
            return "the supersede window does not order on pull_id"
        return None
    return "no ROW_NUMBER()/DENSE_RANK() OVER (PARTITION BY ... ORDER BY ...) window"


def test_the_population_is_whole() -> None:
    """A broken glob is red HERE, not silently green everywhere else."""
    found = _staging_models()
    assert len(found) >= _KNOWN_STAGING_MODELS, (
        f"only {len(found)} staging models discovered under {_MODULES}, "
        f"{_KNOWN_STAGING_MODELS} were measured on 2026-08-30 -- the guard is "
        f"reading the wrong tree, so its verdict below means nothing."
    )


def test_every_staging_model_supersedes_on_pull_id() -> None:
    offenders = {
        path.relative_to(_MODULES).as_posix(): reason
        for path in _staging_models()
        if (reason := _supersede_failure(path)) is not None
    }
    assert not offenders, (
        "a staging model duplicates its rows when the pull that fed it is retried "
        "(execution-substrate, Incomplete if #5). Every raw row carries the pull "
        "that landed it; keep the latest pull per natural key:\n"
        "    QUALIFY ROW_NUMBER() OVER (\n"
        "        PARTITION BY project_id, <natural key>\n"
        "        ORDER BY pull_id DESC\n"
        "    ) = 1\n"
        "  -- or DENSE_RANK() in place of ROW_NUMBER() when the grain legitimately\n"
        "  -- holds several rows within ONE pull: the rank is then on the pull, so\n"
        "  -- rank 1 keeps all of the latest one instead of one row of it.\n"
        + "".join(f"  {model}: {reason}\n" for model, reason in sorted(offenders.items()))
    )
