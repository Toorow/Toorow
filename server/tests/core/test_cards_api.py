"""Tests for the card REST mirror (Story 9.1, Epic 9).

Two layers:
  1. Router(CARDS_ROUTES) + TestClient — exercise each handler (auth bypassed), the
     same pattern as test_datamodel_api.
  2. G-14 lesson: mount CARDS_ROUTES into the REAL build_asgi_app() router and hit
     the endpoint through the full ASGI app, proving the routes are wireable exactly
     as the orchestrator will splice them into admin_api.router.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core.cards_api import CARDS_ROUTES  # noqa: E402
from starlette.routing import Router  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _rows():
    return [
        {
            "date": "2026-07-05",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 100.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-05T00:00:00",
        },
        {
            "date": "2026-07-06",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 120.0,
            "pull_id": "pull_1",
            "loaded_at": "2026-07-06T00:00:00",
        },
    ]


@pytest.fixture()
def client():
    app = Router(routes=CARDS_ROUTES)
    with patch(
        "core.cards_api._check_auth",
        new=AsyncMock(return_value=(True, "test@test")),
    ):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# GET /api/cards/templates
# ---------------------------------------------------------------------------


def test_templates_returns_catalog(client):
    resp = client.get("/api/cards/templates")
    assert resp.status_code == 200
    body = resp.json()
    assert "templates" in body
    assert any(t["id"] == "kpi" for t in body["templates"])


def test_templates_catalog_carries_admin_fields(client):
    """Story 9.8: the admin "Cartes" catalog exposes kind + comment_builder presence."""
    resp = client.get("/api/cards/templates")
    body = resp.json()
    by_id = {t["id"]: t for t in body["templates"]}
    # kind labels context vs kpi.
    assert by_id["kpi"]["kind"] == "kpi"
    assert by_id["connectors"]["kind"] == "context"
    # comment_builder presence: business cards have a deterministic builder, kpi does not.
    assert by_id["conversions"]["comment_builder"] is True
    assert by_id["kpi"]["comment_builder"] is False
    # required/optional metrics + dimensions + widget_uri + fallback_rank all present.
    for key in (
        "required_metrics",
        "optional_dimensions",
        "optional_metrics",
        "widget_uri",
        "fallback_rank",
    ):
        assert key in by_id["keywords"]


def test_templates_per_project_usability(client):
    """Story 9.8: ?project_id= adds per-project usable/missing from the project's fields."""
    # Project maps only sessions+device_category -> usertypes needs both, satisfied;
    # conversions needs 'conversions' -> not usable.
    available = [("sessions",), ("active_users",), ("device_category",)]

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return available

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=_Conn())
    ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
    ):
        resp = client.get("/api/cards/templates?project_id=projA")
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()["templates"]}
    assert by_id["usertypes"]["usable"] is True
    assert by_id["conversions"]["usable"] is False
    assert "conversions" in by_id["conversions"]["missing"]["metrics"]
    # Context card is always usable (app-level sources).
    assert by_id["connectors"]["usable"] is True


def test_templates_dimension_not_satisfied_by_like_named_metric(client):
    """review-epic-9-backend F-6: a template requiring dimension X is NOT usable just
    because a METRIC named X exists (metric and dimension pools are separate).

    We craft a fake module manifest where 'page' is a canonical METRIC (not a dimension)
    and clicks/impressions are metrics. The keywords card requires 'page' as a DIMENSION,
    so even though a field literally named 'page' is mapped (as a metric), keywords must be
    reported NOT usable, with 'page' listed under missing.dimensions.
    """
    from unittest.mock import MagicMock

    # Project maps clicks, impressions, page (all as target_fields in the mapping table).
    available = [("clicks",), ("impressions",), ("page",)]

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return available

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=_Conn())
    ctx.__exit__ = MagicMock(return_value=False)

    # Fake module: 'page' is a canonical METRIC here (deliberately mis-kinded), so the
    # dimension pool has NO 'page'. clicks/impressions are metrics; NO dimensions at all.
    class _FakeMod:
        manifest = {
            "canonical_metric_mapping": {
                "clicks": "clicks",
                "impressions": "impressions",
                "page": "page",  # page classified as a METRIC in the vocabulary
            },
            "canonical_dimension_mapping": {},
        }

    with (
        patch("core.db.get_connection", return_value=ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.cards_api._loaded_modules", return_value=[_FakeMod()]),
    ):
        resp = client.get("/api/cards/templates?project_id=projK")
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()["templates"]}
    # keywords requires page as a DIMENSION; 'page' exists only as a metric -> NOT usable.
    assert by_id["keywords"]["usable"] is False
    assert "page" in by_id["keywords"]["missing"]["dimensions"]
    # sanity: clicks/impressions were satisfied as metrics (not listed missing).
    assert by_id["keywords"]["missing"]["metrics"] == []


