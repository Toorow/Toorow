"""Story 53.1 -- un outil MCP qui nomme un projet refuse, et ne lit pas d'abord.

POURQUOI CE FICHIER ET PAS `test_tools_isolation.py`. Le voisin appelle
`warehouse.query_daily_report(project_id=alpha)` **en direct** : jamais l'outil,
jamais avec une identite. Il etablit << demander alpha rend alpha >>, ce qui est
vrai de n'importe quelle fonction de requete, et reste vert pendant que la faille
existe -- c'est d'ailleurs comme ca qu'elle a survecu. La propriete en cause est
<< une identite beta se voit refuser alpha >>, et elle ne peut se mesurer qu'a
travers la fonction d'outil, avec un jeton.

CE QUE CHAQUE TEST PROUVE, ET DANS CET ORDRE :

  AC4  l'appel passe par la fonction d'outil, avec un jeton d'identite ;
  AC1  une identite sans droit sur le projet est refusee ;
  AC3  la requete metier n'est PAS PARTIE -- un refus qui lit puis jette laisse
       la charge et le temps de reponse repondre a la question qu'il refuse ;
  AC2  refuse, absent et indisponible rendent la MEME enveloppe, donc comparer
       deux refus n'apprend pas qu'un projet existe.

AUCUN POSTGRES. Le point de decision est `resolve_strict_resource_access`, et ce
fichier le remplace par une doublure qui refuse. Ce qui est sous test n'est pas
la resolution d'acces -- elle a ses propres tests pg-gated -- mais le fait que
les outils **l'appellent avant de lire**.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from core import (
    calculated_field_proposals_mcp,
    context_hub_mcp,
    daily_insight_mcp,
    evaluation_mcp,
    evidence_chain_mcp,
    exemplars_mcp,
    feedback_review_mcp,
    golden_question_mcp,
    project_access_mcp,
)
from core import main as core_main
from core.project_resolver import PROJECT_NOT_FOUND_CODE
from fastmcp.exceptions import ToolError

#: LE RESOLVEUR LUI-MEME, capte AVANT que quoi que ce soit ne le double.
#:
#: `a_stranger` remplace `core.main._resolve_project` par une doublure -- c'etait
#: sans consequence tant que le resolveur ne decidait rien, et c'est exactement ce
#: qui rendait sa faille invisible ici. Le vrai objet est retenu a l'import pour
#: que la section du 2026-08-25 puisse l'appeler malgre la doublure.
_THE_RESOLVER = core_main._resolve_project

#: Les outils de `main.py` qui passent par le seam de portee. Les huit premiers
#: sont ceux de 53.1 (`33f1dac4`) ; les cinq derniers -- la famille
#: knowledge/skills/hub -- ont recu le meme seam le 2026-08-07 (`d81f247b`) et
#: sont testes ici pour la meme raison qu'eux : un outil garde dont aucun test ne
#: fait tomber la garde peut la perdre sans qu'une ligne rougisse.
#: `health` n'y est pas : c'est la seule exception, elle est ecrite a sa ligne
#: dans `main.py` (sonde de disponibilite, ne lit aucune ligne de projet).
#:
#: L'appel est construit ici avec le MINIMUM d'arguments valides : ce qui compte
#: est que le refus tombe AVANT le travail, donc les arguments metier n'ont pas
#: besoin d'etre realistes.
_REPAIRED = {
    # Migration 321 : la porte qui RETIRE une assertion publiee. Elle demande
    # `edit`, comme la publication -- desavouer une revendication dans le projet
    # du voisin n'est pas une lecture. Elle entre ici le jour ou elle est ecrite,
    # et non dans la dette de `_PROVEN_BY_READING_ONLY` : une garde qu'aucun appel
    # refuse ne fait tomber peut se perdre sans qu'une ligne rougisse.
    "retract_daily_insight": lambda: daily_insight_mcp.retract_daily_insight(
        project_id="proj_foreign", insight_id="din_foreign", reason="wrong window"
    ),
    "add_knowledge": lambda: core_main.add_knowledge(
        project_id="proj_foreign", title="t", body_md="b"
    ),
    "add_skill": lambda: core_main.add_skill(
        project_id="proj_foreign", name="n", description="d", body_md="b"
    ),
    "get_context_hub": lambda: core_main.get_context_hub(project_id="proj_foreign"),
    "get_knowledge": lambda: core_main.get_knowledge(project_id="proj_foreign"),
    "get_skills": lambda: core_main.get_skills(project_id="proj_foreign"),
    "add_context_event": lambda: core_main.add_context_event(
        project_id="proj_foreign", event_date="2026-08-05", type="business", label="x"
    ),
    # Migration 328 : les deux portes SYMETRIQUES de la precedente. Elles entrent
    # ici le jour ou elles sont ecrites, comme `retract_daily_insight` -- corriger
    # ou retirer l'annotation du projet du voisin n'est pas moins grave que d'y en
    # ecrire une, et une garde qu'aucun appel refuse ne fait tomber se perd sans
    # qu'une ligne rougisse. Appelees sur `context_hub_mcp` : `main.py` ne les
    # re-exporte pas, et un test qui passerait par un re-export mesurerait le
    # re-export.
    "correct_context_event": lambda: context_hub_mcp.correct_context_event(
        project_id="proj_foreign", event_id="evt_foreign", label="x"
    ),
    "withdraw_context_event": lambda: context_hub_mcp.withdraw_context_event(
        project_id="proj_foreign", event_id="evt_foreign", reason="wrong day"
    ),
    "flows_get": lambda: core_main.flows_get(
        project_id="proj_foreign", kind="datastream", id="ds_x"
    ),
    "flows_upsert": lambda: core_main.flows_upsert(
        project_id="proj_foreign", definition={"kind": "report", "id": "r"}
    ),
    # `date_range` est VALIDE a dessein : cet outil refuse une entree malformee
    # avant de resoudre l'acces, et c'est correct -- un `invalid_date_range` ne
    # dit rien du projet, il parle de ce que l'appelant a lui-meme envoye. Sans
    # une plage valide ici, le test mesurerait ce refus-la et jamais la garde.
    "get_daily_report": lambda: core_main.get_daily_report(
        project_id="proj_foreign",
        date_range={"start": "2026-08-01", "end": "2026-08-05"},
    ),
    "get_events": lambda: core_main.get_events(project_id="proj_foreign"),
    "list_connectors": lambda: core_main.list_connectors(project_id="proj_foreign"),
    "search_context": lambda: core_main.search_context(
        query="revenue", project_id="proj_foreign"
    ),
    # AI-67.1 : la Skill ENTIERE, pas ses seules remarques. `get_procedure`
    # resolvait bien un acces -- mais dans le bloc des remarques, donc son refus
    # ne retirait que celles-ci et le corps de la procedure partait quand meme.
    # Il est ici parce que c'est le seul endroit ou la difference se mesure : la
    # garde de conformite 53.1 le comptait protege sur un IMPORT.
    "get_procedure": lambda: core_main.get_procedure(
        name="close-the-month", project_id="proj_foreign"
    ),
    "submit_feedback": lambda: core_main.submit_feedback(
        project_id="proj_foreign", rating=1
    ),
    # Les cinq outils scopes projet trouves HORS de `main.py` le 2026-08-10, une
    # fois la garde d'AC7 elargie a tout `core/`. Trois d'entre eux ecrivent.
    "get_datastream_schedule": lambda: _tool_registered_by(
        "schedule_mcp", "get_datastream_schedule"
    )(project_id="proj_foreign", datastream_id="ds_x"),
    "read_dimension_labels": lambda: _tool_registered_by(
        "dimension_labels_mcp", "read_dimension_labels"
    )(project_id="proj_foreign"),
    "set_datastream_schedule": lambda: _tool_registered_by(
        "schedule_mcp", "set_datastream_schedule"
    )(project_id="proj_foreign", datastream_id="ds_x", cadence="nightly"),
    "set_dimension_label": lambda: _tool_registered_by(
        "dimension_labels_mcp", "set_dimension_label"
    )(
        canonical_dimension="country",
        display_label="Pays",
        scope_level="project",
        project_id="proj_foreign",
    ),
    "stop_datastream_run": lambda: _tool_registered_by(
        "stop_run_mcp", "stop_datastream_run"
    )(project_id="proj_foreign", datastream_id="ds_x", execution_id="ex_x"),
    # Les quatre nes du repliement de la surface MCP (2026-08-13) : dix-sept
    # outils par connecteur remplaces par deux outils generiques bornes au
    # projet, plus les deux du referentiel de dimension. La declaration ECRIT
    # (`update_datastream` puis `commit` sur `data_role`), les trois autres
    # lisent.
    "list_datastreams": lambda: _tool_registered_by(
        "datastream_report_mcp", "list_datastreams"
    )(project_id="proj_foreign"),
    "get_datastream_report": lambda: _tool_registered_by(
        "datastream_report_mcp", "get_datastream_report"
    )(datastream="ds_x", project_id="proj_foreign"),
    "describe_dimension_reference": lambda: _tool_registered_by(
        "dimension_reference_mcp", "describe_dimension_reference"
    )(project_id="proj_foreign", dimension="video"),
    "declare_dimension_reference": lambda: _tool_registered_by(
        "dimension_reference_mcp", "declare_dimension_reference"
    )(project_id="proj_foreign", datastream_id="ds_x"),
    # Les quatre nees en fermant AI-269.
    #
    # `save_notebook` a bascule sur le magasin GOUVERNE le 2026-08-22 (67.23) :
    # il prend `(label, blocks)`, ou chaque bloc epingle une VERSION. Le bloc
    # ci-dessous est BIEN FORME a dessein, meme motif qu'avant -- l'outil doit
    # refuser sur la PORTEE, pas sur la charge, sinon ce test mesurerait la
    # validation des blocs et jamais la garde. Il n'y a plus de `report_ref` a
    # valider contre le catalogue de la plateforme : la reference d'un bloc est
    # une version, resolue DANS le projet, donc apres la garde.
    "save_notebook": lambda: core_main.save_notebook(
        project_id="proj_foreign",
        label="t",
        blocks=[
            {
                "block_key": "b",
                "block_type": "query",
                "as_of_rule": "current",
                "query_spec_version_id": "qsv_EXAMPLE",
            }
        ],
    ),
    # `compose_dossier` (74-1, 2026-09-02) : meme motif que `save_notebook` -- un
    # bloc BIEN FORME, pour que le refus mesure soit celui de la PORTEE et jamais
    # celui de la forme. Il fige des Renders et ecrit un Dossier : une identite
    # sans droit qui passerait creerait des objets chez le voisin.
    "compose_dossier": lambda: _tool_registered_by("dossier_mcp", "compose_dossier")(
        project_id="proj_foreign",
        label="t",
        blocks=[
            {
                "kind": "figure",
                "result_id": "qr_EXAMPLE",
                "visualization_spec_version_id": "vsv_EXAMPLE",
            }
        ],
    ),
    # `run_notebook` ne recoit PAS de project_id : c est la ligne du notebook qui
    # dit a quel projet il appartient, donc la lecture precede necessairement la
    # garde. Le stub rend cette ligne sans base, pour que le test mesure la garde
    # et non l absence de Postgres.
    "run_notebook": lambda: _run_notebook_of_a_foreign_project(),
    # --- La porte MCP des Reports gouvernes (story 67.23, 2026-08-22) --------
    #
    # Les trois arrivent ici parce que `test_a_guard_is_proven_by_a_refused_call_
    # and_not_by_an_import` les a nommes le jour ou ils ont ete ecrits : leur
    # garde n'etait verifiee que par lecture du source, et un import de garde
    # n'est pas une garde. Les arguments sont MINIMAUX et bien formes -- ce qui
    # est mesure est que le refus tombe sur la PORTEE, avant toute lecture, pas
    # que la charge soit refusee.
    "list_reports": lambda: _tool_registered_by(
        "analysis_report_mcp", "list_reports"
    )(project_id="proj_foreign"),
    "get_report_versions": lambda: _tool_registered_by(
        "analysis_report_mcp", "get_report_versions"
    )(project_id="proj_foreign", report_id="rpt_x"),
    "run_report_version": lambda: _tool_registered_by(
        "analysis_report_mcp", "run_report_version"
    )(project_id="proj_foreign", report_version_id="rptv_x"),
    # --- Le parcours gouverne de creation d un Datastream (67.23) ------------
    #
    # Les cinq passent par `_scoped`, qui prouve la portee AVANT toute lecture.
    # Les arguments sont bien formes a dessein : ce qui est mesure est que le
    # refus tombe sur la PORTEE, pas que la charge soit refusee -- sinon le test
    # serait vert avec la garde ouverte.
    "start_datastream_draft": lambda: _tool_registered_by(
        "datastream_wizard_mcp", "start_datastream_draft"
    )(project_id="proj_foreign", idempotency_key="k"),
    "get_datastream_draft": lambda: _tool_registered_by(
        "datastream_wizard_mcp", "get_datastream_draft"
    )(project_id="proj_foreign", draft_id="dsd_x"),
    "observe_datastream_draft": lambda: _tool_registered_by(
        "datastream_wizard_mcp", "observe_datastream_draft"
    )(project_id="proj_foreign", draft_id="dsd_x", expected_revision=1, idempotency_key="k"),
    "compile_datastream_draft": lambda: _tool_registered_by(
        "datastream_wizard_mcp", "compile_datastream_draft"
    )(project_id="proj_foreign", draft_id="dsd_x", expected_revision=1, idempotency_key="k"),
    "materialize_datastream_draft": lambda: _tool_registered_by(
        "datastream_wizard_mcp", "materialize_datastream_draft"
    )(
        project_id="proj_foreign",
        draft_id="dsd_x",
        preview_ref="prev_x",
        acknowledged_warning_ids=[],
        idempotency_key="k",
    ),
    "get_card": lambda: core_main.get_card(project_id="proj_foreign"),
    # La lecture de posture de projet (amendement du 2026-08-17 a
    # `first-figure-path.md`). Elle compose l'enveloppe COMPLETE de l'Overview,
    # donc un refus qui lirait d'abord aurait deja repondu a << ce projet
    # existe-t-il, et a-t-il publie ? >> avant de refuser de le dire.
    "get_project_posture": lambda: _tool_registered_by(
        "project_posture_mcp", "get_project_posture"
    )(project_id="proj_foreign"),
    # Chantier 67-24 : la lecture des liaisons de langue. Elle liste ce que le
    # projet voisin a lie a quelle dimension -- donc ses connecteurs, ses
    # rapports et ses colonnes. Un refus qui lirait d'abord aurait deja compose
    # la carte de sa collecte avant de refuser de la dire.
    "read_language_bindings": lambda: _tool_registered_by(
        "language_bindings_mcp", "read_language_bindings"
    )(project_id="proj_foreign"),
    # La pose d'un taux fixe (amendement du 2026-08-17 a `alignment-register.md`).
    # Elle ECRIT, au rang `manage`, et elle decide ce que vaut chaque montant du
    # projet : un refus qui divulguerait l'existence du projet voisin donnerait a
    # lire la carte de ses devises. Les arguments sont VALIDES a dessein -- meme
    # motif que la plage de dates de `get_daily_report` -- pour que le test mesure
    # la garde et non le refus typé de `core.fx_fixed_rates`.
    "set_fixed_fx_rate": lambda: _tool_registered_by(
        "fx_fixed_rate_mcp", "set_fixed_fx_rate"
    )(
        project_id="proj_foreign",
        base_currency="USD",
        quote_currency="EUR",
        rate="0.92",
        valid_from="2026-01-01",
        valid_to="2026-12-31",
    ),
    # Le verbe Reprocess (67-15b). `prepare` assemble la proposition d'un rejeu
    # depuis la matiere DETENUE d'un flux, `confirm` publie une nouvelle version
    # de sortie : un refus qui lirait d'abord divulguerait a la fois l'existence
    # du projet voisin et celle de son artefact atterri.
    "prepare_datastream_reprocess": lambda: _tool_registered_by(
        "recovery_mcp", "prepare_datastream_reprocess"
    )(datastream_id="ds_foreign", project_id="proj_foreign"),
    "confirm_datastream_reprocess": lambda: _tool_registered_by(
        "recovery_mcp", "confirm_datastream_reprocess"
    )(
        preparation_id="prep_foreign",
        project_id="proj_foreign",
        datastream_id="ds_foreign",
    ),
    # Le helper partage des outils daily-insight, appele par son chemin d'ECRITURE
    # -- celui de `publish_daily_insight`, le seul qui exige `edit`.
    "_daily_insight_scope": lambda: _daily_insight_write_scope(),
    # Les cinq portes du chantier 67-23 -- les quatre surfaces que l'audit du
    # 2026-08-17 a trouvees sans outil. Chacune compose par la MEME fonction que
    # l'ecran, donc un refus qui lirait d'abord aurait deja repondu a la question
    # qu'il refuse : ce que ce projet collecte, ce que son juge a conclu, quel
    # flux fait foi pour un total, ce que son Hub sait.
    "get_data_surface": lambda: _tool_registered_by(
        "data_surface_mcp", "get_data_surface"
    )(project_id="proj_foreign", lens="imports"),
    "get_evaluation_runs": lambda: _tool_registered_by(
        "evaluation_mcp", "get_evaluation_runs"
    )(project_id="proj_foreign"),
    "get_ai_path": lambda: _tool_registered_by(
        "evaluation_mcp", "get_ai_path"
    )(project_id="proj_foreign"),
    "get_context_adherence": lambda: _tool_registered_by(
        "evaluation_mcp", "get_context_adherence"
    )(project_id="proj_foreign"),
    # `concept_id` est FOURNI a dessein, meme motif que la plage de dates de
    # `get_daily_report` : sans lui l'outil refuse en `missing_param` avant de
    # resoudre l'acces -- un refus qui parle de l'argument, jamais du projet --
    # et le test mesurerait celui-la au lieu de la garde.
    "get_metric_grain_authority": lambda: _tool_registered_by(
        "metric_grain_mcp", "get_metric_grain_authority"
    )(project_id="proj_foreign", concept_id="sc_x"),
    # Elle ECRIT dans la file de revue du voisin. Arguments valides pour la meme
    # raison : les refus de forme tombent avant la garde, et c'est correct.
    # Story 75-3 : la porte des exemplaires approuves. Le sujet est VALIDE a
    # dessein -- l'outil refuse un `subject_type` inconnu avant de resoudre la
    # portee, et sans un sujet valide ce test mesurerait ce refus-la.
    "get_exemplars": lambda: exemplars_mcp.get_exemplars(
        subject_type="metric", subject_id="sc_EXAMPLE", project_id="proj_foreign"
    ),
    # Story 75-1 : un appel BIEN FORME sur le projet du voisin -- l'expression est
    # valide, c'est la portee qui refuse.
    "propose_calculated_field": lambda: calculated_field_proposals_mcp.propose_calculated_field(
        project_id="proj_foreign",
        name="cost_per_click",
        expression={"op": "literal", "value_type": "decimal", "value": 1},
        result_id="qr_foreign",
    ),
    "add_context_remark": lambda: _tool_registered_by(
        "context_remark_mcp", "add_context_remark"
    )(
        project_id="proj_foreign",
        node_type="procedure",
        node_id="proc_x",
        note="cette contrainte n'existe plus",
    ),
    # Story 68.1 : la declaration d'un type d'entite. `idempotency_key` est
    # FOURNIE a dessein, meme motif que la plage de `get_daily_report` : sans
    # elle l'outil refuse en `missing_idempotency_key` AVANT la garde -- un
    # refus qui parle de l'appelant, jamais du projet -- et le test mesurerait
    # celui-la. Les trois arguments metier sont valides pour la meme raison.
    "declare_entity_type": lambda: _tool_registered_by(
        "entity_types_mcp", "declare_entity_type"
    )(
        project_id="proj_foreign",
        object_kind="video",
        canonical_key="video_id",
        display_name="Videos",
        idempotency_key="idem_x",
    ),
    "list_entity_types": lambda: _tool_registered_by(
        "entity_types_mcp", "list_entity_types"
    )(project_id="proj_foreign"),
    # Story 49.3 : la porte MCP du change-set Semantic Model. Elle PUBLIE le sens
    # -- ce qu'un nombre compte dans tous les rendus du projet. Les arguments
    # sont VALIDES a dessein, meme motif que la plage de `get_daily_report` et
    # que l'`idempotency_key` de `declare_entity_type` : sans eux l'outil refuse
    # en `missing_param` / `missing_idempotency_key` AVANT la garde -- un refus
    # qui parle de l'appelant, jamais du projet -- et le test mesurerait
    # celui-la au lieu de la portee.
    # La lecture jumelle de la porte ci-dessous. Elle repond << voici ce que CE
    # projet peut adopter >> : un refus qui lirait d'abord aurait deja dit quel
    # vocabulaire le projet porte, et lequel lui manque.
    "list_semantic_metric_presets": lambda: _tool_registered_by(
        "governance_mcp", "list_semantic_metric_presets"
    )(project_id="proj_foreign"),
    "propose_shared_identities": lambda: _tool_registered_by(
        "governance_mcp", "propose_shared_identities"
    )(project_id="proj_foreign"),
    "publish_shared_identity": lambda: _tool_registered_by(
        "governance_mcp", "publish_shared_identity"
    )(
        project_id="proj_foreign",
        intent={
            "action": "pin_identity",
            "canonical_field_id": "mdm_foreign",
            "carriers": [{"datastream_id": "ds_foreign", "column": "date"}],
        },
        idempotency_key="idem-foreign",
    ),
    # La porte d ACCES (2026-09-05). Lire l acces du projet du voisin dirait deja
    # qui y travaille ; en changer une capacite y decide ce que d autres peuvent
    # faire. Les deux entrent le jour ou elles sont ecrites.
    "read_project_access": lambda: project_access_mcp.read_project_access(
        project_id="proj_foreign"
    ),
    "grant_project_access": lambda: project_access_mcp.grant_project_access(
        project_id="proj_foreign",
        intent={"action": "set_capability", "identity": "someone@example.com", "capability": "manage"},
        idempotency_key="idem-foreign",
    ),
    # La porte de PREUVE (2026-09-05) : la chaine du projet du voisin dirait deja
    # ce qui y a ete publie, approuve et par qui. Lecture pure, gardee comme telle.
    "walk_evidence_chain": lambda: evidence_chain_mcp.walk_evidence_chain(
        project_id="proj_foreign"
    ),
    # La porte de RELECTURE du Test (2026-09-05). Lire les reactions du projet du
    # voisin dirait deja ce qu'on y a juge et sur quoi ; en repondre une y ajoute
    # une version immuable a un chef.
    "read_feedback": lambda: feedback_review_mcp.read_feedback(
        project_id="proj_foreign"
    ),
    "review_feedback": lambda: feedback_review_mcp.review_feedback(
        project_id="proj_foreign",
        feedback_id="fb_foreign",
        review={
            "state": "triaged",
            "affected_dimension": "semantic_correctness",
            "human_verdict": "fail",
            "severity": "minor",
            "reason": "foreign",
            "retry_key": "idem-foreign",
        },
    ),
    # Les VERBES de la Regression Run (2026-09-05). Ouvrir un jugement dans le
    # projet du voisin y depense du calcul et y ecrit une cohorte jugee ; en
    # approuver la base decide ce contre quoi TOUTE comparaison future y sera
    # mesuree. Les trois entrent le jour ou elles sont ecrites.
    "open_evaluation_run": lambda: evaluation_mcp.open_evaluation_run(
        project_id="proj_foreign",
        run={
            "run_profile_id": "erp_foreign",
            "context_version_set_id": "cvs_foreign",
            "semantic_view_id": "sv_foreign",
            "semantic_view_version_id": "svv_foreign",
        },
    ),
    "advance_evaluation_run": lambda: evaluation_mcp.advance_evaluation_run(
        project_id="proj_foreign", run_id="er_foreign", action="finalize"
    ),
    "decide_evaluation_run": lambda: evaluation_mcp.decide_evaluation_run(
        project_id="proj_foreign",
        action="approve_baseline",
        decision={
            "run_id": "er_foreign",
            "approved_by": "owner@example.com",
            "approval_reason": "foreign",
        },
    ),
    # La porte de DEFINITION du Test (2026-09-05). Les trois entrent le jour ou
    # elles sont ecrites : une definition publiee dans le projet du voisin fixe ce
    # contre quoi TOUTE execution future y sera jugee, et la lecture dirait deja
    # quelles questions ce projet tient pour sa mesure.
    "list_golden_questions": lambda: golden_question_mcp.list_golden_questions(
        project_id="proj_foreign"
    ),
    "publish_golden_question_version": (
        lambda: golden_question_mcp.publish_golden_question_version(
            project_id="proj_foreign",
            title="Foreign",
            owner="owner@example.com",
            definition={"result_type": "scalar", "severity": "blocking"},
        )
    ),
    "set_golden_question_lifecycle": (
        lambda: golden_question_mcp.set_golden_question_lifecycle(
            project_id="proj_foreign",
            golden_question_id="gq_foreign",
            lifecycle="archived",
        )
    ),
    "publish_semantic_model_change": lambda: _tool_registered_by(
        "governance_mcp", "publish_semantic_model_change"
    )(
        project_id="proj_foreign",
        object_type="semantic-concept",
        intent={
            "action": "create_concept",
            "concept": {"kind": "metric", "name": "clicks"},
        },
        idempotency_key="idem_x",
    ),
    # Story 68.7 : la lecture de decouverte. Elle compose ce que le projet
    # declare, ce qui le designe et ce qui le couvre -- un refus qui lirait
    # d'abord aurait deja repondu a << ce projet reconcilie quoi ? >>.
    "list_entity_reconciliation_context": lambda: _tool_registered_by(
        "entity_context_mcp", "list_entity_reconciliation_context"
    )(project_id="proj_foreign"),
    # LE RESOLVEUR, arbitrage rendu par Jean le 2026-08-25. Il n'est pas un outil
    # enregistre : c'est la fonction que ~30 corps d'outils traversent AVANT leur
    # propre garde, et jusqu'a cette date elle repondait << ce projet existe >> a
    # qui savait l'epeler. Il est appele SANS identite a dessein -- c'est la
    # branche ambiante (`mcp_scope.caller_identity`), celle qu'aucun appelant de
    # production n'emprunte et que rien n'exercerait donc autrement.
    "_resolve_project": lambda: _THE_RESOLVER("proj_foreign"),
}


def _run_notebook_of_a_foreign_project():
    """`run_notebook` sur un notebook qui appartient a quelqu un d autre.

    L outil lit la ligne AVANT de garder, et il le doit : son seul argument est
    un `notebook_id`, donc rien avant cette lecture ne sait de quel projet il
    s agit. Ce que la garde doit tenir est ce qui vient APRES -- et ce stub est
    ce qui permet de le mesurer sans base.
    """
    from unittest.mock import MagicMock

    import core.db as _db
    import core.notebook_mcp as _nb

    row = ("nb_x", "proj_foreign", "t", "adhoc", "last_7d", "", "someone@example.com")
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.description = [
        ("id",), ("project_id",), ("title",), ("report_ref",),
        ("window_rule",), ("narrative_prompt",), ("created_by",),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    # `core.db` is imported INSIDE the function body, so the patch has to land on
    # the module it imports from, not on an attribute `notebook_mcp` does not carry.
    with patch.object(_db, "get_connection") as opened:
        opened.return_value.__enter__.return_value = conn
        return _nb.run_notebook(notebook_id="nb_x")


def _daily_insight_write_scope():
    """Le helper, sur le rang que sa publication demande.

    Il n'est pas enregistre comme outil : c'est la fonction que les quatre outils
    daily-insight traversent, et le rang qu'elle exige est celui que son appelant
    lui passe. Le chemin epingle ici est celui de `publish_daily_insight`, parce
    que c'est lui qui ECRIT.
    """
    from core.daily_insight_mcp import _daily_insight_scope

    return _daily_insight_scope("proj_foreign", minimum_capability="edit")


@pytest.fixture()
def a_stranger(monkeypatch):
    """Une identite authentifiee QUI N'A PAS le projet.

    `TOOROW_AUTH_MODE` doit etre arme : le mode `disabled` porte le seul sujet de
    compatibilite `anonymous`, et l'exempter est la posture self-host voulue. Un
    test qui oublierait cette ligne mesurerait le carve-out, pas la garde.

    LA CONNEXION EST DOUBLEE, ET C'EST LOAD-BEARING. Sans elle, la garde ouvre
    une vraie connexion, echoue avec `OperationalError`, et refuse -- FERME, donc
    correctement. Mais les tests de refus passeraient alors sans jamais atteindre
    la resolution d'acces : verts pour la mauvaise raison, et incapables de voir
    une garde qui aurait ete retiree. La doublure force le chemin nominal, et le
    fail-closed garde son propre test (`..._are_the_same_answer`).
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    token = SimpleNamespace(claims={"sub": "person_stranger"}, client_id="cli")
    monkeypatch.setattr(core_main, "get_access_token", lambda: token)
    # Les outils hors `main.py` resolvent leur identite par un import a l'appel
    # depuis `fastmcp.server.dependencies` : sans cette ligne ils verraient
    # `anonymous` et le test mesurerait un autre chemin de refus que celui vise.
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: token, raising=False
    )
    monkeypatch.setattr(
        "core.main._resolve_project", lambda pid, identity=None: pid or "proj_foreign"
    )
    monkeypatch.setattr("core.db.request_connection", _a_connection_that_opens)
    return token


