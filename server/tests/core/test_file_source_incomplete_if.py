"""Les quatre critères `Incomplete if` de `file-source-ingestion` que le ledger
n'avait JAMAIS jugés.

Mesuré le 2026-08-04 : le document porte 12 critères, le ledger portait 8 entrées
(`0,2,3,4,5,7,9,11`), toutes `false`. Les indices **1, 6, 8 et 10 n'avaient aucune
entrée** — et `finished_work_audit.py` compte une entrée absente comme un critère
OUVERT. Le compte « 4/12 ouverts » mélangeait donc « non bâti » et « non regardé ».

Les quatre étaient bâtis. Ce fichier est le geste qui manquait : les JUGER, en
exécutant, pour que la fermeture du ledger cite une commande et pas une lecture.

Un fichier de jugement n'est pas un doublon des suites de story. Il assere la
phrase du critère, mot pour mot, contre le code — pas l'unité qui l'implémente.
Quand quelqu'un rouvrira un de ces quatre, c'est ici qu'il verra ce qui avait été
prouvé, et par quoi.

Tout OFFLINE : chaque fonction éprouvée ici est pure ou prend un `conn` mocké.
"""

from __future__ import annotations

import hashlib

import pytest
from core.file_source_adaptation import check_adaptation_drift
from core.file_source_producer import detect_source_drift
from core.file_source_template import (
    UnknownCanonicalField,
    compute_content_hash,
    validate_template_contract,
)

_BASE_CONTRACT = {
    "kind": "catalog",
    "class": "planned",
    "grain": "daily",
    "placement": {"metric": "mdm_cost", "period": "mdm_date", "dimension": ["mdm_channel"]},
    "required_fields": ["mdm_cost", "mdm_date"],
    "optional_fields": [],
}


# ===========================================================================
# [1] « a Template cannot be extended with the metrics a client wants on the
#      dimensions it declares, or that extension is not a governed, versioned act »
# ===========================================================================


def test_1_extending_a_template_produces_a_NEW_version_never_an_edit():
    """L'extension change le hash de contenu, donc c'est une version, pas une retouche.

    `app.file_source_templates` est content-hashé et immuable : deux contrats qui
    diffèrent d'un champ ne peuvent pas partager une ligne. C'est ce qui fait de
    l'extension un acte VERSIONNÉ — la moitié « versionné » du critère.
    """
    base = compute_content_hash(validate_template_contract(dict(_BASE_CONTRACT)))
    extended = dict(_BASE_CONTRACT, optional_fields=["mdm_impressions"])
    after = compute_content_hash(validate_template_contract(extended))
    assert base != after, "une extension qui ne change pas le hash écraserait la version d'avant"


def test_1_the_same_contract_reached_twice_keeps_ONE_version():
    """Le corollaire : sans extension, pas de nouvelle version.

    Sinon « versionné » dégénère en « une ligne par clic », et l'historique ne dit
    plus quand la cible a réellement changé.
    """
    once = compute_content_hash(validate_template_contract(dict(_BASE_CONTRACT)))
    twice = compute_content_hash(
        validate_template_contract(dict(_BASE_CONTRACT, required_fields=["mdm_date", "mdm_cost"]))
    )
    # L'ordre déclaré ne fait pas une version : le contrat est normalisé d'abord.
    assert once == twice


def test_1_an_unregistered_field_is_refused_which_is_the_GOVERNED_half():
    """Un client n'étend pas vers n'importe quoi : la cible reste le vocabulaire canonique.

    `_assert_required_fields_registered` (file_source_template.py:342) exige que
    chaque id déclaré soit un `mdm_canonical_fields` ACTIF dans la portée. C'est la
    moitié « gouverné » du critère : l'extension est ouverte au client, le
    vocabulaire ne l'est pas.
    """
    from core.file_source_template import _assert_required_fields_registered

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): self._rows = [("mdm_cost",)]  # mdm_date absent du registre
        def fetchall(self): return self._rows

    class _Conn:
        def cursor(self): return _Cur()

    with pytest.raises(UnknownCanonicalField):
        _assert_required_fields_registered(
            _Conn(), project_id="proj_EXAMPLE", field_ids=["mdm_cost", "mdm_date"]
        )


