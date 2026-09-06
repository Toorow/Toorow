"""Ce que l'agent recoit d'une Skill standardisee -- Story 45.6.

POURQUOI CE FICHIER. La vague du 2026-08-05 a donne a une Skill sa sequence, ses
criteres d'acceptation, ses erreurs courantes, ses mots-cles, ses exigences de
preuve et son anti-declencheur. La console les ecrivait et les rendait ; le
lecteur pour qui ils existent -- celui qui APPLIQUE la procedure -- recevait
`frontmatter_yaml` en BLOC DE TEXTE et devait parser du YAML lui-meme.

CE QUE CES TESTS TIENNENT :

  * la structure arrive LUE, par les memes validateurs que l'ecriture ;
  * le YAML brut reste, parce qu'un client qui le lit aujourd'hui ne doit pas
    casser parce qu'une structure est apparue a cote ;
  * une cle ILLISIBLE ne se confond pas avec une cle ABSENTE -- rendre une liste
    vide pour les deux ferait dire au silence ce qu'il ne dit pas ;
  * le canal LLM reste BORNE (AD-1) : il ne grandit pas avec la sequence.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest

STANDARD_YAML = (
    'name: "local-gate"\n'
    'description: "Run the console gate."\n'
    "steps:\n"
    "  - step: 1\n"
    "    action: read\n"
    '    label: "Read the ratified surface"\n'
    '    target: "docs/product-architecture"\n'
    "  - step: 2\n"
    "    action: run\n"
    '    label: "Run the console suite"\n'
    '    command: "npx vitest run"\n'
    '    stop_if: "any test is red"\n'
    "acceptance:\n"
    '  - "The suite exits 0"\n'
    "common_errors:\n"
    '  - symptom: "Connection refused"\n'
    "    causes:\n"
    '      - "The MCP server is not running"\n'
    "keywords:\n"
    '  - "console"\n'
    "anti_triggers:\n"
    '  - "billing"\n'
)


def _procedure(frontmatter: str, *, name: str = "local-gate") -> dict:
    return {
        "id": "proc_01TEST",
        "project_id": None,
        "name": name,
        "description": "Run the console gate.",
        "frontmatter_yaml": frontmatter,
        "body_md": "## Step 1\n\nOpen the surface document.\n",
        "version_number": 7,
    }


def _call(frontmatter: str):
    """Appeler l'outil MCP en processus, sans base et sans reseau."""
    from core import main

    conn = MagicMock()
    with (
        patch("core.main.get_access_token", return_value=None),
        patch("core.main._resolve_project", side_effect=lambda p, identity=None: p or "default"),
        patch("core.db.get_connection", return_value=contextlib.nullcontext(conn)),
        patch(
            "core.context_search.get_procedure_by_name",
            return_value=_procedure(frontmatter),
        ),
        patch("core.mdm_references.resolve", return_value=None),
        patch("core.mdm_references.tags_of", return_value=[]),
        patch("core.context_review.list_open", return_value=[]),
    ):
        result = main.get_procedure(name="local-gate", project_id="p1")
    payload = result.structured_content
    text = result.content[0].text
    return payload, text


def _skill(payload: dict) -> dict:
    data = payload["data"] if "data" in payload else payload
    return data["procedure"]["skill"]


@pytest.fixture
def standard():
    return _call(STANDARD_YAML)


def test_the_sequence_arrives_read_and_in_order(standard) -> None:
    payload, _text = standard
    skill = _skill(payload)
    assert skill["readable"] is True
    assert [step["step"] for step in skill["steps"]] == [1, 2]
    assert skill["steps"][1] == {
        "step": 2,
        "action": "run",
        "label": "Run the console suite",
        "command": "npx vitest run",
        "stop_if": "any test is red",
    }
    assert skill["acceptance"] == ["The suite exits 0"]
    assert skill["common_errors"] == [
        {"symptom": "Connection refused", "causes": ["The MCP server is not running"]}
    ]
    assert skill["keywords"] == ["console"]
    assert skill["anti_triggers"] == ["billing"]


def test_the_raw_yaml_stays(standard) -> None:
    """Un client qui lit le bloc aujourd'hui ne casse pas parce qu'une structure
    est apparue a cote."""
    payload, _text = standard
    data = payload["data"] if "data" in payload else payload
    assert data["procedure"]["frontmatter_yaml"] == STANDARD_YAML


def test_the_llm_channel_names_the_sequence_without_carrying_it(standard) -> None:
    """AD-1 : le canal borne. Un compte et l'existence d'une branche d'arret --
    pas la sequence en prose."""
    _payload, text = standard
    assert "2-step sequence" in text
    assert "1 stop condition" in text
    assert "declares 1 anti-trigger(s)" in text
    # Le contenu des pas ne monte PAS sur ce canal.
    assert "npx vitest run" not in text
    assert "docs/product-architecture" not in text