@contextmanager
def _a_connection_that_opens(_identity):
    """Une connexion qui s'ouvre, pour que la decision d'acces soit atteinte."""
    yield MagicMock()


def _denied(*_args, **_kwargs):
    return SimpleNamespace(allowed=False, org_id=None, reason="grant_required", capability=None)


def _envelope_of(exc: ToolError) -> str:
    """Le corps du refus, quelle que soit sa forme de transport."""
    text = str(exc)
    try:
        return json.dumps(json.loads(text), sort_keys=True)
    except Exception:
        return text


@pytest.mark.parametrize("tool_name", sorted(_REPAIRED))
def test_a_stranger_is_refused_by_every_repaired_tool(tool_name, a_stranger, monkeypatch):
    """AC1 + AC4 : par l'outil, avec un jeton, et le refus tombe."""
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)

    with pytest.raises(ToolError) as excinfo:
        _REPAIRED[tool_name]()

    assert PROJECT_NOT_FOUND_CODE in str(excinfo.value), (
        f"{tool_name} a refuse avec une enveloppe qui n'est pas celle d'un projet "
        f"absent : {excinfo.value!r}. Une enveloppe distincte pour << interdit >> "
        "est un oracle d'enumeration deguise en mot de securite."
    )


@pytest.mark.parametrize("tool_name", sorted(_REPAIRED))
def test_the_refusal_happens_before_the_work(tool_name, a_stranger, monkeypatch):
    """AC3 : la couche metier n'est jamais atteinte.

    Un refus qui interroge puis jette le resultat a quand meme depense la
    requete : la charge et le temps de reponse repondent alors a la question que
    le refus existe pour laisser sans reponse.
    """
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)

    reads: list[str] = []

    def _tripwire(name):
        def _boom(*_a, **_k):
            reads.append(name)
            raise AssertionError(f"{tool_name} a lu {name} malgre le refus")
        return _boom

    with (
        patch("core.warehouse.query_daily_report", _tripwire("warehouse.query_daily_report")),
        patch("core.context_search.search_context", _tripwire("context_search.search_context")),
        # AI-67.1 : le corps d'une Skill est la charge principale de
        # `get_procedure`. Sans ce fil-piege, l'outil pouvait etre parametre ici
        # tout en continuant de servir la procedure -- c'est exactement l'etat
        # que l'audit du 2026-08-17 a mesure.
        patch(
            "core.context_search.get_procedure_by_name",
            _tripwire("context_search.get_procedure_by_name"),
        ),
        # Chantier 67-24 : la liste des liaisons de langue EST la charge de
        # `read_language_bindings`. Sans ce fil-piege, l'outil pouvait etre
        # parametre ici tout en continuant de composer la carte des colonnes du
        # projet voisin avant de refuser de la rendre.
        patch(
            "core.language_dimensions.list_bindings",
            _tripwire("language_dimensions.list_bindings"),
        ),
        patch("core.flows.get_flow", _tripwire("flows.get_flow")),
        patch("core.flows.upsert_flow", _tripwire("flows.upsert_flow")),
        patch(
            "core.context_events.persist_context_event",
            _tripwire("context_events.persist_context_event"),
        ),
        # La famille knowledge / skills / hub lit et ecrit par `context_store` et
        # `business_taxonomy` : sans ces fils-pieges, les cinq outils ajoutes le
        # 2026-08-07 auraient ete parametres dans ce test sans que rien de leur
        # couche metier ne soit surveille.
        patch("core.context_store.list_topics", _tripwire("context_store.list_topics")),
        patch("core.context_store.create_topic", _tripwire("context_store.create_topic")),
        patch(
            "core.context_store.list_procedures",
            _tripwire("context_store.list_procedures"),
        ),
        # PAS de fil-piege sur `get_topic_by_id` / `get_procedure_by_id` : ces
        # deux noms N'EXISTENT PAS dans `core.context_store` (mesure :
        # `hasattr(...)` -> False), et `main.py:3303` / `main.py:3435` les
        # appellent quand meme. C'est un defaut reel, hors du perimetre de cette
        # story, nomme plutot que masque par un patch qui l'aurait cree.
        patch(
            "core.context_store.create_procedure",
            _tripwire("context_store.create_procedure"),
        ),
        patch(
            "core.business_taxonomy.list_taxonomy",
            _tripwire("business_taxonomy.list_taxonomy"),
        ),
        # Migration 321 : la couche metier de la retractation. Sans ce fil-piege,
        # `retract_daily_insight` pourrait etre parametre ici tout en ecrivant la
        # retractation d'un insight du projet voisin avant de refuser.
        patch(
            "core.daily_insights.retract_insight",
            _tripwire("daily_insights.retract_insight"),
        ),
        # Chantier 67-23 : la couche metier des cinq nouvelles portes. Sans ces
        # fils-pieges elles seraient parametrees ici sans que rien de ce qu'elles
        # lisent ne soit surveille -- la lecon ecrite six lignes plus haut pour
        # la famille knowledge/skills, appliquee le jour ou les outils arrivent
        # et non deux vagues apres.
        patch(
            "core.data_surface.compose_data_surface",
            _tripwire("data_surface.compose_data_surface"),
        ),
        patch(
            "core.evaluation_runs.list_evaluation_runs",
            _tripwire("evaluation_runs.list_evaluation_runs"),
        ),
        patch("core.adherence.adherence_overview", _tripwire("adherence.adherence_overview")),
        patch("core.metric_grain.grain_coverage", _tripwire("metric_grain.grain_coverage")),
        patch(
            "core.context_review.request_review",
            _tripwire("context_review.request_review"),
        ),
        # Story 49.3 : la couche metier de la porte Semantic Model. Ouvrir un
        # change-set ECRIT une ligne dans le projet nomme ; sans ce fil-piege,
        # l'outil pouvait etre parametre ici tout en creant le change-set du
        # voisin avant de refuser de le dire.
        patch(
            "core.semantic_model.create_change_set",
            _tripwire("semantic_model.create_change_set"),
        ),
    ):
        with pytest.raises(ToolError):
            _REPAIRED[tool_name]()

    assert not reads, f"{tool_name} a atteint {reads} avant/malgre son refus"