# ===========================================================================
# [8] « drift re-validation has no specified trigger and no specified outcome,
#      so nothing can legitimately call it »
# ===========================================================================
#
# Le critère demande DEUX choses nommées : quand ça se déclenche, et ce que ça
# produit. Les deux chemins producteurs les nomment, et fail CLOSED vers la porte.


def test_8_a_disappeared_required_source_column_TRIGGERS_revalidation():
    template = {"contract": dict(_BASE_CONTRACT)}
    mapping = {"Cout net": "mdm_cost", "Jour": "mdm_date"}
    verdict = detect_source_drift(template, mapping, current_columns=["Jour"])
    assert verdict["status"] == "needs_revalidation"
    assert "Cout net" in verdict["missing_required_sources"]


def test_8_an_added_or_reordered_column_is_NOT_a_trigger():
    """Le déclencheur est spécifié, donc il est aussi spécifié qu'il ne tire PAS.

    Un déclencheur qui part sur une colonne ajoutée renverrait chaque fichier à la
    porte et personne ne l'appellerait plus — c'est l'autre façon dont ce critère
    reste vrai.
    """
    template = {"contract": dict(_BASE_CONTRACT)}
    mapping = {"Cout net": "mdm_cost", "Jour": "mdm_date"}
    verdict = detect_source_drift(
        template, mapping, current_columns=["Jour", "Commentaire", "Cout net"]
    )
    assert verdict["status"] == "ok"
    assert "Commentaire" in verdict["added_columns"]


def test_8_the_adaptation_path_names_the_same_outcome_on_its_OUTPUT():
    """Une adaptation parse librement : sa dérive se juge sur ce qu'elle PRODUIT.

    Deux chemins, un seul verdict nommé (`needs_revalidation`) — sans quoi
    « rappeler la revalidation » voudrait dire deux choses selon le producteur.
    """
    template = {"contract": dict(_BASE_CONTRACT)}
    ok = check_adaptation_drift(template, [{"mdm_cost": "1", "mdm_date": "2026-03-01"}])
    assert ok["status"] == "ok"
    drifted = check_adaptation_drift(template, [{"mdm_cost": "1"}])
    assert drifted["status"] == "needs_revalidation"
    assert drifted["missing_required"] == ["mdm_date"]
    assert drifted["invalid_row_indexes"] == [0]


def test_8_an_empty_output_is_drift_not_a_clean_pass():
    """Zéro ligne produite ne prouve pas l'absence de champ manquant.

    Un `all()` sur une liste vide rend `True` : c'est la façon exacte dont ce genre
    de garde passe au vert en ne regardant rien.
    """
    template = {"contract": dict(_BASE_CONTRACT)}
    assert check_adaptation_drift(template, [])["status"] == "needs_revalidation"


# ===========================================================================
# [6] « the same file yields different results by upload and by email »
# ===========================================================================
#
# La parité ne se prouve pas en montrant que chaque porte marche : deux portes
# qui écrivent chacune correctement peuvent écrire deux choses. Ce qui la prouve,
# c'est qu'il n'y a QU'UN chemin — et c'est le cas, par construction :
#
#   upload  : _confirm_csv_excel_import  ->  process_inbound_delivery
#   email   : inbound/bridge.py          ->  process_inbound_delivery
#                                            -> ingest_inbound_file -> run_import
#
# L'upload n'a pas d'import à lui. C'est ce qu'il faut empêcher de « simplifier ».


def _source(name: str) -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "core"
    return (root / name).read_text(encoding="utf-8")


