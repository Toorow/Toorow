"""Story 53.4 -- une preuve resout dans la donnee du serveur, ou la publication refuse.

CE QUI A CHANGE, ET POURQUOI LES DEUX MOITIES SONT INSEPARABLES.
`core.main._resolve_evidence_refs` rendait `set()` inconditionnellement. Tant que
c'etait vrai, `evidenceRefs` DEVAIT rester optionnel : le rendre obligatoire
aurait refuse toute publication -- sans ref `evidence_missing`, avec un ref
`evidence_unresolved` -- c'est-a-dire eteint une fonction livree au lieu de la
reparer. Le resolveur et l'obligation atterrissent donc ensemble, et ce fichier
prouve les DEUX SENS : ce qui cite passe, ce qui ne cite pas ne passe pas.

Un test qui ne montrerait que le refus serait indistinguable d'une panne.

TROIS DEFAUTS FERMES APRES COUP, chacun avec sa garde ici :

1. L'obligation etait satisfiable par TAUTOLOGIE. `card:<gabarit>` etait un genre
   de preuve, et la gate 4 exige deja que `card.template` soit dans
   `available_templates` : un insight qui ne citait que le gabarit qu'il rend
   passait sans pointer aucune donnee.
2. La moitie `card:` de l'univers ECHOUAIT OUVERT. Elle venait de
   `_project_topic_catalog`, qui sert les defauts PLATEFORME sur refus d'acces,
   projet irresoluble ou store illisible -- pendant que metriques et dimensions
   tombaient a vide. Deux tiers fermaient, un tiers ouvrait, sous un commentaire
   qui jurait les trois fermes.
3. La provenance n'existait que dans `preview()`, la seule surface que personne
   ne lit. Le payload publie et la route Console la portent maintenant.
"""

from __future__ import annotations

import pytest
from core.daily_insights_schema import authorship_block, validate_published_insight
from core.main import _resolve_evidence_refs

_METRICS = {"clicks", "cost", "conversions"}
_DIMENSIONS = {"campaign", "country"}
_TEMPLATES = {"kpi", "conversions"}


def _universe() -> set[str]:
    return _resolve_evidence_refs(
        available_metrics=_METRICS,
        available_dimensions=_DIMENSIONS,
    )


def _payload(**over) -> dict:
    payload = {
        "schemaVersion": "1",
        "slot": 0,
        "insight": {
            "title": "Conversions flat while spend rises",
            "summary": "s",
            "whyItMatters": "w",
            "confidence": "medium",
        },
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": "conversions"},
        "evidenceRefs": ["metric:conversions"],
    }
    payload.update(over)
    return payload


def _validate(payload: dict):
    return validate_published_insight(
        payload,
        available_metrics=_METRICS,
        available_dimensions=_DIMENSIONS,
        available_templates=_TEMPLATES,
        resolvable_evidence=_universe(),
        freshness_date="2026-07-21",
        has_project_access=True,
        existing_slots=set(),
    )


# --------------------------------------------------------------------------
# AC1 -- le resolveur ne rend que de la donnee du serveur
# --------------------------------------------------------------------------


def test_the_universe_is_what_the_server_measured():
    universe = _universe()
    assert "metric:conversions" in universe
    assert "dimension:country" in universe
    assert len(universe) == len(_METRICS) + len(_DIMENSIONS)


def test_no_ref_in_the_universe_names_a_card_template():
    """DEFAUT 1 -- une preuve pointe une donnee, pas la forme qui l'affiche.

    Un gabarit n'est ni produit pour CE projet ni pour CETTE periode : c'est une
    capacite de rendu. Tant qu'il etait citable, l'univers de preuve contenait
    exactement les ids que la gate 4 verifie deja, donc l'obligation ne demandait
    rien de plus que ce qui etait deja exige.
    """
    assert not [ref for ref in _universe() if ref.startswith("card:")]


