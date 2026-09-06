"""Field-level detector + list_conflicts advisory regression for Story 39.8.

Focused module (does not touch test_datamodel.py / test_datamodel_timezone_gap.py). Verifies:
  - _detect_conflicts emits TIMEZONE_DAY_OFFSET (advisory) when a field is fed by >= 2 streams
    carrying DISTINCT captured report_timezone -- alongside any existing codes, which stay
    byte-unchanged (16). NOT gated on is_monetary (a non-monetary field fed by two timezones
    signals).
  - Same captured timezone across streams -> NO TIMEZONE_DAY_OFFSET (17, AC3).
  - Used-by rows with NO report_timezone -> the signal IS raised and NAMES them (18/18b,
    story 48.3 : un flux non placable est celui dont le jour risque de ne pas coincider).
  - _stream_report_timezone reads the generic key, fail-closed on blank/None (19).
  - list_conflicts renders a TIMEZONE_DAY_OFFSET as an ADVISORY, not dumped in the MEASURE_NULL
    else branch (20).
  - Backward compat: a currency-only field (no tz divergence) produces the same conflicts as
    before 39.8 -- no new key, no new object (21).

is_metric_monetary is patched so these stay offline (no DB), mirroring test_datamodel_timezone_gap.
"""

from __future__ import annotations

from unittest.mock import patch

from core.datamodel import _detect_conflicts, _stream_report_timezone


def _monetary(names_true):
    def fake(name, *, project_id=None):
        return name in names_true

    return patch("core.metric_semantics.is_metric_monetary", side_effect=fake)


# ---------------------------------------------------------------------------
# 16 -- TIMEZONE_DAY_OFFSET fires (advisory) alongside existing codes, byte-unchanged
# ---------------------------------------------------------------------------


def test_day_offset_fires_on_distinct_timezones():  # 16
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A", "report_timezone": "Europe/Paris",
         "source_currency": "EUR"},
        {"module_name": "mod-b", "datastream_name": "B", "report_timezone": "UTC",
         "source_currency": "EUR"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    by_code = {c["code"]: c for c in result}
    assert "TIMEZONE_DAY_OFFSET" in by_code
    offset = by_code["TIMEZONE_DAY_OFFSET"]
    assert offset["severity"] == "advisory"
    assert offset["realignable"] is False
    assert offset["distinct_timezones"] == ["Europe/Paris", "UTC"]
    assert offset["metric"] == "revenue"
    # Existing CURRENCY_CONFLICT still present + unchanged shape (refusal, not advisory).
    assert by_code["CURRENCY_CONFLICT"]["severity"] == "refusal"


def test_day_offset_not_monetary_gated():  # 16b -- fires for a NON-monetary field
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "ga", "datastream_name": "GA", "report_timezone": "Europe/Paris"},
        {"module_name": "gam", "datastream_name": "GAM", "report_timezone": "UTC"},
    ]
    with _monetary(set()):  # NOT monetary
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "TIMEZONE_DAY_OFFSET" in codes  # a day-offset signals regardless of monetary
    assert "CURRENCY_GAP" not in codes  # currency gap stays monetary-gated
    assert "TIMEZONE_GAP" not in codes


# ---------------------------------------------------------------------------
# 17 -- same timezone flags nothing (AC3)
# ---------------------------------------------------------------------------


def test_same_timezone_no_day_offset():  # 17
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A", "report_timezone": "Europe/Paris"},
        {"module_name": "b", "datastream_name": "B", "report_timezone": "Europe/Paris"},
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert "TIMEZONE_DAY_OFFSET" not in {c["code"] for c in result}


# ---------------------------------------------------------------------------
# 18 -- unknown timezone -> le signal sort et nomme le flux (48.3)
# ---------------------------------------------------------------------------


