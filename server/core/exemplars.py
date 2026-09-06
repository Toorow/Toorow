"""Story 75-3 -- the approved exemplars an agent may imitate, pinned and budgeted.

WHAT AN EXEMPLAR IS. The CURRENT version of a Golden Question whose head is
`active`: the business question, the expected path pattern, and the expected
answer shape -- plus, when the evaluation layer has judged one, the observed
walks that passed a comparison against that exact version.

WHAT IT REFUSES TO BE.

  * NOT a reader of the evaluation corpus. That corpus is test code
    (`analyze-and-test.md:243-247`); nothing here opens it, imports it or names
    it, and a test asserts that about this file's source.
  * NOT an inventor of an approval state. There is no approval column on an
    observed walk and this module adds none. "Approved" is derived from rows
    that already exist -- a passing path comparison, on observed evidence, on a
    finalized walk, pinned to the served version -- and the amendment of
    2026-09-05 in `docs/product-architecture/context-hub.md` writes the rule
    down together with the stricter baseline reading it did not take.
  * NOT a source of moving pins. Every identity served here is the exact stored
    one: a Semantic View version, a Business Domain version number, a Query Spec
    version, a measure's `version_id`. A caller can replay what it reads.

THE ORDER IS THE PRODUCT'S, NOT THE DATABASE'S. Most recently STEWARDED first:
`golden_questions.updated_at DESC`, then `id DESC`. Three gestures move that
column -- creating the question, publishing a new version, changing the
lifecycle -- so it is not an activation date and is never served as one: the
payload calls it `last_stewarded_at`, which is what it holds. The id tie-break
makes two calls with the same budget serve the same prefix.

TWO CUTS, AND BOTH ARE REPORTED. The budget is the caller's; `MAX_EXEMPLARS` is
the server's, and a bound nobody is told about is a truncation that lies. The
head query counts every matching active question before it limits, so
`omitted_beyond_max_exemplars` and `omitted_for_budget` add up to
`matching_active_questions - served`, and `truncated` is true when either cut
bit.

THE BUDGET IS AN ESTIMATE, AND SAYS SO. There is no tokenizer in this
deployment. The estimator is the UTF-8 bytes of the canonical JSON divided by
four -- the same one `scripts/mcp_tool_surface_report.py` states for the wire
catalog. `estimated_bytes` and `estimated_tokens` share ONE basis: the sum over
the served exemplars, exemplar by exemplar, which is the very quantity the cut
was made on. The list's own punctuation is not charged to either.

THE BUDGET IS THE MODEL CHANNEL'S, AND IT IS DERIVED FROM IT (2026-09-06).
Gate G16 measured, on revision `mcp-server-00274`, what a budget invented beside
the channel produces:

    "exemplars": {"withheld": "moved_to_app_channel", "bytes": 4391},
    "served": 1, "matching_active_questions": 1

`DEFAULT_BUDGET_TOKENS` was 1200 -- about 4800 bytes at the stated estimator --
while `model_channel.MODEL_CHANNEL_MAX_BYTES` is 4096 for the WHOLE
model-visible `structuredContent`. `partition_envelope` therefore moved the one
key that carries the answer to the app channel and the model was served a
withheld marker: the tool answered, honestly, with the single thing it exists
not to say. So the budget is no longer a number of its own. It is what the
channel has left:

    EXEMPLAR_LIST_MAX_BYTES = MODEL_CHANNEL_MAX_BYTES - ENVELOPE_RESERVE_BYTES

HOW THE RESERVE WAS MEASURED, and it is two numbers added.

  1. THE ENVELOPE AROUND AN EMPTY LIST: 1006 bytes. Serialized with
     `model_channel.serialized_bytes` -- the same expression the gate uses --
     over `_envelope(payload, provenance=..., freshness="live")` where the
     payload is this module's answer with `exemplars: []`: the subject at its
     maximum accepted id length (200 characters, `normalize_subject`), every
     summary counter, `token_estimate_method`, `exemplar_store_state`, the
     `identity` `get_exemplars` appends, and `meta` carrying freshness,
     provenance and an empty `alerts`. The real fixture measures 657; 1006 is
     the worst case a caller can reach, and a reserve sized on the typical case
     is a reserve that fails on the wide one.
  2. THE `meta.ai_settings` BLOCK: 1024 bytes, `ai_settings.ENVELOPE_BLOCK_MAX_BYTES`.
     Story 75-4 puts it in every agent-surface envelope and `partition_envelope`
     never moves a `meta` honesty field, so it is unmovable weight on every
     call. It was unbounded until 2026-09-06 -- 40 rules of 280 characters in
     each of two lists serialize to 23029 bytes -- and `ai_settings.MAX_RULES_BYTES`
     is the write-time bound that now holds it to its declared share.

1006 + 1024 = 2030, declared as 2048: the next power of two, the 18 bytes of
slack covering the list punctuation -- the commas and the two brackets -- that
the per-exemplar estimator deliberately charges to nobody. That
leaves 2048 bytes, 512 tokens, and `DEFAULT_BUDGET_TOKENS` and the clamp ceiling
are BOTH that number -- a caller cannot ask for a budget the channel cannot
carry, and one that tries gets the ceiling with `truncated` saying so.

A HEAD ALONE OVER THE BUDGET IS TRIMMED, NOT WITHHELD AND NOT DROPPED. It used
to be "still served" whole, which is precisely how a 4391-byte list reached a
4096-byte channel. `_trim_head` cuts it in a stated order -- assertions, the
approved walks (their steps first, then the walks themselves), the reference
paths, the question text, and the expected path pattern only when nothing else
was enough -- each cut carried by the counter that already declares it
(`assertions_omitted`, `steps_omitted`, `approved_paths_omitted`,
`reference_paths_omitted`, `question_omitted_chars`) plus `trimmed_for_budget`
on the head and on the payload.

THE PINS OUTLIVE THE WALKS, and that order is the story's (arbitrated
2026-09-06). An exemplar IS its version pins -- the amendment's own words are
"the pins, and they are exact", one entry per reference path with the
`{metric_id, version_id}` measures it carries. The judged walks are
ILLUSTRATIONS of a question that was answered well; a caller that loses them
loses an example, a caller that loses the reference paths loses the identity of
what it is looking at. So the walks go first. A head that carries both cannot
be served whole at any budget -- a reference path plus one judged walk of two
steps measures ~2900 bytes against a 2048-byte list -- so the choice is not
between keeping both and keeping one; it is only about which one survives.
"""