def test_the_resolver_never_echoes_what_the_agent_supplied():
    """Sa signature ne prend PAS le payload, et c'est la garantie structurelle.

    L'ancien docstring promettait << it never echoes the agent's own refs back as
    resolved >> et le tenait en ne rendant rien. Il le tient maintenant en ne
    RECEVANT rien de l'agent : ses deux entrees viennent de ce que le serveur a
    mesure sur la fenetre.
    """
    import inspect

    parameters = set(inspect.signature(_resolve_evidence_refs).parameters)
    assert parameters == {"available_metrics", "available_dimensions"}
    assert "payload" not in parameters
    assert "available_templates" not in parameters, (
        "le catalogue de gabarits est revenu dans l'univers de preuve : "
        "l'obligation redevient satisfiable par tautologie"
    )


def test_the_resolver_is_not_a_constant_any_more():
    """AC7 : il DEPEND de ses entrees. Un `return set()` reviendrait ici.

    La garde est comportementale plutot que textuelle : deux univers differents
    doivent rendre deux ensembles differents, ce qu'aucune constante ne fait.
    """
    empty = _resolve_evidence_refs(available_metrics=set(), available_dimensions=set())
    assert empty == set(), "un univers vide reste vide -- l'echec ferme est voulu"
    assert _universe() != empty, (
        "le resolveur rend la meme chose quelles que soient ses entrees : il est "
        "redevenu une constante, et `evidenceRefs` ne prouve plus rien"
    )


# --------------------------------------------------------------------------
# AC4 -- les DEUX sens, sinon l'obligation ressemble a une panne
# --------------------------------------------------------------------------


def test_a_publication_that_cites_server_data_passes():
    result = _validate(_payload())
    assert result.ok, f"{result.reason_code}: {result.message}"


def test_citing_only_the_card_it_renders_is_not_evidence():
    """DEFAUT 1, par le comportement -- la tautologie est REFUSEE.

    C'etait la mesure du rejet : `evidenceRefs=["card:conversions"]` avec
    `card.template == "conversions"` rendait `ok=True`. La gate 4 exige deja ce
    gabarit ; le citer ne prouve rien. Le refus nomme la faute plutot que de
    dire << malforme >> tout court, parce que c'etait une forme legale hier.
    """
    result = _validate(_payload(evidenceRefs=["card:conversions"]))
    assert not result.ok, (
        "un insight qui ne cite que le gabarit qu'il rend est publiable : "
        "l'obligation de preuve est satisfiable par tautologie"
    )
    assert result.reason_code == "evidence_malformed"
    assert "card template" in (result.message or "")


@pytest.mark.parametrize(
    "refs,expected",
    [
        ([], "evidence_missing"),
        (None, "evidence_missing"),
        (["ev_1"], "evidence_malformed"),
        (["metric"], "evidence_malformed"),
        (["card:conversions"], "evidence_malformed"),
        (["card:not_a_template"], "evidence_malformed"),
        (["metric:conversions", "card:conversions"], "evidence_malformed"),
        (["metric:never_measured"], "evidence_unresolved"),
        (["dimension:never_measured"], "evidence_unresolved"),
        (["metric:conversions", "metric:never_measured"], "evidence_unresolved"),
    ],
)
def test_a_publication_without_resolving_evidence_is_refused(refs, expected):
    """Trois refus distincts, parce que ce sont trois fautes differentes.

    << ne resout pas >> ne dit pas a l'agent s'il a mal ecrit son ref ou s'il a
    trop affirme. Les separer est ce qui rend le message actionnable.
    """
    result = _validate(_payload(evidenceRefs=refs))
    assert not result.ok
    assert result.reason_code == expected, result.message
    assert result.field_path == "evidenceRefs"


def test_the_key_is_absent_entirely():
    payload = _payload()
    del payload["evidenceRefs"]
    result = _validate(payload)
    assert result.reason_code == "evidence_missing"


# --------------------------------------------------------------------------
# AC5 / AC6 -- ce qui est ecrit et ce qui est mesure ne se devinent pas
# --------------------------------------------------------------------------


def _preview(**over):
    from core import daily_insights_tools as tools

    kwargs = {
        "payload": _payload(),
        "available_metrics": _METRICS,
        "available_dimensions": _DIMENSIONS,
        "available_templates": _TEMPLATES,
        "resolvable_evidence": _universe(),
        "freshness_date": "2026-07-21",
        "has_project_access": True,
        "existing_slots": set(),
    }
    kwargs.update(over)
    return tools.preview(**kwargs)


