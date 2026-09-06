"""Un saut STRUCTUREL n'est pas une ligne PERDUE — story 67.25, 2026-08-22.

CE QUE LA PORTE FAISAIT, ET L'ARITHMÉTIQUE QUI LE MONTRE.
`evaluate_rejection_gate` calculait `rejected / (rejected + accepted)` contre un
seuil de 25 % par défaut. Une feuille de plan média de douze lignes qui porte
trois sous-totaux et un total général rend **4/12 = 33 %** — au-dessus du seuil,
donc publication bloquée — alors que ses **huit lignes de plan ont toutes
atterri**. Le fichier est parfait et le produit refuse de le publier.

`file-source-ingestion.md:537` le nommait déjà : « The rejection gate is
calibrated for exports, not for plans. […] a structural skip and a lost row are
not the same event and the gate cannot currently tell them apart. »

**Le parseur savait, lui.** `file_source_producer` émet `subtotal_or_total`,
`group_or_noise` et `merged_amount_carried` depuis toujours : les lignes sont
nommées pour ce qu'elles sont. C'est la porte qui les additionnait avec les
pertes.

CE QUE CES TESTS TIENNENT, ET LE PIÈGE SYMÉTRIQUE QU'ILS ÉVITENT. Sortir ces
lignes du seul dénominateur diluerait le vrai taux de perte : une feuille avec
cent sous-totaux et quatre lignes perdues sur huit passerait. Elles sortent donc
**des deux côtés** — ce ne sont pas des lignes de données refusées, ce sont des
lignes que le format porte.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _gate(**kwargs):
    from core.managed_feed_ledger import evaluate_rejection_gate

    return evaluate_rejection_gate(preferences=None, **kwargs)


# ---------------------------------------------------------------------------
# Le cas qui a motivé la réparation
# ---------------------------------------------------------------------------


def test_a_perfect_plan_sheet_is_no_longer_blocked_by_its_own_subtotals():
    """Douze lignes, trois sous-totaux, un total, huit lignes de plan atterries.

    C'est le cas exact du document. Avant : 4/12 = 33 % > 25 %, bloqué. Après :
    zéro perte, donc rien à bloquer.
    """
    assert _gate(accepted_row_count=8, rejected_row_count=4, structural_skip_count=4) is None
    # Et sans la distinction, la porte bloquait -- la preuve que le test mesure
    # bien la réparation et non une porte devenue indulgente.
    blocked = _gate(accepted_row_count=8, rejected_row_count=4)
    assert blocked is not None
    assert "33.33%" in blocked["detail"]


def test_a_real_loss_still_blocks_even_when_the_sheet_carries_structure():
    """La porte reste une porte : ce qui est perdu compte encore.

    Huit lignes de plan, trois sous-totaux, et QUATRE lignes réellement perdues.
    Le ratio se lit sur les lignes de données seules : 4/12 = 33 %, bloqué.
    """
    issue = _gate(accepted_row_count=8, rejected_row_count=7, structural_skip_count=3)
    assert issue is not None
    assert issue["detail"].startswith("4/12 rows rejected")
    detail = issue["repair"]["fix_source_or_mapping"]
    assert detail["rejected_row_count"] == 4
    assert detail["structural_skip_count"] == 3


def test_the_structure_is_NAMED_in_the_refusal_and_not_silently_dropped():
    """Une personne qui lit le refus doit savoir ce qui n'a pas été compté.

    Sans cette phrase, le compte affiché (4) ne correspondrait à aucun nombre
    visible dans son fichier (11 lignes non atterries), et elle chercherait sept
    lignes fantômes.
    """
    issue = _gate(accepted_row_count=8, rejected_row_count=7, structural_skip_count=3)
    assert "3 structural rows the file declares are not data were not counted" in (
        issue["detail"]
    )


def test_a_refusal_with_no_structure_says_nothing_about_structure():
    """Une phrase qui parle de zéro sous-total est du bruit dans un message d'erreur."""
    issue = _gate(accepted_row_count=1, rejected_row_count=9)
    assert issue is not None
    assert "structural" not in issue["detail"]


# ---------------------------------------------------------------------------
# Le piège symétrique, et le refus qui échoue fermé
# ---------------------------------------------------------------------------


