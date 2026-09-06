"""Story 41.8: the Tax & Fee MCP surface, proved offline.

No database. Every governed validator this surface uses is pure --
``validate_ladder_rules``, ``propose_from_presets``, ``_match_ladder``,
``classify_gaps``, ``derive_applicability`` -- which is exactly what makes an
offline assertion about them meaningful.

Two facts these green tests must NOT be read as covering, repeated here because a
passing suite is where they get forgotten:

* migration 119 is applied to no database, and the ~25 pg-gated tests of Epic 41
  are exactly the ones that would prove its views;
* ``fee_tax_ladder_daily`` has never executed, so nothing in this file proves that
  a composed daily figure exists. This surface reports the ladder DEFINITION.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from core import fee_tax_mcp
from core.mcp_profiles import (
    CatalogValidationError,
    register_profiled,
    reset_registry_for_tests,
)
from core.tax_fee_rule_set import TaxFeeLadder, validate_ladder_rules

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

_CORE = Path(__file__).resolve().parents[2] / "core"
_MODULE = _CORE / "fee_tax_mcp.py"
_SEEDING = _CORE / "tax_fee_preset_seeding.py"
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "import_tax_fee_presets.py"
_MAIN = _CORE / "main.py"


# ---------------------------------------------------------------------------
# Fixtures. The rule builders are copied from `test_tax_fee_rule_set.py` rather
# than reinvented: a hand-written approximation cannot catch a divergence between
# what this surface emits and what the profile accepts.
# ---------------------------------------------------------------------------


class _Recorder:
    """Minimal FastMCP stand-in: records what a module registers."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def tool(self, handler, *, name=None, tags=None, meta=None):
        self.tools[name or handler.__name__] = {"handler": handler, "tags": tags, "meta": meta}
        return handler


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate the registry for each test, then PUT BACK what was there.

    `reset_registry_for_tests()` clears a module-level registry that `core.main`
    populates once, at import, for the whole process. Leaving it cleared -- which
    is what the pre-existing `test_project_capabilities_mcp.py` fixture does --
    empties the catalog for every later test in the same pytest session, and the
    live-registry seam suite then fails for a reason that has nothing to do with
    the code under test. Measured: `pytest tests/core/test_fee_tax_mcp.py
    tests/integration/test_fee_tax_mcp_seams.py` failed three seam assertions
    before this restore existed, and passes with it.
    """
    from core import mcp_profiles

    snapshot = dict(mcp_profiles._REGISTRY.declarations)
    app_only = set(mcp_profiles._APP_ONLY_TOOLS)
    widgets = dict(mcp_profiles._WIDGET_BOUND_TOOLS)
    reset_registry_for_tests()
    try:
        yield
    finally:
        reset_registry_for_tests()
        mcp_profiles._REGISTRY.declarations.update(snapshot)
        mcp_profiles._APP_ONLY_TOOLS.update(app_only)
        mcp_profiles._WIDGET_BOUND_TOOLS.update(widgets)


def _source_evidence(**overrides):
    base = {
        "issuer": "European Commission",
        "reference": "VAT Directive, standard rate table",
        "reference_version": "2026-01",
        "authoritative_url": "https://example.com/vat-rates",
        "published_on": "2026-01-01",
    }
    base.update(overrides)
    return base


def _agency_rule(**overrides):
    rule = {
        "rule_key": "agency_fee",
        "label": "Agency fee",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "AGENCY_FEE",
        "form": "PERCENTAGE",
        "rate": "0.15",
        "base_target": "NET_MEDIA",
        "cascade_phase": 5,
        "sequence_order": 10,
        "effective_from": "2026-01-01",
        "authority_kind": "agency_contract",
        "source_evidence": _source_evidence(
            issuer="Agency",
            reference="Master services agreement, schedule B",
            reference_version="v3",
            authoritative_url=None,
            document_ref="MSA-2026-B",
        ),
    }
    rule.update(overrides)
    return rule


def _country_rule(**overrides):
    rule = {
        "rule_key": "dst_fr",
        "label": "Digital services tax pass-through",
        "scope_kind": "project",
        "scope_ref": None,
        "category": "REGULATORY_TAX",
        "form": "PERCENTAGE",
        "rate": "0.03",
        "base_target": "NET_MEDIA",
        "cascade_phase": 3,
        "sequence_order": 10,
        "conditions": {"country": ["FR"]},
        "effective_from": "2026-01-01",
        "authority_kind": "statutory_reference",
        "source_evidence": _source_evidence(),
        "jurisdiction": {
            "kind": "country",
            "id": "ctry_EXAMPLE",
            "hierarchy_version_id": "mdv_EXAMPLE",
            "label": "France",
        },
        "rest_of_world_posture": "exclude",
        "unknown_posture": "exclude",
    }
    rule.update(overrides)
    return rule


def _ladder(count: int, *, unresolved: int = 0) -> TaxFeeLadder:
    """A normalized ladder of *count* rules, built through the real profile."""
    rules = []
    for index in range(count):
        geographic = index % 2 == 1
        if geographic:
            builder = _country_rule(
                rule_key=f"dst_{index:02d}",
                sequence_order=index,
                rest_of_world_posture=("unresolved" if index // 2 < unresolved else "exclude"),
                unknown_posture="exclude",
            )
        else:
            builder = _agency_rule(rule_key=f"agency_fee_{index:02d}", sequence_order=index)
        rules.append(builder)
    normalized = validate_ladder_rules(rules)
    return TaxFeeLadder(
        rule_set_id="grs_EXAMPLE",
        version_id="grsv_EXAMPLE",
        version_number=3,
        content_hash="c" * 64,
        rounding="half_even",
        default_money_basis="native_source",
        rules=tuple(normalized),
    )


#: EVERY STATEMENT `fee_tax_mcp.ladder_summary` ISSUES, named. Three, and they
#: are the whole read: `_activation` reads the projected view and then the
#: capability pin (`core/fee_tax_mcp.py:269` and `:276`), `_money_policy` reads
#: the money policy view (`core/fee_tax_mcp.py:307`), and the ladder itself
#: arrives through a monkeypatched `try_resolve_tax_fee_ladder`, never as SQL.
#:
#: AI-317: `_Cursor` used to answer `None` to any statement no fragment matched,
#: and on this surface `None` is a MEANINGFUL row -- "this Project has no
#: projected activation", "no Money Policy is confirmed". A fourth read would
#: therefore never have failed; it would have been reported as an absent row, and
#: the summary would have composed a typed gap about a Project never read.
_LADDER_READ = StatementInventory(
    "_Cursor (fee_tax_mcp.ladder_summary)",
    activation="from app.project_tax_fee_activation_v",
    capability="from app.project_capabilities",
    money_policy="from app.project_money_policy_v",
)


class _Cursor:
    """A cursor double that answers a NAMED statement and REFUSES a mutation."""

    def __init__(self, answers: dict[str, object]) -> None:
        self._answers = answers
        self._row: object = None
        self.description: list[tuple[str]] | None = None
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):  # noqa: ARG002
        self.statements.append(sql)
        upper = sql.upper()
        if "INSERT" in upper or "UPDATE " in upper or "DELETE" in upper:
            raise AssertionError(f"this surface must not write: {sql}")
        name = _LADDER_READ.match(sql)
        # DERIVED from the statement: all three are flat SELECTs, so the column
        # list this fake reports is the product's own, never a second copy of it.
        # A SELECT matching no row still HAS a description; only a statement with
        # no result set reports `None`.
        self.description = describe(sql)
        self._row = self._answers[name]

    def fetchone(self):
        return self._row

    def fetchall(self):
        # Each taught statement is a `WHERE project_id = %s` single-row read.
        return [self._row] if self._row is not None else []


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317: on this surface, silence and "no such row" are the same answer.

    The refusal carries the statement that moved AND what the fake still knows,
    because the reader's next two questions are "which query?" and "what did it
    use to look like?".
    """
    cursor = _Cursor(_answers())
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute("SELECT rate FROM app.fee_tax_rules WHERE project_id = %s")
    message = str(raised.value)
    assert "app.fee_tax_rules" in message
    assert "money_policy" in message


