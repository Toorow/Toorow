"""Story 50.4 -- the deny-list, proved by refusal rather than described by a sentence.

`docs/product-architecture/visualization-and-rendering.md:113-119` lists five
things a persisted presentation contract never contains. A paragraph saying so is
honoured by everyone who reads it and by nobody who does not -- including a model
proposing a spec. So this file is a CORPUS: one hostile document per clause and
per known evasion, each asserted to be refused with an exact code and an exact
JSON pointer.

The corpus is exported. `test_admin_api_visualization_specs.py` runs the WHOLE of
it through the model proposal path and asserts byte-identical verdicts, so "an
LLM is validated identically" is a measurement rather than an intention (AC10).

The meta-test at the bottom is what stops the corpus rotting into a token sample:
it reads the five clauses out of the architecture companion and fails if any of
them has no case.
"""

from __future__ import annotations

import pathlib

import pytest
from core.visualization_families import get_family
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    PinnedMembers,
    check_shape_compatibility,
    normalize_document,
)

COMPANION = "docs/product-architecture/visualization-and-rendering.md"

#: `<repo>/server/tests/core/this_file.py` -> `<repo>`. Every path in this file is
#: resolved from here, so the suite proves the same thing from any directory.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: The five clauses of `:113-119`, keyed by a stable id, each with a phrase that
#: must still be present in those lines. If the companion is reworded, the meta-
#: test fails and someone re-reads it -- rather than the corpus quietly covering a
#: contract that no longer exists.
CLAUSES: dict[str, str] = {
    "raw_renderer_options": "raw ECharts options",
    "javascript_functions": "JavaScript functions",
    "html_css_urls_expressions_handlers": (
        "arbitrary HTML, CSS, URLs, expressions or event handlers"
    ),
    "client_transforms": (
        "client-side aggregation, joins, calculated measures or timezone conversion"
    ),
    "connector_field_names": "connector-specific field names",
    "credentials": "credentials, authorization material or host-specific state",
}


def _doc(**overrides) -> dict:
    document = {
        "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": VISUALIZATION_SPEC_SCHEMA_VERSION,
        "family": "bar",
        "bindings": {"measure": ["clicks"], "dimension": ["channel"]},
    }
    document.update(overrides)
    return document


def _label(value: str) -> dict:
    return _doc(labels={"override": {"clicks": value}})


#: The members a pinned Query Spec version actually selected, for the compat-tier
#: cases. Two members, both governed. Everything else is a connector column name.
PINNED = PinnedMembers(
    roles={"clicks": "measure", "channel": "dimension"},
    labels={"clicks": "clicks", "channel": "channel"},
    grain="day",
    comparison="none",
    row_limit=1000,
    query_spec_id="qs_example",
    semantic_view_id="sv_example",
    semantic_view_version_id="svv_example",
)


class Case:
    """One hostile document, the clause it attacks, and the exact verdict expected."""

    def __init__(self, case_id: str, clause: str, document: dict, code: str, subject: str,
                 tier: str = "walk"):
        self.id = case_id
        self.clause = clause
        self.document = document
        self.code = code
        self.subject = subject
        #: `walk` = refused by the closed allow-list walker, with no database.
        #: `compat` = refused by compatibility against the pinned Query Spec
        #: version, which is where a connector column name dies.
        self.tier = tier

    def __repr__(self) -> str:  # pragma: no cover - test ids only
        return self.id