@pytest.mark.parametrize("tool_name", sorted(_REPAIRED))
def test_denied_and_unavailable_are_the_same_answer(tool_name, a_stranger, monkeypatch):
    """AC2 : un refus et une panne sont indistinguables de l'exterieur.

    Sans cette propriete, comparer deux refus apprend qu'un projet existe -- et
    la difference la plus facile a produire est justement de faire tomber la
    base, ce que la garde traite en echouant FERME.
    """
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)
    with pytest.raises(ToolError) as denied:
        _REPAIRED[tool_name]()

    def _explode(*_a, **_k):
        raise RuntimeError("la base est injoignable")

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _explode)
    with pytest.raises(ToolError) as unavailable:
        _REPAIRED[tool_name]()

    assert _envelope_of(denied.value) == _envelope_of(unavailable.value), (
        f"{tool_name} distingue << refuse >> de << indisponible >> :\n"
        f"  refuse       : {denied.value}\n"
        f"  indisponible : {unavailable.value}"
    )


def test_a_holder_of_the_project_is_not_refused(a_stranger, monkeypatch):
    """La garde isole, elle ne ferme pas la porte a tout le monde.

    Sans ce test, un `raise` inconditionnel passerait les trois precedents.
    `search_context` sert de temoin : il rend une enveloppe, pas une exception,
    des que l'acces est accorde.
    """
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(
            allowed=True, org_id="org_x", reason=None, capability="view"
        ),
    )
    with patch("core.context_search.search_context", return_value=[]):
        result = core_main.search_context(query="revenue", project_id="proj_mine")
    assert result is not None


