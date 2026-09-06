"""Un pas de Skill franchi entre dans le chemin observe -- Story 45.7.

POURQUOI CE FICHIER. `app.ai_path_steps` declare `skill_step` comme genre de pas
de premiere classe, epingle `skill_step_id` et `skill_version_id` ensemble par
contrainte, et `level_of` lui reserve le barreau le plus haut de la grille de
lecture. Mesure du 2026-08-05 : AUCUN emetteur n'en produisait. `LEVEL_SKILL`
etait donc un barreau mort, `core/skill_steps.py` un module sans appelant, et un
Expected AI Path exigeant un pas de Skill ne pouvait STRUCTURELLEMENT pas passer
-- le cote observe ne pouvait pas en contenir.

CE QUE CES TESTS TIENNENT, et chacun est un refus autant qu'une capacite :

  * la trace, ou rien -- une fenetre temporelle attribuerait l'appel d'un autre
    operateur a ma Skill, et le chemin observe est une PREUVE, pas une mesure ;
  * un pas sans `tool` n'est pas observable, donc n'est pas observe ;
  * deux pas declarant le meme outil n'en designent aucun ;
  * la version epinglee est celle SERVIE, pas celle du jour.
"""

from __future__ import annotations

import pytest
from core import skill_steps

STEPS = [
    {"step": 1, "action": "read", "label": "Read the target", "target": "docs/"},
    {"step": 2, "action": "run", "label": "Read the daily report", "tool": "get_daily_report"},
    {"step": 3, "action": "analyze", "label": "Explain movers", "tool": "get_kpi_movers"},
]

TRACE = "a" * 32


@pytest.fixture(autouse=True)
def _clean():
    skill_steps.forget_served()
    yield
    skill_steps.forget_served()


# --- la reference de version -----------------------------------------------


def test_the_version_reference_carries_both_halves_of_the_primary_key() -> None:
    """`procedures_versions` n'a AUCUN identifiant de substitution : la reference
    doit porter la procedure ET le numero, sinon elle ne designe rien."""
    reference = skill_steps.version_reference("proc_01", 7)
    assert reference == "proc_01@7"
    assert skill_steps.parse_version_reference(reference) == ("proc_01", 7)


@pytest.mark.parametrize("bad", ["", None, "proc_01", "proc_01@", "@7", "proc_01@v7"])
def test_a_reference_that_designates_nothing_is_refused(bad) -> None:
    assert skill_steps.parse_version_reference(bad) is None


# --- ce qui est observe, et ce qui ne l'est pas -----------------------------


def test_a_declared_tool_called_in_the_same_trace_is_the_step() -> None:
    assert skill_steps.remember_served(
        TRACE, procedure_id="proc_01", version_number=7, steps=STEPS, project_id="p1"
    )
    assert skill_steps.observed_step(TRACE, "get_daily_report", project_id="p1") == (
        "proc_01@7", "2",
    )
    assert skill_steps.observed_step(TRACE, "get_kpi_movers", project_id="p1") == (
        "proc_01@7", "3",
    )
    # Le meme appel, un autre projet : rien. Le sort d'une observation douteuse
    # est de ne pas exister.
    assert skill_steps.observed_step(TRACE, "get_daily_report", project_id="p2") is None


def test_a_tool_the_skill_never_declared_is_not_a_step() -> None:
    skill_steps.remember_served(TRACE, procedure_id="proc_01", version_number=7, steps=STEPS)
    assert skill_steps.observed_step(TRACE, "get_card") is None


def test_without_a_trace_nothing_is_observed() -> None:
    """La fenetre temporelle suffit a `adherence` (une MESURE non bloquante) et
    PAS ici : attribuer l'appel d'un autre operateur a ma Skill serait un faux
    dans la preuve que le Test lit."""
    assert skill_steps.remember_served(
        None, procedure_id="proc_01", version_number=7, steps=STEPS
    ) is False
    skill_steps.remember_served(TRACE, procedure_id="proc_01", version_number=7, steps=STEPS)
    assert skill_steps.observed_step(None, "get_daily_report") is None
    assert skill_steps.observed_step("b" * 32, "get_daily_report") is None


def test_a_step_with_no_tool_is_not_observable_and_is_not_observed() -> None:
    """Le pas 1 agit sur une CIBLE : le serveur ne le voit pas passer, donc il
    n'en dit rien plutot que d'en dire quelque chose."""
    skill_steps.remember_served(TRACE, procedure_id="proc_01", version_number=7, steps=STEPS)
    assert skill_steps.observed_step(TRACE, "docs/") is None
    only_targets = [STEPS[0]]
    assert skill_steps.remember_served(
        "c" * 32, procedure_id="proc_02", version_number=1, steps=only_targets
    ) is False


