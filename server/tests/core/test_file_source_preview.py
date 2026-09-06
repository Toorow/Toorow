"""Story 22.19 -- l'apercu rend ce que l'ecran de revue doit montrer.

POURQUOI CE FICHIER EXISTE. `FileSourceOnboardingReview.tsx` est ecrit depuis des
semaines, a son propre test vert, et n'est monte nulle part. Le tracker en
concluait un travail de MONTAGE. Mesure du 2026-08-01 : meme monte, il
n'afficherait rien. La seule route d'apercu appelle `build_preview` -- l'apercu
CSV/Excel brut -- qui ne resout pas le producteur source-fichier, n'appelle pas
`evaluate_landing_gate`, et ne rend ni placement, ni confiance par champ.

Le seul appelant du gate hors son module etait `run_import` : l'IMPORT. Une
personne ne pouvait donc decouvrir qu'un champ requis manque qu'en tentant
l'import -- c'est-a-dire apres coup, ce que la cible refuse
(`datastream-workbench-and-wizard.md`, etape 5 : « bounded masked sample, parse
errors, schema/profile, import coverage, DQ gates » AVANT publication).

CE QUE PROUVE CE FICHIER : l'apercu compose les trois briques deja livrees --
`recognize_columns` (22.17), `stamp_placement` (22.14), `evaluate_landing_gate`
(22.15) -- sans en reecrire aucune. C'est du cablage, pas de la conception ; les
trois existaient et aucune n'etait atteinte depuis l'apercu.

CE QU'IL NE PROUVE PAS : que l'ecran affiche correctement le resultat. Le
montage et la migration MUI -> components/ui sont l'etape suivante, et cette
etape-ci est son prerequis -- un ecran sans donnees ne se monte pas.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from tests.core.test_file_source_wiring import (  # noqa: E402
    CSV_BYTES,
    DATASTREAM_ID,
    MAPPING_PAYLOAD,
    MAPPING_PAYLOAD_MISSING_REQUIRED,
    MAPPING_VERSION_ID,
    PROJECT_ID,
    TEMPLATE_CONTRACT,
    _FakeConn,
    _responses,
)


def _preview(mapping_payload=MAPPING_PAYLOAD, *, template_contract=None, source_ref=None):
    from core.csv_excel_import import build_file_source_preview

    kwargs = {"mapping_payload": mapping_payload, "template_contract": template_contract}
    if source_ref is not None:
        kwargs["source_ref"] = source_ref
    conn = _FakeConn(_responses(**kwargs))
    return build_file_source_preview(
        conn,
        CSV_BYTES,
        project_id=PROJECT_ID,
        datastream_id=DATASTREAM_ID,
        mapping_version_id=MAPPING_VERSION_ID,
        filename="plan.csv",
    )


def test_a_datastream_without_a_template_gets_no_file_source_preview():
    """Le chemin CSV/Excel reste EXACTEMENT ce qu'il etait : None, pas un objet vide.

    Un apercu file-source fabrique pour un datastream qui n'en a pas ferait
    croire a une chaine qui n'existe pas pour lui.
    """
    assert _preview(source_ref="file_upload") is None


def test_the_preview_reports_the_gate_before_any_import():
    """Le refus est visible AVANT l'import, pas decouvert pendant.

    `clicks` est requis par le template et aucune liaison confirmee ne l'atteint.
    """
    preview = _preview(MAPPING_PAYLOAD_MISSING_REQUIRED)

    assert preview is not None
    assert preview["gate"]["passed"] is False
    assert "clicks" in preview["gate"]["missing_required"]


def test_a_complete_mapping_passes_the_gate_in_the_preview():
    """Le controle : sans lui, le test precedent passerait en bloquant tout."""
    preview = _preview(MAPPING_PAYLOAD)

    assert preview is not None
    assert preview["gate"]["passed"] is True
    assert preview["gate"]["missing_required"] == []


def test_the_preview_carries_one_field_per_source_column_with_its_confidence():
    """Ce que l'ecran affiche par ligne : la colonne, sa cible, sa confiance.

    Les en-tetes du fichier sont en francais (`Date`, `Clics`) -- c'est la raison
    d'etre d'un template, et donc le cas qui doit etre rendu.
    """
    preview = _preview(MAPPING_PAYLOAD)

    by_column = {f["source_column"]: f for f in preview["fields"]}
    assert set(by_column) == {"Date", "Clics"}
    for field in by_column.values():
        assert "canonical_target" in field
        assert "status" in field
        # Jamais un pourcentage fabrique : None est une valeur admise, pas 0.
        assert "confidence" in field


def test_the_preview_carries_the_declared_placement():
    """La classe de matrice (22.14) est ce qui rend le fichier reconciliable."""
    preview = _preview(MAPPING_PAYLOAD)

    assert preview["placement"]["class"] == "actual"
    assert preview["placement"]["metric"] == "clicks"
    assert preview["placement"]["period"] == "day"


def test_a_template_with_no_class_is_reported_not_raised():
    """AD-6 dans un apercu se RACONTE ; il ne leve pas.

    `run_import` refuse l'import -- c'est juste, il est sur le point d'ecrire.
    L'apercu existe precisement pour dire pourquoi ca refusera : lever ici
    rendrait un 500 la ou la personne attend une explication.
    """
    contract = {k: v for k, v in TEMPLATE_CONTRACT.items() if k != "class"}
    preview = _preview(MAPPING_PAYLOAD, template_contract=contract)

    assert preview is not None
    assert preview["blocked"] is True
    assert preview["reason"] == "no_placement_class"
    assert preview["gate"] is None
    assert preview["placement"] is None


def test_the_preview_writes_nothing():
    """Un apercu qui ouvrirait une ligne de registre ne serait plus un apercu."""
    from core.csv_excel_import import build_file_source_preview

    conn = _FakeConn(_responses())
    build_file_source_preview(
        conn,
        CSV_BYTES,
        project_id=PROJECT_ID,
        datastream_id=DATASTREAM_ID,
        mapping_version_id=MAPPING_VERSION_ID,
        filename="plan.csv",
    )
    assert conn.committed is False


def test_the_preview_reuses_the_import_gate_rather_than_scoring_again():
    """Un second scoreur est la faute que 22.20 a deja commise.

    L'apercu et l'import doivent rendre le MEME verdict sur les memes entrees :
    sinon une personne voit vert et decouvre rouge, ce qui est pire que pas
    d'apercu du tout.
    """
    from core.file_source_gate import evaluate_landing_gate
    from core.file_source_producer import produce

    preview = _preview(MAPPING_PAYLOAD_MISSING_REQUIRED)

    mapping = {"Date": "day"}
    result = produce(CSV_BYTES, TEMPLATE_CONTRACT, mapping)
    direct = evaluate_landing_gate(
        TEMPLATE_CONTRACT,
        mapping,
        columns=[spec.name for spec in result.columns],
        rows=result.rows,
    )
    assert preview["gate"]["passed"] == direct["passed"]
    assert preview["gate"]["missing_required"] == direct["missing_required"]


@pytest.mark.parametrize("key", ["fields", "placement", "gate", "blocked", "reason"])
def test_the_preview_shape_is_stable(key: str):
    """Les cinq cles que la console lit. Une cle absente est un ecran casse."""
    preview = _preview(MAPPING_PAYLOAD)
    assert key in preview


# ---------------------------------------------------------------------------
# Le seam : la ROUTE le porte-t-elle ? (sinon c'est une fonction sans appelant)
# ---------------------------------------------------------------------------


def _seam_response(responses: dict):
    """POST /api/datastreams/{id}/imports/preview contre l'app ASGI reelle."""
    import base64
    from contextlib import contextmanager
    from unittest.mock import AsyncMock, patch

    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    conn = _FakeConn(responses)

    @contextmanager
    def _get_connection():
        yield conn

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "o@example.com"))),
        patch("core.project_access.identity_has_project_role", return_value=True),
        patch("core.db.get_connection", new=_get_connection),
    ):
        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        return client.post(
            f"/api/datastreams/{DATASTREAM_ID}/imports/preview",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "project_id": PROJECT_ID,
                "file_base64": base64.b64encode(CSV_BYTES).decode(),
                "filename": "plan.csv",
                "mapping_version_id": MAPPING_VERSION_ID,
            },
        )


