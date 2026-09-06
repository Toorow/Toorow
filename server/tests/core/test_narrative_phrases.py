"""Un récit se compose par CLÉ, jamais par phrase.

Ces tests s'épinglent sur le MÉCANISME ratifié le 2026-08-25
(`docs/product-architecture/analyze-and-test.md`, « how a narrative sentence is
rendered »), qui rend mécanique l'arbitrage de Jean du 2026-08-22 : un récit se
rend dans la langue du lecteur, donc une phrase de récit écrite en dur -- dans
quelque langue que ce soit -- est le défaut.

Ils ne vérifient donc PAS une phrase française. Ils vérifient que :

  * le catalogue est cohérent (une valeur dans la langue par défaut, des
    placeholders qui se rendent, une clé anglaise stable) ;
  * les producteurs de récit composent par clé -- la ligne rendue est
    exactement ce que le catalogue rend pour cette clé, quelles que soient ses
    valeurs ;
  * la langue est un PARAMÈTRE de rendu, pas une constante ;
  * le garde qui tient tout cela rougit vraiment.

Le seul test qui cite une phrase est celui qui prouve que la clé
`context_missing` est le SEUL endroit où la ligne AD-9 est épelée -- une
exception assumée, parce que cette ligne-là est un verbatim contractuel.
"""

from __future__ import annotations

import importlib.util
import re
import string
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server"))

from core import narrative as narrative_module  # noqa: E402
from core import narrative_phrases as cat  # noqa: E402

GUARD = ROOT / "scripts" / "check_narrative_no_raw.py"


def _guard():
    spec = importlib.util.spec_from_file_location("check_narrative_no_raw", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fields(template: str) -> set[str]:
    return {
        name
        for _text, name, _spec, _conv in string.Formatter().parse(template)
        if name
    }


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------


def test_every_key_is_english_and_stable_shaped():
    """A key is compared by code, so it is English and lowercase (doc:1976)."""
    bad = [k for k in cat.PHRASES if not re.fullmatch(r"[a-z0-9_]+", k)]
    assert bad == [], f"clés non conformes : {bad}"


def test_every_entry_ships_the_default_language():
    missing = [k for k, v in cat.PHRASES.items() if cat.DEFAULT_NARRATIVE_LANGUAGE not in v]
    assert missing == [], f"clés sans la langue par défaut : {missing}"


def test_the_catalogue_carries_only_languages_a_request_can_ask_for():
    """A column no request can reach is dead copy -- an Incomplete if of the doc."""
    declared = set(cat.SUPPORTED_NARRATIVE_LANGUAGES)
    assert cat.DEFAULT_NARRATIVE_LANGUAGE in declared
    for key, templates in cat.PHRASES.items():
        assert set(templates) <= declared, f"{key} porte une langue non déclarée"
    for language in cat.METRIC_LABELS:
        assert language in declared
    for language in cat.DIMENSION_DEFAULT_WORDS:
        assert language in declared


def test_every_template_renders_with_its_own_fields():
    for key, templates in cat.PHRASES.items():
        for language, template in templates.items():
            fields = _fields(template)
            rendered = cat.phrase(key, language=language, **{f: "X" for f in fields})
            assert isinstance(rendered, str) and rendered != ""


def test_no_template_uses_a_positional_placeholder():
    """A positional `{}` would tie the words to an argument order."""
    for key, templates in cat.PHRASES.items():
        for template in templates.values():
            names = [
                name
                for _t, name, _s, _c in string.Formatter().parse(template)
                if name is not None
            ]
            assert all(name for name in names), f"{key} porte un placeholder positionnel"


def test_an_unknown_key_is_a_named_failure_not_a_printed_identifier():
    with pytest.raises(cat.UnknownPhraseKey):
        cat.phrase("no_such_narrative_key")


def test_the_language_is_a_render_parameter_with_a_declared_default():
    """The default is the ABSENCE of a deduction, and it is named."""
    assert cat.DEFAULT_NARRATIVE_LANGUAGE in cat.SUPPORTED_NARRATIVE_LANGUAGES
    # An unsupported language falls back to the default: a reader gets a sentence,
    # never a key.
    assert cat.phrase("context_missing", language="xx") == cat.phrase("context_missing")
    assert cat.phrase(
        "context_missing", language=cat.DEFAULT_NARRATIVE_LANGUAGE
    ) == cat.phrase("context_missing")


def test_an_unknown_metric_keeps_its_own_key():
    """AD-2: naming a measure nobody declared would invent a name."""
    assert cat.metric_label("no_such_metric") == "no_such_metric"
    assert cat.metric_label("clicks") != "clicks"


def test_an_undeclared_dimension_keeps_its_own_name():
    assert cat.dimension_default("no_such_dimension") == "no_such_dimension"
    for name in cat.DIMENSION_DEFAULT_WORDS[cat.DEFAULT_NARRATIVE_LANGUAGE]:
        assert cat.dimension_default(name) != name


# ---------------------------------------------------------------------------
# The producers compose BY KEY -- pinned on the mechanism, not on the words
# ---------------------------------------------------------------------------


def test_a_card_line_is_exactly_what_the_catalogue_renders_for_its_key():
    citation = "(connector:fact_daily_kpi, pull_1)"
    produced = narrative_module.build_keywords_comment(
        block_data={},
        rollup={"clicks": {"value": 1234}},
        context_events=[],
        pull_ids=["pull_1"],
    ).splitlines()
    assert produced[0] == cat.phrase(
        "keywords_total_clicks", value="1 234", citation=citation
    )
    assert produced[-1] == cat.phrase("context_missing")


def test_the_dimension_fallback_is_the_catalogue_word_never_the_identifier():
    """Story 27.9 contract, unchanged: only the spelling of the fallback moved."""
    assert narrative_module.dimension_word(None, "query", cat.dimension_default("query")) == (
        cat.dimension_default("query")
    )
    assert narrative_module.dimension_word(
        {"query": {"label_source": "client", "display_label": "Mot-clé"}},
        "query",
        cat.dimension_default("query"),
    ) == "Mot-clé"


def test_the_ad9_absence_line_is_spelled_once_in_the_whole_repository():
    """The AD-9 verbatim is a contract: one spelling, and it is the catalogue's."""
    assert narrative_module._CONTEXT_MISSING_LINE == cat.phrase("context_missing")
    line = cat.phrase("context_missing")
    covered = [
        ROOT / "server" / "core" / "narrative.py",
        ROOT / "server" / "core" / "summarizer.py",
        ROOT / "server" / "core" / "anomaly_alerts.py",
        ROOT / "server" / "core" / "reports.py",
        ROOT / "server" / "core" / "analyze_render_mcp.py",
    ]
    for path in covered:
        assert line not in path.read_text(encoding="utf-8"), f"{path.name} ré-épelle la ligne"


def test_the_three_alert_sentences_resolve_through_one_catalogue():
    """« Un seul langage d'alerte par produit » -- the module carried two."""
    source = (ROOT / "server" / "core" / "anomaly_alerts.py").read_text(encoding="utf-8")
    assert "Anomaly detected on" not in source
    for key in ("anomaly_line", "anomaly_widget_message", "anomaly_firing_message"):
        assert f'phrase(\n                    "{key}"' in source or f'"{key}"' in source


# ---------------------------------------------------------------------------
# The instrument
# ---------------------------------------------------------------------------


def test_rule_d_is_green_on_the_tree():
    assert _guard().prose_offences(ROOT) == []


def test_rule_d_reddens_on_a_re_injected_phrase(tmp_path):
    """An instrument that cannot be put in the red has never proved it reddens."""
    guard = _guard()
    module = tmp_path / "fake_narrative.py"
    module.write_text(
        'def build():\n'
        '    return "Une phrase de recit ecrite en dur"\n',
        encoding="utf-8",
    )
    offences = guard.prose_offences(tmp_path, ("fake_narrative.py",))
    assert len(offences) == 1
    assert "phrase de recit ecrite en dur" in offences[0]


def test_rule_d_leaves_operator_messages_and_logs_alone(tmp_path):
    """The doc keeps a refusal and a log in English, in the code (doc:1974-1978)."""
    guard = _guard()
    module = tmp_path / "fake_narrative.py"
    module.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n\n"
        "def build():\n"
        '    """Une docstring explique, elle n est rendue a personne."""\n'
        '    logger.warning("reports: freshness not evaluated for %s", 1)\n'
        '    rows = "SELECT id FROM app.reports"\n'
        "    if not rows:\n"
        '        raise ValueError("Report not found: name the gesture")\n'
        "    return rows\n",
        encoding="utf-8",
    )
    assert guard.prose_offences(tmp_path, ("fake_narrative.py",)) == []


