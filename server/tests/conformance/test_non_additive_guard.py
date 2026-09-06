"""Une metrique non additive ne se somme pas -- sur les SEPT, pas sur une.

Une seule implementation (`scripts/check_non_additive_guard.py`), deux portes :
celle-ci pour le run local, `make check-non-additive-guard` pour la CI.

CE QUE CES TESTS TIENNENT, ET CE N EST PAS LA LISTE. Le garde precedent etait un
grep sur `SUM(average_position)` et il avait trois trous, dont un seul comptait
vraiment : il ne connaissait qu UN nom quand `dim_metric.csv` en declare sept.
Six metriques non additives pouvaient etre sommees sans que rien ne rougisse.

Les deux autres se prouvent aussi, parce qu un garde qui rougit a tort se
desactive : `^[^-]` prenait tout commentaire INDENTE pour du code, et le motif
litteral laissait passer la casse et la qualification par alias.

ET LA LIGNE A NE PAS FRANCHIR : `SUM(average_position * impressions)` est la
ponderation, et c est la seule facon JUSTE de calculer cette metrique. Un garde
qui la refuserait aurait interdit la reparation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_non_additive_guard.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_non_additive_guard", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_non_additive_metric_is_summed_on_its_own_in_a_mart():
    problems = _module().offences()
    assert problems == [], "\n".join(problems)


def test_the_guard_watches_every_metric_the_catalogue_declares_non_additive():
    """LE TROU QUI COMPTAIT : il en surveillait UN sur sept.

    Les noms sont DERIVES du seed, jamais ecrits ici -- une huitieme metrique
    declaree demain entre dans le garde sans que personne y pense. Ce test
    verifie qu ils sont bien derives, pas qu ils sont ces sept-la.
    """
    module = _module()
    names = module.non_additive_metric_names()
    assert {"average_position", "roas", "ctr", "cpa"} <= names
    assert len(names) >= 7, f"le catalogue en declare {len(names)}"
    # Et une additive n y entre pas.
    assert "clicks" not in names and "impressions" not in names


def test_a_bare_sum_is_caught_whatever_its_case_or_its_alias(tmp_path):
    """Le motif litteral laissait passer trois formes de la meme faute."""
    module = _module()
    marts = tmp_path / "marts"
    marts.mkdir()
    (marts / "wrong.sql").write_text(
        "SELECT\n"
        "  sum(average_position) AS a,\n"
        "  SUM( ctr ) AS b,\n"
        "  SUM(f.roas) AS c\n"
        "FROM x f\n",
        encoding="utf-8",
    )
    problems = module.offences(marts=marts, metrics=frozenset({"average_position", "ctr", "roas"}))
    assert len(problems) == 3, problems
    assert any(":2:" in problem for problem in problems)
    assert any(":3:" in problem for problem in problems)
    assert any(":4:" in problem for problem in problems)


def test_the_weighted_formula_is_never_refused(tmp_path):
    """La seule facon JUSTE de calculer `average_position`.

    Un garde qui la refuserait aurait interdit `semantic_avg_position`, qui est
    exactement la reparation que la regle AD-4 demande.
    """
    module = _module()
    marts = tmp_path / "marts"
    marts.mkdir()
    (marts / "right.sql").write_text(
        "SELECT SUM(average_position * impressions) / "
        "NULLIF(SUM(impressions), 0) AS average_position\n",
        encoding="utf-8",
    )
    assert module.offences(marts=marts, metrics=frozenset({"average_position"})) == []


def test_an_indented_comment_is_a_comment(tmp_path):
    """`^[^-]` prenait pour du code toute ligne dont le 1er caractere n est pas `-`.

    La grande majorite des commentaires SQL de ce depot sont INDENTES : le garde
    rougissait donc sur de la prose, ce qui est la facon la plus sure de se faire
    desactiver.
    """
    module = _module()
    marts = tmp_path / "marts"
    marts.mkdir()
    (marts / "documented.sql").write_text(
        "-- SUM(average_position) is what this file exists to avoid.\n"
        "    -- and so is SUM(ctr), said here where it is indented\n"
        "{# SUM(roas) in a jinja comment #}\n"
        "SELECT 1 AS ok  -- never SUM(cpa)\n",
        encoding="utf-8",
    )
    metrics = frozenset({"average_position", "ctr", "roas", "cpa"})
    assert module.offences(marts=marts, metrics=metrics) == []