def test_the_route_carries_the_file_source_preview():
    """AI-88 en une phrase : une fonction que la route n'appelle pas ne sert a rien.

    C'est exactement le defaut que toute la chaine source-fichier portait --
    `run_import` acceptait un producteur depuis 22.12 et aucun de ses trois
    appelants reels n'en passait un.
    """
    resp = _seam_response(_responses())
    assert resp.status_code == 200
    payload = resp.json()
    assert "file_source" in payload, "la route ne porte pas l'apercu source-fichier"
    assert payload["file_source"] is not None
    assert payload["file_source"]["gate"]["passed"] is True
    assert payload["file_source"]["placement"]["class"] == "actual"


def test_the_route_reports_a_blocking_gate_rather_than_failing():
    """Un champ requis non lie se DIT dans l'apercu ; il ne casse pas la reponse."""
    resp = _seam_response(_responses(mapping_payload=MAPPING_PAYLOAD_MISSING_REQUIRED))
    assert resp.status_code == 200
    gate = resp.json()["file_source"]["gate"]
    assert gate["passed"] is False
    assert "clicks" in gate["missing_required"]


def test_a_datastream_without_a_template_keeps_the_preview_it_had():
    """Le chemin CSV/Excel est inchange : meme 200, meme corps, `file_source` a None."""
    resp = _seam_response(_responses(source_ref="file_upload"))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["file_source"] is None
    # Les cles historiques de l'apercu sont toujours la.
    for key in ("format", "columns", "row_count", "preview_rows", "content_hash"):
        assert key in payload