from __future__ import annotations

import json
from typing import Any

from core.model_channel import MODEL_CHANNEL_MAX_BYTES

#: The three ways a caller can name what it is about to answer. Each is a join
#: that already exists; none of them widens to "everything".
SUBJECT_TYPES = ("metric", "view", "topic")

#: The one lifecycle this surface serves. The epic says "published"; the product
#: word in `golden_questions.LIFECYCLES` is `active`, and there is no third.
SERVED_LIFECYCLE = "active"

#: Bytes per token. A stated estimator, never a measurement.
BYTES_PER_TOKEN = 4

#: What the envelope around the list costs before a single exemplar is added:
#: 1006 bytes of AD-1 envelope at the widest ids a caller can name, plus the
#: 1024 bytes `ai_settings.ENVELOPE_BLOCK_MAX_BYTES` reserves for
#: `meta.ai_settings`. 2030 measured, 2048 declared -- the module docstring
#: carries the measurement and how it was taken.
ENVELOPE_RESERVE_BYTES = 2048

#: All the model channel has left for the list itself. NOT a number of its own:
#: a budget larger than this is a budget whose answer `partition_envelope` moves
#: to the app channel, which serves the model a withheld marker instead of the
#: exemplars (G16, revision `mcp-server-00274`).
EXEMPLAR_LIST_MAX_BYTES = MODEL_CHANNEL_MAX_BYTES - ENVELOPE_RESERVE_BYTES

DEFAULT_BUDGET_TOKENS = EXEMPLAR_LIST_MAX_BYTES // BYTES_PER_TOKEN
MIN_BUDGET_TOKENS = 120
#: The ceiling IS the default: there is nothing above it the channel can carry.
MAX_BUDGET_TOKENS = DEFAULT_BUDGET_TOKENS

#: Hard bounds, applied before the budget. A budget large enough to ask for the
#: whole Project would make one read unbounded, which no read here may be.
#: `MAX_EXEMPLARS` is DECLARED to the caller -- it rides the payload as
#: `max_exemplars` and its cut rides `omitted_beyond_max_exemplars` -- because a
#: bound the caller cannot see is one it cannot raise its budget past.
MAX_EXEMPLARS = 25
MAX_REFERENCE_PATHS = 8
MAX_MEASURES = 12
MAX_ASSERTIONS = 8
MAX_APPROVED_PATHS = 3
MAX_PATH_STEPS = 12