class _Connection:
    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.cursors: list[_Cursor] = []

    def cursor(self):
        cursor = _Cursor(self.answers)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        raise AssertionError("this read path must not commit")

    def rollback(self):
        return None

    @property
    def statements(self) -> list[str]:
        return [sql for cursor in self.cursors for sql in cursor.statements]


_ACTIVE_ROW = (
    "proj_EXAMPLE",
    "pcv_EXAMPLE",
    "enabled",
    True,
    "grs_EXAMPLE",
    "grsv_EXAMPLE",
    "c" * 64,
    "half_even",
    "native_source",
    2,
    "EUR",
    "grsv_MONEY",
    "m" * 64,
)


def _answers(*, active: bool = True, money: bool = True, capability_pinned: bool = True):
    row = list(_ACTIVE_ROW)
    row[3] = active
    # Keyed by the INVENTORY NAME, not by a fragment of the product's SQL: the
    # fragment lives in `_LADDER_READ`, in one place, and goes stale loudly.
    answers: dict[str, object] = {
        "activation": tuple(row),
        "capability": (
            ("enabled", "pcv_EXAMPLE") if capability_pinned else ("disabled", None)
        ),
        "money_policy": (("grs_MONEY", "grsv_MONEY") if money else None),
    }
    return answers


# ---------------------------------------------------------------------------
# E.2 -- declaration and registration.
# ---------------------------------------------------------------------------