def test_structure_does_not_DILUTE_the_loss_rate():
    """Le défaut inverse : sortir les sauts du dénominateur SEUL.

    Cent sous-totaux, huit lignes de plan, quatre perdues. Avec la structure au
    dénominateur, 4/108 = 3,7 % passerait ; la moitié des lignes de plan a
    pourtant été perdue. Ils sortent des DEUX côtés, donc 4/8 = 50 %, bloqué.
    """
    issue = _gate(accepted_row_count=4, rejected_row_count=104, structural_skip_count=100)
    assert issue is not None
    assert issue["detail"].startswith("4/8 rows rejected (50.00%)")


def test_more_skips_than_rejections_fails_CLOSED():
    """Deux comptes qui se contredisent ne se soustraient pas en silence.

    Soustraire aveuglément rendrait un `lost` négatif, donc un ratio négatif, donc
    une porte franchie par un fichier dont on ne sait rien. Le refus dit que les
    comptes désaccordent, et il nomme le geste : reparser.
    """
    issue = _gate(accepted_row_count=10, rejected_row_count=2, structural_skip_count=5)
    assert issue is not None
    assert "the counts disagree" in issue["detail"]
    assert issue["repair"] == {"reparse_source": True}


def test_a_negative_structural_count_fails_CLOSED_like_the_others():
    issue = _gate(accepted_row_count=10, rejected_row_count=1, structural_skip_count=-1)
    assert issue is not None
    assert issue["repair"] == {"reparse_source": True}


def test_a_sheet_that_is_ONLY_structure_publishes_nothing_and_blocks_nothing():
    """Zéro ligne de donnée, zéro perte : il n'y a rien à refuser.

    C'est le cas d'un onglet de récapitulation. Bloquer dirait « trop de rejets »
    sur un fichier qui n'a rien perdu du tout.
    """
    assert _gate(accepted_row_count=0, rejected_row_count=6, structural_skip_count=6) is None


# ---------------------------------------------------------------------------
# La compatibilité, et la source des codes
# ---------------------------------------------------------------------------


def test_a_caller_that_does_not_distinguish_gets_exactly_the_old_behaviour():
    """Le défaut à 0 : la réparation n'a change aucun appelant qu'elle n'a pas touché."""
    for accepted, rejected in ((8, 4), (9, 1), (1, 9), (0, 0), (5, 0)):
        assert _gate(
            accepted_row_count=accepted, rejected_row_count=rejected
        ) == _gate(
            accepted_row_count=accepted,
            rejected_row_count=rejected,
            structural_skip_count=0,
        )


def test_the_structural_codes_come_from_the_producer_and_are_not_retyped():
    """Une seconde orthographe ferait une seconde taxonomie, et un jour un écart."""
    from core.file_source_producer import (
        RULE_GROUP_OR_NOISE,
        RULE_MERGED_AMOUNT_CARRIED,
        RULE_SUBTOTAL_OR_TOTAL,
    )
    from core.managed_feed_ledger import _structural_rules

    assert _structural_rules() == frozenset(
        {RULE_SUBTOTAL_OR_TOTAL, RULE_GROUP_OR_NOISE, RULE_MERGED_AMOUNT_CARRIED}
    )


@pytest.mark.parametrize(
    "rule",
    ["missing_dates", "bad_amount", "inverted_dates", "required", "unparsable_metric"],
)
def test_a_real_defect_is_never_filed_as_structure(rule):
    """La liste est FERMÉE, et c'est ce qui l'empêche de devenir une échappatoire.

    `missing_dates`, `bad_amount` et `inverted_dates` sont des lignes que le
    fichier voulait poser et qui n'ont pas atterri : ce sont des pertes. Les
    ranger avec les sous-totaux rendrait la porte incapable de bloquer quoi que
    ce soit.
    """
    from core.managed_feed_ledger import _structural_rules

    assert rule not in _structural_rules()


def test_the_counter_reads_the_rule_and_ignores_everything_else():
    from core.file_source_producer import RULE_SUBTOTAL_OR_TOTAL
    from core.managed_feed_ledger import count_structural_skips

    rows = [
        {"rule": RULE_SUBTOTAL_OR_TOTAL},
        {"rule": "bad_amount"},
        {"rule": RULE_SUBTOTAL_OR_TOTAL},
        {},  # une ligne sans regle n'est pas de la structure : elle est inconnue
        {"rule": None},
    ]
    assert count_structural_skips(rows) == 2
    assert count_structural_skips(None) == 0