# 18 / 18b -- REECRITS le 2026-08-01. Ils asseraient le SILENCE sur un flux
# dont le fuseau est inconnu, c'est-a-dire exactement ce que la story 48.3 a
# retire du moteur en le nommant « the defect » (timezone_signal.py:180-184 :
# « With two known streams agreeing and three unplaceable, this function used to
# return None -- and an incomplete comparison rendered as a healthy one »).
#
# La reparation de 48.3 vivait dans le moteur et n'etait atteignable par AUCUN
# parcours : `datamodel.py` gardait l'appel derriere `len({fuseaux CONNUS}) >= 2`,
# ce qui recreait l'exclusion a l'exterieur. Ces deux tests epinglaient la garde,
# donc ils epinglaient le defaut -- la meme classe qu'AI-81.
#
# La cible tranche, et ce n'est pas un arbitrage a demander (alignment-register :
# « True open arbitrations: none ») : capabilities/reporting-timezone.md exige
# « expose day-offset risk », nomme les « unresolved sources » dans Project
# Settings, et son critere [1] est « cross-source daily reconciliation ignores
# different day boundaries ». Un flux qu'on ne peut pas placer sur une horloge
# est celui dont le jour risque le plus de ne pas coincider.
#
# Ils n'asserent donc plus l'absence : ils asserent que le signal SORT **et**
# qu'il NOMME le flux non placable. Une assertion plus forte, pas plus faible.


def test_unknown_timezone_is_surfaced_not_silenced():  # 18
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A"},  # no report_timezone
        {"module_name": "b", "datastream_name": "B"},  # no report_timezone
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    signal = next((c for c in result if c["code"] == "TIMEZONE_DAY_OFFSET"), None)
    assert signal is not None, "deux flux non placables rendus comme sains"
    assert signal["severity"] == "advisory"
    assert {u["datastream"] for u in signal["unplaced_streams"]} == {"A", "B"}


def test_one_known_one_unknown_is_surfaced_not_silenced():  # 18b
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A", "report_timezone": "UTC"},
        {"module_name": "b", "datastream_name": "B"},  # unknown
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    signal = next((c for c in result if c["code"] == "TIMEZONE_DAY_OFFSET"), None)
    assert signal is not None, "un flux non placable efface par l'exclusion amont"
    assert [u["datastream"] for u in signal["unplaced_streams"]] == ["B"]


def test_two_known_agreeing_and_nothing_unplaceable_stays_silent():  # 18c
    """Le seul cas qui ne signale rien -- la garde ne doit pas devenir bavarde."""
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A", "report_timezone": "UTC"},
        {"module_name": "b", "datastream_name": "B", "report_timezone": "UTC"},
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert "TIMEZONE_DAY_OFFSET" not in {c["code"] for c in result}


# ---------------------------------------------------------------------------
# 19 -- _stream_report_timezone seam (generic key, fail-closed)
# ---------------------------------------------------------------------------


def test_stream_report_timezone_reads_generic_key():  # 19
    assert _stream_report_timezone({"report_timezone": "Europe/Paris"}) == "Europe/Paris"
    assert _stream_report_timezone({"captured_report_timezone": "UTC"}) == "UTC"
    assert _stream_report_timezone({"report_timezone": "  UTC  "}) == "UTC"


def test_stream_report_timezone_fail_closed_on_blank():  # 19b
    assert _stream_report_timezone({"report_timezone": ""}) is None
    assert _stream_report_timezone({"report_timezone": "   "}) is None
    assert _stream_report_timezone({"report_timezone": None}) is None
    assert _stream_report_timezone({}) is None
    assert _stream_report_timezone({"report_timezone": 123}) is None


# ---------------------------------------------------------------------------
# 20 -- list_conflicts renders TIMEZONE_DAY_OFFSET as an advisory (not the else/MEASURE_NULL)
# ---------------------------------------------------------------------------


def test_list_conflicts_renders_day_offset_as_advisory():  # 20
    from unittest.mock import MagicMock

    from core import conflict_resolutions

    offset_conflict = {
        "code": "TIMEZONE_DAY_OFFSET",
        "message": "cross-source day offset",
        "affected_streams": ["A", "B"],
        "severity": "advisory",
        "report_timezones": [],
        "distinct_timezones": ["Europe/Paris", "UTC"],
        "realignable": False,
        "metric": "revenue",
    }
    detail = {
        "name": "revenue",
        "display_name": "Revenue",
        "data_type": "currency",
        "field_kind": "metric",
        "measure": "sum",
        "status": "approved",
        "used_by": [
            {"module_name": "mod-a"},
            {"module_name": "mod-b"},
        ],
        "conflicts": [offset_conflict],
    }
    conn = MagicMock()
    with patch(
        "core.datamodel.list_target_fields", return_value=[{"name": "revenue"}]
    ), patch(
        "core.datamodel.get_target_field", return_value=detail
    ), patch.object(
        conflict_resolutions, "_fetch_resolutions_for_field", return_value=[]
    ):
        out = conflict_resolutions.list_conflicts(project_id=None, conn=conn)
    assert len(out) == 1
    entry = out[0]
    assert entry["conflict"]["code"] == "TIMEZONE_DAY_OFFSET"
    assert entry["conflict"]["severity"] == "advisory"
    # Advisory -> empty resolutions_by_module (not a per-module currency bind), rendered
    # EXPLICITLY (the key is present, the advisory carried through intact).
    assert entry["resolutions_by_module"] == {}