def _register() -> _Recorder:
    recorder = _Recorder()
    fee_tax_mcp.register(recorder)
    return recorder




def test_no_tool_here_declares_the_obsolete_write_effect() -> None:
    assert 'effect="write"' not in _MODULE.read_text(encoding="utf-8")


def test_a_prepare_cannot_be_declared_as_authorizing_here() -> None:
    """Section D.1 in executable form: the shape the sprint line asked for cannot exist."""
    recorder = _Recorder()

    def add_fee_tax_rule():
        return None

    for mode in ("human", "host"):
        reset_registry_for_tests()
        with pytest.raises(CatalogValidationError, match="cannot authorize"):
            register_profiled(
                recorder,
                add_fee_tax_rule,
                profile="governance",
                effect="prepare",
                data_class="operational",
                confirmation_mode=mode,
            )


def test_the_absent_confirmed_write_is_declared_and_explained() -> None:
    reason = fee_tax_mcp.CONFIRMED_WRITE_ABSENT_REASON
    assert isinstance(reason, str) and reason.strip()
    assert "bind_confirmation_presence" in reason
    assert "ENTRY_COMMANDS" in reason


def test_the_legacy_tax_commands_are_not_mounted() -> None:
    source = _MAIN.read_text(encoding="utf-8")
    assert "_register_fee_tax_mcp" not in source


# ---------------------------------------------------------------------------
# E.3 -- the read tool.
# ---------------------------------------------------------------------------


def _summary(monkeypatch, *, ladder, active=True, money=True, capability_pinned=True):
    monkeypatch.setattr(
        "core.tax_fee_rule_set.try_resolve_tax_fee_ladder", lambda conn, **kw: ladder
    )
    conn = _Connection(
        _answers(active=active, money=money, capability_pinned=capability_pinned)
    )
    return fee_tax_mcp.ladder_summary(conn, "proj_EXAMPLE"), conn


def test_a_project_with_no_published_ladder_gets_a_typed_gap_not_an_empty_list(
    monkeypatch,
) -> None:
    summary, _conn = _summary(monkeypatch, ladder=None, active=False)
    assert summary["ladder"] is None, "an absent ladder must never be an empty rule list"
    codes = {gap["code"] for gap in summary["gaps"]}
    assert fee_tax_mcp.GAP_LADDER_UNPUBLISHED in codes
    assert summary["confirmed"] is False


def test_the_read_names_the_exact_version_and_content_hash(monkeypatch) -> None:
    summary, _conn = _summary(monkeypatch, ladder=_ladder(4))
    block = summary["ladder"]
    assert block["version_id"] == "grsv_EXAMPLE"
    assert block["version_number"] == 3
    assert block["content_hash"] == "c" * 64
    assert block["rounding"] == "half_even"
    assert block["default_money_basis"] == "native_source"
    assert summary["activation"]["reporting_currency"] == "EUR"
    assert summary["activation"]["money_policy_version_id"] == "grsv_MONEY"