def test_the_preview_says_which_fields_the_model_wrote():
    out = _preview()
    assert out["ok"], out
    authorship = out["authorship"]
    assert authorship["confidence"] == "derived", (
        "la confiance lue par une surface est MESUREE par le serveur, jamais "
        "declaree par l'auteur de la prose (`proactive-assertions.md`)"
    )
    # Le mot du modele survit, nomme comme le sien, et ne decide rien.
    assert authorship["declaredConfidence"] == "medium"
    # La preview ne MESURE pas : elle n'a pas les lignes du projet. Le bloc est
    # None, ce que toute surface lit `unmeasurable` -- jamais un niveau par defaut.
    assert authorship["derivedConfidence"] is None
    assert "insight.summary" in authorship["modelAuthored"]
    assert "insight.whyItMatters" in authorship["modelAuthored"]
    assert authorship["evidenceRefs"] == ["metric:conversions"]


def test_the_published_payload_carries_its_own_provenance():
    """DEFAUT 3 -- la provenance vivait la ou personne ne regarde.

    `authorship` n'existait que dans la reponse de `preview()`. Le payload
    PUBLIE -- l'artefact durable et autonome que toute surface relit -- ne la
    portait pas, donc la surface qui dessine `confidence` devait encore deviner.
    `proactive-assertions.md:162` : << the model-authored prose is visibly
    distinguishable from cited server data >>.
    """
    from unittest.mock import MagicMock

    from core import daily_insights_tools as tools

    persisted: dict = {}

    def _record_run(**kwargs):
        persisted.update(kwargs)
        return "dir_TEST"

    ack = tools.publish(
        project_id="proj_EXAMPLE",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload()],
        validate_ctx={
            "available_metrics": _METRICS,
            "available_dimensions": _DIMENSIONS,
            "available_templates": _TEMPLATES,
            "resolvable_evidence": _universe(),
            "freshness_date": "2026-07-21",
            "has_project_access": True,
            "existing_slots": set(),
        },
        conn=MagicMock(),
        record_run_fn=_record_run,
    )
    assert ack["ok"], ack
    stored = persisted["items"][0].payload
    assert stored["authorship"]["confidence"] == "derived"
    assert "insight.summary" in stored["authorship"]["modelAuthored"]


def test_the_enriched_payload_still_validates_and_is_idempotent():
    """La provenance persistee doit pouvoir etre RELUE par la meme barriere.

    Sinon un artefact publie serait invalide contre son propre contrat, et le
    premier code qui le revalide -- un partage, une reprise, un audit -- lirait
    un refus la ou rien n'a change.
    """
    payload = _payload()
    enriched = {**payload, "authorship": authorship_block(payload)}
    assert _validate(enriched).ok
    assert authorship_block(enriched) == enriched["authorship"]


def test_an_agent_that_declares_its_own_provenance_is_refused():
    """La provenance est DERIVEE. Declaree par l'auteur de la prose, elle ne
    prouve rien -- exactement le defaut que `confidence` porte deja."""
    forged = _payload()
    forged["authorship"] = {
        "modelAuthored": [],
        "confidence": "derived",
        "declaredConfidence": "high",
        "derivedConfidence": {"reading": "high"},
        "evidenceRefs": [],
    }
    result = _validate(forged)
    assert result.reason_code == "authorship_declared", result.message


def test_the_console_route_never_serves_a_payload_without_provenance():
    """Les lignes ECRITES AVANT cette story n'ont pas d'`authorship` stockee.

    La route la DERIVE a la lecture (jamais ne la fabrique : `authorship_block`
    ne lit que le payload), pour que la Console n'ait pas deux cas a distinguer.
    """
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    from starlette.testclient import TestClient

    legacy = {"id": "din_1", "slot": 0, "payload": _payload()}  # sans `authorship`

    @contextmanager
    def _conn():
        yield MagicMock()

    async def _auth_ok(request):
        return True, "test_user"

    with (
        patch("core.api_auth.authenticate_api_request", new=_auth_ok),
        patch("core.db.get_connection", new=_conn),
        patch("core.project_access.identity_can_read_project", return_value=True),
        patch("core.daily_insights.get_insight", return_value=legacy),
    ):
        from core.main import build_asgi_app

        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        resp = client.get("/api/daily-insights/insights/din_1?project_id=proj_EXAMPLE")

    assert resp.status_code == 200, resp.text
    authorship = resp.json()["payload"]["authorship"]
    assert authorship["confidence"] == "derived"
    assert authorship["evidenceRefs"] == ["metric:conversions"]