# ---------------------------------------------------------------------------
# AC5 -- la CAPACITE exigee, et pas seulement le fait qu'une garde existe.
#
# LE TROU QUE CETTE SECTION FERME (relecture du 2026-08-10). Le distinguo
# view / edit / manage est reel dans le code (`project_access._CAPABILITY_ORDER`,
# compare au rang dans `resolve_strict_resource_access`). Il n'etait tenu par
# AUCUN test : retrograder les cinq `minimum_capability="edit"` en `"view"`
# laissait **29 tests verts**. Un porteur `view` pouvait donc ECRIRE chez le
# voisin, et rien ne rougissait. Le patron existait pourtant deja sur la surface
# REST jumelle -- `tests/core/test_admin_api_context.py:418` :
#     assert guard.call_args.kwargs["minimum_capability"] == "edit"
# La classe n'avait ete traitee que d'un cote.
#
# POURQUOI UNE LISTE ET PAS UNE DERIVATION. << Qui ecrit >> n'est pas derivable
# honnetement ici : le signal le plus evident, `conn.commit()` dans le corps,
# designe `search_context` (il commite le sort des candidats ecartes,
# AI-157) et rate `flows_upsert` et `add_context_event`, qui delèguent leur
# ecriture. Une derivation inventee aurait classe un outil de travers en se
# donnant l'air d'etre calculee. La liste est donc explicite -- et
# `test_every_guarded_tool_is_classified` la fait vieillir bruyamment : le compte
# des outils gardes, lui, EST calcule, et tout nouvel arrivant doit etre range.
# ---------------------------------------------------------------------------

#: Les ecritures. Une ecriture croisee ne fuit pas une donnee, elle en FABRIQUE
#: une chez le voisin -- et ce que produisent `add_context_event` et
#: `add_knowledge` devient la section << Why >> de toute narration batie dessus.
_WRITE_TOOLS = frozenset({
    # Story 75-1 : la porte de PROMOTION d'un calcul d'exploration. `edit` et
    # non `view` -- contrairement a `add_context_remark`, dont la remarque ne
    # porte aucune charge typee : accepter celle-ci OUVRE un change-set sur le
    # modele semantique du projet nomme.
    "propose_calculated_field",  # app.calculated_field_proposals (75-1)
    "add_context_event",       # app.context_events
    # Les deux portes symetriques (migration 328). Elles ecrivent la MEME table :
    # corriger une annotation change ce que toute narration citera desormais, et
    # la retirer decide qu'aucune ne la citera plus. `view` ne suffit ni pour
    # l'une ni pour l'autre.
    "correct_context_event",   # app.context_events + app.context_event_revisions
    "withdraw_context_event",  # app.context_events (supersede, jamais delete)
    "add_knowledge",           # app.context_topics
    "add_skill",               # app.procedures
    "flows_upsert",            # app.datastreams / app.report_overrides
    "submit_feedback",         # app.feedback
    # Les trois trouvees hors `main.py` par la garde d'AC7 elargie (2026-08-10).
    "set_dimension_label",     # app.dimension_labels -- ce que TOUS ses rendus affichent
    "set_datastream_schedule",  # app.datastreams -- depense le quota du voisin
    "stop_datastream_run",     # app.datastream_executions -- arrete sa collecte
    # Elle ECRIT : `update_datastream` puis `commit` pour poser le role
    # REFERENCE. Ce que ce role change, c'est ce que TOUS les rendus du projet
    # affichent comme nom d'une valeur -- une ecriture qui demanderait `view`
    # laisserait un porteur `view` renommer chez le voisin.
    "declare_dimension_reference",  # app.datastreams.data_role
    # Les trois arrivees en fermant AI-269 : elles passaient par une garde qui
    # s'OUVRE quand la base tombe (`_assert_project_access`) ou par un
    # `except Exception` qui laissait passer, et leur refus disait `forbidden` --
    # donc << ce projet existe >>.
    "save_notebook",           # app.analysis_notebooks + sa version 1 (67.23)
    "compose_dossier",         # app.renders (un par figure) + app.analysis_dossiers (74-1)
    "run_notebook",            # app.analysis_notebook_runs -- et le budget d'entrepot
    # Il CREE un Result et depense le budget d'entrepot du projet : une execution
    # croisee ne fuit pas une donnee, elle en FABRIQUE une. Ses deux voisins de
    # la meme porte (`list_reports`, `get_report_versions`) sont des lectures et
    # n'y sont pas.
    "run_report_version",      # app.analysis_report_runs + un Result (67.23)
    # --- Le parcours gouverne (67.23). Quatre ECRITURES et une lecture --------
    #
    # `get_datastream_draft` est dans `_READ_TOOLS`. Les quatre autres ecrivent :
    # ouvrir un brouillon pose une ligne, observer et compiler en posent d'autres,
    # et materialiser CREE un Datastream. Une seule demanderait `view` qu'un
    # porteur `view` creerait un flux chez le voisin.
    "start_datastream_draft",      # app.datastream_setup_drafts
    "observe_datastream_draft",    # app.datastream_setup_observations
    "compile_datastream_draft",    # app.datastream_preconfiguration_proposals
    "materialize_datastream_draft",  # app.datastreams + ses versions + un candidat
    # Le helper partage des quatre outils daily-insight. Il porte la capacite de
    # son appelant : `publish_daily_insight` demande `edit`, les trois lectures
    # `view`. Il est classe ECRITURE parce que c'est le rang que le test lui
    # demandera, et que la garde doit tenir sur le chemin qui PUBLIE.
    "_daily_insight_scope",    # app.daily_insights, via publish_daily_insight
})

