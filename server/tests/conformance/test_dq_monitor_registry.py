"""One place knows the DQ monitors, and every layer reads it -- story 59.5.

THE DEFECT THIS FILE CLOSES. Eleven places named the monitors on 2026-08-08 and
none of them agreed: 7 dispatched keys, 8 publishable ones, 6 displayable types
(with `dq_null_rate` and `dq_zero_rows` filtered out of every DQ summary), 6
French labels, 9 firing sites, a guard expecting 7 types while reading 9, a
docstring claiming 5 monitors, a dossier listing 5 others, and a console rendering
two f-strings. Nothing was observably broken because `app.dq_issues` is empty on
both bases -- which is precisely the condition under which a vocabulary rots
unnoticed.

Each test below RED-LIGHTS a monitor that some layer does not know about. None of
them names the monitor of the day: a guard that hard-codes `zero_rows` is a guard
that will be silent for the tenth check.

THE LAST TWO TESTS ARE THE ONES THAT GIVE THE OTHERS THEIR TEETH. They run the
same detectors over a PLANTED registry, a PLANTED document and each of the four
guarded sources with a literal type list appended, and require each to fire -- a
guard that cannot fail proves nothing (pattern:
`ui/admin/src/__tests__/DatastreamsListVocabulary.test.tsx:25-28`).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from core import controls_quality, dq_api, dq_governance, dq_monitor_registry, dq_monitors

ROOT = Path(__file__).resolve().parents[3]
GLOSSARY = ROOT / "docs" / "product-architecture" / "glossary.md"
DOSSIER = ROOT / "doc" / "datastream" / "04-logs-observability-and-anomalies.md"
RUN_ANOMALIES = ROOT / "ui" / "admin" / "src" / "datastreams" / "workbench" / "RunAnomalies.tsx"
INFRA_ALERTS_GUARD = ROOT / "server" / "tests" / "test_infra_alerts.py"
DQ_MONITORS_SOURCE = ROOT / "server" / "core" / "dq_monitors.py"
DQ_ISSUE_ROWS_SOURCE = ROOT / "server" / "core" / "dq_issue_rows.py"

#: Words that betray a French label. A short list on purpose: these are the six
#: words the labels this story replaced were built from, plus the accents no
#: English label carries.
_FRENCH_LABEL_MARKERS = (
    "ponctualite",
    "doublons",
    "coherence",
    "lignes",
    "rejetees",
    "geographie",
    "resolue",
    "non ",
)


# ---------------------------------------------------------------------------
# The registry is total: every monitor answers every question.
# ---------------------------------------------------------------------------


def test_every_monitor_is_classified_on_every_axis() -> None:
    assert dq_monitor_registry.DQ_MONITORS, "the registry is empty"
    kinds = {dq_monitor_registry.TARGET_DATASTREAM, dq_monitor_registry.TARGET_PROJECT}
    for monitor in dq_monitor_registry.DQ_MONITORS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", monitor.key), monitor.key
        assert monitor.alert_type.startswith("dq_"), monitor.key
        assert monitor.target_kind in kinds, monitor.key
        assert monitor.label.strip(), monitor.key
        # A dispatched monitor is walked per Datastream, so it cannot be scoped to
        # a project: the sweep would give it the wrong denominator.
        if monitor.dispatched:
            assert monitor.target_kind == dq_monitor_registry.TARGET_DATASTREAM, monitor.key
        # A `firing_kind` exists only to disambiguate a SHARED alert type.
        if monitor.firing_kind:
            siblings = [
                other
                for other in dq_monitor_registry.DQ_MONITORS
                if other.alert_type == monitor.alert_type
            ]
            assert len(siblings) > 1, monitor.key


def test_every_label_is_english() -> None:
    """A label reaches `app.dq_monitors.label`, the summary and the run panel."""
    offenders = [
        monitor.key
        for monitor in dq_monitor_registry.DQ_MONITORS
        if any(marker in monitor.label.lower() for marker in _FRENCH_LABEL_MARKERS)
        or not monitor.label.isascii()
    ]
    assert not offenders, f"non-English monitor label(s): {offenders}"


def test_the_derived_tuples_are_derived_and_not_restated() -> None:
    registry = dq_monitor_registry
    assert set(registry.DISPATCHED_KEYS) <= set(registry.BY_KEY)
    assert set(registry.PUBLISHABLE_KEYS) <= set(registry.BY_KEY)
    # Every dispatched monitor is publishable: the sweep evaluates it nightly, so a
    # policy that cannot name it is a policy that cannot govern what already runs.
    assert set(registry.DISPATCHED_KEYS) <= set(registry.PUBLISHABLE_KEYS)
    assert set(registry.PUBLISHABLE_ALERT_TYPES) <= set(registry.FIRING_ALERT_TYPES)
    assert set(registry.LABELS_BY_ALERT_TYPE) == set(registry.PUBLISHABLE_ALERT_TYPES)


def test_a_project_scoped_monitor_cannot_be_asked_for_on_a_datastream() -> None:
    """Arbitrage 4, and the reason the scope is a FIELD rather than a comment.

    `app.dq_monitors.target_kind` has no `project` value (migration 145:306-307),
    so `ensure_monitor` refuses one. The registry names the scope; it does not
    pretend the database can hold it.
    """
    assert dq_monitor_registry.TARGET_PROJECT not in dq_governance.TARGET_KINDS
    project_scoped = [
        m.key
        for m in dq_monitor_registry.DQ_MONITORS
        if m.target_kind == dq_monitor_registry.TARGET_PROJECT
    ]
    assert project_scoped, "the scope field is pointless if nothing uses it"
    for key in project_scoped:
        assert key not in dq_monitor_registry.DISPATCHED_KEYS
        assert key not in dq_monitor_registry.DATASTREAM_SCOPED_KEYS


# ---------------------------------------------------------------------------
# Layer 1 -- the checks that exist.
# ---------------------------------------------------------------------------


def _check_function_keys(source: str) -> set[str]:
    """The `_check_<key>` functions a module defines, read from the AST."""
    tree = ast.parse(source)
    return {
        node.name[len("_check_") :]
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_check_")
    }


def test_every_check_function_has_a_registry_entry_and_the_reverse() -> None:
    """Nine functions, nine entries. A tenth check added is a red test."""
    functions = _check_function_keys(DQ_MONITORS_SOURCE.read_text(encoding="utf-8"))
    assert functions == set(dq_monitor_registry.BY_KEY), (
        "only in dq_monitors.py="
        f"{sorted(functions - set(dq_monitor_registry.BY_KEY))}, "
        "only in the registry="
        f"{sorted(set(dq_monitor_registry.BY_KEY) - functions)}"
    )


def test_the_dispatch_table_is_the_registry() -> None:
    assert tuple(dq_monitors.CHECK_PROFILES) == dq_monitor_registry.DISPATCHED_KEYS


def test_the_replay_table_is_the_registry() -> None:
    """`REPLAY_PROFILES` was the eleventh list, and it outlived the other ten.

    It held seven keys written by hand, and its parity with `CHECK_PROFILES` was
    asserted by one test in ANOTHER file -- the exact shape
    `test_no_layer_redeclares_the_dq_type_list` refuses, for the reason its own
    docstring gives: an equality proves one list matches today and proves nothing
    about a second standing next to it. It is derived now.
    """
    from core import dq_issue_rows

    assert tuple(dq_issue_rows.REPLAY_PROFILES) == dq_monitor_registry.DISPATCHED_KEYS


def test_a_new_monitor_reaches_the_replay_table_carrying_its_own_answer() -> None:
    """The teeth of the test above: derivation must be a MECHANISM, not a match.

    A tenth monitor added to the registry must land in the replay table on the
    side its `has_faulty_rows` field states -- and that field has no default a
    reader can forget, so the answer cannot be omitted into silence.
    """
    from core import dq_issue_rows

    def planted(**kwargs) -> dq_monitor_registry.DqMonitor:
        return dq_monitor_registry.DqMonitor(
            key="planted_tenth",
            alert_type="dq_planted_tenth",
            label="Planted tenth",
            target_kind=dq_monitor_registry.TARGET_DATASTREAM,
            dispatched=True,
            publishable=True,
            **kwargs,
        )

    by_nature = dq_issue_rows._profile_for(planted(has_faulty_rows=False))
    assert by_nature.replayable_rows is False
    assert by_nature.absence_reason == dq_issue_rows.NO_FAULTY_ROW_BY_NATURE

    # It HAS faulty rows and nothing replays them yet: the honest sentence is
    # about this build, never about the client's data.
    not_built = dq_issue_rows._profile_for(planted(has_faulty_rows=True))
    assert not_built.replayable_rows is False
    assert not_built.absence_reason == dq_issue_rows.NO_REPLAY_IMPLEMENTED
    assert by_nature.absence_reason != not_built.absence_reason


def test_every_dispatched_check_answers_a_verdict_not_a_boolean() -> None:
    """`governance.md` [8]: "no issue" may never stand for "nothing was measured".

    Measured 2026-08-16, before this guard: `_check_timeliness`, `_check_duplication`
    and `_check_schema` were annotated `-> bool` and returned a bare `False` on
    TWELVE exits that meant the check could not run -- no raw table, an
    unreachable warehouse, a column list nobody could read, a day inside the
    extraction offset, an hour before the deadline. The sweep reports every one
    of those as "no issue", and the `*_skip` debug lines were the only place the
    difference survived.

    A `bool` has no room for the distinction, so the annotation IS the guard: a
    dispatched check that goes back to one cannot carry a reason.
    """
    source = DQ_MONITORS_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    returns = {
        node.name: ast.unparse(node.returns) if node.returns is not None else None
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    offenders = []
    for key in dq_monitor_registry.DISPATCHED_KEYS:
        name = f"_check_{key}"
        assert name in returns, f"{name} is dispatched and does not exist"
        if returns[name] not in ("CheckVerdict", "'CheckVerdict'", '"CheckVerdict"'):
            offenders.append(f"{name} -> {returns[name]}")
    assert not offenders, (
        "a dispatched check must answer a CheckVerdict, so that an evaluation it "
        f"could not take is not_applicable or unavailable and never a pass: {offenders}"
    )


def _literal_monitor_keys(source: str) -> list[str]:
    """Monitor keys written as string literals, minus the ones a replay binds.

    `_REPLAY_IMPLEMENTATIONS` is allowed to name what it implements: that is a
    binding to a function, not a vocabulary. Every other literal key is a second
    list in the making.
    """
    tree = ast.parse(source)
    implemented: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "_REPLAY_IMPLEMENTATIONS"
        ):
            implemented = {
                key.value
                for key in getattr(node.value, "keys", [])
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    return sorted(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in dq_monitor_registry.BY_KEY
        and node.value not in implemented
    )


def test_no_monitor_key_is_written_by_hand_in_the_replay_module() -> None:
    """Measured before this guard: `dq_issue_rows.py` held all seven as literals."""
    source = DQ_ISSUE_ROWS_SOURCE.read_text(encoding="utf-8")
    offenders = _literal_monitor_keys(source)
    assert not offenders, (
        "dq_issue_rows.py names monitor keys as literals instead of reading "
        f"core.dq_monitor_registry: {offenders}"
    )


def test_the_replay_key_guard_fires_on_a_planted_list() -> None:
    """Its teeth: a guard that cannot fail proves nothing (this file's rule)."""
    source = DQ_ISSUE_ROWS_SOURCE.read_text(encoding="utf-8")
    planted = source + '\n_SNEAKY = ("volume", "timeliness", "schema")\n'
    assert _literal_monitor_keys(planted) == ["schema", "timeliness", "volume"]


def test_the_module_docstring_names_every_monitor_and_counts_none() -> None:
    """It claimed five, listed seven and omitted `dq_geography` entirely."""
    doc = dq_monitors.__doc__ or ""
    missing = [key for key in dq_monitor_registry.BY_KEY if key not in doc]
    assert not missing, f"the dq_monitors docstring does not name: {missing}"
    assert not re.search(r"all \d+ monitors", doc), (
        "a hard-coded monitor count in the docstring goes stale the day a check "
        "is added -- name the registry instead"
    )


# ---------------------------------------------------------------------------
# Layer 2 -- what a policy may publish, and what a reader may display.
# ---------------------------------------------------------------------------


def test_a_published_policy_names_exactly_the_publishable_monitors() -> None:
    assert controls_quality.DQ_CHECKS == dq_monitor_registry.PUBLISHABLE_KEYS


def test_the_dq_reader_displays_every_publishable_type() -> None:
    """It displayed SIX, so `dq_null_rate` and `dq_zero_rows` were filtered out.

    `_DQ_TYPES` is not a cosmetic list: `dq_api.py:426,671` REFUSE a monitor
    filter absent from it, and `:739,1213` build the summary from it. Five live
    callers of `fetch_dq_report_data` read that summary.
    """
    assert dq_api._DQ_TYPES == dq_monitor_registry.PUBLISHABLE_ALERT_TYPES
    assert dq_api._MONITOR_LABELS == dq_monitor_registry.LABELS_BY_ALERT_TYPE


def test_every_displayed_type_has_a_label() -> None:
    """`dq_api.py:293` subscripts the labels directly -- a gap is a KeyError."""
    for alert_type in dq_api._DQ_TYPES:
        assert dq_api._MONITOR_LABELS[alert_type]


# ---------------------------------------------------------------------------
# Layer 3 -- the governed monitor row, and the name a person reads.
# ---------------------------------------------------------------------------


def test_the_stored_label_is_built_from_the_registry() -> None:
    """`app.dq_monitors.label` is the only monitor name that reaches a screen.

    `dq_issue_rows.py:324` selects it as `monitor_label` and
    `RunAnomalies.tsx:237` renders it, so a French entry here is French on a run.
    """
    assert dq_monitor_registry.instance_label("null_rate", "Fixture stream") == (
        "Null rate: Fixture stream"
    )
    assert dq_monitor_registry.instance_label("zero_rows", "Fixture stream") == (
        "Zero rows: Fixture stream"
    )
    # Bounded to the 160 characters `app.dq_monitors.label` accepts.
    assert len(dq_monitor_registry.instance_label("volume", "x" * 400)) == 160


def _hand_written_labels(source: str) -> list[str]:
    """Every `label` a module composes itself instead of asking the registry.

    Read from the AST and keyed on the NAME `label`, not on the text: the firing
    MESSAGES legitimately open with the monitor's own word ("Timeliness: no valid
    extraction for ..."), and a detector that matched the prose would forbid the
    English sentence it is supposed to protect.
    """

    def _is_registry_call(node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        return getattr(node.func, "attr", getattr(node.func, "id", "")) == "instance_label"

    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        value = None
        if isinstance(node, ast.keyword) and node.arg == "label":
            value = node.value
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "label" for target in node.targets
        ):
            value = node.value
        if value is None or _is_registry_call(value):
            continue
        # A pass-through (`label=label`, `label=head["label"]`) carries no
        # vocabulary of its own; only a composed STRING does.
        if isinstance(value, (ast.JoinedStr, ast.Constant, ast.BinOp)):
            offenders.append(f"line {node.lineno}")
    return offenders


def test_no_monitor_module_mints_its_own_label() -> None:
    """A second `f"Null rate: ..."` is a second vocabulary, and it drifts."""
    offenders: list[str] = []
    for relative in (
        "server/core/dq_null_rate.py",
        "server/core/dq_zero_rows.py",
        "server/core/dq_monitor_bridge.py",
        "server/core/dq_monitors.py",
    ):
        offenders += [
            f"{relative}:{hit}"
            for hit in _hand_written_labels((ROOT / relative).read_text(encoding="utf-8"))
        ]
    assert not offenders, (
        "these modules compose a monitor label themselves instead of calling "
        f"dq_monitor_registry.instance_label: {offenders}"
    )


def test_the_monitor_name_is_minted_once_and_fits_the_column() -> None:
    name = dq_monitor_registry.monitor_name("zero_rows", "ds_ABC123")
    assert name == "zero_rows_ds_abc123"
    assert dq_monitor_registry.monitor_name("not_a_monitor", "ds_1") is None
    assert dq_monitor_registry.monitor_name("volume", "") is None


# ---------------------------------------------------------------------------
# Layer 4 -- the guards and the documents.
# ---------------------------------------------------------------------------


def _hardcoded_dq_type_list(source: str) -> list[str]:
    """A run of three or more `dq_*` literals in one place -- a second truth."""
    known = set(dq_monitor_registry.FIRING_ALERT_TYPES)
    offenders: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            names = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
        if len([name for name in names if name in known]) >= 3:
            offenders.append(f"line {node.lineno}: {sorted(set(names) & known)}")
    return offenders


#: Every layer that HELD one of the eleven divergent lists and now derives it. The
#: guard runs over all four, not over the test file alone: an equality assertion
#: stays green the day a module restates a list that happens to match the registry,
#: and the day after -- when the tenth check lands -- it is a twelfth divergence.
#: `server/core/dq_monitor_registry.py` is the one place exempted, by construction:
#: it is where the list is supposed to live.
_MUST_NOT_REDECLARE_THE_TYPES = (
    "server/tests/test_infra_alerts.py",
    "server/core/dq_api.py",
    "server/core/controls_quality.py",
    "server/core/dq_monitors.py",
)


def _redeclared_dq_type_lists(relative: str, source: str | None = None) -> list[str]:
    """The offenders of one file, each named with its path AND its line."""
    text = source if source is not None else (ROOT / relative).read_text(encoding="utf-8")
    return [f"{relative}:{hit}" for hit in _hardcoded_dq_type_list(text)]


def test_the_firing_message_guard_reads_the_registry() -> None:
    """Its expected list held 7 types and omitted the `dq_geography` it reads."""
    source = INFRA_ALERTS_GUARD.read_text(encoding="utf-8")
    assert "dq_monitor_registry" in source, (
        "tests/test_infra_alerts.py must expect the registry's types, not its own list"
    )


@pytest.mark.parametrize("relative", _MUST_NOT_REDECLARE_THE_TYPES)
def test_no_layer_redeclares_the_dq_type_list(relative: str) -> None:
    """The equality assertions above cannot see a SECOND list that agrees today.

    `_DQ_TYPES == PUBLISHABLE_ALERT_TYPES` proves one list is the registry's; it
    proves nothing about a new `("dq_volume", "dq_timeliness", "dq_schema")`
    standing next to it, which is exactly the shape the eleven divergent lists
    started as. Only `dq_monitor_registry.py` may hold the literals.
    """
    offenders = _redeclared_dq_type_lists(relative)
    assert not offenders, (
        f"{relative} re-declares a literal list of dq_* types instead of reading "
        f"core.dq_monitor_registry: {offenders}"
    )


def _document_gaps(text: str) -> list[str]:
    """The registry labels a ratified document does not name."""
    lowered = text.lower()
    return [
        monitor.label
        for monitor in dq_monitor_registry.DQ_MONITORS
        if monitor.label.lower() not in lowered
    ]


def test_the_glossary_names_every_monitor() -> None:
    """The plan's proof: "un test qui compare la liste du code au glossaire".

    It named NONE of them before this story -- one line spoke of monitors, and it
    was the `Check` stage.
    """
    gaps = _document_gaps(GLOSSARY.read_text(encoding="utf-8"))
    assert not gaps, f"docs/product-architecture/glossary.md does not name: {gaps}"


def test_the_datastream_dossier_names_every_monitor_and_no_absent_table() -> None:
    text = DOSSIER.read_text(encoding="utf-8")
    gaps = _document_gaps(text)
    assert not gaps, f"doc/datastream/04 does not name: {gaps}"
    assert "dq_monitor_issues" not in text, (
        "`app.dq_monitor_issues` does not exist -- the table is `app.dq_issues`"
    )


def test_the_run_panel_reads_the_stored_label_and_names_its_absence() -> None:
    """No monitor vocabulary in the console: it renders what the server named.

    Re-stated 2026-09-02 with the label class (commit 31f13af0): the browser
    never composes a label out of an identifier -- the served label renders,
    and a NULL label is a NAMED state, not a `dqm_...` printed at the person.
    """
    source = RUN_ANOMALIES.read_text(encoding="utf-8")
    assert 'issue.monitor_label ?? "Monitor could not be read"' in source
    assert "?? issue.monitor_id" not in source, (
        "the run panel fell back to printing the monitor id again"
    )
    invented = [
        monitor.alert_type
        for monitor in dq_monitor_registry.DQ_MONITORS
        if monitor.alert_type in source
    ]
    assert not invented, f"RunAnomalies.tsx holds its own monitor vocabulary: {invented}"


# ---------------------------------------------------------------------------
# The teeth: every detector above is run against a planted defect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detector", "planted", "what"),
    [
        (
            _check_function_keys,
            "def _check_a_tenth_monitor(ds):\n    return False\n",
            "a check function with no registry entry",
        ),
        (
            lambda source: _hardcoded_dq_type_list(source),
            'EXPECTED = ("dq_volume", "dq_timeliness", "dq_schema")\n',
            "a guard re-declaring the type list",
        ),
        (
            lambda text: _document_gaps(text),
            "# A document that names Volume and nothing else\n",
            "a document that has fallen behind the registry",
        ),
        (
            _hand_written_labels,
            'label = f"Null rate: {name}"\n',
            "a module composing a monitor label by hand",
        ),
    ],
)
def test_each_detector_fires_on_a_planted_defect(detector, planted, what) -> None:
    """A guard that cannot fail is a comment with a green tick."""
    result = detector(planted)
    if what.startswith("a check function"):
        assert result - set(dq_monitor_registry.BY_KEY) == {"a_tenth_monitor"}, what
    else:
        assert result, what