def test_two_steps_declaring_the_same_tool_designate_neither() -> None:
    """Choisir le premier serait un tirage au sort presente comme une observation."""
    ambiguous = [
        {"step": 1, "action": "run", "label": "first", "tool": "get_daily_report"},
        {"step": 2, "action": "run", "label": "second", "tool": "get_daily_report"},
    ]
    skill_steps.remember_served(TRACE, procedure_id="proc_01", version_number=7, steps=ambiguous)
    assert skill_steps.observed_step(TRACE, "get_daily_report") is None


def test_the_pin_is_the_version_that_was_served() -> None:
    """Une Skill qui avance ensuite ne re-etiquette pas une execution deja faite
    -- la meme regle que le `policy_snapshot` de la migration 150."""
    skill_steps.remember_served(TRACE, procedure_id="proc_01", version_number=7, steps=STEPS)
    served_at_7 = skill_steps.observed_step(TRACE, "get_daily_report")
    skill_steps.remember_served("d" * 32, procedure_id="proc_01", version_number=8, steps=STEPS)
    assert served_at_7 == ("proc_01@7", "2")
    assert skill_steps.observed_step(TRACE, "get_daily_report") == ("proc_01@7", "2")


# --- les deux pertes silencieuses que la reparation avait creees ------------
#
# Relecture adversariale du 2026-08-05, defauts #3 et #4 de la story 45.7 :
# l'accumulation posee pour reparer l'ecrasement en a introduit deux autres, et
# aucune n'etait couverte. Une perte silencieuse est pire qu'un refus : le refus
# se lit dans la trace, la perte ne se lit nulle part.


def test_the_same_skill_served_twice_in_a_trace_still_designates_its_step() -> None:
    """DEFAUT #3. Un agent qui relit sa propre Skill -- le cas courant --
    empilait le meme couple deux fois, `len(candidates) != 1` devenait vrai, et
    l'observation disparaissait sans un mot. Deux fois le meme pas n'est pas une
    ambiguite : c'est le meme pas."""
    for _ in range(3):
        skill_steps.remember_served(
            TRACE, procedure_id="proc_01", version_number=7, steps=STEPS,
            project_id="p1",
        )
    assert skill_steps.observed_step(TRACE, "get_daily_report", project_id="p1") == (
        "proc_01@7", "2",
    )
    # Et l'ambiguite REELLE tient toujours : une AUTRE Skill declarant le meme
    # outil n'en designe aucune.
    skill_steps.remember_served(
        TRACE, procedure_id="proc_02", version_number=1,
        steps=[{"step": 1, "action": "run", "label": "x", "tool": "get_daily_report"}],
        project_id="p1",
    )
    assert skill_steps.observed_step(TRACE, "get_daily_report", project_id="p1") is None


def test_a_trace_that_crosses_two_projects_keeps_both_memories() -> None:
    """DEFAUT #4. L'entree etait REMPLACEE des que le projet changeait : servir
    A dans p1 puis B dans p2 dans la meme trace effacait p1, et
    `observed_step(T, 'get_daily_report', 'p1')` rendait `None`. Une perte, non
    declaree et non testee."""
    skill_steps.remember_served(
        TRACE, procedure_id="proc_01", version_number=7, steps=STEPS, project_id="p1"
    )
    skill_steps.remember_served(
        TRACE, procedure_id="proc_02", version_number=2,
        steps=[{"step": 1, "action": "run", "label": "b", "tool": "get_kpi_movers"}],
        project_id="p2",
    )
    assert skill_steps.observed_step(TRACE, "get_daily_report", project_id="p1") == (
        "proc_01@7", "2",
    )
    assert skill_steps.observed_step(TRACE, "get_kpi_movers", project_id="p2") == (
        "proc_02@2", "1",
    )
    # Et l'etancheite entre projets tient : p2 n'a jamais servi cette Skill-la.
    assert skill_steps.observed_step(TRACE, "get_daily_report", project_id="p2") is None


def test_the_memory_is_bounded_in_time_and_in_number(monkeypatch) -> None:
    """Une memoire de session sans borne est une fuite : elle est bornee par les
    deux, et le test le prouve plutot que le commentaire."""
    monkeypatch.setattr(skill_steps, "_SERVED_MAX", 3)
    for index in range(10):
        skill_steps.remember_served(
            f"{index:032d}", procedure_id="proc_01", version_number=1, steps=STEPS
        )
    assert len(skill_steps._served) <= 3

    skill_steps.forget_served()
    skill_steps.remember_served(TRACE, procedure_id="proc_01", version_number=7, steps=STEPS)
    monkeypatch.setattr(skill_steps, "_SERVED_TTL_SECONDS", -1)
    assert skill_steps.observed_step(TRACE, "get_daily_report") is None