def test_the_channel_does_not_grow_with_the_sequence() -> None:
    """La borne se PROUVE, et sur deux entrees qui ne different QUE par le
    nombre de pas -- sinon le test compare deux autres choses et passe pour de
    mauvaises raisons."""

    def _with(count: int) -> str:
        return 'name: "s"\ndescription: "d"\nsteps:\n' + "".join(
            f'  - step: {i}\n    action: read\n'
            f'    label: "a step whose label is long enough to be noticed {i}"\n'
            f'    target: "docs/product-architecture"\n'
            for i in range(1, count + 1)
        )

    _p2, two = _call(_with(2))
    _p40, forty = _call(_with(40))
    # Un seul caractere d'ecart : « 40-step » au lieu de « 2-step ».
    assert len(forty) - len(two) == 1
    assert "2-step sequence" in two
    assert "40-step sequence" in forty


def test_a_legacy_skill_gets_empty_lists_and_nothing_invented() -> None:
    """Les Skills ecrites avant cette vague rendent ce qu'elles rendaient."""
    payload, text = _call('name: "old"\ndescription: "d"\n')
    skill = _skill(payload)
    assert skill["readable"] is True
    assert skill["steps"] == []
    assert skill["anti_triggers"] == []
    assert "sequence" not in text


def test_an_unreadable_frontmatter_is_not_an_absent_one() -> None:
    """« on n'a pas pu la lire » ne se dit pas « elle n'a pas de sequence »."""
    payload, text = _call("name: [unclosed\n")
    skill = _skill(payload)
    assert skill["readable"] is False
    assert skill["steps"] == []
    assert "could not be read" in text


def test_one_malformed_key_does_not_hide_the_others() -> None:
    """Une cle refusee est NOMMEE, et les cles valides arrivent quand meme."""
    payload, text = _call(
        'name: "s"\ndescription: "d"\n'
        "steps:\n"
        "  - step: 1\n"
        "    action: teleport\n"        # action hors du vocabulaire ferme
        '    label: "x"\n'
        '    target: "t"\n'
        "keywords:\n"
        '  - "console"\n'
    )
    skill = _skill(payload)
    assert skill["readable"] is True
    assert skill["unreadable_keys"] == ["steps"]
    assert skill["steps"] == []
    assert skill["keywords"] == ["console"]
    assert "unreadable: steps" in text


def test_the_payload_is_json_serialisable(standard) -> None:
    """Il part sur `structuredContent` : ce qui ne se serialise pas n'arrive pas."""
    payload, _text = standard
    json.dumps(payload)


# --- sorties de relecture adversariale du 2026-08-05 -----------------------


def test_a_non_string_yaml_key_does_not_take_the_whole_skill_down() -> None:
    """REJECT du 2026-08-05, et le lecteur promettait le contraire.

    Une cle YAML peut etre un ENTIER (`2: oops`). Les validateurs joignaient les
    cles inconnues sans les convertir -> `TypeError`, que le lecteur n'attrapait
    pas (il ne prenait que `ValueError`) et qui remontait jusqu'a l'outil : la
    Skill ENTIERE disparaissait, corps et YAML brut compris. Exactement le
    contraire du mode de defaillance que cette story existe pour empecher.
    """
    payload, text = _call(
        'name: "s"\ndescription: "d"\n'
        "steps:\n"
        "  - step: 1\n"
        "    action: read\n"
        '    label: "x"\n'
        '    target: "t"\n'
        "    2: oops\n"
    )
    skill = _skill(payload)
    assert skill["unreadable_keys"] == ["steps"]
    assert "unreadable: steps" in text
    # Et ce qui n'a rien a voir avec la cle fautive arrive quand meme.
    data = payload["data"] if "data" in payload else payload
    assert data["procedure"]["body_md"] != ""
    assert "2: oops" in data["procedure"]["frontmatter_yaml"]


def test_the_same_shape_is_a_readable_refusal_on_the_write_path() -> None:
    """Le meme defaut rendait un 500 a l'enregistrement au lieu d'un 422."""
    import pytest as _pytest
    from core.context_store import validate_procedure_frontmatter

    with _pytest.raises(ValueError, match="unsupported keys"):
        validate_procedure_frontmatter(
            'name: "s"\ndescription: "d"\n'
            "steps:\n  - step: 1\n    action: read\n    label: \"x\"\n"
            '    target: "t"\n    2: oops\n'
        )