#: A second truth, in the exact shape the eleven lists took: three types, ordered
#: like a hand-maintained tuple, and agreeing with the registry ON THE DAY it is
#: written. It is planted into each guarded source in turn.
_PLANTED_TYPE_LIST = '\n_EXPECTED_DQ_TYPES = ("dq_volume", "dq_timeliness", "dq_schema")\n'


@pytest.mark.parametrize("relative", _MUST_NOT_REDECLARE_THE_TYPES)
def test_the_redeclaration_guard_fires_on_each_guarded_source(relative: str) -> None:
    """The guard is run over the REAL file with the defect appended to it.

    Green on four clean files proves only that the reader found nothing. This
    plants the list at a known line of each guarded source and requires the guard
    to name that file AND that line -- so an offender is reported where it lives,
    not as a bare boolean.
    """
    source = (ROOT / relative).read_text(encoding="utf-8")
    planted_line = source.count("\n") + 2
    offenders = _redeclared_dq_type_lists(relative, source + _PLANTED_TYPE_LIST)
    assert offenders, f"the guard is blind to a re-declared type list in {relative}"
    assert any(
        offender.startswith(f"{relative}:line {planted_line}:") for offender in offenders
    ), f"the guard fired without naming {relative}:line {planted_line}: {offenders}"