# ---------------------------------------------------------------------------
# 22.16 -- la derive se VOIT avant l'import (et ne decide rien)
# ---------------------------------------------------------------------------


def _preview_bytes(data: bytes, mapping_payload=MAPPING_PAYLOAD):
    from core.csv_excel_import import build_file_source_preview

    conn = _FakeConn(_responses(mapping_payload=mapping_payload))
    return build_file_source_preview(
        conn,
        data,
        project_id=PROJECT_ID,
        datastream_id=DATASTREAM_ID,
        mapping_version_id=MAPPING_VERSION_ID,
        filename="plan.csv",
    )


def test_the_preview_reports_source_drift():
    """`detect_source_drift` existait depuis 22.16 et n'avait AUCUN appelant.

    Le fichier arrive avec des en-tetes renommes : les colonnes SOURCE que le
    template lie a un champ requis ont disparu. AD-8 dit que ce fichier doit
    re-entrer dans le gate pour re-confirmation humaine, jamais etre re-mappe en
    silence -- et une personne doit pouvoir le voir AVANT d'importer.
    """
    renamed = b"Datum,Klicks\n2026-07-01,5\n2026-07-02,7\n"
    preview = _preview_bytes(renamed)

    assert preview is not None
    assert preview["drift"]["status"] == "needs_revalidation"
    assert sorted(preview["drift"]["missing_required_sources"]) == ["Clics", "Date"]


def test_an_unchanged_file_reports_no_drift():
    """Le controle : sans lui, le test precedent passerait en criant a la derive."""
    preview = _preview_bytes(CSV_BYTES)

    assert preview["drift"]["status"] == "ok"
    assert preview["drift"]["missing_required_sources"] == []


def test_drift_reenters_the_gate_before_confirmation():
    """A disappeared required source never remains confirmable (AD-8)."""
    renamed = b"Datum,Klicks\n2026-07-01,5\n"
    preview = _preview_bytes(renamed)

    assert preview["blocked"] is True
    assert preview["reason"] == "source_drift"
    assert preview["gate"] is not None
def test_explicit_missing_template_binding_fails_closed():
    from core.csv_excel_import import CsvExcelImportError, build_file_source_preview

    responses = _responses()
    responses["template"] = None
    conn = _FakeConn(responses)
    with pytest.raises(CsvExcelImportError) as exc:
        build_file_source_preview(
            conn,
            CSV_BYTES,
            project_id=PROJECT_ID,
            datastream_id=DATASTREAM_ID,
            mapping_version_id=MAPPING_VERSION_ID,
            filename="plan.csv",
        )
    assert exc.value.code == "file_source_template_unavailable"