def test_unresolved_geographic_postures_are_reported_separately_from_the_rules(
    monkeypatch,
) -> None:
    summary, _conn = _summary(monkeypatch, ladder=_ladder(4, unresolved=1))
    block = summary["ladder"]
    assert block["unresolved_geography_rule_keys"], "an undecided posture must be visible"
    codes = {gap["code"] for gap in summary["gaps"]}
    assert fee_tax_mcp.GAP_LADDER_GEOGRAPHY_UNRESOLVED in codes
    # Complete as a document, incomplete as an answer: two different states.
    assert block["rule_count"] == 4


def test_the_rule_list_is_bounded_and_says_what_it_withheld(monkeypatch) -> None:
    summary, _conn = _summary(monkeypatch, ladder=_ladder(40))
    block = summary["ladder"]
    assert block["rule_count"] == 40, "the TRUE total is always stated"
    assert 0 < len(block["rules"]) <= fee_tax_mcp._MAX_LADDER_RULES
    assert len(block["rules"]) + block["rules_withheld"] == 40


def test_the_bound_is_measured_so_a_verbose_ladder_degrades_instead_of_failing(
    monkeypatch,
) -> None:
    """A count tuned on a fixture would REFUSE here; a byte budget withholds instead."""
    from core import model_channel

    narrow, _conn = _summary(monkeypatch, ladder=_ladder(40))
    verbose_rules = [
        dict(rule, label="A deliberately long rule label " * 4)
        for rule in _ladder(40).rules
    ]
    verbose = TaxFeeLadder(
        rule_set_id="grs_EXAMPLE",
        version_id="grsv_EXAMPLE",
        version_number=3,
        content_hash="c" * 64,
        rounding="half_even",
        default_money_basis="native_source",
        rules=tuple(verbose_rules),
    )
    wide, _conn2 = _summary(monkeypatch, ladder=verbose)
    assert wide["ladder"]["rules_withheld"] >= narrow["ladder"]["rules_withheld"]
    for block in (narrow["ladder"], wide["ladder"]):
        assert (
            model_channel.serialized_bytes(block["rules"])
            <= fee_tax_mcp._LADDER_RULES_BYTE_BUDGET
        )
    # And the label itself is capped, marked rather than silently shortened.
    assert wide["ladder"]["rules"][0]["label"].endswith("...")




def test_a_bounded_rule_summary_carries_no_rate_and_no_conditions(monkeypatch) -> None:
    summary, _conn = _summary(monkeypatch, ladder=_ladder(4))
    for rule in summary["ladder"]["rules"]:
        for forbidden in ("rate", "tiers", "conditions", "amount_micros", "cpm_micros"):
            assert forbidden not in rule, forbidden
        assert rule["source_reference_version"], "the source version is what makes it findable"
        assert "source_evidence" not in rule






def test_the_matcher_refusal_codes_are_not_reimplemented_here() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    for code in (
        "source_type_out_of_scope",
        "scoped_to_another_datastream",
        "scoped_to_another_plan_version",
        "required_input_unavailable",
    ):
        assert source.count(code) == 0, f"{code} must come from the matcher, not from here"




def test_the_read_answers_while_the_capability_is_inactive_and_says_so(monkeypatch) -> None:
    summary, _conn = _summary(monkeypatch, ladder=_ladder(3), active=False)
    assert summary["ladder"] is not None, "a ladder stays readable while the capability is off"
    assert summary["activation"]["tax_fees_active"] is False
    codes = {gap["code"] for gap in summary["gaps"]}
    assert fee_tax_mcp.GAP_CAPABILITY_INACTIVE in codes


def test_the_headline_never_reads_as_a_composed_invoice_total(monkeypatch) -> None:
    """`fee_tax_ladder_daily` has never executed; no summary may imply a total."""
    summary, _conn = _summary(monkeypatch, ladder=_ladder(6, unresolved=1))
    headline = summary["headline"].lower()
    for forbidden in ("total of", "gross total is", "eur ", "amount"):
        assert forbidden not in headline, headline
    assert "define" in headline


# ---------------------------------------------------------------------------
# E.4 -- auto-populate.
# ---------------------------------------------------------------------------