#: Les lectures. `search_context` est ICI malgre son `conn.commit()` : ce qu'il
#: commite est le sort des candidats de sa propre recherche, pas une donnee que
#: l'appelant a demande a ecrire.
_READ_TOOLS = frozenset({
    # Story 75-3 : les exemplaires approuves (golden questions actives, chemins
    # observes approuves) d une metrique, d une Vue ou d un topic -- une lecture.
    "get_exemplars",
    # La lecture des AI Paths d un Projet -- les siens compris (2026-09-05) :
    # liste, pas d un chemin, chemin derriere un Result. Rien n est ecrit.
    "get_ai_path",
    # La lecture de la porte de RELECTURE du Test (2026-09-05) : les reactions du
    # Projet, une reaction dans son contexte exact, les agregats, les negatifs
    # critiques non resolus. Son ecriture voisine n'est pas ici : elle passe par
    # `resolve_strict_resource_access` au rang `edit`.
    "read_feedback",
    # La lecture de la porte de PREUVE (2026-09-05) : trois lentilles sur la
    # chaine -- lignage, versions et approbations, activite auditee.
    "walk_evidence_chain",
    # La lecture de la porte d ACCES (2026-09-05) : qui atteint ce Projet, a
    # quelle capacite, et d ou elle vient. Son ecriture voisine passe par
    # `resolve_strict_resource_access` au rang `manage`.
    "read_project_access",
    # La lecture de la porte de DEFINITION du Test (2026-09-05) : ce que ce
    # Projet tient pour digne de confiance, et les choix gouvernes qu une
    # nouvelle version peut epingler. Ses deux ecritures voisines ne sont pas
    # ici : elles passent par `resolve_strict_resource_access` au rang `edit`,
    # comme `publish_shared_identity`, et non par le sceau de portee.
    "list_golden_questions",
    # Les deux lectures de la porte MCP des Reports gouvernes (67.23). Elles
    # n'ecrivent rien : `list_reports` compte, `get_report_versions` rend ce
    # qu'une version epingle. La troisieme de cette porte,
    # `run_report_version`, est dans `_WRITE_TOOLS` -- elle cree un Result.
    "get_report_versions",
    "list_reports",
    # La seule lecture du parcours gouverne (67.23) : elle rend l'etat entier
    # d'un brouillon a une adresse, et n'ecrit rien.
    "get_datastream_draft",
    "flows_get",
    "get_context_hub",
    "get_daily_report",
    "get_events",
    "get_knowledge",
    "get_skills",
    "list_connectors",
    "search_context",
    "get_datastream_schedule",
    "read_dimension_labels",
    # Les trois lectures nees du repliement de la surface MCP (2026-08-13) :
    # dix-sept outils par connecteur ont ete remplaces par deux outils generiques
    # bornes au projet, et le troisieme dit seulement quel flux NOMME une
    # dimension.
    "list_datastreams",
    "get_datastream_report",
    "describe_dimension_reference",
    # AI-269 : elle lisait derriere un `except Exception` qui la laissait passer
    # quand la base tombait, et refusait en `forbidden`.
    "get_card",
    # AI-67.1 : elle resolvait un acces qui ne gouvernait que ses remarques.
    "get_procedure",
    # Chantier 67-23 : les quatre lectures des surfaces que l'audit du 2026-08-17
    # a trouvees sans porte MCP. Toutes composent par la MEME fonction que
    # l'ecran, donc leur refus doit tomber avant la composition et non apres.
    "get_data_surface",
    "get_evaluation_runs",
    "get_context_adherence",
    "get_metric_grain_authority",
    # Chantier 67-24 : elle LIT les liaisons de langue du projet. Declarer ou
    # retirer une liaison reste un geste humain sur la surface REST, au rang
    # `manage` -- aucun outil MCP n'ecrit dans cette famille.
    "read_language_bindings",
    # Le resolveur (2026-08-25). Il est classe LECTURE parce que c'est le seul
    # rang qu'il PEUT demander : il ne sait pas si son appelant s'apprete a lire
    # ou a ecrire. Le rang d'une ecriture reste celui de l'outil, et c'est
    # pourquoi la garde de l'outil n'est pas devenue redondante.
    "_resolve_project",
})

#: LES ECRITURES QUE LE PRODUIT GOUVERNE AU RANG `view`, ET LA RAISON DE CHACUNE.
#:
#: POURQUOI UN TROISIEME SEAU PLUTOT QU'UN CLASSEMENT DE TRAVERS. Le distinguo
#: ci-dessus dit << une ecriture exige `edit` >>, et il a raison pour tout ce
#: qu'il couvre : une ecriture croisee FABRIQUE une ligne chez le voisin. Mais
#: une remarque de revue n'est pas de cette classe, et le produit l'a deja
#: tranche : `context_api._request_review` garde le MEME geste au rang `view`,
#: avec sa raison ecrite a sa ligne -- << n'importe quel consommateur de la
#: connaissance peut signaler un noeud, pas seulement ceux qui l'ecrivent >>. La
#: ranger dans `_WRITE_TOOLS` ferait rougir un test pour une divergence
#: inexistante ; la ranger dans `_READ_TOOLS` mentirait sur ce qu'elle fait.
#:
#: CE SEAU N'EST PAS UNE ECHAPPATOIRE. Chaque entree porte la PHRASE qui dit
#: quelle porte deja livree tient le meme geste au meme rang -- meme modele que
#: `_NO_TENANT_SCOPE` dans la garde de conformite, et
#: `test_a_view_rank_write_matches_the_console_door` verifie que le rang exige
#: est bien `view` et pas davantage. Y deposer une vraie ecriture de contenu
#: demanderait d'ecrire une phrase fausse et verifiable.
_VIEW_RANK_WRITES = {
    "add_context_remark": (
        "Depose une remarque dans la file de revue du Context Hub. Le meme geste "
        "est garde au rang `view` par la console (`context_api._request_review`, "
        "seam `_strict_url_project_allowed(..., minimum_capability='view')`) : "
        "signaler un noeud appartient a qui le CONSOMME. La remarque ne touche "
        "aucun contenu gouverne -- seul un `member` peut l'accepter ou la "
        "decliner -- et le producteur machine qui existait deja "
        "(`context_review.propose_missing_link`) ecrit depuis un chemin de "
        "lecture."
    ),
}

#: Les modules de `core/` qui portent un outil passant par le seam. `main.py`
#: l'appelle par son alias `_refuse_unless_project_scope` ; les autres par son
#: nom, `refuse_unless_project_scope`. Les deux sont couverts par la meme
#: recherche de sous-chaine.
_MODULES_WITH_A_GUARDED_TOOL = (
    "main.py",
    "dimension_labels_mcp.py",
    "schedule_mcp.py",
    "stop_run_mcp.py",
)


def _tools_calling_the_scope_seam() -> set[str]:
    """Calcule, pas recopie : toute fonction de `core/` qui appelle le seam.

    Balaye `core/*.py` en entier, et pas seulement `main.py` : c'est la meme
    lecon que celle d'AC7 -- une classification ancree sur un fichier laisse la
    capacite des outils des autres modules epinglee nulle part.
    """
    import ast
    from pathlib import Path

    core = Path(core_main.__file__).parent
    found = set()
    for path in sorted(core.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "refuse_unless_project_scope":
                continue  # la definition du seam n'est pas un appelant
            # LE CORPS PROPRE, sans les fonctions imbriquees. Les trois modules
            # hors `main.py` definissent leurs outils DANS `register(mcp)` :
            # sans ce retrait, `register` lui-meme comptait comme outil garde.
            own = _body_without_nested_functions(source, node)
            if "refuse_unless_project_scope(" in own:
                found.add(node.name)
    return found


def _body_without_nested_functions(source: str, node) -> str:
    import ast

    inner = [
        child for child in ast.walk(node)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node
    ]
    hidden = {
        line
        for child in inner
        for line in range(child.lineno, (child.end_lineno or child.lineno) + 1)
    }
    lines = source.splitlines()
    return "\n".join(
        lines[i - 1]
        for i in range(node.lineno, (node.end_lineno or node.lineno) + 1)
        if i not in hidden
    )


def _tool_registered_by(module_name: str, tool_name: str):
    """Le handler qu'un module enregistre, capte a l'enregistrement.

    Ces outils sont des fonctions LOCALES de `register(mcp)` : il n'existe aucun
    attribut de module a appeler. Le patron est celui de leurs propres tests
    (`test_stop_run_mcp._register`), reutilise plutot que reinvente.
    """
    import importlib

    import core.mcp_profiles as profiles

    module = importlib.import_module(f"core.{module_name}")
    captured: dict[str, object] = {}

    def _record(_mcp, handler, **_kwargs):
        captured[handler.__name__] = handler
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        module.register(object())
    finally:
        profiles.register_profiled = original
    return captured[tool_name]


def test_every_guarded_tool_is_classified():
    """Un outil garde qui n'est ni lecture ni ecriture ici fait ROUGIR ce test.

    C'est le cliquet de la liste ci-dessus. Sans lui, un outil ajoute demain
    recevrait sa garde et sa capacite ne serait epinglee nulle part -- exactement
    l'etat que la relecture a trouve, mais pour un nom de plus.
    """
    guarded = _tools_calling_the_scope_seam()
    classified = _WRITE_TOOLS | _READ_TOOLS | set(_VIEW_RANK_WRITES)
    assert guarded == classified, (
        "l'inventaire des outils gardes de `main.py` a bouge :\n"
        f"  gardes mais non classes : {sorted(guarded - classified)}\n"
        f"  classes mais plus gardes : {sorted(classified - guarded)}\n\n"
        "Range chaque nom dans _WRITE_TOOLS ou _READ_TOOLS. Un outil qui ECRIT "
        "et qui demanderait `view` laisse un porteur `view` ecrire chez le voisin. "
        "_VIEW_RANK_WRITES est le troisieme seau, et il coute une phrase qui NOMME "
        "la porte deja livree tenant le meme geste au meme rang."
    )
    empty = sorted(name for name, why in _VIEW_RANK_WRITES.items() if not (why or "").strip())
    assert not empty, (
        "entree(s) de `_VIEW_RANK_WRITES` sans raison ecrite :\n  " + "\n  ".join(empty)
    )
    overlap = sorted(set(_VIEW_RANK_WRITES) & (_WRITE_TOOLS | _READ_TOOLS))
    assert not overlap, (
        "outil(s) ranges dans deux seaux a la fois :\n  " + "\n  ".join(overlap)
    )


def _capability_demanded_by(tool_name: str, monkeypatch) -> str:
    """La capacite que l'outil exige REELLEMENT, capturee a travers l'appel."""
    seen: list[str] = []

    def _record(*_args, **kwargs):
        seen.append(kwargs.get("minimum_capability", "view"))
        return SimpleNamespace(
            allowed=False, org_id=None, reason="grant_required", capability=None
        )

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _record)
    with pytest.raises(ToolError):
        _REPAIRED[tool_name]()
    assert seen, f"{tool_name} n'a jamais atteint la resolution d'acces"
    return seen[0]