def test_the_preview_reads_the_four_international_axes_of_a_file():
    """38-16 AC3 : « Preview REPORTS ... date/timezone, currency/unit ... ».

    Le verbe est `reports`. Rien ici n'est un choix : pays, langue, devise et
    fuseau sortent d'une norme internationale predefinie, identique pour tout
    client, et les listes d'alias gouvernees existent pour que les conventions
    que les outils externes ecrivent -- `Andorra`, `afr`, `dirham`,
    `europe/paris` -- resolvent vers le meme code pour tout le monde.

    LES QUATRE, pas les deux que l'AC nomme : c'est UN mecanisme sur quatre
    registres, et n'en brancher que deux laisserait la colonne pays et la colonne
    langue d'un fichier client non lues au prochain ecran, pour la meme raison.

    Mesure sur l'EFFET -- ce que le rapport contient -- et non sur l'appel d'une
    fonction : un rapport qui nommerait la bonne colonne avec le mauvais code
    passerait un controle d'appel.
    """
    data = (
        b"Date,Clics,Pays,Devise,Langue,Fuseau\n"
        b"2026-07-01,5,Andorra,EUR,Afrikaans,europe/paris\n"
        b"2026-07-02,7,FR,dirham,afr,Europe/Kiev\n"
    )
    preview = _preview_bytes(data)

    by_column = {item["source_column"]: item["readings"] for item in preview["vocabularies"]}

    assert "Pays" in by_column, f"la colonne pays n'est pas lue : {list(by_column)}"
    pays = next(r for r in by_column["Pays"] if r["vocabulary"] == "country")
    assert pays["resolved"] == 2
    assert sorted(pays["canonical_values"]) == ["AD", "FR"]

    devise = next(r for r in by_column["Devise"] if r["vocabulary"] == "currency")
    assert sorted(devise["canonical_values"]) == ["AED", "EUR"], "un alias d'outil n'est pas lu"

    langue = next(r for r in by_column["Langue"] if r["vocabulary"] == "language")
    assert langue["canonical_values"] == ("af",) or list(langue["canonical_values"]) == ["af"]

    fuseau = next(r for r in by_column["Fuseau"] if r["vocabulary"] == "timezone")
    # `europe/paris` en minuscules ET le lien retire `Europe/Kiev` : les deux sont
    # le MEME identifiant ecrit autrement, pas des zones nouvelles.
    assert sorted(fuseau["canonical_values"]) == ["Europe/Kyiv", "Europe/Paris"]


def test_a_blocked_preview_still_reports_what_it_read():
    """Les branches bloquees existent pour donner de quoi LEVER le blocage.

    « votre colonne de dates porte un fuseau que rien ne reconnait » est souvent
    le blocage lui-meme ; le taire sur le seul chemin ou la personne le lit
    rendrait le rapport inutile a l'instant ou il sert.
    """
    renamed = b"Datum,Klicks,Pays\n2026-07-01,5,FR\n"
    preview = _preview_bytes(renamed)

    assert preview["blocked"] is True
    assert any(item["source_column"] == "Pays" for item in preview["vocabularies"])


# ---------------------------------------------------------------------------
# Story 60.6 -- la personne reconnait SON fichier, et lit ce que chaque colonne
# devient. `recognize_columns` recoit deja les lignes d'echantillon
# (`csv_excel_import.build_file_source_preview`) et n'en rendait aucune valeur :
# devant 46 en-tetes inconnus, l'ecran ne donnait rien a reconnaitre.
# ---------------------------------------------------------------------------


def test_every_previewed_column_carries_an_example_value_from_the_sample():
    """La valeur est LUE, jamais fabriquee : c'est la premiere ligne du fichier."""
    preview = _preview(MAPPING_PAYLOAD)

    by_column = {f["source_column"]: f for f in preview["fields"]}
    assert by_column["Date"]["sample_value"] == "2026-07-01"
    assert by_column["Clics"]["sample_value"] == "5"


def test_a_column_the_sample_leaves_empty_says_no_sample_value():
    """Jamais une chaine vide, jamais un `0` : les deux se lisent comme une donnee."""
    from core.column_treatments import NO_SAMPLE_VALUE

    preview = _preview_bytes(b"Date,Clics,Commentaire\n2026-07-01,5,\n")

    by_column = {f["source_column"]: f for f in preview["fields"]}
    assert by_column["Commentaire"]["sample_value"] == NO_SAMPLE_VALUE
    # Le controle : une colonne renseignee ne prend pas la meme phrase.
    assert by_column["Clics"]["sample_value"] == "5"