# ---------------------------------------------------------------------------
# 21 -- backward compat: currency-only field (no tz divergence) unchanged
# ---------------------------------------------------------------------------


def test_currency_only_field_unchanged_no_day_offset():  # 21
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    # Two modules, SAME captured timezone -> no day-offset; currency conflict still fires.
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A", "report_timezone": "UTC",
         "source_currency": "USD"},
        {"module_name": "mod-b", "datastream_name": "B", "report_timezone": "UTC",
         "source_currency": "EUR"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = [c["code"] for c in result]
    assert "TIMEZONE_DAY_OFFSET" not in codes
    assert "CURRENCY_CONFLICT" in codes  # existing behaviour intact


# ---------------------------------------------------------------------------
# AI-132 -- LE LEVIER A UN PRODUCTEUR, ET L'UTILISATEUR L'APPREND
#
# `report_timezone_has_lever` etait LU par `_detect_conflicts` et ECRIT nulle
# part : `has_lever` valait toujours False et la branche levier du moteur etait
# inatteignable par tout parcours (classe AI-85). Les 51 tests timezone etaient
# verts parce qu'ils passent `has_lever` EN ENTREE du moteur pur -- aucun
# n'exigeait un producteur. C'est ce que ces tests exigent.
#
# Deux faits distincts, et il fallait les deux : que le drapeau soit PRODUIT
# (depuis la declaration du connecteur) et qu'il soit DIT (l'ecran ne rend que
# `code`, `message`, `affected_streams` -- une cle non affichee n'informe
# personne).
# ---------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, name, manifest):
        self.name = name
        self.manifest = manifest


def _with_manifests(*modules):
    """Patch le registre des modules charges -- la source de la declaration."""
    return patch("core.main._loaded_modules", list(modules))


def _lever_manifest(hint="Set the report time zone in the source's report settings."):
    return {"source_capabilities": {"time_context": {
        "locus": "network", "fallback": "gap",
        "adjustment_lever": {"available": True, "hint": hint},
    }}}


_TWO_ZONES = [
    {"module_name": "mod-a", "datastream_name": "A", "report_timezone": "Europe/Paris"},
    {"module_name": "mod-b", "datastream_name": "B", "report_timezone": "UTC"},
]


def _offset(used_by, *modules):
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    with _monetary(set()), _with_manifests(*modules):
        result = _detect_conflicts(field, used_by, project_id=None)
    return next((c for c in result if c["code"] == "TIMEZONE_DAY_OFFSET"), None)


def test_a_declared_lever_reaches_the_signal_from_the_manifest():
    """Le producteur existe : la declaration du connecteur remplit `has_lever`.

    C'est LE test qui manquait. Sans lui, la branche levier reste une intention.
    """
    offset = _offset(_TWO_ZONES, _FakeModule("mod-a", _lever_manifest()), _FakeModule("mod-b", {}))
    assert offset is not None
    by_stream = {s["datastream"]: s for s in offset["report_timezones"]}
    assert by_stream["A"]["has_lever"] is True
    assert by_stream["A"]["lever_hint"] == (
        "Set the report time zone in the source's report settings."
    )
    # Et l'autre flux, non declare, reste honnetement sans levier.
    assert by_stream["B"]["has_lever"] is False
    assert by_stream["B"]["lever_hint"] is None


def test_the_message_POINTS_at_the_source_when_a_lever_exists():
    """Une cle que l'ecran n'affiche pas n'informe personne.

    L'ecran de mapping ne rend que `code`, `message` et `affected_streams` : le
    levier n'atteint l'utilisateur QUE par le message.
    """
    offset = _offset(_TWO_ZONES, _FakeModule("mod-a", _lever_manifest()), _FakeModule("mod-b", {}))
    assert "can be changed at the source" in offset["message"]
    assert "Set the report time zone in the source's report settings." in offset["message"]
    assert "'A'" in offset["message"]


def test_the_message_SAYS_no_lever_rather_than_staying_silent():
    """« On ne m'a rien dit » et « il n'y a rien a faire » ne doivent pas se lire pareil.

    L'AC ratifiee l'exige mot pour mot : une source sans levier est rapportee
    « fixed on the detected timezone, no lever available ».
    """
    offset = _offset(_TWO_ZONES, _FakeModule("mod-a", {}), _FakeModule("mod-b", {}))
    assert "no lever available" in offset["message"]
    assert "can be changed at the source" not in offset["message"]


def test_a_lever_declared_without_a_hint_is_reported_as_absent():
    """Pointer quelqu'un vers « quelque part dans la source » est pire que rien."""
    manifest = {"source_capabilities": {"time_context": {
        "locus": "network", "adjustment_lever": {"available": True},
    }}}
    offset = _offset(_TWO_ZONES, _FakeModule("mod-a", manifest), _FakeModule("mod-b", {}))
    by_stream = {s["datastream"]: s for s in offset["report_timezones"]}
    assert by_stream["A"]["has_lever"] is False
    assert "no lever available" in offset["message"]


def test_the_phantom_used_by_keys_no_longer_decide_anything():
    """Regression AI-132 : les deux cles lues-jamais-ecrites ne pilotent plus rien.

    Si un jour une ligne used-by en portait une, elle ne doit pas contredire la
    declaration du connecteur -- sinon on aurait DEUX producteurs, dont un faux.
    """
    used_by = [
        {**_TWO_ZONES[0], "report_timezone_has_lever": True,
         "report_timezone_lever_hint": "a phantom hint"},
        _TWO_ZONES[1],
    ]
    offset = _offset(used_by, _FakeModule("mod-a", {}), _FakeModule("mod-b", {}))
    by_stream = {s["datastream"]: s for s in offset["report_timezones"]}
    assert by_stream["A"]["has_lever"] is False
    assert "a phantom hint" not in offset["message"]


def test_an_unreadable_module_registry_reads_as_no_lever_never_as_one():
    """Fail-soft : un registre en panne fait perdre un levier, jamais l'inverse."""
    with patch("core.main._loaded_modules", side_effect=RuntimeError("boom")):
        offset = _offset(_TWO_ZONES)
    assert offset is not None
    assert all(s["has_lever"] is False for s in offset["report_timezones"])


# ---------------------------------------------------------------------------
# AI-132 (correction) — UNE SEULE RESOLUTION DU LEVIER
#
# J'ai livre une seconde resolution sans trouver la premiere. `capability_compilers._lever`
# (story 48.3) DERIVAIT deja le levier du locus ; la mienne exigeait une declaration
# explicite. Sur un manifeste `locus: network` l'ecran de capacite disait « un levier
# existe » et le signal de mapping « no lever available » : le meme produit, deux reponses
# opposees sur la meme source. Deux producteurs dont un faux est pire que pas de
# producteur -- on ne sait plus lequel croire.
#
# Ces tests epinglent l'accord. Ils echouent si quelqu'un redonne une resolution propre
# a l'un des deux appelants.
# ---------------------------------------------------------------------------


def test_the_two_surfaces_answer_the_SAME_thing_about_the_same_source():
    """L'ecran de capacite et le signal de mapping ne doivent jamais se contredire."""
    from core.capability_compilers import _lever
    from core.report_timezone import signal_lever

    for declaration in (
        {"locus": "network", "fallback": "gap"},
        {"locus": "property"},
        {"locus": "account"},
        {"locus": "fixed", "fixed_zone": "UTC"},
        {"locus": "none"},
        {"locus": "network", "adjustment_lever": {"available": False}},
        {"locus": "fixed", "adjustment_lever": {"available": True, "hint": "Do it there."}},
        {},
        None,
    ):
        screen = _lever(declaration, None)["available"]
        signal = signal_lever(declaration)["has_lever"]
        assert screen == signal, f"surfaces disagree on {declaration!r}"


def test_a_locus_that_is_a_source_setting_offers_a_lever_without_any_extra_declaration():
    """La semantique de 48.3 est PRESERVEE : elle etait livree, et elle etait la bonne.

    Un fuseau de reseau/propriete/compte se change bien chez le fournisseur. Exiger une
    declaration explicite aurait fait REGRESSER l'ecran de capacite.
    """
    manifest = {"source_capabilities": {"time_context": {"locus": "network"}}}
    offset = _offset(_TWO_ZONES, _FakeModule("mod-a", manifest), _FakeModule("mod-b", {}))
    by_stream = {s["datastream"]: s for s in offset["report_timezones"]}
    assert by_stream["A"]["has_lever"] is True
    assert "can be changed at the source" in offset["message"]


def test_an_explicit_declaration_can_CONTRADICT_the_locus_heuristic():
    """C'est la raison d'etre de la declaration explicite : dire NON la ou le locus dit oui.

    Sans elle, un connecteur dont le fuseau reseau n'est PAS modifiable par le client
    n'aurait aucun moyen de le dire.
    """
    manifest = {"source_capabilities": {"time_context": {
        "locus": "network", "adjustment_lever": {"available": False},
    }}}
    offset = _offset(_TWO_ZONES, _FakeModule("mod-a", manifest), _FakeModule("mod-b", {}))
    by_stream = {s["datastream"]: s for s in offset["report_timezones"]}
    assert by_stream["A"]["has_lever"] is False
    assert "no lever available" in offset["message"]


# ---------------------------------------------------------------------------
# AI-167 — le fuseau vient de ce qu'un RUN a OBSERVE
#
# `_stream_report_timezone` le dit lui-meme : « today the used-by SELECT carries none of
# these ». En production chaque flux etait donc non placable, et le signal ne pouvait dire
# que « N flux sans fuseau resolvable » -- jamais « ces flux tirent leur jour sur des
# horloges differentes ». La branche existait, aucun parcours ne l'atteignait : la meme
# classe que le levier et que l'evidence jamais ecrite.
#
# ⚠️ Sans patch, `observed_zones_for_datastreams` echoue en douceur (pas de base) et rend
# {} : un test qui ne le patche pas passe SANS RIEN PROUVER du cablage.
# ---------------------------------------------------------------------------


def _with_observed(zones):
    return patch("core.time_boundary.observed_zones_for_datastreams", lambda *a, **k: zones)


_NO_TZ_IN_USED_BY = [
    {"module_name": "mod-a", "datastream_name": "A", "datastream_id": "ds_a"},
    {"module_name": "mod-b", "datastream_name": "B", "datastream_id": "ds_b"},
]


def test_the_observed_zone_reaches_the_signal_when_the_used_by_row_carries_none():
    """LE test qui manquait : ce qu'un run a observe nomme les horloges a l'ecran."""
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    with _monetary(set()), _with_observed({"ds_a": "Europe/Paris", "ds_b": "America/New_York"}):
        result = _detect_conflicts(field, _NO_TZ_IN_USED_BY, project_id="proj_1")
    signal = next(c for c in result if c["code"] == "TIMEZONE_DAY_OFFSET")
    assert signal["distinct_timezones"] == ["America/New_York", "Europe/Paris"]
    assert signal["unplaced_streams"] == []
    assert "different clocks" in signal["message"]


def test_a_stream_no_run_has_observed_stays_UNPLACED_not_invented():
    """Fail-closed : pas d'observation, pas de fuseau -- jamais un UTC fabrique."""
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    with _monetary(set()), _with_observed({"ds_a": "Europe/Paris"}):
        result = _detect_conflicts(field, _NO_TZ_IN_USED_BY, project_id="proj_1")
    signal = next(c for c in result if c["code"] == "TIMEZONE_DAY_OFFSET")
    assert [u["datastream"] for u in signal["unplaced_streams"]] == ["B"]
    assert signal["distinct_timezones"] == ["Europe/Paris"]


def test_the_used_by_key_still_WINS_over_the_observed_read():
    """Le jour ou la SELECT portera la cle generique, elle gagne sans qu'on touche ici."""
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {**_NO_TZ_IN_USED_BY[0], "report_timezone": "Asia/Tokyo"},
        _NO_TZ_IN_USED_BY[1],
    ]
    with _monetary(set()), _with_observed({"ds_a": "Europe/Paris", "ds_b": "UTC"}):
        result = _detect_conflicts(field, used_by, project_id="proj_1")
    signal = next(c for c in result if c["code"] == "TIMEZONE_DAY_OFFSET")
    assert "Asia/Tokyo" in signal["distinct_timezones"]
    assert "Europe/Paris" not in signal["distinct_timezones"]