#: Below this a truncated question is no longer a question, so the ladder stops
#: cutting it and moves on to the expected pattern instead.
MIN_QUESTION_CHARS = 80

STORE_AVAILABLE = "available"
STORE_UNAVAILABLE = "unavailable"

TOKEN_ESTIMATE_METHOD = "utf-8 bytes of the canonical JSON // 4"


class ExemplarSubjectRefused(ValueError):
    """A subject this surface cannot resolve -- refused by name, never widened."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(value: Any) -> str:
    """The one serialization every estimate here is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def estimated_bytes(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def estimate_tokens(value: Any) -> int:
    """UTF-8 bytes of the canonical JSON // 4. An estimate, and named as one."""
    return estimated_bytes(value) // BYTES_PER_TOKEN


def normalize_subject(subject_type: Any, subject_id: Any) -> tuple[str, str]:
    """Resolve the subject BEFORE anything is read.

    Deliberately outside the store: a subject nobody can name is a refusal the
    caller can repair, not an outage, and the resilience path around the read
    must never be able to swallow it into "no exemplars found".
    """
    kind = subject_type.strip().lower() if isinstance(subject_type, str) else ""
    if kind not in SUBJECT_TYPES:
        raise ExemplarSubjectRefused(
            "unknown_subject_type",
            f"`{subject_type}` is not one of {', '.join(SUBJECT_TYPES)}",
        )
    identifier = subject_id.strip() if isinstance(subject_id, str) else ""
    if not identifier or len(identifier) > 200:
        raise ExemplarSubjectRefused(
            "missing_subject_id",
            f"a {kind} exemplar lookup names the {kind} it is about",
        )
    return kind, identifier


def clamp_budget(budget_tokens: Any) -> int:
    """A budget outside the declared range is clamped, never honoured blindly."""
    try:
        wanted = int(budget_tokens)
    except (TypeError, ValueError):
        wanted = DEFAULT_BUDGET_TOKENS
    return max(MIN_BUDGET_TOKENS, min(wanted, MAX_BUDGET_TOKENS))


def org_for_project(conn, project_id: str) -> str | None:
    """The organization of a Project the caller has already been allowed to read.

    Read from the Project row rather than re-derived from the access decision:
    the scope guard has already answered "may this caller read this Project",
    and asking a second authority the same question is how two answers start to
    disagree.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# The three selectors. Each one is an existing join, written once.
# ---------------------------------------------------------------------------

#: A reference path of the served version, resolved to its Query Spec version.
#: Both selectors that go through a Query Spec share it, so they cannot drift.
_REFERENCE_PATH_JOIN = """
    FROM app.golden_question_reference_paths rp
    JOIN app.query_spec_versions qsv
      ON qsv.id = rp.query_spec_version_id
     AND qsv.org_id = rp.org_id AND qsv.project_id = rp.project_id
   WHERE rp.golden_question_version_id = v.id
     AND rp.org_id = q.org_id AND rp.project_id = q.project_id
"""

#: A caller may name the View or one of its versions; both are exact pins.
_VIEW_CLAUSE = "(v.semantic_view_id = %s OR v.semantic_view_version_id = %s)"

#: `spec.measures` holds `{"id", "version_id"}` entries -- containment matches
#: the id without pretending to know a version the caller did not name.
_METRIC_CLAUSE = f"EXISTS (SELECT 1 {_REFERENCE_PATH_JOIN} AND qsv.spec -> 'measures' @> %s::jsonb)"

_TOPIC_CLAUSE = f"""EXISTS (
    SELECT 1 {_REFERENCE_PATH_JOIN}
      AND EXISTS (
            SELECT 1
              FROM app.answerable_topic_query_bindings b
              JOIN app.answerable_topics t
                ON t.id = b.answerable_topic_id
               AND t.org_id = b.org_id AND t.project_id = b.project_id
             WHERE b.query_spec_version_id = rp.query_spec_version_id
               AND b.org_id = rp.org_id AND b.project_id = rp.project_id
               AND t.topic_key = %s
          )
)"""


def _subject_predicate(subject_type: str, subject_id: str) -> tuple[str, list[Any]]:
    if subject_type == "view":
        return _VIEW_CLAUSE, [subject_id, subject_id]
    if subject_type == "metric":
        return _METRIC_CLAUSE, [json.dumps([{"id": subject_id}])]
    return _TOPIC_CLAUSE, [subject_id]


#: `q.updated_at` is aliased HERE and ordered on BY THAT ALIAS below: the column
#: that decides the prefix is the same one the payload serves, under the same
#: name, so a caller reading `last_stewarded_at` reads the sort key itself.
_HEAD_COLUMNS = """
    q.id, q.title, q.owner, q.lifecycle, q.updated_at AS last_stewarded_at,
    v.id, v.version_number, v.question, v.time_boundary,
    v.expected_ai_path, v.expected_result, v.result_type, v.severity,
    v.capability_tags, v.semantic_view_id, v.semantic_view_version_id,
    v.semantic_view_version_role, v.business_domain_id,
    v.business_domain_version_number, v.content_hash, v.contract_version
"""


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _bounded(items: list[Any], limit: int) -> tuple[list[Any], int]:
    return items[:limit], max(0, len(items) - limit)


def _fetch_heads(
    conn, *, org_id: str, project_id: str, subject_type: str, subject_id: str
) -> tuple[list[dict[str, Any]], int]:
    """The bounded prefix of matching heads, AND how many matched in all.

    `COUNT(*) OVER ()` is evaluated over the whole matching set before `LIMIT`
    takes its slice, so the second number costs no second query and no second
    predicate that could drift from the first.
    """
    clause, params = _subject_predicate(subject_type, subject_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_HEAD_COLUMNS}, COUNT(*) OVER () AS matching_total
              FROM app.golden_questions q
              JOIN app.golden_question_versions v
                ON v.id = q.current_version_id
               AND v.org_id = q.org_id AND v.project_id = q.project_id
             WHERE q.org_id = %s AND q.project_id = %s
               AND q.lifecycle = %s
               AND {clause}
             ORDER BY last_stewarded_at DESC, q.id DESC
             LIMIT %s
            """,
            (org_id, project_id, SERVED_LIFECYCLE, *params, MAX_EXEMPLARS),
        )
        rows = cur.fetchall()
    matching_total = int(rows[0][21]) if rows else 0
    heads: list[dict[str, Any]] = []
    for r in rows:
        assertions, assertions_omitted = _bounded(list(r[10] or []), MAX_ASSERTIONS)
        heads.append(
            {
                "golden_question_id": r[0],
                "title": r[1],
                "owner": r[2],
                "lifecycle": r[3],
                # NOT `activated_at`: `golden_questions.updated_at` moves on
                # create, on a new version and on any lifecycle change. Naming
                # it after one of the three would have a caller read an
                # activation date off a re-titled question.
                "last_stewarded_at": _iso(r[4]),
                "golden_question_version_id": r[5],
                "version_number": r[6],
                "question": r[7],
                "time_boundary": r[8],
                "expected_ai_path": r[9] or {},
                "expected_answer_shape": {
                    "result_type": r[11],
                    "severity": r[12],
                    "capability_tags": list(r[13] or []),
                    "assertions": assertions,
                    "assertions_omitted": assertions_omitted,
                },
                "pins": {
                    "golden_question_version_id": r[5],
                    "version_number": r[6],
                    "content_hash": r[19],
                    "contract_version": r[20],
                    "semantic_view_id": r[14],
                    "semantic_view_version_id": r[15],
                    "semantic_view_version_role": r[16],
                    "business_domain_id": r[17],
                    "business_domain_version_number": r[18],
                    "reference_paths": [],
                    "reference_paths_omitted": 0,
                },
                "approved_paths": [],
                "approved_paths_omitted": 0,
            }
        )
    return heads, matching_total