def test_every_previewed_column_carries_its_treatment_word():
    """Les trois statuts du recognizer restent la source du mot (plan de test 5).

    `Clics` est `matched` : elle devient `clicks`, donc `Direct`. `Commentaire`
    est `unmatched` -- le recognizer refuse de la forcer (« never force-mapped »)
    -- et l'appeler `Direct` reclamerait une liaison qui n'existe pas. C'est
    l'inventaire de ce qui reste a decider, pas une colonne traitee.
    """
    from core.column_treatments import DIRECT, NOT_DECIDED

    preview = _preview_bytes(b"Date,Clics,Commentaire\n2026-07-01,5,note\n")

    by_column = {f["source_column"]: f for f in preview["fields"]}
    assert by_column["Clics"]["status"] == "matched"
    assert by_column["Clics"]["treatment"] == DIRECT
    assert by_column["Commentaire"]["status"] == "unmatched"
    assert by_column["Commentaire"]["treatment"] == NOT_DECIDED


def test_an_excluded_column_is_still_listed_and_says_so():
    """« Refuse : cacher les colonnes ecartees. » L'inventaire reste complet."""
    from copy import deepcopy

    from core.column_treatments import EXCLUDED

    payload = deepcopy(MAPPING_PAYLOAD)
    payload["fields"].append(
        {
            "field_id": "Commentaire",
            "physical_type": "string",
            "binding": {
                "canonical_target": None,
                "mdm_target": None,
                "status": "excluded",
                "blocking_reason": None,
                "confirmed_by": "owner@example.com",
                "confirmed_reason": "not needed for this report",
            },
        }
    )
    preview = _preview_bytes(b"Date,Clics,Commentaire\n2026-07-01,5,note\n", payload)

    by_column = {f["source_column"]: f for f in preview["fields"]}
    assert "Commentaire" in by_column, "une colonne ecartee disparue est l'inventaire perdu"
    assert by_column["Commentaire"]["treatment"] == EXCLUDED


def test_a_blocked_preview_carries_a_LIST_of_columns_like_every_other_return():
    """La branche qui existe pour LEVER un blocage servait un objet, pas des lignes.

    `fields` recevait le resultat entier du recognizer -- un objet portant
    `fields`, `mapping` et `ambiguities` -- alors que l'ecran itere dessus.
    """
    contract = {k: v for k, v in TEMPLATE_CONTRACT.items() if k != "class"}
    preview = _preview(MAPPING_PAYLOAD, template_contract=contract)

    assert isinstance(preview["fields"], list)
    assert {f["source_column"] for f in preview["fields"]} == {"Date", "Clics"}


# ---------------------------------------------------------------------------
# Story 38.16 AC3 -- les sept lectures qu'un apercu doit RAPPORTER.
#
# Quatre etaient la : couverture des champs requis (`gate`), identite/grain
# (`placement`), date/fuseau et devise-valeurs (`vocabularies`). Trois ne l'etaient
# pas, et leur matiere etait deja calculee ou a une jointure de distance.
# ---------------------------------------------------------------------------


def test_the_preview_summarises_what_the_parse_rejected():
    """<< Row-level validation summaries >>, la derniere des sept.

    `ParseResult` porte `rejected` -- une entree par echec, avec son numero de
    ligne, sa colonne et son code de regle stable -- et `detected_row_count`.
    L'apercu les calculait et les jetait, donc une personne apprenait qu'un
    cinquieme de son fichier n'atterrirait pas EN L'IMPORTANT.
    """
    preview = _preview()
    report = preview["row_validation"]

    assert report is not None
    assert report["detected_row_count"] >= report["accepted_row_count"]
    assert report["rejected_row_count"] == sum(
        entry["count"] for entry in report["by_rule"]
    )
    # UN RESUME, PAS LES LIGNES. Les valeurs rejetees sont du texte source
    # borne, et la discipline de cet apercu est qu'une valeur fournisseur reste
    # non instructionnelle et minimalement divulguee (AC5).
    for entry in report["by_rule"]:
        assert set(entry) == {"rule", "count", "first_row_number", "fields"}


def test_a_rate_is_not_reported_when_nothing_was_counted():
    """0 rejetee sur un total inconnu n'est pas 0 %.

    Un taux propre sur une analyse qui n'a lu aucune ligne rassure sur
    exactement la mauvaise chose. Meme regle que le registre d'import.
    """
    from core.csv_excel_import import _row_validation_report

    class _Empty:
        rejected: list = []
        rows: list = []
        detected_row_count = 0

    report = _row_validation_report(_Empty())
    assert report["rejected_row_pct"] is None
    assert report["by_rule"] == []