CORPUS: tuple[Case, ...] = (
    # -- raw renderer options -------------------------------------------------
    Case("echarts_series_array", "raw_renderer_options",
         _doc(series=[{"type": "bar", "data": [1, 2, 3]}]), "unknown_field", "/series"),
    Case("echarts_full_option", "raw_renderer_options",
         _doc(option={"xAxis": {}, "series": []}), "unknown_field", "/option"),
    Case("echarts_x_axis", "raw_renderer_options",
         _doc(xAxis={"type": "category"}), "unknown_field", "/xAxis"),
    Case("echarts_dataset", "raw_renderer_options",
         _doc(dataset={"source": [[1, 2]]}), "unknown_field", "/dataset"),
    Case("echarts_encode_inside_a_well", "raw_renderer_options",
         _doc(bindings={"measure": {"encode": {"x": 0}}, "dimension": ["channel"]}),
         "unknown_field", "/bindings/measure/encode"),
    Case("echarts_tooltip_formatter", "raw_renderer_options",
         _doc(tooltip={"formatter": "{b}: {c}"}), "unknown_field", "/tooltip"),
    Case("d3_select_all_nested", "raw_renderer_options",
         _doc(axes={"y": {"selectAll": "rect"}}), "unknown_field", "/axes/y/selectAll"),
    Case("d3_append_nested", "raw_renderer_options",
         _doc(legend={"append": "g"}), "unknown_field", "/legend/append"),

    # -- JavaScript functions -------------------------------------------------
    Case("arrow_function_label", "javascript_functions",
         _label("(d) => d.value * 2"), "invalid_value", "/labels/override/clicks"),
    Case("function_expression_label", "javascript_functions",
         _label("function(d){return d}"), "invalid_value", "/labels/override/clicks"),
    Case("javascript_uri_annotation", "javascript_functions",
         _doc(annotations=[{"evidence_id": "javascript:alert(1)", "anchor": "datum"}]),
         "invalid_value", "/annotations/0/evidence_id"),
    Case("d3_call_in_a_label", "javascript_functions",
         _label("d3.select('body')"), "invalid_value", "/labels/override/clicks"),
    # The evasion the plain `"function("` / `"function ("` substrings left open.
    # A tab, or two spaces, and the marker no longer matched. `_CODE_PATTERNS`
    # now matches `function` + any whitespace + `(`.
    Case("function_keyword_with_a_tab_before_the_call", "javascript_functions",
         _label("function\t(d) return d"), "invalid_value", "/labels/override/clicks"),
    Case("function_keyword_with_two_spaces_before_the_call", "javascript_functions",
         _label("function  (d) return d"), "invalid_value", "/labels/override/clicks"),
    Case("new_function_constructor_in_a_label", "javascript_functions",
         _label("new Function"), "invalid_value", "/labels/override/clicks"),
    Case("d3_format_as_a_number_style", "javascript_functions",
         _doc(formatting={"number_style": "d3.format"}), "invalid_value",
         "/formatting/number_style"),

    # -- HTML, CSS, URLs, expressions, event handlers -------------------------
    Case("html_key", "html_css_urls_expressions_handlers",
         _doc(html="<b>hi</b>"), "unknown_field", "/html"),
    Case("style_key", "html_css_urls_expressions_handlers",
         _doc(style={"color": "red"}), "unknown_field", "/style"),
    Case("css_class_key", "html_css_urls_expressions_handlers",
         _doc(**{"class": "chart-root"}), "unknown_field", "/class"),
    Case("url_key", "html_css_urls_expressions_handlers",
         _doc(url="https://example.com/chart"), "unknown_field", "/url"),
    Case("event_handler_key", "html_css_urls_expressions_handlers",
         _doc(onClick="doThing()"), "unknown_field", "/onClick"),
    Case("script_tag_label", "html_css_urls_expressions_handlers",
         _label("<script>alert(1)</script>"), "invalid_value", "/labels/override/clicks"),
    # The JSON decoder turns `<script>` into `<script>` before it reaches
    # the walker, so a unicode-escaped payload meets exactly the same rule.
    Case("unicode_escaped_script_label", "html_css_urls_expressions_handlers",
         _label("<script>alert(1)</script>"), "invalid_value",
         "/labels/override/clicks"),
    Case("data_uri_label", "html_css_urls_expressions_handlers",
         _label("data:text/html;base64,PHNjcmlwdD4="), "invalid_value",
         "/labels/override/clicks"),
    Case("hex_colour_literal_label", "html_css_urls_expressions_handlers",
         _label("#ff0000"), "invalid_value", "/labels/override/clicks"),
    Case("hex_colour_literal_as_colour_role", "html_css_urls_expressions_handlers",
         _doc(color={"role": "#00ff00"}), "invalid_value", "/color/role"),
    Case("template_interpolation_label", "html_css_urls_expressions_handlers",
         _label("${constructor.constructor}"), "invalid_value", "/labels/override/clicks"),
    Case("external_url_annotation", "html_css_urls_expressions_handlers",
         _doc(annotations=[{"evidence_id": "https://example.com/e", "anchor": "datum"}]),
         "invalid_value", "/annotations/0/evidence_id"),

    # -- client aggregation, joins, calculated measures, timezone -------------
    Case("client_aggregate", "client_transforms",
         _doc(aggregate="sum"), "query_owned_field", "/aggregate"),
    Case("client_join", "client_transforms",
         _doc(join={"left": "a", "right": "b"}), "query_owned_field", "/join"),
    Case("client_formula", "client_transforms",
         _doc(formula="clicks / cost"), "query_owned_field", "/formula"),
    Case("client_calculated_measure", "client_transforms",
         _doc(calculate={"cpc": "cost/clicks"}), "query_owned_field", "/calculate"),
    Case("client_expression_in_a_well", "client_transforms",
         _doc(bindings={"measure": {"expression": "sum(x)"}, "dimension": ["channel"]}),
         "query_owned_field", "/bindings/measure/expression"),
    Case("client_timezone", "client_transforms",
         _doc(timezone="UTC"), "query_owned_field", "/timezone"),
    Case("client_grain", "client_transforms",
         _doc(grain="week"), "query_owned_field", "/grain"),
    Case("client_filter", "client_transforms",
         _doc(filters=[{"member_id": "channel", "operator": "eq", "value": "paid"}]),
         "query_owned_field", "/filters"),
    Case("client_row_limit", "client_transforms",
         _doc(row_limit=50), "query_owned_field", "/row_limit"),

    # -- connector field names bypassing Semantic View identities -------------
    Case("connector_column_in_a_well", "connector_field_names",
         _doc(bindings={"measure": ["clicks"],
                        "dimension": ["ga4_sessionDefaultChannelGrouping"]}),
         "unknown_member", "/bindings/dimension/0", "compat"),
    Case("connector_column_in_a_threshold", "connector_field_names",
         _doc(thresholds=[{"member_id": "stripe_charges.amount", "comparator": "gt",
                           "value": 10}]),
         "unknown_member", "/thresholds/0/member_id", "compat"),
    Case("connector_column_in_evidence", "connector_field_names",
         _doc(evidence={"datum_fields": ["hubspot_contacts.email"]}),
         "unknown_member", "/evidence/datum_fields/0", "compat"),
    Case("connector_column_in_a_label_override", "connector_field_names",
         _doc(labels={"override": {"meta_ads.spend": "Spend"}}),
         "unknown_member", "/labels/override/meta_ads.spend", "compat"),

    # -- credentials, authorization material, host-specific state -------------
    Case("bearer_token_label", "credentials",
         _label("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"), "invalid_value",
         "/labels/override/clicks"),
    Case("jwt_shaped_annotation", "credentials",
         _doc(annotations=[{"evidence_id": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9",
                            "anchor": "datum"}]),
         "invalid_value", "/annotations/0/evidence_id"),
    Case("secret_key_label", "credentials",
         _label("sk_live_abcdefghijklmnop"), "invalid_value", "/labels/override/clicks"),
    Case("api_key_assignment_label", "credentials",
         _label("api_key=abcdef123456"), "invalid_value", "/labels/override/clicks"),
    Case("authorization_key", "credentials",
         _doc(authorization="Basic dXNlcjpwYXNz"), "unknown_field", "/authorization"),
    Case("host_state_key", "credentials",
         _doc(host_state={"session": "abc"}), "unknown_field", "/host_state"),

    # -- known evasion that belongs to no single clause: undisclosed truncation
    Case("top_n_not_display_only", "raw_renderer_options",
         _doc(top_n={"n": 5, "display_only": False}), "truncation_not_disclosed",
         "/top_n/display_only"),
)