def _attach_reference_paths(
    conn, heads: list[dict[str, Any]], *, org_id: str, project_id: str, subject: tuple[str, str]
) -> None:
    """The Query Spec versions a question declares, with the measures they pin."""
    version_ids = [h["golden_question_version_id"] for h in heads]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rp.golden_question_version_id, rp.ordinal, rp.query_spec_version_id,
                   rp.role, qsv.query_spec_id, qsv.version_number,
                   qsv.semantic_view_id, qsv.semantic_view_version_id,
                   qsv.spec -> 'measures'
              FROM app.golden_question_reference_paths rp
              LEFT JOIN app.query_spec_versions qsv
                ON qsv.id = rp.query_spec_version_id
               AND qsv.org_id = rp.org_id AND qsv.project_id = rp.project_id
             WHERE rp.org_id = %s AND rp.project_id = %s
               AND rp.golden_question_version_id = ANY(%s)
             ORDER BY rp.golden_question_version_id, rp.ordinal
            """,
            (org_id, project_id, version_ids),
        )
        rows = cur.fetchall()
    subject_type, subject_id = subject
    by_version: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        measures, measures_omitted = _bounded(
            [
                {"metric_id": m.get("id"), "version_id": m.get("version_id")}
                for m in (r[8] or [])
                if isinstance(m, dict)
            ],
            MAX_MEASURES,
        )
        entry = {
            "ordinal": r[1],
            "query_spec_version_id": r[2],
            "role": r[3],
            "query_spec_id": r[4],
            "query_spec_version_number": r[5],
            "semantic_view_id": r[6],
            "semantic_view_version_id": r[7],
            "measures": measures,
            "measures_omitted": measures_omitted,
        }
        by_version.setdefault(str(r[0]), []).append(entry)
    for head in heads:
        paths, omitted = _bounded(
            by_version.get(str(head["golden_question_version_id"]), []), MAX_REFERENCE_PATHS
        )
        head["pins"]["reference_paths"] = paths
        head["pins"]["reference_paths_omitted"] = omitted
        if subject_type == "metric":
            # The exact version of the metric the caller asked about, read off the
            # pin the Query Spec version already holds. Never inferred from
            # today's Semantic View: the question was written against THAT one.
            head["pins"]["subject_metric_version_id"] = next(
                (
                    m["version_id"]
                    for path in paths
                    for m in path["measures"]
                    if m["metric_id"] == subject_id
                ),
                None,
            )


def _attach_approved_paths(
    conn, heads: list[dict[str, Any]], *, org_id: str, project_id: str
) -> None:
    """The observed walks a comparison judged `pass` against THIS exact version.

    The four conditions are the amendment's, and every one of them is a stored
    column: a passing verdict, observed evidence, a finalized walk, and a case
    pinned to the version being served.

    `evidence_state = 'observed'` is stated here and ENFORCED by the database:
    `ck_evaluation_path_comparisons_state_matches_pin` makes it the same fact as
    `observed_ai_path_id IS NOT NULL`, which the inner join already demands. It
    is written out because a reader looking for the amendment's four conditions
    must find four, not three and an inference.
    """
    version_ids = [h["golden_question_version_id"] for h in heads]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rc.golden_question_version_id, c.observed_ai_path_id, c.path_verdict,
                   c.expected_pattern_hash, c.matched_alternative_key,
                   p.outcome, p.actor, p.ended_at, rc.run_id, c.id
              FROM app.evaluation_path_comparisons c
              JOIN app.evaluation_run_cases rc
                ON rc.id = c.case_id
               AND rc.org_id = c.org_id AND rc.project_id = c.project_id
              JOIN app.ai_paths p
                ON p.id = c.observed_ai_path_id
               AND p.org_id = c.org_id AND p.project_id = c.project_id
             WHERE c.org_id = %s AND c.project_id = %s
               AND c.path_verdict = 'pass'
               AND c.evidence_state = 'observed'
               AND p.lifecycle = 'finalized'
               AND rc.golden_question_version_id = ANY(%s)
             ORDER BY rc.golden_question_version_id, p.ended_at DESC, c.id DESC
            """,
            (org_id, project_id, version_ids),
        )
        rows = cur.fetchall()
    by_version: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_version.setdefault(str(r[0]), []).append(
            {
                "observed_ai_path_id": r[1],
                "path_verdict": r[2],
                "expected_pattern_hash": r[3],
                "matched_alternative_key": r[4],
                "outcome": r[5],
                "walked_by": r[6],
                "ended_at": _iso(r[7]),
                "evaluation_run_id": r[8],
                "steps": [],
                "steps_omitted": 0,
            }
        )
    served: dict[str, dict[str, Any]] = {}
    for head in heads:
        paths, omitted = _bounded(
            by_version.get(str(head["golden_question_version_id"]), []), MAX_APPROVED_PATHS
        )
        head["approved_paths"] = paths
        head["approved_paths_omitted"] = omitted
        for path in paths:
            served[str(path["observed_ai_path_id"])] = path
    if served:
        _attach_steps(conn, served)