def test_the_declared_schema_and_the_enforced_shape_agree():
    """Le schema DIT la forme, la barriere la FAIT respecter -- une seule forme.

    `PUBLISHED_INSIGHT_SCHEMA` est un contrat LU, pas un validateur execute : ce
    module verifie ses formes a la main. Les deux peuvent donc diverger en
    silence, et un agent obeirait alors a une regle que le serveur ne demande
    pas. Ce test les tient ensemble.
    """
    from core.daily_insights_schema import (
        _EVIDENCE_REF,
        EVIDENCE_KINDS,
        PUBLISHED_INSIGHT_SCHEMA,
    )

    declared = PUBLISHED_INSIGHT_SCHEMA["properties"]["evidenceRefs"]
    assert declared["minItems"] == 1
    assert declared["items"]["pattern"] == _EVIDENCE_REF.pattern
    assert "evidenceRefs" in PUBLISHED_INSIGHT_SCHEMA["required"]
    assert EVIDENCE_KINDS == ("metric", "dimension"), (
        "un genre de preuve a ete ajoute ou retire : dire lequel, et pourquoi il "
        "designe une donnee produite pour CE projet et CETTE periode"
    )


# --------------------------------------------------------------------------
# DEFAUT 2 -- le catalogue de gabarits echouait OUVERT sur le chemin de
# publication, sous un commentaire qui jurait le contraire
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "project_not_readable",  # refus d'acces
        "no_project_named",  # projet irresoluble
        "answerable_topic_catalog_unavailable",  # store illisible
    ],
)
def test_an_unresolved_catalog_publishes_nothing(monkeypatch, reason):
    """Les trois portes de sortie de `_project_topic_catalog` rendent les
    defauts PLATEFORME. Sur le chemin de publication, elles doivent fermer.

    Le code de raison etait jete (`_project_topic_catalog(project_id)[0]`), donc
    une panne Postgres re-admettait en silence une question que le projet avait
    retiree -- pendant que metriques et dimensions, elles, tombaient a vide.
    """
    from core import answerable_topics as topics
    from core import main as core_main

    monkeypatch.setattr(
        core_main, "_project_topic_catalog", lambda pid: (topics.default_catalog(), reason)
    )
    templates, out_reason = core_main._publishable_card_templates("proj_EXAMPLE")
    assert templates == set(), (
        "les gabarits de la plateforme sont servis comme ceux du projet sur une "
        "panne : la publication passe contre une question retiree"
    )
    assert out_reason == reason, "la raison est jetee -- le refus ne peut plus la nommer"


def test_a_resolved_catalog_still_publishes(monkeypatch):
    """Le sens inverse, sinon la fermeture est indistinguable d'une panne."""
    from core import main as core_main

    monkeypatch.setattr(
        core_main,
        "_project_topic_catalog",
        lambda pid: ([{"id": "kpi"}, {"id": "conversions"}], None),
    )
    templates, out_reason = core_main._publishable_card_templates("proj_EXAMPLE")
    assert templates == {"kpi", "conversions"}
    assert out_reason is None


def test_the_refusal_names_the_outage_and_not_the_project():
    """`template_unknown` dirait << ce projet ne publie pas cette carte >> --
    une affirmation SUR LE PROJET produite par une panne du store."""
    out = _preview(available_templates=set(), catalog_reason="answerable_topic_catalog_unavailable")
    assert out["ok"] is False
    assert out["reasonCode"] == "catalog_unresolved", out
    assert "answerable_topic_catalog_unavailable" in out["message"]