@pytest.mark.parametrize("tool_name", sorted(_WRITE_TOOLS))
def test_a_write_demands_edit_and_not_view(tool_name, a_stranger, monkeypatch):
    """AC5. Retrograder l'un de ces cinq en `view` fait rougir CETTE ligne."""
    assert _capability_demanded_by(tool_name, monkeypatch) == "edit", (
        f"{tool_name} ECRIT et se contente de `view` : un porteur en lecture "
        "seule peut alors fabriquer une ligne dans le projet du voisin."
    )


@pytest.mark.parametrize("tool_name", sorted(_VIEW_RANK_WRITES))
def test_a_view_rank_write_matches_the_console_door(tool_name, a_stranger, monkeypatch):
    """Le troisieme seau exige `view`, ET PAS DAVANTAGE NON PLUS.

    Le test tient les deux bords. Un rang qui monterait a `edit` ferait diverger
    la porte MCP de la porte console pour UN geste -- deux reponses a << qui peut
    signaler un noeud >>. Un rang qui n'atteindrait pas la resolution du tout
    serait une ecriture non gardee, et `_capability_demanded_by` echoue alors sur
    sa propre assertion.
    """
    assert _capability_demanded_by(tool_name, monkeypatch) == "view", (
        f"{tool_name} n'exige plus le rang que la console tient pour le meme "
        f"geste. La raison inscrite au seau dit : {_VIEW_RANK_WRITES[tool_name]}"
    )


@pytest.mark.parametrize("tool_name", sorted(_READ_TOOLS))
def test_a_read_demands_view(tool_name, a_stranger, monkeypatch):
    """L'autre moitie du distinguo : sans elle, tout exiger `edit` passerait.

    Une garde uniforme n'est pas une garde graduee. Ce test rend le rang
    OBSERVABLE dans les deux sens, donc la difference entre lire et ecrire
    devient une propriete mesuree et non une convention d'ecriture.
    """
    assert _capability_demanded_by(tool_name, monkeypatch) == "view", (
        f"{tool_name} lit et exige `edit` : soit c'est une ecriture mal classee, "
        "soit la garde refuse a des lecteurs legitimes."
    )


def test_the_auth_disabled_local_operator_still_works(monkeypatch):
    """Le self-host mono-operateur n'est pas casse par la garde.

    `anonymous` sous `TOOROW_AUTH_MODE=disabled` est le seul sujet de
    compatibilite, et c'est `core.db` qui detient cette regle -- la garde la lui
    demande au lieu d'en ecrire une quatrieme copie.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    monkeypatch.setattr(core_main, "get_access_token", lambda: None)
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid or "default")

    def _never(*_a, **_k):
        raise AssertionError("le mode disabled a interroge la base pour anonymous")

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _never)
    with patch("core.context_search.search_context", return_value=[]):
        assert core_main.search_context(query="x", project_id="default") is not None


# ---------------------------------------------------------------------------
# LA PORTEE ORG ET LA PORTEE PLATEFORME -- le trou du 2026-08-21.
#
# CE QUE CETTE SECTION FERME, ET POURQUOI LE CLIQUET CI-DESSUS NE L'A PAS VU.
# `set_dimension_label` etait deja parametre dans `_REPAIRED` -- mais avec
# `scope_level="project"` et rien d'autre. Il exercait donc la seule branche que
# l'outil gardait, et restait VERT pendant qu'un appel
# `set_dimension_label(scope_level="org", org_id=<voisin>, project_id=None)` ne
# traversait AUCUN controle de portee : le `if project_id:` du module ne se
# declenchait pas.
#
# LA LECON EST DE CLASSE, PAS D'INSTANCE. Une garde qui n'exerce qu'une branche
# d'un parametre prouve la branche, jamais la portee. Un test parametre sur un
# `scope_level` doit donc porter TOUTES ses valeurs, et
# `test_every_org_guarded_tool_is_classified` fait vieillir bruyamment la liste
# le jour ou un outil de plus prend un `org_id`.
#
# LA REGLE N'EST PAS INVENTEE ICI NON PLUS. Elle est celle que la porte REST du
# meme etat tient deja (`core/dimension_lineage_api.py:15-17`) : une lecture
# exige l'appartenance a l'organisation, une ecriture exige owner/admin, et la
# portee PLATEFORME est refusee parce que les seeds en sont l'autorite.
# ---------------------------------------------------------------------------

from core.mcp_scope import ORG_NOT_FOUND_CODE, PLATFORM_SCOPE_CODE  # noqa: E402

#: Les appels a PORTEE ORGANISATION -- ceux que `_REPAIRED` ne composait pas.
#: `project_id` est ABSENT a dessein : c'est tout le defaut. Un appel qui en
#: porterait un serait garde par le seam projet et ce test mesurerait celui-la.
_ORG_SCOPED = {
    "read_dimension_labels@org": lambda: _tool_registered_by(
        "dimension_labels_mcp", "read_dimension_labels"
    )(org_id="org_foreign"),
    "set_dimension_label@org": lambda: _tool_registered_by(
        "dimension_labels_mcp", "set_dimension_label"
    )(
        canonical_dimension="country",
        display_label="Pays",
        scope_level="org",
        org_id="org_foreign",
    ),
}

#: Le rang que chaque appel doit exiger. `manage` et pas `edit` : l'appartenance
#: a une organisation n'a que deux rangs (`project_access._ORG_MANAGE_ROLES` est
#: owner/admin, tout le reste est membre), et c'est le rang que
#: `_guard_org_and_project(..., manage=True)` demande sur la porte REST jumelle.
_ORG_RANK_DEMANDED = {
    "read_dimension_labels@org": "view",
    "set_dimension_label@org": "manage",
}


def _org_access_recorder(monkeypatch, *, allowed: bool = False, explode: bool = False):
    """Remplace les deux questions d'appartenance et note LAQUELLE a ete posee.

    C'est ce qui rend le rang observable : `identity_has_org_access` est le rang
    `view`, `identity_can_manage_org` le rang `manage`. Un outil qui ecrirait en
    ne posant que la premiere apparait ici, et nulle part ailleurs.
    """
    seen: list[str] = []

    def _make(rank):
        def _answer(*_a, **_k):
            seen.append(rank)
            if explode:
                raise RuntimeError("la base est injoignable")
            return allowed

        return _answer

    monkeypatch.setattr("core.project_access.identity_has_org_access", _make("view"))
    monkeypatch.setattr("core.project_access.identity_can_manage_org", _make("manage"))
    return seen


@pytest.mark.parametrize("call_name", sorted(_ORG_SCOPED))
def test_a_stranger_is_refused_at_the_org_scope(call_name, a_stranger, monkeypatch):
    """Le pendant d'AC1 pour la branche `org`, qui n'avait aucun controle."""
    _org_access_recorder(monkeypatch)

    with pytest.raises(ToolError) as excinfo:
        _ORG_SCOPED[call_name]()

    assert ORG_NOT_FOUND_CODE in str(excinfo.value), (
        f"{call_name} a refuse avec une enveloppe qui n'est pas celle d'une "
        f"organisation absente : {excinfo.value!r}. Une enveloppe distincte pour "
        "<< interdit >> apprend a l'appelant que l'organisation existe."
    )


@pytest.mark.parametrize("call_name", sorted(_ORG_SCOPED))
def test_the_org_refusal_happens_before_the_work(call_name, a_stranger, monkeypatch):
    """Le pendant d'AC3 : ni la cascade ni l'UPSERT ne partent."""
    _org_access_recorder(monkeypatch)

    reads: list[str] = []

    def _tripwire(name):
        def _boom(*_a, **_k):
            reads.append(name)
            raise AssertionError(f"{call_name} a atteint {name} malgre le refus")

        return _boom

    with (
        patch(
            "core.dimension_conformance.resolve_dimension_labels",
            _tripwire("dimension_conformance.resolve_dimension_labels"),
        ),
        patch(
            "core.dimension_conformance.set_dimension_label",
            _tripwire("dimension_conformance.set_dimension_label"),
        ),
    ):
        with pytest.raises(ToolError):
            _ORG_SCOPED[call_name]()

    assert not reads, f"{call_name} a atteint {reads} avant/malgre son refus"


@pytest.mark.parametrize("call_name", sorted(_ORG_SCOPED))
def test_org_denied_and_unavailable_are_the_same_answer(
    call_name, a_stranger, monkeypatch
):
    """Le pendant d'AC2 : la garde d'organisation echoue FERME, comme celle du projet."""
    _org_access_recorder(monkeypatch)
    with pytest.raises(ToolError) as denied:
        _ORG_SCOPED[call_name]()

    _org_access_recorder(monkeypatch, explode=True)
    with pytest.raises(ToolError) as unavailable:
        _ORG_SCOPED[call_name]()

    assert _envelope_of(denied.value) == _envelope_of(unavailable.value), (
        f"{call_name} distingue << refuse >> de << indisponible >> :\n"
        f"  refuse       : {denied.value}\n"
        f"  indisponible : {unavailable.value}"
    )


@pytest.mark.parametrize("call_name", sorted(_ORG_SCOPED))
def test_the_org_rank_demanded_is_the_one_the_console_door_demands(
    call_name, a_stranger, monkeypatch
):
    """Le pendant d'AC5 : une ECRITURE d'organisation exige owner/admin.

    Retrograder `set_dimension_label@org` au rang `view` -- c'est-a-dire se
    contenter de l'appartenance simple -- laisserait un membre `viewer` renommer
    une dimension pour toute l'organisation, dans tous ses rendus. La porte REST
    jumelle exige `identity_can_manage_org` pour le meme geste.
    """
    seen = _org_access_recorder(monkeypatch)
    with pytest.raises(ToolError):
        _ORG_SCOPED[call_name]()

    assert seen, f"{call_name} n'a jamais atteint la resolution d'appartenance"
    assert seen[0] == _ORG_RANK_DEMANDED[call_name], (
        f"{call_name} a demande le rang {seen[0]!r} au lieu de "
        f"{_ORG_RANK_DEMANDED[call_name]!r}."
    )


@pytest.mark.parametrize("call_name", sorted(_ORG_SCOPED))
def test_a_holder_of_the_org_is_not_refused(call_name, a_stranger, monkeypatch):
    """La garde isole, elle ne ferme pas la porte a tout le monde.

    Sans ce test, un `raise` inconditionnel sur la branche `org` passerait les
    quatre precedents -- et casserait le seul parcours que cette branche sert.
    """
    _org_access_recorder(monkeypatch, allowed=True)

    with (
        patch("core.dimension_conformance.resolve_dimension_labels", lambda **_k: {}),
        patch(
            "core.dimension_conformance.set_dimension_label",
            lambda **_k: {"id": "dl_EXAMPLE"},
        ),
    ):
        result = _ORG_SCOPED[call_name]()

    assert result is not None
    assert "error" not in result, f"{call_name} refuse un porteur legitime : {result}"


def test_the_platform_scope_is_never_written_from_a_tool(a_stranger, monkeypatch):
    """La portee PLATEFORME n'a pas de porte MCP, et son refus dit ou aller.

    Elle n'est pas une question d'existence : les entrees de plateforme sont
    livrees par les seeds et lues par TOUTES les organisations. La porte REST du
    meme etat repond deja `403` (`dimension_lineage_api.py:250-256`) ; ce module
    etait la seule surface qui acceptait le mot.
    """
    seen = _org_access_recorder(monkeypatch)

    def _never(**_k):
        raise AssertionError("la plateforme a ete ecrite depuis un outil")

    with patch("core.dimension_conformance.set_dimension_label", _never):
        with pytest.raises(ToolError) as excinfo:
            _tool_registered_by("dimension_labels_mcp", "set_dimension_label")(
                canonical_dimension="country",
                display_label="Pays",
                scope_level="platform",
            )

    assert PLATFORM_SCOPE_CODE in str(
        excinfo.value
    ), f"la portee plateforme a refuse autrement : {excinfo.value!r}"
    assert not seen, (
        "le refus de plateforme a resolu une appartenance : il ne depend de "
        "personne, et une resolution ici serait un temps de reponse qui parle."
    )


def test_a_declared_scope_without_its_id_is_refused_before_the_store(
    a_stranger, monkeypatch
):
    """`scope_level="org"` sans `org_id` ne garde rien -- donc il ne passe pas.

    C'est le trou juste a cote du trou : sans identifiant il n'y a rien a
    resoudre, et l'appel descendait tel quel vers le magasin.
    """
    _org_access_recorder(monkeypatch)
    tool = _tool_registered_by("dimension_labels_mcp", "set_dimension_label")

    def _never(**_k):
        raise AssertionError("le magasin a ete atteint sans portee resolue")

    with patch("core.dimension_conformance.set_dimension_label", _never):
        out = tool(
            canonical_dimension="country", display_label="Pays", scope_level="org"
        )
        assert out["error"] == "missing_scope"
        out = tool(
            canonical_dimension="country", display_label="Pays", scope_level="project"
        )
        assert out["error"] == "missing_scope"


#: Les appels a portee organisation, classes -- le cliquet de la liste ci-dessus.
_ORG_READS = frozenset({"read_dimension_labels"})
_ORG_WRITES = frozenset({"set_dimension_label"})


def _tools_calling_the_org_seam() -> set[str]:
    """Calcule, pas recopie : toute fonction de `core/` qui appelle le seam ORG.

    Meme instrument que `_tools_calling_the_scope_seam`, sur l'autre seam. Le
    jour ou un outil de plus accepte un `org_id`, il apparait ici et doit etre
    range -- sinon il recevrait sa garde sans que sa BRANCHE soit exercee nulle
    part, ce qui est exactement l'etat que cette section repare.
    """
    import ast
    from pathlib import Path

    core = Path(core_main.__file__).parent
    found = set()
    for path in sorted(core.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "refuse_unless_org_scope":
                continue  # la definition du seam n'est pas un appelant
            own = _body_without_nested_functions(source, node)
            if "refuse_unless_org_scope(" in own:
                found.add(node.name)
    return found


def test_every_org_guarded_tool_is_classified():
    """Un outil garde au scope ORG qui n'est ni lecture ni ecriture fait ROUGIR."""
    guarded = _tools_calling_the_org_seam()
    classified = _ORG_READS | _ORG_WRITES
    assert guarded == classified, (
        "l'inventaire des outils gardes au scope ORG a bouge :\n"
        f"  gardes mais non classes : {sorted(guarded - classified)}\n"
        f"  classes mais plus gardes : {sorted(classified - guarded)}\n\n"
        "Range chaque nom, ET ajoute son appel a `_ORG_SCOPED` : une garde dont "
        "la branche `org` n'est exercee par aucun test peut disparaitre sans "
        "qu'une ligne rougisse -- c'est le defaut du 2026-08-21."
    )
    unexercised = sorted(
        name
        for name in classified
        if not any(key.startswith(f"{name}@") for key in _ORG_SCOPED)
    )
    assert not unexercised, (
        "outil(s) gardes au scope ORG dont aucun appel n'exerce la branche :\n  "
        + "\n  ".join(unexercised)
    )