def _preset(**overrides):
    from core.tax_fee_presets import PresetVersion

    base = dict(
        id="tfpv_EXAMPLE",
        preset_key="seed_regulatory_tax_fr",
        version_number=1,
        status="published",
        label="France Digital Services Tax (TSN/GAFA) 3%",
        issuer="toorow shared reference (unattributed seed row)",
        source_reference="dbt/seeds/country_tax_defaults.csv",
        source_reference_version="country_tax_defaults@0123456789abcdef",
        authoritative_url=None,
        document_ref=None,
        jurisdiction_kind="country",
        jurisdiction_code="FR",
        taxable_subject="Unspecified by the seed.",
        service_scope=None,
        category="REGULATORY_TAX",
        form="PERCENTAGE",
        rate=Decimal("0.030000"),
        amount_micros=None,
        cpm_micros=None,
        currency=None,
        base_target="NET_MEDIA",
        thresholds=(),
        qualifications=(
            {
                "code": "provider_passes_through",
                "question": "Does this platform actually pass this tax through to you?",
                "proven": False,
                "evidence": None,
            },
            {
                "code": "threshold_met",
                "question": "Are the thresholds met by the party that would charge it?",
                "proven": False,
                "evidence": None,
            },
        ),
        assumptions=(
            "Default starting point: French DST headline rate commonly passed through by "
            "ad platforms. Confirm against your platform invoice and your tax advisor "
            "before use.",
        ),
        published_on=None,
        effective_from=date(2019, 1, 1),
        effective_to=None,
        last_verified_on=None,
        verification_status="unverified",
        content_hash="p" * 64,
    )
    base.update(overrides)
    return PresetVersion(**base)


class _Projection:
    hierarchy_version_id = "mdv_EXAMPLE"
    market_of_value = {"FR": "mkt_EXAMPLE", "DE": "mkt_EXAMPLE"}


def _auto(monkeypatch, *, presets, projection=_Projection(), active=True):
    monkeypatch.setattr(fee_tax_mcp, "_authorize", lambda *a, **k: ("owner", "org_EXAMPLE"))
    monkeypatch.setattr("core.country_registry.load_projection", lambda conn, **kw: projection)
    monkeypatch.setattr("core.tax_fee_presets.list_preset_versions", lambda conn, **kw: presets)
    conn = _Connection(_answers(active=active))
    monkeypatch.setattr("core.db.get_connection", lambda: _Ctx(conn))
    return fee_tax_mcp._auto_populate_tax_rules("proj_EXAMPLE")


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_exc):
        return False


def _data(result) -> dict:
    return result.structured_content["data"]


