#: La porte d'upload a demenage sous AD-40 : `_confirm_csv_excel_import` vit
#: dans `file_import_api.py` et `admin_api.py` n'en assemble plus que la route.
#: Les deux fichiers sont interroges, pas le nouveau seul -- un garde qui suit
#: le code sans garder l'ancien emplacement laisse revenir ce qu'il refusait.
_UPLOAD_DOOR = ("admin_api.py", "file_import_api.py")


def test_6_the_upload_door_has_no_import_path_of_its_own():
    """La porte d'upload ne doit JAMAIS appeler `run_import` directement.

    Le jour où elle le fait, l'upload a repris un chemin à lui et la parité est
    perdue en silence — les deux portes continueront de « marcher ».
    """
    for name in _UPLOAD_DOOR:
        calls = [
            line for line in _source(name).splitlines()
            if "run_import(" in line and not line.lstrip().startswith("#")
        ]
        assert calls == [], f"{name} a repris un chemin d'import a lui : {calls}"


def test_6_both_doors_reach_the_one_shared_delivery_function():
    upload = "".join(_source(name) for name in _UPLOAD_DOOR)
    assert "from core.inbound_processing import" in upload
    assert "core.managed_file_dispatch" in upload

    bridge = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "inbound" / "bridge.py"
    ).read_text(encoding="utf-8")
    assert "from core.inbound_processing import process_inbound_delivery" in bridge

    # Et le bout partagé atteint bien l'unique moteur d'import.
    assert "from core.csv_excel_import import run_import" in _source("inbound_ingest.py")


def test_6_the_ONE_deliberate_difference_is_the_operator_override_only():
    """Une seule chose diffère, et elle est voulue : `force_empty_publish`.

    Un fichier arrivé par e-mail est non surveillé — il ne peut pas porter la
    dérogation « publier un import vide » qu'un opérateur assume explicitement à
    l'écran. Le nommer ici est la différence entre une exception documentée et un
    écart qu'une session future prendra pour un bug et « réparera » en accordant
    la dérogation à l'inbound.
    """
    ingest = _source("inbound_ingest.py")
    assert "force_empty_publish=False" in ingest
    # La POLITIQUE, elle, est lue au même endroit que le chemin direct.
    assert "_read_allow_empty_publication" in ingest


# ===========================================================================
# [10] « a Template can be locked without a human having confirmed a passing
#       preview, or that confirmation cannot be proven from the Template's own record »
# ===========================================================================


def test_10_the_store_refuses_to_lock_on_a_gate_that_did_not_pass():
    """La moitié machine : pas de verrou sans gate passé.

    Mesuré au passage, et c'est la raison pour laquelle ce critère méritait d'être
    regardé deux fois : la docstring de `lock_adaptation_template` AFFIRME
    « a human confirmed it (22.19) » alors que la fonction ne vérifie que le gate.
    La garde humaine est réelle, mais elle est dans la PORTE
    (`file_source_template_api.py`), pas ici — un lecteur qui s'arrête à la
    docstring conclut faux dans un sens comme dans l'autre.
    """
    from unittest.mock import patch

    from core.file_source_adaptation import lock_adaptation_template
    from core.file_source_gate import GateNotPassed

    with patch("core.file_source_adaptation.create_file_source_template") as create:
        with pytest.raises(GateNotPassed):
            lock_adaptation_template(
                object(), project_id="p", org_id="o", template_code="BESPOKE",
                py_source="def adapt(b, t): return {'rows': [], 'rejected': []}\n",
                placement_class="actual", placement=dict(_BASE_CONTRACT["placement"]),
                required_fields=["mdm_cost"], grain="daily", created_by="u",
                gate_result={"passed": False, "ambiguities": []},
                sample_content_hash="0" * 64,
                datastream_id="ds_1", mapping_version_id="dmap_1", plan_version_id="dpv_1",
            )
    create.assert_not_called()