@pytest.mark.parametrize(
    ("label", "frontmatter"),
    [
        (
            # LE SITE DE PREMIER NIVEAU -- `context_store.py:357`.
            "top-level key",
            'name: "s"\ndescription: "d"\n2: oops\n',
        ),
        (
            # LE SITE `tool_bindings[i]` -- `context_store.py:389`.
            "tool_bindings key",
            'name: "s"\ndescription: "d"\n'
            "tool_bindings:\n  - step: 1\n    tool: t\n    3: oops\n",
        ),
        (
            # Le site DEJA repare le 2026-08-05, garde en regression.
            "nested steps key",
            'name: "s"\ndescription: "d"\n'
            'steps:\n  - step: 1\n    action: read\n    label: "x"\n'
            '    target: "t"\n    2: oops\n',
        ),
    ],
)
def test_every_unsupported_key_refusal_survives_a_non_string_yaml_key(
    label, frontmatter
) -> None:
    """REJECT 45.6 du 2026-08-05 : la reparation << aux deux endroits >> en
    couvrait UN.

        cd server && python -c "from core.context_store import
          validate_procedure_frontmatter as v; v('name: \"s\"\\n
          description: \"d\"\\n2: oops\\n')"
        # TypeError: sequence item 0: expected str instance, int found

    Un `TypeError` n'est pas un `ValueError` : `context_api.py:653-659` rend
    alors 500 `db_error` la ou le contrat promet 422 `invalid_frontmatter`.
    Un code d'erreur promis puis non rendu est un contrat rompu, pas un detail.

    La classe est PARAMETREE plutot qu'illustree : le test qui existait ne
    couvrait que la cle imbriquee, donc il etait vert sur un defaut vivant.
    """
    from core.context_store import validate_procedure_frontmatter

    with pytest.raises(ValueError, match="unsupported keys"):
        validate_procedure_frontmatter(frontmatter)


def test_the_refusal_the_contract_promises_is_the_one_the_route_returns() -> None:
    """Le type d'exception EST le code HTTP -- `context_api.py:653-659` :
    `ValueError` -> 422 `invalid_frontmatter`, tout le reste -> 500 `db_error`.
    Sans cette lecture, << ValueError >> reste une convention orale.
    """
    import inspect

    from core import context_api

    source = inspect.getsource(context_api._create_procedure)
    frontmatter_branch = source.index('{"code": "invalid_frontmatter"')
    handler = source.rindex("except ", 0, frontmatter_branch)
    assert source[handler:frontmatter_branch].startswith("except ValueError as exc:")
    assert "status_code=422" in source[frontmatter_branch:frontmatter_branch + 160]
    # Et tout ce qui n'est pas un `ValueError` tombe dans le 500 : c'est ce que
    # les deux `join` non convertis faisaient de la refus de cle inconnue.
    assert "db_error" in source[source.index("except Exception as exc:"):]


def test_an_empty_frontmatter_is_read_and_carries_nothing() -> None:
    """REJECT du 2026-08-05 : un commentaire seul se LIT parfaitement et ne
    porte rien. Le dire « illisible » etait le mensonge de la story, retourne."""
    for text_in in ("# a hand-written comment\n", "   \n"):
        payload, summary = _call(text_in)
        skill = _skill(payload)
        assert skill["readable"] is True, text_in
        assert skill["steps"] == []
        assert "could not be read" not in summary


def test_the_raw_yaml_and_the_body_survive_an_unreadable_frontmatter() -> None:
    """La matrice l'exige, et rien ne l'exigeait dans le test."""
    payload, _text = _call("name: [unclosed\n")
    data = payload["data"] if "data" in payload else payload
    assert data["procedure"]["frontmatter_yaml"] == "name: [unclosed\n"
    assert data["procedure"]["body_md"] != ""


def test_the_previous_walk_is_served_with_the_skill_in_one_sentence() -> None:
    """2026-09-05: the next walk starts from what the previous one skipped."""
    walks = [
        {"path_id": "aip_last", "started_at": "2026-09-05T13:10:01+00:00", "verdict": "fail",
         "skill_version": "proc_01TEST@7", "crossed": ["1", "3"], "skipped": ["2"],
         "skipped_labels": ["Read the Result and its evidence before saying anything about it."]},
    ]
    with (
        patch("core.db.request_connection", return_value=contextlib.nullcontext(MagicMock())),
        patch("core.ai_paths.previous_walks", return_value=walks),
    ):
        payload, text = _call(STANDARD_YAML)
    data = payload["data"] if "data" in payload else payload
    assert data["previous_walks"] == walks
    assert "last walk here" in text and "verdict fail" in text and "skipped 2" in text
    assert "Read the Result and its evidence" in text


def test_a_skill_without_a_previous_walk_is_served_exactly_as_before() -> None:
    with (
        patch("core.db.request_connection", return_value=contextlib.nullcontext(MagicMock())),
        patch("core.ai_paths.previous_walks", return_value=[]),
    ):
        payload, text = _call(STANDARD_YAML)
    data = payload["data"] if "data" in payload else payload
    assert data["previous_walks"] == []
    assert "last walk" not in text