def test_the_summary_orders_by_damage_and_names_the_first_row():
    """Ce qu'un operateur decide, c'est la FORME du degat : quelle regle,
    combien, et ou est la premiere -- un numero de ligne qu'il ouvre dans son
    propre fichier."""
    from core.csv_excel_import import RejectedRow, _row_validation_report

    class _Result:
        rows = [{}, {}]
        detected_row_count = 7
        rejected = [
            RejectedRow(row_number=9, field_name="cost", rule="bad_amount",
                        reason="", rejected_value=""),
            RejectedRow(row_number=4, field_name="cost", rule="bad_amount",
                        reason="", rejected_value=""),
            RejectedRow(row_number=6, field_name="", rule="subtotal_or_total",
                        reason="", rejected_value=""),
        ]

    report = _row_validation_report(_Result())
    assert [entry["rule"] for entry in report["by_rule"]] == [
        "bad_amount",
        "subtotal_or_total",
    ]
    assert report["by_rule"][0]["count"] == 2
    assert report["by_rule"][0]["first_row_number"] == 4
    assert report["by_rule"][0]["fields"] == ["cost"]


def test_the_preview_reports_how_each_column_will_be_read():
    """<< Coercions >>. Le recognizer repond << quel champ canonique est cette
    colonne ? >> ; l'AC3 demande la seconde question, que l'ecran ne montrait pas :
    << et qu'arrive-t-il aux VALEURS dedans ? >>

    Une colonne de dates lue sous `%d.%m.%Y` et la meme lue sous le defaut
    rapportent toutes deux `matched`, et une seule des deux est juste.
    """
    preview = _preview()
    assert isinstance(preview["coercions"], list)
    for entry in preview["coercions"]:
        assert entry["coercion"] in {"date", "amount"}
        assert set(entry) == {"column", "coercion", "declared_format"}


def test_a_declared_date_format_is_reported_and_an_absent_one_is_not_invented():
    """`None` dit << le defaut de l'analyseur >>, et ce n'est PAS le meme enonce
    qu'un format declare. Un ecran qui imprimerait un defaut ici cacherait la
    declaration qui manque -- et deviner un format depuis un echantillon serait
    l'apercu inventant la chose meme qu'il existe pour faire verifier.
    """
    from core.csv_excel_import import _coercion_report

    class _Spec:
        def __init__(self, name):
            self.name = name

    class _Result:
        columns = [_Spec("start"), _Spec("end"), _Spec("cost"), _Spec("brand")]

    declared = _coercion_report(
        {
            "contract": {
                "reshape": {
                    "start_field": "start",
                    "end_field": "end",
                    "amount_field": "cost",
                    "date_format": "%d.%m.%Y",
                }
            }
        },
        _Result(),
    )
    by_column = {entry["column"]: entry for entry in declared}
    assert by_column["start"]["declared_format"] == "%d.%m.%Y"
    assert by_column["cost"]["coercion"] == "amount"
    # Une colonne qui ne subit aucune coercion n'apparait pas : un inventaire de
    # toutes les colonnes enterrerait les deux qui comptent.
    assert "brand" not in by_column

    absent = _coercion_report(
        {"contract": {"reshape": {"start_field": "start", "amount_field": "cost"}}},
        _Result(),
    )
    assert {entry["column"]: entry["declared_format"] for entry in absent}["start"] is None