def test_10_accepted_warnings_require_a_written_reason():
    """La moitié humaine, côté magasin : accepter une ambiguïté SANS la motiver est refusé.

    Une confirmation qui accepte des avertissements sans dire pourquoi n'est pas
    une confirmation : c'est un clic. La raison est ce qui reste au dossier.
    """
    from unittest.mock import patch

    from core.file_source_adaptation import lock_adaptation_template

    with patch("core.file_source_adaptation.create_file_source_template") as create:
        with pytest.raises(ValueError, match="warning_reason"):
            lock_adaptation_template(
                object(), project_id="p", org_id="o", template_code="BESPOKE",
                py_source="def adapt(b, t): return {'rows': [], 'rejected': []}\n",
                placement_class="actual", placement=dict(_BASE_CONTRACT["placement"]),
                required_fields=["mdm_cost"], grain="daily", created_by="u",
                gate_result={"passed": True, "ambiguities": [{"code": "x"}]},
                accepted_warnings=[{"code": "x"}], warning_reason="   ",
                sample_content_hash="0" * 64,
                datastream_id="ds_1", mapping_version_id="dmap_1", plan_version_id="dpv_1",
            )
    create.assert_not_called()


def test_10_the_confirmation_is_PROVABLE_from_the_template_record():
    """La seconde moitié du critère, littéralement.

    La preuve doit lier trois choses, sinon elle ne prouve rien : QUEL template
    (`template_id` + `template_content_hash`), sur QUEL échantillon
    (`sample_content_hash`), et par QUI (`actor`). Sans le hash d'échantillon, la
    confirmation survivrait à un changement de fichier.
    """
    from unittest.mock import patch

    from core.file_source_adaptation import lock_adaptation_template

    seen: dict = {}

    def _fake_create(conn, **kwargs):
        return {"id": "fst_1", "content_hash": "b" * 64, "version_number": 1}

    def _fake_confirm(conn, **kwargs):
        seen.update(kwargs)
        return {"confirmed": True, "operation_id": "op_1"}

    sample = hashlib.sha256(b"vendor,cost\nGoogle,100").hexdigest()
    with patch("core.file_source_adaptation.create_file_source_template", _fake_create), patch(
        "core.file_source_gate.confirm_adaptation_template", _fake_confirm
    ):
        lock_adaptation_template(
            object(), project_id="p", org_id="o", template_code="BESPOKE",
            py_source="def adapt(b, t): return {'rows': [], 'rejected': []}\n",
            placement_class="actual", placement=dict(_BASE_CONTRACT["placement"]),
            required_fields=["mdm_cost"], grain="daily", created_by="operator@example.com",
            gate_result={"passed": True, "ambiguities": []},
            sample_content_hash=sample, sample_filename="plan-fr.csv",
            datastream_id="ds_1", mapping_version_id="dmap_1", plan_version_id="dpv_1",
        )

    evidence = seen["evidence"]
    assert evidence["template_id"] == "fst_1"
    assert evidence["template_content_hash"] == "b" * 64
    assert evidence["sample_content_hash"] == sample
    assert evidence["actor"] == "operator@example.com"
    assert evidence["confirmed_at"]


def test_10_the_door_recomputes_the_gate_instead_of_trusting_the_caller():
    """Ce qui ferme vraiment la première moitié du critère, et qui n'est PAS dans le magasin.

    `file_source_template_api.py` rejoue l'adaptation et relit le gate de SA
    propre exécution (`gate = reanalysis.get("gate")`) avant de verrouiller. Un
    appelant qui poste `{"gate": {"passed": true}}` ne verrouille rien. Épinglé
    sur la SOURCE parce que la route est un chemin HTTP à trois dépendances de
    base : ce qu'on doit empêcher, c'est qu'une session suivante « simplifie » en
    faisant confiance au corps de la requête.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2] / "core" / "file_source_template_api.py"
    text = source.read_text(encoding="utf-8")
    assert 'gate = reanalysis.get("gate")' in text, (
        "la porte doit relire le gate de sa propre reanalyse, jamais celui du corps"
    )
    assert '"gate_not_passed"' in text
    assert '"warnings_require_decision"' in text
