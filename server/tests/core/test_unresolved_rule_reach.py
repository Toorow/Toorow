"""La portee d'une regle de reparation, avant que quiconque la confirme.

`unresolved-values.md` S3.3 exige la phrase *"this rule also matches 42 of the
128 remaining values"* AVANT la confirmation, et donne la raison : c'est elle qui
transforme 200 paires en une regle. Une phrase qui arrive apres le clic ne
transforme rien -- elle constate.

CE QUE CES TESTS GARDENT, ET POURQUOI CHACUN EXISTE :

  * la phrase elle-meme, y compris ses deux cas limites (zero autre valeur, une
    seule) -- un "1 values" est le genre de defaut qu'un ecran livre et que
    personne ne signale ;
  * `absent_at_source` HORS du compte : aucune paire ne repare une valeur absente
    a la source, donc la compter serait promettre une reparation impossible ;
  * la valeur ouverte EXCLUE : la phrase dit "also", donc compter celle qui est
    deja a l'ecran gonfle chaque nombre de un ;
  * un motif invalide qui rend `also_matches: None` et non `0` -- un zero se lit
    "cette regle ne touche rien", ce qui est exactement le contraire de "cette
    regle n'a pas ete jouee" (AD-9).
"""

from __future__ import annotations

import pytest
from core.unresolved_rule_reach import (
    MATCH_MODES,
    InvalidRule,
    compile_rule,
    rule_reach,
    sentence,
)


def _value(source: str, occurrences: int = 1, *, pairable: bool = True) -> dict:
    """Une ligne a la forme que `_value` de l'API rend."""
    return {
        "source_value": source,
        "occurrences": occurrences,
        "action": {"pair_editor": pairable},
    }


CORPUS = [
    _value("FR - Paris", 10),
    _value("FR - Lyon", 5),
    _value("fr - Nice", 3),
    _value("DE - Berlin", 7),
    _value("(not set)", 100, pairable=False),
]


# ---------------------------------------------------------------------------
# Les quatre modes
# ---------------------------------------------------------------------------


def test_exact_matches_one_and_only_one():
    reach = rule_reach(CORPUS, pattern="FR - Lyon", mode="exact")
    assert reach["valid"] is True
    assert reach["also_matches"] == 1
    assert [s["source_value"] for s in reach["sample"]] == ["FR - Lyon"]


def test_exact_case_insensitive_is_the_mode_that_catches_the_lowercase_twin():
    """Le cas qui justifie le mode : `fr - Nice` et `FR - Nice` sont la meme ville."""
    exact = rule_reach(CORPUS, pattern="FR - Nice", mode="exact")
    assert exact["also_matches"] == 0
    ci = rule_reach(CORPUS, pattern="FR - Nice", mode="exact_ci")
    assert ci["also_matches"] == 1
    assert ci["sample"][0]["source_value"] == "fr - Nice"


def test_contains_is_what_turns_two_hundred_pairs_into_one_rule():
    reach = rule_reach(CORPUS, pattern="FR - ", mode="contains")
    assert reach["also_matches"] == 2  # Paris, Lyon -- `fr - Nice` est en minuscules
    assert reach["occurrences"] == 15


def test_regex_reaches_what_contains_cannot():
    reach = rule_reach(CORPUS, pattern=r"^(FR|DE) - ", mode="regex")
    assert reach["also_matches"] == 3


# ---------------------------------------------------------------------------
# Les deux refus construits dans le module
# ---------------------------------------------------------------------------


def test_a_value_absent_at_source_is_never_counted():
    """Aucune paire ne repare `(not set)` -- la compter promet l'impossible.

    Le motif ci-dessous matche TOUT. Si `pair_editor: false` n'etait pas filtre,
    le compte serait 4 et la personne lirait qu'une regle repare 100 occurrences
    qu'elle ne touchera jamais.
    """
    reach = rule_reach(CORPUS, pattern="", mode="contains")
    # `contains ""` est refuse en amont (motif vide), donc on prend un motif vrai.
    reach = rule_reach(CORPUS, pattern=".", mode="regex")
    assert reach["also_matches"] == 4
    assert all(s["source_value"] != "(not set)" for s in reach["sample"])
    # Et le denominateur non plus ne le compte pas.
    assert reach["remaining"] == 4


def test_a_row_without_typing_is_not_assumed_repairable():
    """L'absence de `action` est une reponse manquante, pas un oui."""
    reach = rule_reach([{"source_value": "X", "occurrences": 1}], pattern=".", mode="regex")
    assert reach["also_matches"] == 0
    assert reach["remaining"] == 0


@pytest.mark.parametrize(
    ("pattern", "mode", "reason"),
    [
        ("", "exact", "empty_pattern"),
        ("x", "fuzzy", "unknown_match_mode"),
        ("(", "regex", "pattern_does_not_compile"),
        ("a" * 513, "contains", "pattern_too_long"),
    ],
)
def test_a_rule_that_cannot_run_says_so_and_never_answers_zero(pattern, mode, reason):
    reach = rule_reach(CORPUS, pattern=pattern, mode=mode)
    assert reach["valid"] is False
    assert reach["reason"] == reason
    # LE POINT. `0` se lirait "cette regle est sans danger, elle ne touche rien".
    assert reach["also_matches"] is None
    assert reach["occurrences"] is None
    assert reach["message"]