def test_templates_per_project_access_denied_404(client):
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    with (
        patch("core.db.get_connection", return_value=ctx),
        patch("core.project_access.identity_has_project_access", return_value=False),
    ):
        resp = client.get("/api/cards/templates?project_id=projB")
    assert resp.status_code == 404


def test_templates_unauthorized():
    app = Router(routes=CARDS_ROUTES)
    with patch("core.cards_api._check_auth", new=AsyncMock(return_value=(False, ""))):
        with TestClient(app) as c:
            assert c.get("/api/cards/templates").status_code == 401


def test_get_card_connectors_via_rest_needs_no_metrics(client):
    """Story 9.8: a context card is reachable over REST with only project_id+template."""
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    with (
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection", side_effect=RuntimeError("db down")),
    ):
        # DB down inside the card resolver -> designed empty (still 200).
        resp = client.get("/api/cards?project_id=default&template=connectors")
    assert resp.status_code == 200
    assert resp.json()["widget_uri"] == "ui://core/card-connectors"


# ---------------------------------------------------------------------------
# GET /api/cards
# ---------------------------------------------------------------------------


def test_get_card_requires_project_id(client):
    resp = client.get("/api/cards?metrics=sessions")
    assert resp.status_code == 400


def test_get_card_requires_metrics_or_report_ref(client):
    resp = client.get("/api/cards?project_id=default")
    assert resp.status_code == 400


