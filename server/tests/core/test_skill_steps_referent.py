"""Le referent d'un pas de Skill -- et les trois refus qu'il ne confond pas.

POURQUOI. `app.ai_path_steps` epingle `skill_version_id` et `skill_step_id`
ensemble par contrainte, declare `skill_step` comme genre de pas de premiere
classe... et AUCUNE table de pas n'existait. Le referent etait reserve et vide :
une trace pouvait declarer « j'ai execute le pas X de la version Y » sans que
rien ne dise ce qu'etait le pas X.

CE QUE CES PORTES TIENNENT :

  * le referent est le couple (version, numero) -- pas une table. Une Skill
    porte deja sa sequence, `procedures_versions` en garde une copie immuable,
    et dupliquer aurait produit deux copies qui divergent ;
  * TROIS refus distincts. Version inconnue, sequence absente, pas inexistant --
    les fondre ferait lire « ce pas n'existe pas » la ou la Skill n'a jamais eu
    de sequence, et on chercherait le defaut au mauvais endroit.
"""

from __future__ import annotations

from core.skill_steps import (
    NO_SEQUENCE,
    UNKNOWN_STEP,
    UNKNOWN_VERSION,
    describes_a_step,
    resolve,
    step_reference,
)

_WITH_STEPS = (
    'name: demo\ndescription: "d"\n'
    "steps:\n"
    "  - step: 1\n    action: read\n    label: Read the spec\n    target: docs/a.md\n"
    "  - step: 2\n    action: run\n    label: Run it\n    command: make test\n"
)


class _Conn:
    """Rend la version demandee, ou rien. Ecrit a la main plutot que moque :
    la porte doit prouver la resolution, pas qu'un mock a ete appele."""

    def __init__(self, rows: dict[tuple[str, int], tuple[str, str]]) -> None:
        self._rows = rows
        self._answer: tuple | None = None

    def cursor(self) -> "_Conn":
        return self

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _sql: str, params: tuple) -> None:
        self._answer = self._rows.get((params[0], params[1]))

    def fetchone(self) -> tuple | None:
        return self._answer


def _conn(frontmatter: str = _WITH_STEPS) -> _Conn:
    return _Conn({("proc_1", 3): (frontmatter, "demo")})


def test_a_step_reference_is_the_number_as_text() -> None:
    """Lui donner une FORME est ce qui le rend resolvable ; libre, il ne serait
    qu'un commentaire."""
    assert step_reference(2) == "2"
    assert step_reference(" 2 ") == "2"


def test_the_couple_version_and_number_resolves_to_the_step() -> None:
    out = resolve(_conn(), procedure_id="proc_1", version_number=3, skill_step_id="2")
    assert out["found"] is True
    assert out["skill_name"] == "demo"
    assert out["step"] == {"step": 2, "action": "run", "label": "Run it",
                           "command": "make test"}


def test_an_unknown_version_is_not_a_missing_step() -> None:
    out = resolve(_conn(), procedure_id="proc_1", version_number=99, skill_step_id="1")
    assert out == {"found": False, "reason": UNKNOWN_VERSION}


def test_a_skill_without_a_sequence_is_not_a_missing_step() -> None:
    out = resolve(_conn('name: demo\ndescription: "d"\n'),
                  procedure_id="proc_1", version_number=3, skill_step_id="1")
    assert out["reason"] == NO_SEQUENCE
    assert out["skill_name"] == "demo"


def test_an_old_version_the_validator_can_no_longer_read_is_a_missing_sequence() -> None:
    """Une version ANCIENNE peut ne plus satisfaire le validateur d'aujourd'hui.
    C'est une sequence qu'on ne sait pas lire, pas un pas absent."""
    out = resolve(_conn("name: [unclosed"), procedure_id="proc_1",
                  version_number=3, skill_step_id="1")
    assert out["reason"] == NO_SEQUENCE


def test_a_step_that_does_not_exist_says_which_ones_do() -> None:
    out = resolve(_conn(), procedure_id="proc_1", version_number=3, skill_step_id="9")
    assert out["reason"] == UNKNOWN_STEP
    assert out["declared_steps"] == ["1", "2"]


def test_the_guard_refuses_a_trace_that_pins_a_step_that_never_existed() -> None:
    """Sans ce controle, un chemin observe peut pretendre avoir suivi une
    sequence qu'il n'a pas suivie -- et la comparaison attendu/observe devient
    muette au moment ou elle compte."""
    assert describes_a_step(_conn(), procedure_id="proc_1", version_number=3,
                            skill_step_id="1") is True
    assert describes_a_step(_conn(), procedure_id="proc_1", version_number=3,
                            skill_step_id="42") is False