def test_the_module_never_imports_the_superseded_auto_population() -> None:
    """B.3: the name collision that would reproduce the exact defect 48.4 removed."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "core.fee_tax_country_defaults" not in imported


# ---------------------------------------------------------------------------
# E.5 -- the prepare path.
# ---------------------------------------------------------------------------


class _PrepareWorld:
    """Records what the change-set services were handed, and refuses a publication."""

    def __init__(self, monkeypatch, *, published_rules=None, money=True, pinned=True):
        self.created: dict | None = None
        self.prepared_for: str | None = None
        self.token = "single-use-secret-value"
        self.conn = _Connection(_answers(money=money, capability_pinned=pinned))
        self.conn.commit = lambda: None
        monkeypatch.setattr(fee_tax_mcp, "_authorize", lambda *a, **k: ("owner", "org_EXAMPLE"))
        monkeypatch.setattr("core.db.get_connection", lambda: _Ctx(self.conn))
        head = {"id": "grs_EXAMPLE", "family": "tax_fee"}
        version = {
            "id": "grsv_EXAMPLE",
            "payload": {"rounding": "half_even", "default_money_basis": "native_source"},
            "ordered_rules": published_rules if published_rules is not None else [],
        }
        monkeypatch.setattr(
            "core.governance_rule_sets.active_version",
            lambda conn, **kw: ((head, version) if published_rules is not None else None),
        )
        monkeypatch.setattr("core.governance_rule_sets.fetch_rule_set", lambda conn, **kw: head)
        monkeypatch.setattr(
            "core.governance_rule_sets.ensure_rule_set",
            lambda conn, **kw: pytest.fail("the head already exists here"),
        )
        monkeypatch.setattr(
            "core.governance_rule_sets.draft_version",
            lambda *a, **k: pytest.fail("this surface must not draft a version"),
        )
        monkeypatch.setattr(
            "core.governance_rule_sets.publish_version",
            lambda *a, **k: pytest.fail("this surface must not publish a version"),
        )
        monkeypatch.setattr(
            "core.controls_change_sets.create_change_set", self._create_change_set
        )
        monkeypatch.setattr(
            "core.controls_change_sets.prepare_change_set", self._prepare_change_set
        )

    def _create_change_set(self, conn, **kwargs):
        self.created = kwargs
        return {"id": "ccs_EXAMPLE", "state": "draft"}

    def _prepare_change_set(self, conn, *, project_id, change_set_id, actor):  # noqa: ARG002
        self.prepared_for = change_set_id
        return (
            {
                "id": change_set_id,
                "state": "prepared",
                "dependency_fingerprint": "f" * 64,
                "diff": {"object_type": "rule-set", "intent_keys": ["ordered_rules"]},
                "impact": {"object_type": "rule-set", "active_exceptions": 0},
            },
            self.token,
        )
























# ---------------------------------------------------------------------------
# E.6 -- source-level guards. The class, not the instance.
# ---------------------------------------------------------------------------


def _string_constants(path: Path) -> set[str]:
    """Every string LITERAL in real code -- docstrings excluded.

    These assertions inspect what the module DOES, not what it says. This module
    names the retired constructs on purpose, to record why they are gone; a prose
    mention is documentation, a live literal is the defect.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def _called_names(path: Path) -> set[str]:
    """Every attribute or bare name this module CALLS."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_the_module_never_touches_the_superseded_rule_store() -> None:
    literals = _string_constants(_MODULE)
    for forbidden in ("app.fee_tax_rules", "fee_tax_rules.create_rule"):
        assert not any(forbidden in literal for literal in literals), forbidden
    called = _called_names(_MODULE)
    for forbidden in ("create_rule", "update_rule", "list_rules", "set_fee_tax_alignment"):
        assert forbidden not in called, forbidden




def test_no_production_identifier_appears_in_this_story_s_files() -> None:
    # Assembled from parts on purpose: written as one literal, this pattern would
    # match ITSELF in this file -- an instrument that is part of its own result.
    prefixes = ("proj", "conn", "ds")
    pattern = re.compile(
        "|".join([*(f"{prefix}_01" for prefix in prefixes), r"@gmail\.com"])
    )
    for path in (
        _MODULE,
        _SEEDING,
        _SCRIPT,
        Path(__file__),
        Path(__file__).resolve().parents[1] / "integration" / "test_fee_tax_mcp_seams.py",
    ):
        if not path.exists():
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), path


# ---------------------------------------------------------------------------
# E.7 -- the preset importer.
# ---------------------------------------------------------------------------


class _PresetStore:
    """An in-memory stand-in for `app.tax_fee_preset_versions`, keyed by content."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def count(self) -> int:
        return len(self.rows)


#: THE ONE STATEMENT `import_seed_presets` ISSUES against this fake. Everything
#: else it does goes through `record_preset_version`, which the test replaces
#: (`core/tax_fee_preset_seeding.py:87`, the before/after count).
#:
#: AI-317: the `else` here answered `None`, and `_preset_count` reads
#: `cur.fetchone()[0]` -- so an unmodelled read would have died inside the
#: PRODUCT with `TypeError: NoneType object is not subscriptable`, naming the
#: module instead of the fixture.
_PRESET_STORE = StatementInventory(
    "_StoreCursor (tax_fee_preset_seeding._preset_count)",
    preset_count=("select count(*)", "from app.tax_fee_preset_versions"),
)


class _StoreCursor:
    def __init__(self, store: _PresetStore) -> None:
        self._store = store
        self._row: object = None
        self.description: list[tuple[str]] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):  # noqa: ARG002
        _PRESET_STORE.match(sql)
        # Named rather than derived: `describe` refuses an aggregate projection,
        # and psycopg reports `count` for `COUNT(*)`.
        self.description = [("count",)]
        self._row = (self._store.count(),)

    def fetchone(self):
        return self._row


def test_the_store_fake_refuses_a_statement_it_was_never_taught() -> None:
    """The untaught statement here is a REAL one: the idempotency lookup.

    `record_preset_version` is monkeypatched in this file, so its content-hash
    read never reaches this fake. The day it does, the fake says so instead of
    answering a count.
    """
    store = _PresetStore()
    with pytest.raises(UnknownStatement) as raised:
        _StoreCursor(store).execute(
            "SELECT id FROM app.tax_fee_preset_versions WHERE content_hash = %s"
        )
    message = str(raised.value)
    assert "where content_hash" in message
    assert "preset_count" in message