def refuse(case: Case) -> list[dict]:
    """Run one case through the real production path for its tier."""
    normalized, refusals = normalize_document(case.document)
    verdicts = [r.as_dict() for r in refusals]
    if case.tier == "compat":
        family = get_family(normalized.get("family") or "")
        assert family is not None
        verdicts += [
            r.as_dict() for r in check_shape_compatibility(normalized, family, PINNED)
        ]
    return verdicts


@pytest.mark.parametrize("case", CORPUS, ids=[c.id for c in CORPUS])
def test_every_hostile_document_is_refused_with_its_exact_code_and_subject(case: Case):
    verdicts = refuse(case)
    assert verdicts, f"{case.id} was ACCEPTED; the deny-list did not hold"
    matched = [v for v in verdicts if v["code"] == case.code and v["subject"] == case.subject]
    assert matched, (
        f"{case.id}: expected code={case.code} subject={case.subject}, got "
        f"{[(v['code'], v['subject']) for v in verdicts]}"
    )


@pytest.mark.parametrize("case", CORPUS, ids=[c.id for c in CORPUS])
def test_every_refusal_carries_a_remedy_a_person_can_act_on(case: Case):
    """AC4: `message` states the fact, `remedy` names one action. Both, always."""
    for verdict in refuse(case):
        assert verdict["message"], case.id
        assert verdict["remedy"], f"{case.id}: `{verdict['code']}` has no remedy"


def test_no_refusal_ever_suggests_a_near_miss_member_by_name():
    """Story 50.1's rule, kept here: a suggestion invites a caller to accept a
    member they did not ask for, which is a semantic substitution."""
    for case in CORPUS:
        for verdict in refuse(case):
            text = f"{verdict['message']} {verdict['remedy']}".lower()
            assert "did you mean" not in text, case.id
            assert "closest" not in text, case.id