def test_publish_refuses_on_an_unresolved_catalog():
    from unittest.mock import MagicMock

    from core import daily_insights_tools as tools

    def _must_not_run(**kwargs):
        raise AssertionError("record_run appele alors que le catalogue est une panne")

    ack = tools.publish(
        project_id="proj_EXAMPLE",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload()],
        validate_ctx={
            "available_metrics": _METRICS,
            "available_dimensions": _DIMENSIONS,
            "available_templates": set(),
            "resolvable_evidence": _universe(),
            "freshness_date": "2026-07-21",
            "has_project_access": True,
            "existing_slots": set(),
        },
        catalog_reason="project_not_readable",
        conn=MagicMock(),
        record_run_fn=_must_not_run,
    )
    assert ack["ok"] is False
    assert ack["reasonCode"] == "catalog_unresolved", ack


def test_the_published_payload_carries_a_confidence_the_server_measured():
    """La confiance lue est MESUREE a la publication, pas declaree par le modele.

    `proactive-assertions.md` (<< Incomplete if >>) : *a confidence level is
    declared by the author of the claim rather than derived*. Le payload de
    reference declare `medium` ; les lignes du projet ne portent qu'une moitie des
    membres cites, donc le serveur lit `low`. Le mot du modele survit sous
    `declaredConfidence` et ne decide rien.
    """
    from unittest.mock import MagicMock

    from core import daily_insights_tools as tools
    from core.insight_confidence import derive_insight_confidence

    persisted = {}

    def _record_run(**kw):
        persisted.update(kw)
        return "dir_EXAMPLE"

    rows = [
        {
            "date": "2026-07-21",
            "loaded_at": "2026-07-21T05:00:00+00:00",
            "connector": "google-ads",
            "pull_id": "pull_EXAMPLE",
            "metric": "conversions",
        }
    ]
    payload = _payload()
    payload["evidenceRefs"] = ["metric:conversions"]

    ack = tools.publish(
        project_id="proj_EXAMPLE",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[payload],
        validate_ctx={
            "available_metrics": _METRICS,
            "available_dimensions": _DIMENSIONS,
            "available_templates": _TEMPLATES,
            "resolvable_evidence": _universe(),
            "freshness_date": "2026-07-21",
            "has_project_access": True,
            "existing_slots": set(),
        },
        conn=MagicMock(),
        record_run_fn=_record_run,
        confidence_fn=lambda p: derive_insight_confidence(p, rows=rows),
    )

    assert ack["ok"], ack
    authorship = persisted["items"][0].payload["authorship"]
    assert authorship["confidence"] == "derived"
    assert authorship["declaredConfidence"] == "medium"
    derived = authorship["derivedConfidence"]
    assert derived["reading"] == "high"
    assert derived["citedMembers"] == ["metric:conversions"]
    assert derived["limitingTerm"] in derived["terms"]
    # Et la contrainte soeur : aucune quatrieme valeur ne fusionne les trois.
    assert "score" not in derived


def test_without_a_measurement_the_publication_states_no_level_at_all():
    """Pas de `confidence_fn` -> pas de niveau, jamais un niveau par defaut.

    C'est le cas qui rendrait la derivation cosmetique : un chemin de publication
    qui, faute de mesure, retomberait sur le mot du modele publierait exactement
    l'objet que cette regle retire, avec l'autorite du serveur en plus.
    """
    from unittest.mock import MagicMock

    from core import daily_insights_tools as tools

    persisted = {}

    def _record_run(**kw):
        persisted.update(kw)
        return "dir_EXAMPLE"

    ack = tools.publish(
        project_id="proj_EXAMPLE",
        insight_date="2026-07-21",
        status="published",
        item_payloads=[_payload()],
        validate_ctx={
            "available_metrics": _METRICS,
            "available_dimensions": _DIMENSIONS,
            "available_templates": _TEMPLATES,
            "resolvable_evidence": _universe(),
            "freshness_date": "2026-07-21",
            "has_project_access": True,
            "existing_slots": set(),
        },
        conn=MagicMock(),
        record_run_fn=_record_run,
    )

    assert ack["ok"], ack
    authorship = persisted["items"][0].payload["authorship"]
    assert authorship["derivedConfidence"] is None
    assert authorship["declaredConfidence"] == "medium"