class _StoreConnection:
    def __init__(self, store: _PresetStore) -> None:
        self._store = store

    def cursor(self):
        return _StoreCursor(self._store)


def _seed_csv(tmp_path, *, note="Confirm with your tax advisor.", rate="0.030000"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "country_tax_defaults.csv"
    path.write_text(
        "iso_code,tax_category,form,rate,label,source_note,effective_from\n"
        f'FR,REGULATORY_TAX,PERCENTAGE,{rate},France DST,"{note}",2019-01-01\n'
        f'DE,SALES_TAX,PERCENTAGE,0.190000,Germany VAT,"{note}",2007-01-01\n',
        encoding="utf-8",
    )
    return path


def test_the_importer_is_idempotent_by_content(monkeypatch, tmp_path) -> None:
    from core import tax_fee_preset_seeding

    store = _PresetStore()
    conn = _StoreConnection(store)

    def _record(conn_arg, *, payload, org_id, actor, publish=False):  # noqa: ARG001
        from core.governance_rule_sets import content_hash
        from core.tax_fee_presets import validate_preset

        digest = content_hash({**validate_preset(payload), "org_id": org_id})
        if digest in store.rows:
            return store.rows[digest]["id"]
        store.rows[digest] = {"id": f"tfpv_{len(store.rows)}", "publish": publish}
        return store.rows[digest]["id"]

    monkeypatch.setattr("core.tax_fee_presets.record_preset_version", _record)
    path = _seed_csv(tmp_path)

    first = tax_fee_preset_seeding.import_seed_presets(
        conn, actor="owner@example.com", publish=True, path=path
    )
    assert (first["rows"], first["created"], first["unchanged"]) == (2, 2, 0)

    second = tax_fee_preset_seeding.import_seed_presets(
        conn, actor="owner@example.com", publish=True, path=path
    )
    assert (second["rows"], second["created"], second["unchanged"]) == (2, 0, 2)
    assert second["preset_version_ids"] == first["preset_version_ids"]


def test_a_row_with_no_source_note_aborts_the_whole_import(tmp_path) -> None:
    from core.fee_tax_country_defaults import CountryTaxDefaultsError
    from core.tax_fee_preset_seeding import seed_preset_payloads

    path = _seed_csv(tmp_path, note="")
    with pytest.raises(CountryTaxDefaultsError):
        seed_preset_payloads(path)


def test_the_source_reference_version_changes_when_the_seed_changes(tmp_path) -> None:
    from core.tax_fee_preset_seeding import seed_source_reference_version

    first = seed_source_reference_version(_seed_csv(tmp_path / "a"))
    second = seed_source_reference_version(_seed_csv(tmp_path / "b", rate="0.040000"))
    third = seed_source_reference_version(_seed_csv(tmp_path / "c", note="Weaker caveat."))
    assert first != second, "a corrected rate must produce a new version"
    assert first != third, "a weakened caveat must produce a new version too"
    assert first.startswith("country_tax_defaults@")
    # Same bytes, same version: the digest is over content, not over a run time.
    assert first == seed_source_reference_version(_seed_csv(tmp_path / "d"))


def test_the_importer_carries_the_seed_caveat_into_every_payload(tmp_path) -> None:
    from core.tax_fee_preset_seeding import seed_preset_payloads

    note = "Confirm against your platform invoice and your tax advisor before use."
    payloads = seed_preset_payloads(_seed_csv(tmp_path, note=note))
    assert payloads
    for payload in payloads:
        assert payload["assumptions"] == [note]
        assert payload["verification_status"] == "unverified"
        assert payload["qualifications"], "a country and a rate are not a qualification"


# ---------------------------------------------------------------------------
# AC14 -- one answer, two doors.
# ---------------------------------------------------------------------------




def test_the_capability_read_fails_soft_like_its_three_siblings(monkeypatch) -> None:
    from core import project_capabilities_mcp

    def _boom(conn, project_id):
        raise RuntimeError("owner unreadable")

    monkeypatch.setattr(fee_tax_mcp, "ladder_summary", _boom)
    assert project_capabilities_mcp._foundation_summary(None, "proj_EXAMPLE", "tax_fees") == {}