def test_get_card_returns_envelope(client):
    with (
        patch("core.warehouse.query_daily_report", return_value=_rows()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
        patch("core.cards._fetch_r6_adhoc", return_value=(None, None)),
        patch("core.cards._fetch_card_config", return_value={}),
    ):
        resp = client.get("/api/cards?project_id=default&metrics=sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["widget_uri"] == "ui://core/card-kpi"
    assert body["envelope"]["data"]["card_id"] == "kpi"
    assert body["envelope"]["meta"]["card_selection"]["chosen"] == "kpi"
    assert body["summary"].strip()


def test_get_card_unknown_template_404(client):
    with (
        patch("core.warehouse.query_daily_report", return_value=_rows()),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection", side_effect=RuntimeError("offline")),
    ):
        resp = client.get("/api/cards?project_id=default&metrics=sessions&template=nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# G-14: routes wire into the real build_asgi_app router
# ---------------------------------------------------------------------------


def test_cards_routes_wire_through_build_asgi_app():
    """Splice CARDS_ROUTES into the real ASGI app and hit /api/cards/templates.

    Proves the routes mount + dispatch through the full stack exactly as the
    orchestrator will add them to admin_api.router (the G-14 lesson: verify the
    REST mirror through build_asgi_app, not just an isolated Router).
    """
    from core.admin_api import router as admin_router
    from core.main import build_asgi_app

    # Add the card routes to the shared admin router (idempotent for the test run).
    existing = {(r.path, tuple(sorted(r.methods or []))) for r in admin_router.routes}
    for route in CARDS_ROUTES:
        key = (route.path, tuple(sorted(route.methods or [])))
        if key not in existing:
            admin_router.routes.append(route)

    app = build_asgi_app()
    with patch("core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get("/api/cards/templates", headers={"Host": "localhost"})
    assert resp.status_code == 200
    assert any(t["id"] == "kpi" for t in resp.json()["templates"])


# ---------------------------------------------------------------------------
# Story 27.6: synonym enrichment of the canonical vocabulary (additive, fail-soft).
# ---------------------------------------------------------------------------


class _SynMod:
    """A fake module whose manifest declares one canonical metric + one dimension."""

    manifest = {
        "canonical_metric_mapping": {"revenue": "revenue"},
        "canonical_dimension_mapping": {"country": "country"},
    }


def test_flatten_synonym_terms_forms():
    """§35: dict-with-terms, bare strings, and noise are handled purely."""
    from core.cards_api import _flatten_synonym_terms

    assert _flatten_synonym_terms([{"lang": "fr", "terms": ["CA", "chiffre"]}]) == {
        "CA",
        "chiffre",
    }
    assert _flatten_synonym_terms(["CA", "revenue"]) == {"CA", "revenue"}
    # Noise: None, non-list, non-string members -> empty set.
    assert _flatten_synonym_terms(None) == set()
    assert _flatten_synonym_terms("CA") == set()
    assert _flatten_synonym_terms([None, 123, {"no_terms": 1}]) == set()


def test_synonym_terms_land_in_metric_pool_not_dimension():
    """§36: resolved synonyms enrich the METRIC pool of _canonical_vocabulary."""
    from core import cards_api

    resolved = {
        "revenue": {"synonyms": [{"lang": "fr", "terms": ["CA"]}]},
    }
    with (
        patch("core.metric_semantics._load_definition_rows", return_value=list(resolved.values())),
        patch(
            "core.metric_semantics.reduce_definitions_by_specificity",
            return_value=resolved,
        ),
        patch("core.cards_api._loaded_modules", return_value=[_SynMod()]),
    ):
        metrics, dimensions = cards_api._canonical_vocabulary()
    assert "CA" in metrics  # synonym added to the metric pool
    assert "revenue" in metrics
    assert "CA" not in dimensions  # never the dimension pool
    assert "country" in dimensions


def test_synonym_enrichment_fail_soft_matches_baseline():
    """§37 (key): DB down -> _synonym_metric_terms()==set() and vocabulary == baseline."""
    from core import cards_api

    with patch("core.cards_api._loaded_modules", return_value=[_SynMod()]):
        # Baseline: no layer-27 rows at all (patch loader to raise) == manifests only.
        with patch(
            "core.metric_semantics._load_definition_rows",
            side_effect=RuntimeError("db down"),
        ):
            assert cards_api._synonym_metric_terms() == set()
            metrics_down, dims_down = cards_api._canonical_vocabulary()

    assert metrics_down == {"revenue"}
    assert dims_down == {"country"}


def test_synonym_collision_deduplicates():
    """§39: a synonym equal to an existing canonical is a no-op (set dedup)."""
    from core import cards_api

    resolved = {"revenue": {"synonyms": ["revenue"]}}
    with (
        patch("core.metric_semantics._load_definition_rows", return_value=list(resolved.values())),
        patch(
            "core.metric_semantics.reduce_definitions_by_specificity",
            return_value=resolved,
        ),
        patch("core.cards_api._loaded_modules", return_value=[_SynMod()]),
    ):
        metrics, _ = cards_api._canonical_vocabulary()
    assert metrics == {"revenue"}  # collision deduped, no phantom entry


def test_synonym_colliding_with_dimension_excluded_from_metric_pool():
    """F-2 (27.6): a synonym equal to a canonical DIMENSION name never enters the metric pool.

    _SynMod declares the canonical dimension 'country'. A definition whose synonyms include
    'country' must NOT push 'country' into the metric pool (it would make the field both a
    metric and a dimension -- ambiguous classification). The dimensions are subtracted before
    the union, so 'country' stays a pure dimension while a genuine synonym ('CA') still lands.
    """
    from core import cards_api

    resolved = {
        "revenue": {"synonyms": [{"lang": "fr", "terms": ["CA", "country"]}]},
    }
    with (
        patch("core.metric_semantics._load_definition_rows", return_value=list(resolved.values())),
        patch(
            "core.metric_semantics.reduce_definitions_by_specificity",
            return_value=resolved,
        ),
        patch("core.cards_api._loaded_modules", return_value=[_SynMod()]),
    ):
        metrics, dimensions = cards_api._canonical_vocabulary()
    assert "country" not in metrics  # dimension synonym kept OUT of the metric pool
    assert "country" in dimensions  # stays a pure dimension
    assert "CA" in metrics  # a genuine (non-colliding) synonym still enriches metrics