def _attach_steps(conn, served: dict[str, dict[str, Any]]) -> None:
    """The ordered walk, bounded. What a model imitates is the SHAPE, not the detail."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT path_id, ordinal, step_kind, owner_workspace, owner_object_type,
                   owner_object_id, owner_version_id, tool_name, outcome
              FROM app.ai_path_steps
             WHERE path_id = ANY(%s)
             ORDER BY path_id, ordinal
            """,
            (list(served),),
        )
        rows = cur.fetchall()
    collected: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        collected.setdefault(str(r[0]), []).append(
            {
                "ordinal": r[1],
                "step_kind": r[2],
                "owner_workspace": r[3],
                "owner_object_type": r[4],
                "owner_object_id": r[5],
                "owner_version_id": r[6],
                "tool_name": r[7],
                "outcome": r[8],
            }
        )
    for path_id, path in served.items():
        steps, omitted = _bounded(collected.get(path_id, []), MAX_PATH_STEPS)
        path["steps"] = steps
        path["steps_omitted"] = omitted


def _trim_head(head: dict[str, Any], budget_tokens: int) -> dict[str, Any]:
    """Shrink ONE exemplar until it fits, and DECLARE every cut it took.

    Called only when the head standing alone is over the budget -- which, since
    the budget is the model channel's, means the head standing alone is over the
    channel. The two answers this replaces were both wrong: serving it whole made
    `model_channel.partition_envelope` move the list to the app channel and put a
    `moved_to_app_channel` marker in front of the model (G16, revision
    `mcp-server-00274`), and dropping it would answer "this subject has no
    governed exemplar", which is the one thing it does not mean.

    THE ORDER IS FROM THE MOST REPLACEABLE TO THE LEAST, and every rung uses the
    counter the payload already carries, so no cut is silent:

      1. `assertions` -- the finest grain of the expected shape, and the one a
         model can re-read from the Golden Question itself.
      2. the approved walks: their `steps` first (a walk without its tail is
         still a walk that passed), then whole walks. They are ILLUSTRATIONS of
         a question answered well, and they are the heaviest thing here.
      3. `reference_paths` -- the Query Spec versions and the measures they pin.
         AFTER the walks (arbitrated 2026-09-06): these are the exemplar's
         IDENTITY, and a head that keeps a walk while dropping the pins tells a
         caller what somebody once did without telling it what against.
      4. the `question` text, down to `MIN_QUESTION_CHARS` -- late, because it is
         what the exemplar is FOR.
      5. `expected_ai_path`, and only when nothing above was enough. It is
         replaced by a stated descriptor rather than removed, and the head still
         carries `golden_question_version_id` + `content_hash`: the pattern is
         one exact read away.
    """

    def fits() -> bool:
        return estimate_tokens(head) <= budget_tokens

    if fits():
        return head
    head["trimmed_for_budget"] = True

    shape = head["expected_answer_shape"]
    while shape["assertions"] and not fits():
        shape["assertions"].pop()
        shape["assertions_omitted"] += 1

    while head["approved_paths"] and not fits():
        last = head["approved_paths"][-1]
        if last["steps"]:
            last["steps"].pop()
            last["steps_omitted"] += 1
            continue
        head["approved_paths"].pop()
        head["approved_paths_omitted"] += 1

    pins = head["pins"]
    while pins["reference_paths"] and not fits():
        pins["reference_paths"].pop()
        pins["reference_paths_omitted"] += 1

    question = head.get("question") or ""
    while not fits() and len(head.get("question") or "") > MIN_QUESTION_CHARS:
        over = max(1, (estimate_tokens(head) - budget_tokens) * BYTES_PER_TOKEN)
        keep = max(MIN_QUESTION_CHARS, len(head["question"]) - over)
        head["question"] = head["question"][:keep]
        head["question_omitted_chars"] = len(question) - keep

    if not fits() and head.get("expected_ai_path"):
        head["expected_ai_path"] = {
            "omitted_for_budget": True,
            "bytes": estimated_bytes(head["expected_ai_path"]),
        }
    return head


