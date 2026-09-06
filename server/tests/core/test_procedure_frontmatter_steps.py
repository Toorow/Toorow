"""Une Skill porte sa sequence, ses criteres d'acceptation et ses erreurs courantes.

POURQUOI. Jean, 2026-08-03 : « moi j'attends une sequence dans le skill ; quels
sont les criteres d'acceptation, si clairement formules ; quels sont les erreurs
courantes -- pour que ca soit lisible dans une UI. » Puis : « te manque pas le
target ou l'outil dans ton step pour que ca soit ultra clair ? »

DEUX NIVEAUX, ET C'EST LA DISTINCTION QUI COMPTE :

    niveau SKILL   `acceptance`, `common_errors` -- communs a toute la procedure
    niveau PAS     `steps` -- ce qu'on fait, dans l'ordre

Les trois cles etaient deja ECRIVABLES et ne valaient rien :
`validate_procedure_frontmatter` rendait `dict(data)`, donc toute cle inconnue
etait preservee SANS forme garantie. Une interface ne peut pas rendre ce qu'elle
ne peut pas croire.

CE QUE CES PORTES TIENNENT, chacune venue d'une faute mesuree le meme jour :

  * l'ORDRE et l'UNICITE d'un pas -- sans quoi une sequence n'en est pas une ;
  * le vocabulaire FERME des actions -- il decide des icones de la console, donc
    une septieme valeur inventee casserait le rendu sans rien signaler ;
  * CE SUR QUOI LE PAS AGIT -- cible, commande ou outil, au moins un. Un pas qui
    dit ce qu'il fait sans dire sur quoi est le pas vague qu'on veut eliminer ;
  * la branche d'echec `stop_if` -- elle existait dans la recette de tache et
    nulle part dans une Skill ;
  * le lien symptome -> causes -- un seul `SKILL.md` sur cinquante en portait un,
    et en prose.
"""

from __future__ import annotations

import pytest
from core.context_store import STEP_ACTIONS, validate_procedure_frontmatter

BASE = 'name: demo\ndescription: "d"\n'


def _fm(extra: str) -> dict:
    return validate_procedure_frontmatter(BASE + extra)


# --- la sequence, au niveau du PAS ----------------------------------------


def test_a_step_says_what_it_does_and_what_it_acts_on() -> None:
    parsed = _fm(
        "steps:\n"
        "  - step: 1\n"
        "    action: read\n"
        "    label: Read the SPEC document\n"
        "    target: docs/product-architecture/data.md\n"
        '    stop_if: "NO RATIFIED DOCUMENT"\n'
        "  - step: 2\n"
        "    action: run\n"
        "    label: Run the screen test\n"
        "    command: npx vitest run\n"
        "  - step: 3\n"
        "    action: analyze\n"
        "    label: Read the card the host built\n"
        "    tool: get_card\n"
    )
    assert parsed["steps"] == [
        {
            "step": 1,
            "action": "read",
            "label": "Read the SPEC document",
            "target": "docs/product-architecture/data.md",
            "stop_if": "NO RATIFIED DOCUMENT",
        },
        {"step": 2, "action": "run", "label": "Run the screen test",
         "command": "npx vitest run"},
        {"step": 3, "action": "analyze", "label": "Read the card the host built",
         "tool": "get_card"},
    ]


def test_a_step_that_never_says_what_it_acts_on_is_refused() -> None:
    """Une action et un libelle, sans cible ni commande ni outil, forment
    exactement le pas vague qu'une interface ne peut pas afficher."""
    with pytest.raises(ValueError, match="must declare what it acts on"):
        _fm("steps:\n  - step: 1\n    action: read\n    label: Read something\n")


def test_target_command_and_tool_are_three_different_things() -> None:
    """`tool_bindings.tool` melangeait l'outil MCP et la commande sous une seule
    cle -- ce qui obligeait a laisser les commandes en prose. Ici les trois
    coexistent sur le meme pas."""
    step = _fm(
        "steps:\n"
        "  - step: 1\n"
        "    action: edit\n"
        "    label: Fix the screen\n"
        "    target: ui/admin/src/shell/pages/Screen.tsx\n"
        "    command: npx vitest run\n"
        "    tool: get_card\n"
    )["steps"][0]
    assert step["target"] == "ui/admin/src/shell/pages/Screen.tsx"
    assert step["command"] == "npx vitest run"
    assert step["tool"] == "get_card"