def test_the_preview_reports_what_each_target_field_is_measured_in():
    """L'autre moitie du << currency/unit >> de l'AC3.

    `vocabularies` lit les codes devise presents dans le FICHIER. Ceci lit ce que
    le champ canonique sur lequel il atterrit DECLARE contenir. Une colonne
    etiquetee EUR atterrissant sur un champ dont le `currency_scope` est autre
    chose est une erreur qu'aucune des deux lectures n'attrape seule.
    """
    from core.csv_excel_import import build_file_source_preview

    from tests.core.test_file_source_wiring import _FakeConn, _responses

    # Les identifiants sont ceux que le registre FRAPPE (`mdm_<ULID>`), et le NOM
    # est la deuxieme colonne : l ecran de revision imprimait `mdm_01KZ... is
    # measured in micros` parce que ce rapport ne portait que l id. Une fixture
    # lisible (`"cost"`) rendait cette phrase acceptable a la lecture alors que la
    # production ne produit jamais ce mot -- le meme piege que le rail du Builder.
    cost_id = "mdm_01EXAMPLE00000000000001"
    day_id = "mdm_01EXAMPLE00000000000002"
    responses = _responses()
    responses["canonical_fields"] = [
        (cost_id, "Net cost", "metric", "micros", "reporting", "sum", False),
        (day_id, "Reporting day", "dimension", None, None, None, False),
    ]
    preview = build_file_source_preview(
        _FakeConn(responses),
        CSV_BYTES,
        project_id=PROJECT_ID,
        datastream_id=DATASTREAM_ID,
        mapping_version_id=MAPPING_VERSION_ID,
        filename="plan.csv",
    )
    by_field = {entry["field_id"]: entry for entry in preview["units"]}
    # LE MOT VOYAGE AVEC L ID : l ecran n a plus a le composer, donc il n a plus
    # a se rabattre sur l identifiant.
    assert by_field[cost_id]["canonical_name"] == "Net cost"
    assert by_field[day_id]["canonical_name"] == "Reporting day"
    assert by_field[cost_id]["unit"] == "micros"
    assert by_field[cost_id]["currency_scope"] == "reporting"
    assert by_field[cost_id]["aggregation"] == "sum"
    assert by_field[cost_id]["non_additive"] is False
    # Une dimension ne porte ni unite ni agregation, et le rapport le dit tel
    # quel plutot que de combler.
    assert by_field[day_id]["unit"] is None


def test_the_classification_says_it_does_not_know_and_names_why():
    """JE NE SAIS PAS, ET JE LE DIS.

    Le vocabulaire est ratifie et en usage (`datastream_field_mapping.py`), et la
    porte de sensibilite du chemin Epic-36 refuse deja un champ `credentials`
    lie. Mais cette valeur arrive avec les metadonnees de schema d'une source
    API, et un FICHIER n'en porte aucune : la classification d'un champ canonique
    appartient au CHAMP, et `app.mdm_canonical_fields` (migration 032) declare
    `unit`, `currency_scope`, `aggregation`, `non_additive` -- et aucune colonne
    de classification.

    Deriver une classification depuis les valeurs de l'echantillon serait
    exactement l'invention que ce depot interdit : une colonne de nombres n'est
    pas une preuve qu'elle n'est pas financiere, et un ecran qui dirait `none`
    sur cette base serait pire qu'un ecran qui ne dit rien.
    """
    preview = _preview()
    classification = preview["classification"]

    assert classification["declared_by"] is None
    assert (
        classification["undeclared_reason"]
        == "canonical_field_registry_declares_no_classification"
    )
    # Le vocabulaire RATIFIE voyage avec le refus, pour que l'ecran suivant n'ait
    # pas a le redecouvrir -- ni a en inventer un voisin.
    assert classification["vocabulary"] == [
        "none",
        "pii",
        "financial",
        "credentials",
        "internal",
        "unknown",
    ]
    assert classification["fields"]
    assert all(entry["classification"] == "unknown" for entry in classification["fields"])


def test_the_seven_readings_of_ac3_are_all_present():
    """L'AC3 en nomme SEPT. Une par une elles se defendent ; ensemble elles sont
    la liste que la story demande, et une liste incomplete se lit comme une liste
    complete."""
    preview = _preview()
    assert preview["gate"] is not None                # 1. couverture des requis
    assert "coercions" in preview                     # 2. coercions
    assert "vocabularies" in preview                  # 3. date / fuseau
    assert "units" in preview                         # 4. devise / unite
    assert preview["placement"] is not None           # 5. identite / grain
    assert preview["classification"] is not None      # 6. classification sensible
    assert preview["row_validation"] is not None      # 7. validation ligne a ligne


def test_a_blocked_preview_still_carries_what_lifts_the_block():
    """Ces branches existent pour donner de quoi lever le blocage.

    << ce fichier ne declare pas de format de date >> et << la moitie des lignes
    sont des sous-totaux >> en font souvent partie -- c'est la raison qui y met
    deja `vocabularies`.
    """
    preview = _preview(MAPPING_PAYLOAD_MISSING_REQUIRED)
    for key in ("coercions", "row_validation", "units", "classification"):
        assert key in preview