def test_the_refusal_names_a_gesture_and_not_a_cause():
    reach = rule_reach(CORPUS, pattern="(", mode="regex")
    assert "switch" in reach["message"] and "contains" in reach["message"]


def test_compile_rule_raises_for_a_caller_that_wants_the_exception():
    with pytest.raises(InvalidRule) as exc:
        compile_rule("(", "regex")
    assert exc.value.reason == "pattern_does_not_compile"


# ---------------------------------------------------------------------------
# `also`, et la phrase
# ---------------------------------------------------------------------------


def test_the_value_the_drawer_was_opened_on_is_excluded():
    """La phrase dit "ALSO" : compter celle qui est deja a l'ecran gonfle tout."""
    without = rule_reach(CORPUS, pattern="FR - ", mode="contains")
    with_exclude = rule_reach(
        CORPUS, pattern="FR - ", mode="contains", exclude="FR - Paris"
    )
    assert without["also_matches"] == 2
    assert with_exclude["also_matches"] == 1
    assert with_exclude["remaining"] == without["remaining"] - 1


def test_the_sentence_is_the_one_the_document_writes():
    reach = rule_reach(CORPUS, pattern="FR - ", mode="contains", exclude="FR - Paris")
    assert sentence(reach) == "This rule also matches 1 of the 3 remaining values."


def test_the_sentence_is_plural_when_it_should_be():
    reach = rule_reach(CORPUS, pattern="FR - ", mode="contains")
    assert sentence(reach) == "This rule also matches 2 of the 4 remaining values."


def test_the_sentence_of_a_rule_that_reaches_nothing_else_does_not_read_as_a_failure():
    reach = rule_reach(
        CORPUS, pattern="DE - Berlin", mode="exact", exclude="DE - Berlin"
    )
    assert sentence(reach) == (
        "This rule matches this value only, and none of the 3 others."
    )


def test_the_sentence_of_the_last_value_left_says_that_instead_of_zero_of_zero():
    reach = rule_reach(
        [_value("only one")], pattern="only one", mode="exact", exclude="only one"
    )
    assert sentence(reach) == "This is the only value left to repair on this dimension."


def test_the_sentence_of_an_invalid_rule_is_the_refusal_itself():
    reach = rule_reach(CORPUS, pattern="(", mode="regex")
    assert sentence(reach) == reach["message"]


# ---------------------------------------------------------------------------
# Les bornes
# ---------------------------------------------------------------------------


def test_the_count_is_exact_over_the_whole_corpus_and_only_the_sample_is_cut():
    """Un echantillon coupe qui couperait aussi le COMPTE ferait mentir la phrase."""
    corpus = [_value(f"v{i}", 1) for i in range(200)]
    reach = rule_reach(corpus, pattern="v", mode="contains")
    assert reach["also_matches"] == 200
    assert len(reach["sample"]) == 50
    assert reach["sample_truncated"] is True


def test_a_corpus_beyond_the_cap_says_so_rather_than_answering_on_a_slice_in_silence():
    corpus = [_value(f"v{i}", 1) for i in range(5001)]
    reach = rule_reach(corpus, pattern="v", mode="contains")
    assert reach["corpus_truncated"] is True
    assert reach["also_matches"] == 5000


def test_every_declared_mode_is_compilable():
    """La liste et le code ne peuvent pas diverger sans que ceci rougisse."""
    for mode in MATCH_MODES:
        assert compile_rule("x", mode)("x") is True


def test_the_noun_agrees_with_the_total_and_not_with_the_subset():
    """Le document accorde sur `remaining` : "42 of the 128 remaining VALUES".

    Accorde sur `also`, la phrase rend "1 of the 3 remaining value" -- le defaut
    que ce module dit vouloir eviter, ecrit a l'envers. Le seul singulier legitime
    est celui d'un total de un.
    """
    one_left = rule_reach(
        [_value("a"), _value("b")], pattern="a", mode="exact", exclude="b"
    )
    assert sentence(one_left) == "This rule also matches 1 of the 1 remaining value."
    subset = rule_reach(CORPUS, pattern="FR - Paris", mode="exact")
    assert subset["also_matches"] == 1 and subset["remaining"] == 4
    assert sentence(subset).endswith("4 remaining values.")


def test_the_whole_matched_list_travels_even_when_the_sample_is_cut():
    """Le magasin tient des PAIRES : la confirmation deplie la regle.

    `sample` sert a lire et se coupe a 50 ; `matched_values` sert a ECRIRE et ne
    se coupe pas. Confondre les deux ferait qu'une regle annoncee sur 200 valeurs
    en reparerait 50, et le compte affiche aurait eu raison.
    """
    corpus = [_value(f"v{i}", 1) for i in range(200)]
    reach = rule_reach(corpus, pattern="v", mode="contains")
    assert len(reach["sample"]) == 50
    assert len(reach["matched_values"]) == 200
    assert reach["matched_values"][0] == "v0"


def test_an_invalid_rule_unfolds_to_nothing_rather_than_to_the_whole_corpus():
    reach = rule_reach(CORPUS, pattern="(", mode="regex")
    assert reach["matched_values"] == []