# ---------------------------------------------------------------------------
# Le LECTEUR cote surfaces de chemin -- Story 45.7, AC1 et AC2
# ---------------------------------------------------------------------------
#
# REJECT du 2026-08-05 :
#     cd server && grep -rn "skill_steps" --include=*.py . | grep -v tests/ \
#       | grep -v core/skill_steps.py
#       -> 4 resultats, AUCUN n'est `resolve`
# `ai_paths_api` rendait le `skill_version_id` brut et
# `trace_observation.context_skills_lens` le couple brut : ni intitule, ni
# action, a aucune des deux versions. Le referent etait ecrit, teste, et servi
# a personne -- AC1 et AC2 n'avaient pas de code.


def _versions_conn(rows: dict[tuple[str, int], tuple[str, str]]) -> _Conn:
    return _Conn(rows)


def test_a_pinned_step_resolves_to_what_that_version_declared() -> None:
    """AC1 : le pas resout vers l'intitule et l'action que la version SERVIE
    declarait."""
    from core.skill_steps import PIN_RESOLVED, resolve_pin

    out = resolve_pin(_conn(), skill_version_id="proc_1@3", skill_step_id="2")
    assert out["state"] == PIN_RESOLVED
    assert out["skill_name"] == "demo"
    assert out["action"] == "run"
    assert out["label"] == "Run it"
    assert out["command"] == "make test"
    assert out["skill_version_id"] == "proc_1@3"


def test_the_pin_resolves_against_the_version_served_after_the_skill_moves() -> None:
    """AC2 : la Skill avance en version 4 avec une AUTRE sequence ; le chemin
    deja enregistre continue de resoudre contre la 3."""
    from core.skill_steps import resolve_pin

    moved = _versions_conn({
        ("proc_1", 3): (_WITH_STEPS, "demo"),
        ("proc_1", 4): (
            'name: demo\ndescription: "d"\n'
            "steps:\n  - step: 1\n    action: analyze\n    label: Something else\n"
            "    target: docs/b.md\n",
            "demo",
        ),
    })
    served = resolve_pin(moved, skill_version_id="proc_1@3", skill_step_id="2")
    assert served["label"] == "Run it"

    current = resolve_pin(moved, skill_version_id="proc_1@4", skill_step_id="1")
    assert current["label"] == "Something else"


def test_the_three_refusals_travel_to_the_surface_and_stay_distinct() -> None:
    """Les fondre ferait lire << ce pas n'existe pas >> la ou la Skill n'a
    jamais eu de sequence -- et on chercherait le defaut au mauvais endroit."""
    from core.skill_steps import resolve_pin

    assert resolve_pin(_conn(), skill_version_id="proc_1@99",
                       skill_step_id="1")["state"] == UNKNOWN_VERSION
    assert resolve_pin(_conn('name: demo\ndescription: "d"\n'),
                       skill_version_id="proc_1@3",
                       skill_step_id="1")["state"] == NO_SEQUENCE
    assert resolve_pin(_conn(), skill_version_id="proc_1@3",
                       skill_step_id="9")["state"] == UNKNOWN_STEP
    # Une reference qui ne designe aucun couple n'est pas un pas absent.
    assert resolve_pin(_conn(), skill_version_id="not-a-reference",
                       skill_step_id="1")["state"] == UNKNOWN_VERSION


def test_half_a_pin_reads_as_no_pin_and_a_failed_read_says_unavailable() -> None:
    """`unavailable` et pas une sequence vide : << on n'a pas pu lire >> n'est
    pas << il n'y a rien >>. C'est la regle d'AI-158, appliquee au referent."""
    from core.skill_steps import PIN_UNAVAILABLE, resolve_pin

    assert resolve_pin(_conn(), skill_version_id="proc_1@3", skill_step_id=None) is None
    assert resolve_pin(_conn(), skill_version_id=None, skill_step_id="1") is None

    class _Broken:
        def cursor(self):
            raise RuntimeError("the store is down")

    out = resolve_pin(_Broken(), skill_version_id="proc_1@3", skill_step_id="2")
    assert out["state"] == PIN_UNAVAILABLE


def test_the_served_sequence_is_readable_per_trace_with_its_required_flags() -> None:
    """2026-09-05: the policy a path pins and the closure rule read the whole served sequence."""
    from core import skill_steps

    skill_steps.forget_served()
    steps = [
        {"step": 1, "tool": "execute_analyze_query_spec", "required": True},
        {"step": 2, "tool": "analyze_result", "required": True},
        {"step": 3, "target": "the publications feed"},
    ]
    assert skill_steps.remember_served("t" * 32, procedure_id="proc_x", version_number=2, steps=steps, project_id="p1")
    served = skill_steps.served_sequences("t" * 32, project_id="p1")
    assert served == {
        "proc_x@2": [
            {"step": "1", "tool": "execute_analyze_query_spec", "required": True},
            {"step": "2", "tool": "analyze_result", "required": True},
            {"step": "3", "tool": None, "required": False},
        ]
    }
    assert skill_steps.served_sequences("t" * 32, project_id="p2") == {}
    assert skill_steps.served_sequences(None, project_id="p1") == {}
    skill_steps.forget_served()