# ---------------------------------------------------------------------------
# The meta-test: the corpus cannot rot into a token sample.
# ---------------------------------------------------------------------------


def _companion_clause_lines() -> str:
    # Anchored on THIS FILE, never on the process working directory. Opened by a
    # CWD-relative path, AC3's clause-coverage meta-test -- the single guard that
    # keeps the corpus from rotting into a token sample -- raised
    # `FileNotFoundError` under `cd server && pytest ...` and so did not run at
    # all outside the repository root. A guard that silently does not run is not
    # a guard.
    text = REPO_ROOT.joinpath(COMPANION).read_text(encoding="utf-8")
    start = text.index("The persisted contract never contains:")
    return text[start : start + 700]


def test_the_corpus_has_at_least_thirty_cases():
    assert len(CORPUS) >= 30, f"the corpus has shrunk to {len(CORPUS)} cases"


def test_every_clause_of_the_companion_has_at_least_one_case():
    """The clause-coverage meta-test AC3 asks for, by name."""
    block = _companion_clause_lines()
    covered = {case.clause for case in CORPUS}
    for clause_id, phrase in CLAUSES.items():
        assert phrase in block, (
            f"the companion no longer contains `{phrase}`; re-read {COMPANION} "
            f"before editing this corpus"
        )
        assert clause_id in covered, f"no corpus case covers the `{clause_id}` clause"


def test_every_corpus_case_names_a_declared_clause():
    for case in CORPUS:
        assert case.clause in CLAUSES, f"{case.id} names an undeclared clause"


# ---------------------------------------------------------------------------
# The one place this story's enforcement is NARROWER than AC3's table, pinned
# from both sides so the narrowing cannot widen or vanish by accident.
# ---------------------------------------------------------------------------

#: Governed strings that carry the bare word `function` and MUST be accepted.
#: `job_function` is a real member id -- LinkedIn Ads targets on job function --
#: and "Job Function" is a real governed label. AC3's table as first written
#: ("a leaf carrying `function` ... fails") would have made both unexpressible,
#: so the table is corrected to name the CALL and this corpus pins the reading.
GOVERNED_STRINGS_CARRYING_THE_WORD_FUNCTION = (
    "Job Function",
    "Job function, net",
    "function",
)


@pytest.mark.parametrize("label", GOVERNED_STRINGS_CARRYING_THE_WORD_FUNCTION)
def test_a_governed_label_carrying_the_bare_word_function_is_accepted(label):
    """The other half of the `function` rule, and the reason it is a call.

    A refusal a person cannot act on is as bad as no refusal: refusing "Job
    Function" would leave a legitimate governed label with no expressible form.
    Nothing is lost -- a function BODY needs `{`, `}` or `=>`, and `_FORBIDDEN_CHARS`
    already refuses those in every string leaf.
    """
    _normalized, refusals = normalize_document(_label(label))
    assert refusals == [], f"`{label}` was refused: {[r.as_dict() for r in refusals]}"


@pytest.mark.parametrize(
    "member_id", ["job_function", "linkedin_ads.job_function", "function_group"]
)
def test_a_governed_member_id_carrying_the_word_function_survives_the_walker(member_id):
    """The same rule at the binding, which is where it would actually bite: a
    member id the walker refused could never be bound, whatever the pinned Query
    Spec selected."""
    document = _doc(bindings={"measure": ["clicks"], "dimension": [member_id]})
    _normalized, refusals = normalize_document(document)
    assert refusals == [], [r.as_dict() for r in refusals]


@pytest.mark.parametrize(
    "spelling",
    ["function(d){}", "function (d)", "function\t(d)", "function  (d)", "FUNCTION(d)"],
)
def test_every_spelling_of_a_function_call_is_refused(spelling):
    """Whitespace between the keyword and the parenthesis is not a loophole."""
    _normalized, refusals = normalize_document(_label(spelling))
    assert any(r.code == "invalid_value" for r in refusals), spelling


def test_the_accessible_table_fallback_cannot_be_switched_off():
    """AC8: there is no accepted value of `table_fallback` other than the literal."""
    for attempt in ("optional", "none", "false", "", "not-required"):
        _n, refusals = normalize_document(
            _doc(accessibility={"table_fallback": attempt, "summary_source": "result_manifest"})
        )
        assert any(r.code == "invalid_value" for r in refusals), attempt
    _n, refusals = normalize_document(_doc(accessibility={"table_fallback": True}))
    assert any(r.code == "invalid_value" for r in refusals)
    # And a document that simply omits it still carries it.
    normalized, refusals = normalize_document(_doc())
    assert refusals == []
    assert normalized["accessibility"]["table_fallback"] == "required"