def test_rule_d_reddens_on_a_narrative_function_NOBODY_declared(tmp_path):
    """THE HOLE THIS CLOSES, proved on a throwaway tree.

    Until 2026-08-31 rule D walked a declared list of function NAMES. A covered
    file could therefore grow a narrative-producing function and stay green
    forever -- not because it was clean, but because the guard never looked
    inside it. That is exactly how `reports._state_the_limiting_term` and
    `analyze_render_mcp.build_summary` spelled French sentences under a green
    gate for a week.

    Here `build` is named-and-exempt and `undeclared_writer` is named NOWHERE.
    The old rule returned ZERO offences on this file; the derived one must name
    the undeclared function's sentence, because absence from the list now means
    SCANNED.
    """
    guard = _guard()
    module = tmp_path / "fake_narrative.py"
    module.write_text(
        "def build():\n"
        '    return "Une phrase dispensee explicitement"\n'
        "\n"
        "def undeclared_writer():\n"
        '    return "Une phrase que personne n a declaree"\n',
        encoding="utf-8",
    )
    offences = guard.prose_offences(
        tmp_path,
        ("fake_narrative.py",),
        {"fake_narrative.py": {"build": "dispensee pour ce test"}},
    )
    assert len(offences) == 1, offences
    assert "personne n a declaree" in offences[0]
    # ...and the exemption really exempts: if it did not, this test would pass
    # for the wrong reason and prove nothing about the derivation.
    assert "dispensee explicitement" not in offences[0]


def test_an_exemption_that_names_no_function_is_itself_an_offence(tmp_path):
    """A ghost dispensation reads as a fact about the file, and is not one."""
    guard = _guard()
    module = tmp_path / "fake_narrative.py"
    module.write_text("def other():\n    return 1\n", encoding="utf-8")
    offences = guard.prose_offences(
        tmp_path,
        ("fake_narrative.py",),
        {"fake_narrative.py": {"build": "renommee ailleurs"}},
    )
    assert any("exemption sans fonction" in o for o in offences)


def test_the_guard_prints_its_perimeter():
    """A partial guard that stays silent about its perimeter reads as a whole one."""
    lines = _guard()._perimeter_lines()
    joined = "\n".join(lines)
    assert "PORTEE" in joined
    for relative in _guard().NARRATIVE_FILES:
        assert relative in joined
    # It must also say HOW the perimeter is obtained: "these files, entirely" is
    # a different promise from "these named functions", and a reader has to be
    # able to tell which one they are being handed.
    assert "EN ENTIER" in joined
    assert "narrative_phrases.py" in joined