# ---------------------------------------------------------------------------
# LE RESOLVEUR REPOND `project_not_found` -- arbitrage rendu par Jean 2026-08-25
#
# CE QUE CETTE SECTION MESURE, ET QUE LES TESTS CI-DESSUS NE POUVAIENT PAS.
# `a_stranger` remplace `core.main._resolve_project` par une doublure. Tant que
# le resolveur ne decidait rien, cela ne coutait rien -- et c'est precisement ce
# qui rendait sa faille invisible ICI : la fonction qui repondait << ce projet
# existe >> a qui savait l'epeler etait la seule que le harnais de refus ne
# faisait jamais tourner. La doublure est donc retiree dans cette section.
#
# ET LA GARDE DE L'OUTIL EST UN FIL-PIEGE, PAS UN NO-OP. Neutraliser la garde
# aurait prouve que quelque chose refuse ; la faire EXPLOSER prouve que le refus
# vient du resolveur, AVANT elle. Sans cette inversion, le test serait vert avec
# le resolveur grand ouvert -- exactement le faux vert que CAV-09 nomme.
#
# LA PORTEE, ET PAS UNE VALEUR. La lecon du 2026-08-21 (`set_dimension_label`
# exerce sur la seule branche `project`) est appliquee dans les deux directions :
# l'appel est parametre sur PLUSIEURS outils, de cinq modules differents, en
# lecture comme en ecriture, et `test_every_resolver_caller_propagates_the_refusal`
# derive du source la liste de TOUS les appelants pour qu'aucun n'avale le refus
# sans une phrase ecrite.
# ---------------------------------------------------------------------------

#: Les appels PAR L'OUTIL qui doivent refuser sur le resolveur seul. Choisis pour
#: couvrir cinq modules, les deux rangs, et les deux facons dont un corps nomme
#: son identite (`identity` deja resolue, ou resolue juste au-dessus du
#: resolveur). Un seul de ces appels prouverait la branche, jamais la portee.
_THROUGH_THE_TOOL = {
    "search_context": lambda: core_main.search_context(
        query="revenue", project_id="proj_foreign"
    ),
    "get_events": lambda: core_main.get_events(project_id="proj_foreign"),
    "get_context_hub": lambda: core_main.get_context_hub(project_id="proj_foreign"),
    "get_knowledge": lambda: core_main.get_knowledge(project_id="proj_foreign"),
    "list_connectors": lambda: core_main.list_connectors(project_id="proj_foreign"),
    "get_daily_report": lambda: core_main.get_daily_report(
        project_id="proj_foreign",
        date_range={"start": "2026-08-01", "end": "2026-08-05"},
    ),
    "flows_get": lambda: core_main.flows_get(
        project_id="proj_foreign", kind="datastream", id="ds_x"
    ),
    # Les trois ECRITURES : une ecriture croisee ne fuit pas une donnee, elle en
    # FABRIQUE une, et son chemin passe par le meme resolveur.
    "add_knowledge": lambda: core_main.add_knowledge(
        project_id="proj_foreign", title="t", body_md="b"
    ),
    "add_context_event": lambda: core_main.add_context_event(
        project_id="proj_foreign", event_date="2026-08-05", type="business", label="x"
    ),
    "submit_feedback": lambda: core_main.submit_feedback(
        project_id="proj_foreign", rating=1
    ),
}


def _arm_the_stranger_at_the_resolver(monkeypatch):
    """Ce que la fixture pose : tout `a_stranger`, MOINS la doublure du resolveur.

    C'est une fonction et pas seulement une fixture parce qu'un
    `monkeypatch.undo()` au milieu d'un test defait aussi la fixture, et un test
    qui continuerait sans la reposer mesurerait un autre chemin de refus.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    token = SimpleNamespace(claims={"sub": "person_stranger"}, client_id="cli")
    monkeypatch.setattr(core_main, "get_access_token", lambda: token)
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: token, raising=False
    )
    monkeypatch.setattr("core.db.request_connection", _a_connection_that_opens)

    def _the_tools_own_guard(*_a, **_k):
        raise AssertionError(
            "la garde de l'outil a ete atteinte : le resolveur a laisse passer un "
            "etranger sur un projet qu'il n'a pas le droit de voir"
        )

    # Les outils de cette section importent le seam par l'alias de `core.main`
    # (`_refuse_unless_project_scope`) ; le resolveur, lui, appelle
    # `core.mcp_scope.refuse_unless_project_scope`. Doubler l'alias ne le touche
    # donc pas -- c'est ce qui rend les deux controles distinguables.
    monkeypatch.setattr(core_main, "_refuse_unless_project_scope", _the_tools_own_guard)
    return token


@pytest.fixture()
def a_stranger_at_the_resolver(monkeypatch):
    """Une identite authentifiee sans droit, et AUCUNE doublure du resolveur."""
    return _arm_the_stranger_at_the_resolver(monkeypatch)


@pytest.mark.parametrize("tool_name", sorted(_THROUGH_THE_TOOL))
def test_the_resolver_refuses_a_stranger_before_the_tools_own_guard(
    tool_name, a_stranger_at_the_resolver, monkeypatch
):
    """Un projet invisible rend l'enveloppe d'un projet ABSENT, et rien d'autre."""
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)

    with pytest.raises(ToolError) as excinfo:
        _THROUGH_THE_TOOL[tool_name]()

    assert PROJECT_NOT_FOUND_CODE in str(excinfo.value), (
        f"{tool_name} a refuse avec une enveloppe qui n'est pas celle d'un projet "
        f"absent : {excinfo.value!r}. Une enveloppe distincte pour << interdit >> "
        "est un oracle d'enumeration deguise en mot de securite."
    )