def list_exemplars(
    conn,
    *,
    org_id: str,
    project_id: str,
    subject_type: str,
    subject_id: str,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> dict[str, Any]:
    """The approved exemplars for one subject: deterministic, pinned, budgeted.

    The list served is always a PREFIX of the ordering. Skipping a heavy
    exemplar to fit a lighter one behind it would answer a different question
    ("the cheapest exemplars") than the one asked ("the most recent ones"), and
    two callers with different budgets would then disagree about what is most
    recent.

    TWO CUTS ARE COUNTED SEPARATELY AND BOTH ARE ADMITTED. `MAX_EXEMPLARS` bites
    first and is not the caller's to raise; the budget bites second and is. A
    caller that sees `omitted_beyond_max_exemplars > 0` knows a larger budget
    will not help and must narrow its subject instead.
    """
    subject_type, subject_id = normalize_subject(subject_type, subject_id)
    budget = clamp_budget(budget_tokens)

    heads, matching_total = _fetch_heads(
        conn,
        org_id=org_id,
        project_id=project_id,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if heads:
        _attach_reference_paths(
            conn,
            heads,
            org_id=org_id,
            project_id=project_id,
            subject=(subject_type, subject_id),
        )
        _attach_approved_paths(conn, heads, org_id=org_id, project_id=project_id)

    served: list[dict[str, Any]] = []
    spent = 0
    spent_bytes = 0
    for head in heads:
        remaining = budget - spent
        cost = estimate_tokens(head)
        if cost > remaining:
            if served:
                # A later exemplar does not jump the queue: the list is a PREFIX
                # of the ordering, and the counters below say what was left out.
                break
            # NOTHING is served yet and this one head is already over the whole
            # budget -- so over the model channel. Trimmed to fit, cut by cut,
            # every cut declared. Never served whole (`partition_envelope` would
            # then withhold the entire list) and never dropped.
            _trim_head(head, remaining)
            cost = estimate_tokens(head)
        served.append(head)
        spent += cost
        spent_bytes += estimated_bytes(head)

    omitted_for_budget = len(heads) - len(served)
    omitted_beyond_bound = max(0, matching_total - len(heads))
    omitted_total = omitted_for_budget + omitted_beyond_bound
    return {
        "subject": {"type": subject_type, "id": subject_id},
        "project_id": project_id,
        "lifecycle_served": SERVED_LIFECYCLE,
        "exemplars": served,
        "served": len(served),
        "matching_active_questions": matching_total,
        "max_exemplars": MAX_EXEMPLARS,
        "omitted_for_budget": omitted_for_budget,
        "omitted_beyond_max_exemplars": omitted_beyond_bound,
        "omitted_total": omitted_total,
        "truncated": omitted_total > 0,
        # A THIRD cut, and it is inside an exemplar rather than between two. A
        # payload that hid it would read as a whole exemplar that is not one.
        "trimmed_for_budget": any(h.get("trimmed_for_budget") for h in served),
        "budget_tokens": budget,
        "estimated_tokens": spent,
        "estimated_bytes": spent_bytes,
        "token_estimate_method": TOKEN_ESTIMATE_METHOD,
        "exemplar_store_state": STORE_AVAILABLE,
    }


def unavailable(
    subject_type: str, subject_id: str, project_id: str, budget: int
) -> dict[str, Any]:
    """What an unreadable store answers -- NOT an empty list.

    `search_context` learned this the hard way (AI-83): an unavailable store and
    an empty one read identically, so a model answered "nothing is defined here"
    from its own priors. The two are separate words here.
    """
    return {
        "subject": {"type": subject_type, "id": subject_id},
        "project_id": project_id,
        "lifecycle_served": SERVED_LIFECYCLE,
        "exemplars": [],
        "served": 0,
        # NOT zero because nothing matched -- nothing was counted at all. The
        # counters are present so the payload keeps ONE shape, and
        # `exemplar_store_state` is the field that says what they mean.
        "matching_active_questions": 0,
        "max_exemplars": MAX_EXEMPLARS,
        "omitted_for_budget": 0,
        "omitted_beyond_max_exemplars": 0,
        "omitted_total": 0,
        "truncated": False,
        "trimmed_for_budget": False,
        "budget_tokens": budget,
        "estimated_tokens": 0,
        "estimated_bytes": 0,
        "token_estimate_method": TOKEN_ESTIMATE_METHOD,
        "exemplar_store_state": STORE_UNAVAILABLE,
    }
