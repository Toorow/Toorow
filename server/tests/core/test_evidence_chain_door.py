# -*- coding: utf-8 -*-
"""La porte MCP de PREUVE -- re-parcourir la chaine dont un chiffre vient.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST CELA. C'est le verdict le plus net
des dix que l'audit de gap du 2026-09-05 a mesures, a la lettre : *« walk the
chain back -- lineage, provenance, versions, approvals, audited activity. No tool
walks lineage, versions or the audit log. `partial` -- MCP plane missing: **a
model cannot re-walk the chain it is asked to trust.** »* Le produit promet qu'un
modele compose une figure gouvernee et sait dire d'ou elle vient ; il pouvait la
composer et pas la verifier.

La porte est batie sur UNE promesse : **elle compose par les MEMES fonctions que
l'ecran Evidence** -- `compose_governance_collection` et
`compose_governance_object`, avec `section="evidence"`. Une seconde derivation
laisserait un modele et une personne ne pas etre d'accord sur la provenance de la
MEME figure, ce qui est exactement le defaut que cette surface existe pour
retirer. Une doublure le mesure au lieu de le lire.

Et sur une seconde promesse, qu'un test doit tenir parce qu'aucune revue ne la
verra : **parcourir une chaine ne peut pas la changer**. La porte est en lecture
entiere ; le test lit la source et refuse tout appel d'ecriture.

AUCUN POSTGRES : le CHAINAGE est sous test, pas la lecture -- le composeur a ses
propres tests. La garde de portee a son harnais a elle.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

IDENTITY = "person_tester"
PROJECT = "proj_EXAMPLE"
ORG = "org_EXAMPLE"


def _tool():
    import core.evidence_chain_mcp as door
    import core.mcp_profiles as profiles

    captured: dict[str, object] = {}

    def _record(_mcp, handler, **_kwargs):
        captured[handler.__name__] = handler
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        door.register(object())
    finally:
        profiles.register_profiled = original
    return captured["walk_evidence_chain"]


def _declaration() -> dict:
    import core.evidence_chain_mcp as door
    import core.mcp_profiles as profiles

    seen: dict[str, dict] = {}

    def _record(_mcp, handler, **kwargs):
        seen[handler.__name__] = kwargs
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        door.register(object())
    finally:
        profiles.register_profiled = original
    return seen["walk_evidence_chain"]


@pytest.fixture()
def a_reader(monkeypatch):
    import core.db as core_db
    import core.evidence_chain_mcp as door
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)
    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(door, "_org_of", lambda _conn, _project: ORG)

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


def _payload(result) -> dict:
    return result.structured_content["data"]


def _refusal(excinfo) -> dict:
    return json.loads(str(excinfo.value))


# --------------------------------------------------------------------------- #
# 1. LA promesse : le composeur de l'ecran, avec ses arguments.
# --------------------------------------------------------------------------- #


def test_the_lens_is_composed_by_the_screens_own_function(a_reader, monkeypatch):
    import core.governance_read_model as model

    seen: dict = {}

    def _compose(project_id, section, conn, *, lens, org_id, query=None):
        seen.update(
            {
                "project_id": project_id,
                "section": section,
                "lens": lens,
                "org_id": org_id,
                "query": query,
                "conn": conn,
            }
        )
        return {"items": []}

    monkeypatch.setattr(model, "compose_governance_collection", _compose)
    _tool()(project_id=PROJECT, lens="versions-approvals")

    assert seen["section"] == "evidence"
    assert seen["lens"] == "versions-approvals"
    assert seen["org_id"] == ORG
    assert seen["conn"] is not None


def test_the_default_lens_is_the_contracts_own_and_not_a_literal_here(a_reader, monkeypatch):
    """Le defaut vient du contrat de la section, jamais d'une chaine ecrite ici.

    Une copie vieillit ; le jour ou la section change de lentille par defaut,
    cette porte ouvrirait la mauvaise et un lien partage mentirait.
    """
    import core.governance_read_model as model

    seen: dict = {}
    monkeypatch.setattr(
        model,
        "compose_governance_collection",
        lambda *a, **k: seen.update(k) or {"items": []},
    )
    _tool()(project_id=PROJECT)
    assert seen["lens"] == model.resolve_section("evidence").default_lens

    source = Path(__import__("core.evidence_chain_mcp", fromlist=["x"]).__file__)
    text = source.read_text(encoding="utf-8")
    for lens in model.resolve_section("evidence").lenses:
        assert f'"{lens}"' not in text, (
            f"la lentille `{lens}` est recopiee dans la porte : le contrat de la "
            "section cesserait d'etre la seule reponse"
        )


def test_naming_an_object_opens_it_by_exact_identity(a_reader, monkeypatch):
    """Le 201e Enregistrement de preuve s'ouvre encore : la borne est d'AFFICHAGE."""
    import core.governance_read_model as model

    seen: dict = {}

    def _object(project_id, section, object_type, object_id, conn, *, org_id):
        seen.update({"section": section, "object_type": object_type, "object_id": object_id})
        return {"overview": {"id": object_id}}

    monkeypatch.setattr(model, "compose_governance_object", _object)
    monkeypatch.setattr(
        model,
        "compose_governance_collection",
        lambda *a, **k: pytest.fail("naming an object must not scan the lens"),
    )
    result = _tool()(
        project_id=PROJECT, object_type="object-version", object_id="ov_1"
    )
    assert seen["section"] == "evidence"
    assert seen["object_type"] == "object-version"
    assert _payload(result)["object_id"] == "ov_1"


def test_the_cursor_and_filters_reach_the_composer_that_owns_them(a_reader, monkeypatch):
    """Un filtre vise une lentille qui pagine, ou il est REFUSE par le composeur.

    Cette porte ne decide pas laquelle : elle transmet, et le composeur refuse.
    Un filtre ignore rend une page qui ne correspond pas a l'adresse qui l'a
    produite.
    """
    import core.governance_read_model as model

    seen: dict = {}
    monkeypatch.setattr(
        model,
        "compose_governance_collection",
        lambda *a, **k: seen.update(k) or {"items": []},
    )
    _tool()(project_id=PROJECT, cursor="c-2", filters={"actor": "someone@example.com"})
    assert seen["query"] == {"actor": "someone@example.com", "cursor": "c-2"}


# --------------------------------------------------------------------------- #
# 2. Lecture entiere -- parcourir une chaine ne peut pas la changer.
# --------------------------------------------------------------------------- #


def test_the_door_calls_no_writer_at_all():
    """La seconde promesse, et aucune revue ne la verra : rien ici n'ecrit.

    Ni une approbation, ni une retraite, ni une annotation, ni l'enregistrement
    d'une inspection -- `app_record_evidence_inspection` reste app-only, parce
    que dire qu'une inspection a eu lieu est le geste de l'app au moment ou une
    personne regarde, pas celui d'un modele.
    """
    import core.evidence_chain_mcp as door

    text = Path(door.__file__).read_text(encoding="utf-8")
    # Un COMMIT est une chaine complete, pas un nom a appeler : lui ajouter une
    # parenthese le rendait introuvable, et le mutant qui en glissait un passait.
    # Mesure 2026-09-05, sur ce test meme.
    assert "conn.commit" not in text, "un commit dans une porte de lecture entiere"
    for writer in (
        "app_record_evidence_inspection",
        "approve_baseline",
        "publish_semantic_model_change",
        "insert_audit_row",
        "append_review_version",
    ):
        assert f"{writer}(" not in text, (
            f"`{writer}` apparait dans une porte qui doit etre en lecture entiere"
        )


def test_an_empty_lens_says_why_in_the_composers_own_words(a_reader, monkeypatch):
    import core.governance_read_model as model

    monkeypatch.setattr(
        model,
        "compose_governance_collection",
        lambda *a, **k: {
            "items": [],
            "unavailable_reasons": [{"message": "No governed publication yet."}],
        },
    )
    text = _tool()(project_id=PROJECT).content[0].text
    assert "No governed publication yet." in text


def test_an_empty_lens_without_a_reason_still_names_the_gesture(a_reader, monkeypatch):
    """Une liste vide dit pourquoi et nomme le geste -- jamais une table."""
    import core.governance_read_model as model

    monkeypatch.setattr(
        model, "compose_governance_collection", lambda *a, **k: {"items": []}
    )
    text = _tool()(project_id=PROJECT).content[0].text
    assert "published, approved or acted on" in text
    assert "app." not in text


# --------------------------------------------------------------------------- #
# 3. Les refus de forme et le rang declare.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"project_id": ""}, "missing_param"),
        ({"project_id": PROJECT, "lens": "everything"}, "unknown_lens"),
        # Un seul des deux : la porte ne devine pas l'autre.
        ({"project_id": PROJECT, "object_type": "object-version"}, "missing_param"),
        ({"project_id": PROJECT, "object_id": "ov_1"}, "missing_param"),
        (
            {"project_id": PROJECT, "object_type": "semantic-view", "object_id": "sv_1"},
            "unknown_object_type",
        ),
        ({"project_id": PROJECT, "filters": "actor=me"}, "invalid_param"),
    ],
)
def test_the_door_refuses_the_argument_before_it_opens_anything(monkeypatch, kwargs, code):
    import core.db as core_db
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)

    def _never(_identity):  # pragma: no cover -- il doit ne jamais etre appele
        raise AssertionError("no connection may be opened for a malformed call")

    monkeypatch.setattr(core_db, "request_connection", _never)
    with pytest.raises(ToolError) as excinfo:
        _tool()(**kwargs)
    assert _refusal(excinfo)["code"] == code


def test_the_three_object_types_of_the_contract_are_all_accepted(a_reader, monkeypatch):
    """La porte accepte exactement ce que le contrat declare -- ni plus, ni moins."""
    import core.governance_read_model as model

    monkeypatch.setattr(
        model, "compose_governance_object", lambda *a, **k: {"overview": {}}
    )
    declared = [o.object_type for o in model.resolve_section("evidence").objects]
    assert declared, "la section Evidence ne declare plus aucun type d'objet"
    for object_type in declared:
        result = _tool()(project_id=PROJECT, object_type=object_type, object_id="x_1")
        assert _payload(result)["object_type"] == object_type


def test_the_declaration_is_a_read_at_the_rank_its_neighbours_hold():
    """Le rang suit l'EFFET, pas le sujet.

    Sinon toute lecture d'un objet gouverne monterait en `governance`, et le
    profil qui existe pour les lectures sures n'en tiendrait plus aucune qui
    compte.
    """
    d = _declaration()
    assert (d["profile"], d["effect"], d["confirmation_mode"]) == (
        "insights",
        "read",
        "none",
    )