@pytest.mark.parametrize("tool_name", sorted(_THROUGH_THE_TOOL))
def test_the_resolver_refusal_reaches_no_business_read(
    tool_name, a_stranger_at_the_resolver, monkeypatch
):
    """Le refus tombe AVANT la couche metier, et pas seulement avant la reponse.

    Un refus qui interroge puis jette a deja depense la requete : la charge et le
    temps de reponse repondent alors a la question que le refus existe pour
    laisser sans reponse.
    """
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)

    reads: list[str] = []

    def _tripwire(name):
        def _boom(*_a, **_k):
            reads.append(name)
            raise AssertionError(f"{tool_name} a lu {name} malgre le refus")

        return _boom

    with (
        patch(
            "core.warehouse.query_daily_report",
            _tripwire("warehouse.query_daily_report"),
        ),
        patch(
            "core.context_search.search_context",
            _tripwire("context_search.search_context"),
        ),
        patch("core.flows.get_flow", _tripwire("flows.get_flow")),
        patch(
            "core.context_events.persist_context_event",
            _tripwire("context_events.persist_context_event"),
        ),
        patch("core.context_store.list_topics", _tripwire("context_store.list_topics")),
        patch(
            "core.context_store.create_topic", _tripwire("context_store.create_topic")
        ),
        patch(
            "core.context_store.list_procedures",
            _tripwire("context_store.list_procedures"),
        ),
        patch(
            "core.business_taxonomy.list_taxonomy",
            _tripwire("business_taxonomy.list_taxonomy"),
        ),
    ):
        with pytest.raises(ToolError):
            _THROUGH_THE_TOOL[tool_name]()

    assert not reads, f"{tool_name} a atteint {reads} avant/malgre son refus"


def _an_absent_project(*_args, **_kwargs):
    from core.project_resolver import _raise_not_found  # noqa: PLC0415

    _raise_not_found()


@pytest.mark.parametrize("tool_name", sorted(_THROUGH_THE_TOOL))
def test_absent_denied_and_unavailable_are_one_envelope_at_the_resolver(
    tool_name, a_stranger_at_the_resolver, monkeypatch
):
    """LES TROIS REPONSES SONT LA MEME, AU CARACTERE PRES.

    C'est la moitie << pas d'oracle >> de l'arbitrage, et elle se mesure sur les
    TROIS causes, pas deux : un projet qui n'existe pas, un projet qu'on n'a pas
    le droit de voir, et une base injoignable. La comparaison porte sur
    l'enveloppe entiere -- code ET message -- parce qu'un mot different suffit a
    faire d'un refus un oracle, et qu'un champ ajoute << pour aider >> serait le
    detail qui divulgue l'existence.
    """
    # 1. ABSENT : le magasin ne connait pas ce projet.
    monkeypatch.setattr("core.project_resolver.resolve_project_id", _an_absent_project)
    with pytest.raises(ToolError) as absent:
        _THROUGH_THE_TOOL[tool_name]()

    monkeypatch.undo()
    _arm_the_stranger_at_the_resolver(monkeypatch)

    # 2. INTERDIT : le projet existe, l'appelant n'y a pas droit.
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)
    with pytest.raises(ToolError) as denied:
        _THROUGH_THE_TOOL[tool_name]()

    # 3. INDISPONIBLE : la decision ne peut pas etre prise.
    def _explode(*_a, **_k):
        raise RuntimeError("la base est injoignable")

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _explode)
    with pytest.raises(ToolError) as unavailable:
        _THROUGH_THE_TOOL[tool_name]()

    assert (
        _envelope_of(absent.value)
        == _envelope_of(denied.value)
        == _envelope_of(unavailable.value)
    ), (
        f"{tool_name} distingue les trois causes :\n"
        f"  absent       : {absent.value}\n"
        f"  interdit     : {denied.value}\n"
        f"  indisponible : {unavailable.value}"
    )


def test_a_holder_of_the_project_passes_the_resolver(
    a_stranger_at_the_resolver, monkeypatch
):
    """Le resolveur isole, il ne ferme pas la porte a tout le monde.

    Sans ce test, un `raise` inconditionnel dans `_resolve_project` passerait les
    trois precedents -- et casserait tous les parcours qu'il sert.
    """
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(
            allowed=True, org_id="org_x", reason=None, capability="view"
        ),
    )
    assert _THE_RESOLVER("proj_mine", "person_stranger") == "proj_mine"


def test_a_database_outage_is_not_a_way_in(a_stranger_at_the_resolver, monkeypatch):
    """La resilience de la moitie EXISTENCE n'ouvre pas la moitie ACCES.

    `_resolve_project` laisse passer la valeur quand la base est injoignable, et
    c'est une posture ecrite depuis 7.1 (les doublures de connexion sans base).
    Elle n'est pas un chemin d'entree : la resolution d'acces tourne ensuite,
    quoi qu'il soit arrive au-dessus, et elle echoue FERME. Sans cette ligne, le
    moyen le moins cher d'atteindre le projet du voisin serait de faire tomber la
    base -- le defaut que `core/mcp_scope.py` nomme dans sa propriete 2.
    """

    @contextmanager
    def _a_connection_that_never_opens(_identity):
        raise RuntimeError("la base est injoignable")
        yield  # pragma: no cover -- jamais atteint

    monkeypatch.setattr("core.db.request_connection", _a_connection_that_never_opens)
    with pytest.raises(ToolError) as excinfo:
        _THE_RESOLVER("proj_foreign", "person_stranger")

    assert PROJECT_NOT_FOUND_CODE in str(excinfo.value)


#: LES SEULS APPELANTS AUTORISES A AVALER LE REFUS, ET CE QU'ILS SERVENT ALORS.
#:
#: Meme modele que `_NO_TENANT_SCOPE` et `_VIEW_RANK_WRITES` : un ledger nom ->
#: RAISON, a sens unique, que le test ci-dessous fait vieillir bruyamment. Le
#: defaut qu'il empeche est celui du 2026-08-17 sur `get_procedure` : une garde
#: qui refusait bien, dans le bloc de ses remarques, pendant que le corps de la
#: Skill partait quand meme. Une entree coute une phrase VERIFIABLE disant ce que
#: l'appelant sert sur un refus -- et la seule reponse acceptable est << rien du
#: projet nomme >>.
_MAY_SWALLOW_THE_REFUSAL = {
    "cards_mcp.py:_project_topic_catalog": (
        "Sert le catalogue par DEFAUT (`answerable_topics.default_catalog`) avec "
        "la raison `CATALOG_ACCESS_DENIED_REASON` : du vocabulaire produit livre "
        "avec le produit, identique pour tout le monde. Aucune ligne du projet "
        "nomme ne part."
    ),
}


def _callers_that_swallow_the_resolver_refusal() -> set[str]:
    """Derive du source : qui appelle `_resolve_project` sous un `except` muet.

    Un `except` qui ne re-leve pas AVALE le refus. La liste est calculee a chaque
    execution parce qu'une liste ecrite a la main vieillirait exactement comme
    celle du plan d'epic 53 -- quatre outils annonces, neuf mesures cinq jours
    plus tard.
    """
    import ast
    from pathlib import Path

    core = Path(core_main.__file__).parent
    swallowing: set[str] = set()
    for path in sorted(core.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        enclosing: dict[int, str] = {}
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(func):
                enclosing.setdefault(id(child), func.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls_it = any(
                isinstance(inner, ast.Call)
                and (
                    getattr(inner.func, "id", None) == "_resolve_project"
                    or getattr(inner.func, "attr", None) == "_resolve_project"
                )
                for statement in node.body
                for inner in ast.walk(statement)
            )
            if not calls_it:
                continue
            muffles = any(
                not any(isinstance(inner, ast.Raise) for inner in ast.walk(handler))
                for handler in node.handlers
            )
            if muffles:
                swallowing.add(f"{path.name}:{enclosing.get(id(node), '<module>')}")
    return swallowing


def test_every_resolver_caller_propagates_the_refusal():
    """UN APPELANT QUI AVALE LE REFUS EST LE DEFAUT `get_procedure`, PAS UNE GARDE.

    C'est la moitie PORTEE de cette section. Les tests ci-dessus prouvent que le
    resolveur refuse ; celui-ci prouve que le refus ARRIVE -- qu'aucun des ~30
    appelants ne l'attrape pour servir quand meme le contenu du projet nomme.
    Sans lui, la preuve porterait sur les dix appels ecrits a la main et sur eux
    seuls : une valeur exercee, jamais la portee.
    """
    swallowing = _callers_that_swallow_the_resolver_refusal()
    unexpected = sorted(swallowing - set(_MAY_SWALLOW_THE_REFUSAL))
    assert not unexpected, (
        "appelant(s) de `_resolve_project` qui avalent son refus :\n  "
        + "\n  ".join(unexpected)
        + "\n\nSoit l'appelant laisse remonter `project_not_found`, soit il entre "
        "dans `_MAY_SWALLOW_THE_REFUSAL` avec la phrase qui dit ce qu'il sert "
        "alors -- et la seule reponse acceptable est << rien du projet nomme >>. "
        "Un refus qui ne retire qu'un sous-champ est le defaut mesure sur "
        "`get_procedure` le 2026-08-17."
    )
    stale = sorted(set(_MAY_SWALLOW_THE_REFUSAL) - swallowing)
    assert not stale, (
        "appelant(s) inscrits comme avalant le refus mais qui le propagent "
        "desormais :\n  "
        + "\n  ".join(stale)
        + "\n\nRetire-les : la reparation doit compter."
    )
    empty = sorted(
        name
        for name, why in _MAY_SWALLOW_THE_REFUSAL.items()
        if not (why or "").strip()
    )
    assert not empty, (
        "entree(s) de `_MAY_SWALLOW_THE_REFUSAL` sans raison ecrite :\n  "
        + "\n  ".join(empty)
    )