def test_the_action_vocabulary_is_closed() -> None:
    """Une septieme valeur casserait le rendu sans que rien ne le signale."""
    assert STEP_ACTIONS == {"read", "run", "edit", "analyze", "suggest", "create-issue"}
    with pytest.raises(ValueError, match="action must be one of"):
        _fm("steps:\n  - step: 1\n    action: deploy\n    label: x\n    target: y\n")


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        (
            "steps:\n"
            "  - step: 2\n    action: read\n    label: a\n    target: x\n"
            "  - step: 1\n    action: read\n    label: b\n    target: y\n",
            "ordered by step",
        ),
        (
            "steps:\n"
            "  - step: 1\n    action: read\n    label: a\n    target: x\n"
            "  - step: 1\n    action: read\n    label: b\n    target: y\n",
            "duplicated",
        ),
        (
            "steps:\n  - step: 0\n    action: read\n    label: a\n    target: x\n",
            "positive integer",
        ),
        (
            "steps:\n  - step: 1\n    action: read\n    label: a\n    target: x\n    retry: yes\n",
            "unsupported keys",
        ),
        (
            "steps:\n  - step: 1\n    action: read\n    target: x\n",
            "label must be a non-empty string",
        ),
    ],
)
def test_a_sequence_that_is_not_one_is_refused(frontmatter: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _fm(frontmatter)


# --- les criteres d'acceptation, au niveau du SKILL ------------------------


def test_acceptance_is_a_deduplicated_list_of_statements() -> None:
    parsed = _fm('acceptance:\n  - "Both RUN numbers appear in the answer"\n')
    assert parsed["acceptance"] == ["Both RUN numbers appear in the answer"]

    with pytest.raises(ValueError, match="duplicate value"):
        _fm('acceptance:\n  - "same"\n  - "same"\n')


# --- les erreurs courantes, au niveau du SKILL ----------------------------


def test_common_errors_bind_a_symptom_to_its_causes() -> None:
    parsed = _fm(
        "common_errors:\n"
        '  - symptom: "Connection refused"\n'
        "    causes:\n"
        '      - "the MCP server is not running"\n'
        '      - "the API key is invalid"\n'
    )
    assert parsed["common_errors"] == [
        {
            "symptom": "Connection refused",
            "causes": ["the MCP server is not running", "the API key is invalid"],
        }
    ]


def test_a_symptom_without_a_cause_is_refused() -> None:
    """Un symptome sans cause est un constat, pas une aide."""
    with pytest.raises(ValueError, match="causes must be a non-empty list"):
        _fm('common_errors:\n  - symptom: "Connection refused"\n    causes: []\n')


# --- le controle negatif ---------------------------------------------------


def test_the_three_keys_stay_optional_and_nothing_existing_breaks() -> None:
    """Toute Skill ecrite avant ce jour doit rester valide -- sinon on a casse la base."""
    parsed = validate_procedure_frontmatter(
        BASE + "tool_bindings:\n  - step: 1\n    tool: get_card\nmdm_tags:\n  - x\n"
    )
    assert "steps" not in parsed
    assert "acceptance" not in parsed
    assert "common_errors" not in parsed
    assert parsed["tool_bindings"] == [{"step": 1, "tool": "get_card"}]


# --- ce que la CONSOLE ecrit, relu par le serveur --------------------------


def test_the_exact_block_the_console_writes_is_accepted(  # AI-210
) -> None:
    """Le frontmatter que l'editeur de la console produit, octet pour octet.

    POURQUOI CE TEST EXISTE. `SkillEditorDrawer` savait rendre une sequence et ne
    savait pas l'ECRIRE : les cinq cles validees ici n'avaient aucun champ de
    saisie. L'editeur les ecrit desormais, en style BLOC et en scalaires
    double-quote (`serializeSkillFrontmatter`, SkillStepList.tsx). Un editeur qui
    produit un YAML que ce validateur refuse rendrait un 422 a chaque
    enregistrement -- et la seule facon de le savoir est de relire ici la forme
    exacte qu'il ecrit, pas une forme equivalente ecrite a la main.

    Le libelle porte deux-points ET guillemets : c'est ce que la forme
    double-quote existe pour survivre.
    """
    console_output = (
        'name: "Local gate"\n'
        'description: "Run the console gate."\n'
        "steps:\n"
        "  - step: 1\n"
        "    action: run\n"
        '    label: "Read the \\"Incomplete if\\" section: every criterion"\n'
        '    command: "npx vitest run"\n'
        '    stop_if: "any test is red"\n'
        "acceptance:\n"
        '  - "The suite exits 0"\n'
        "keywords:\n"
        '  - "console"\n'
        "evidence_requirements:\n"
        '  - "The command and its number"\n'
        "common_errors:\n"
        '  - symptom: "Connection refused"\n'
        "    causes:\n"
        '      - "The MCP server is not running"\n'
    )
    parsed = validate_procedure_frontmatter(console_output)
    assert parsed["steps"] == [
        {
            "step": 1,
            "action": "run",
            "label": 'Read the "Incomplete if" section: every criterion',
            "command": "npx vitest run",
            "stop_if": "any test is red",
        }
    ]
    assert parsed["acceptance"] == ["The suite exits 0"]
    assert parsed["keywords"] == ["console"]
    assert parsed["evidence_requirements"] == ["The command and its number"]
    assert parsed["common_errors"] == [
        {"symptom": "Connection refused", "causes": ["The MCP server is not running"]}
    ]


# --- Story 45.5 : quand la Skill ne doit PAS se declencher ------------------


def test_anti_triggers_are_normalized_terms() -> None:
    parsed = _fm('anti_triggers:\n  - "  facturation  "\n  - "paie"\n')
    assert parsed["anti_triggers"] == ["facturation", "paie"]


def test_the_same_anti_trigger_twice_is_refused_whatever_its_case() -> None:
    """Deux fois le meme terme n'exclut pas deux fois -- c'est une faute de
    saisie, et la laisser passer ferait croire a un second signal."""
    with pytest.raises(ValueError, match="duplicate value"):
        _fm('anti_triggers:\n  - "facturation"\n  - "Facturation"\n')


def test_anti_triggers_stay_optional() -> None:
    """Les 50 Skills ecrites avant cette story restent valides."""
    assert "anti_triggers" not in validate_procedure_frontmatter(BASE)


def test_an_unreadable_frontmatter_declares_no_anti_trigger() -> None:
    """Le classement lit ceci sur CHAQUE candidat : il ne doit jamais lever."""
    from core.context_store import read_anti_triggers

    assert read_anti_triggers('anti_triggers:\n  - "facturation"\n') == ["facturation"]
    assert read_anti_triggers("name: [unclosed") == []
    assert read_anti_triggers("anti_triggers: not-a-list") == []
    assert read_anti_triggers(None) == []