# ---------------------------------------------------------------------------
# A refusal a person reads is written in English -- CLAUDE.md, "tout en anglais"
# ---------------------------------------------------------------------------


#: The tests still pinned to a French refusal, MEASURED on 2026-08-17. The list
#: may only shrink. It is not a whitelist of things that are fine: each entry is
#: a test that can only pass while the message it reads stays French, and
#: `server/core` still holds 267 such messages -- translating them is a sweep of
#: its own, not a side-effect of this file.
_TESTS_STILL_PINNED_TO_FRENCH = (
    "core/test_conflict_resolutions.py:102",
    "core/test_datamodel.py:607",
    "core/test_datamodel.py:623",
    "core/test_datamodel.py:639",
    "core/test_datastreams.py:358",
)


def test_no_new_test_pins_a_french_word_from_a_refusal() -> None:
    """A ratchet, because twice on 2026-08-17 a red came from this exact shape.

    Someone puts a user-facing message into English -- which CLAUDE.md requires
    -- and the `pytest.raises(match=...)` that pinned the French word goes red.
    Read backwards, the expectation is worse than the red it causes: a test that
    can only pass while a refusal stays French is a test that ARGUES for keeping
    it French.

    Three sites were repaired the same day: two already-red in `test_datamodel`
    (their message had been translated and the test was not told) and one green
    for the wrong reason in `test_conflict_resolutions` -- it matched a message
    that was itself still French, so both sides were moved to English together.

    A fourth case was left alone deliberately: `mcp_profiles.py` is being
    translated by another session right now and is uncommitted. Its test will go
    red for them, and a guard is not the place to reach into work in flight.
    """
    import re

    tests_root = Path(__file__).resolve().parents[1]
    french = re.compile(
        r"""match=["'][^"']*\b(introuvable|inexistant|invalide|obligatoire|interdit|"""
        r"""echoue|manquant|refuse)\b""",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in tests_root.rglob("test_*.py"):
        if "__pycache__" in path.parts or path == Path(__file__).resolve():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if french.search(line):
                offenders.append(f"{path.relative_to(tests_root).as_posix()}:{number}")

    appeared = sorted(set(offenders) - set(_TESTS_STILL_PINNED_TO_FRENCH))
    assert not appeared, (
        "a NEW test pins a French word from a refusal. The message a person reads "
        "is English (CLAUDE.md), so this expectation can only hold while the "
        f"product breaks that rule: {appeared}"
    )
