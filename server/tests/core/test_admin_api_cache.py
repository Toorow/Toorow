"""Tests for Story 19.3 -- GET /api/admin/cache/status et
POST /api/admin/cache/rebuild.

Coverage :
  - GET /api/admin/cache/status :
      fresh   (manifeste frais, TOOROW_CACHE_ENABLED=true)
      stale   (manifeste perime, TOOROW_CACHE_ENABLED=true)
      no-cache (pas de manifeste, TOOROW_CACHE_ENABLED=true)
      disabled (TOOROW_CACHE_ENABLED=false)
  - GET /api/admin/cache/status : 401 sans auth.
  - GET /api/admin/cache/status : hit rate calcule depuis get_cache_stats().
  - POST /api/admin/cache/rebuild : rebuild audite (performed_by = identite REELLE).
  - POST /api/admin/cache/rebuild : 401 sans auth.
  - POST /api/admin/cache/rebuild : 403 si TOOROW_CACHE_ENABLED=false.
  - POST /api/admin/cache/rebuild : invariant f -- un rebuild qui echoue retourne
    {"status": "failed"} sans 500 brut.
  - Preuve par test qu'un cache non reconstruit apres re-pull est detecte "stale"
    (la regle _cache_is_fresh de 19.2 retourne False => cache_state="stale").

Strategie :
  - build_asgi_app (AI-56) + TestClient.
  - AsyncMock _check_auth (pattern test_admin_api_google_status.py).
  - Mock read_manifest (cache_warehouse) + _cache_is_fresh + get_cache_stats
    (warehouse) pour isoler du fichier DuckDB reel.
  - Mock cache_warehouse.rebuild_cache pour les tests rebuild.
  - Pas de DB reelle, pas de fichier DuckDB.

SCHEDULER_ENABLED=false, HEALTH_POLLER_ENABLED=false (pattern habituel).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth_ok():
    """Patch _check_auth pour qu'il retourne (True, 'test-user')."""
    return patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(True, "test-user")),
    )


def _auth_fail():
    """Patch _check_auth pour qu'il retourne (False, None) -> 401."""
    return patch(
        "core.admin_api._check_auth",
        new=AsyncMock(return_value=(False, None)),
    )


_SAMPLE_MANIFEST = {
    "schema_version": 1,
    "cache_built_at": "2026-07-19T01:00:00+00:00",
    "min_date": "2026-06-19",
    "max_date": "2026-07-19",
    "tables": ["fact_daily_kpi", "semantic_ctr"],
    "row_counts": {"fact_daily_kpi": 500, "semantic_ctr": 120},
    "project_ids": ["proj_alpha"],
}

_SAMPLE_STATS = {"hit": 40, "miss-relation": 5, "bypass-stale": 2}


# ---------------------------------------------------------------------------
# GET /api/admin/cache/status
# ---------------------------------------------------------------------------


class TestCacheStatus:
    """Tests pour GET /api/admin/cache/status (seam build_asgi_app, AI-56)."""

    def _get(self, env_override: dict | None = None):
        env = {"TOOROW_CACHE_ENABLED": "true", **(env_override or {})}
        with _auth_ok(), patch.dict(os.environ, env, clear=False):
            client = _build_client()
            return client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )

    def test_returns_401_without_auth(self):
        """Requete sans auth -> 401."""
        with _auth_fail(), patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}):
            client = _build_client()
            resp = client.get("/api/admin/cache/status")
        assert resp.status_code == 401

    def test_state_disabled_when_cache_not_enabled(self):
        """TOOROW_CACHE_ENABLED=false -> cache_state='disabled' (AI-56 : valeurs assertees)."""
        with _auth_ok(), patch.dict(
            os.environ, {"TOOROW_CACHE_ENABLED": "false"}, clear=False
        ):
            client = _build_client()
            resp = client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_state"] == "disabled"
        assert data["cache_enabled"] is False
        assert data["tables"] == []
        assert data["row_counts"] == {}

    def test_state_no_cache_when_manifest_absent(self):
        """Manifeste absent -> cache_state='no-cache' (ephemere perdu, invariant f)."""
        with (
            _auth_ok(),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}, clear=False),
            patch("core.cache_warehouse.read_manifest", return_value=None),
            patch("core.warehouse.get_cache_stats", return_value={}),
        ):
            client = _build_client()
            resp = client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_state"] == "no-cache"
        assert data["cache_enabled"] is True
        assert data["cache_built_at"] is None

    def test_state_fresh_when_cache_is_fresh(self):
        """Manifeste frais -> cache_state='fresh' ; tables + row_counts presentes (AI-56)."""
        with (
            _auth_ok(),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}, clear=False),
            patch("core.cache_warehouse.read_manifest", return_value=_SAMPLE_MANIFEST),
            patch("core.warehouse.get_cache_stats", return_value=_SAMPLE_STATS),
            patch("core.warehouse._cache_is_fresh", return_value=True),
        ):
            client = _build_client()
            resp = client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        # AI-56 : assertions sur les valeurs, pas juste HTTP 200.
        assert data["cache_state"] == "fresh"
        assert data["cache_enabled"] is True
        assert data["cache_built_at"] == "2026-07-19T01:00:00+00:00"
        assert data["min_date"] == "2026-06-19"
        assert data["max_date"] == "2026-07-19"
        assert "fact_daily_kpi" in data["tables"]
        assert data["row_counts"]["fact_daily_kpi"] == 500
        assert data["project_ids"] == ["proj_alpha"]
        # Hit rate = 40 / (40 + 5 + 2) = 40/47
        assert data["hit_rate"] is not None
        assert abs(data["hit_rate"] - 40 / 47) < 1e-6
        assert data["age_seconds"] is not None

    def test_state_stale_when_cache_is_stale(self):
        """Manifeste perime -> cache_state='stale' (invariant c : jamais du frais mensonger).

        Ceci prouve que la regle _cache_is_fresh de 19.2 est REUTILISEE (pas
        dupliquee) et que le cache non reconstruit apres re-pull est detecte 'stale'.
        """
        with (
            _auth_ok(),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}, clear=False),
            patch("core.cache_warehouse.read_manifest", return_value=_SAMPLE_MANIFEST),
            patch("core.warehouse.get_cache_stats", return_value={}),
            patch("core.warehouse._cache_is_fresh", return_value=False),
        ):
            client = _build_client()
            resp = client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_state"] == "stale"
        # Les metadonnees du manifeste sont toujours exposees (pour diagnostiquer l'age).
        assert data["cache_built_at"] is not None
        assert data["min_date"] == "2026-06-19"

    def test_hit_rate_none_when_no_decisions(self):
        """Aucune decision prise -> hit_rate=null (division par zero evitee)."""
        with (
            _auth_ok(),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}, clear=False),
            patch("core.cache_warehouse.read_manifest", return_value=_SAMPLE_MANIFEST),
            patch("core.warehouse.get_cache_stats", return_value={}),
            patch("core.warehouse._cache_is_fresh", return_value=True),
        ):
            client = _build_client()
            resp = client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit_rate"] is None


# ---------------------------------------------------------------------------
# POST /api/admin/cache/rebuild
# ---------------------------------------------------------------------------


class TestCacheRebuild:
    """Tests pour POST /api/admin/cache/rebuild (seam build_asgi_app, AI-56)."""

    def _post(self, rebuild_result: dict, env_override: dict | None = None):
        env = {"TOOROW_CACHE_ENABLED": "true", **(env_override or {})}
        with (
            _auth_ok(),
            patch.dict(os.environ, env, clear=False),
            patch("core.cache_warehouse.rebuild_cache", return_value=rebuild_result),
            patch("core.admin_api.write_audit_row") as audit_mock,
        ):
            client = _build_client()
            resp = client.post(
                "/api/admin/cache/rebuild",
                headers={"Authorization": "Bearer test-secret"},
            )
        return resp, audit_mock

    def test_returns_401_without_auth(self):
        """Requete sans auth -> 401."""
        with _auth_fail(), patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}):
            client = _build_client()
            resp = client.post("/api/admin/cache/rebuild")
        assert resp.status_code == 401

    def test_returns_403_when_cache_disabled(self):
        """TOOROW_CACHE_ENABLED=false -> 403 (inutile de reconstruire un cache desactive)."""
        with (
            _auth_ok(),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "false"}, clear=False),
            patch("core.admin_api.write_audit_row") as audit_mock,
        ):
            client = _build_client()
            resp = client.post(
                "/api/admin/cache/rebuild",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 403
        data = resp.json()
        assert data["code"] == "cache_disabled"
        # L'audit est quand meme ecrit (observabilite de la tentative).
        assert audit_mock.called

    def test_rebuild_ok_returns_result_and_performed_by(self):
        """Rebuild reussi -> 200 avec status/tables/row_counts + performed_by (AI-56 + AD-14)."""
        rebuild_result = {
            "status": "ok",
            "tables": ["fact_daily_kpi"],
            "row_counts": {"fact_daily_kpi": 300},
            "project_ids": ["proj_alpha"],
            "min_date": "2026-06-19",
            "max_date": "2026-07-19",
            "cache_built_at": "2026-07-19T02:00:00+00:00",
            "cache_path": "cache_warehouse.duckdb",
            "reason": None,
        }
        resp, audit_mock = self._post(rebuild_result)
        assert resp.status_code == 200
        data = resp.json()
        # AI-56 : valeurs assertees.
        assert data["status"] == "ok"
        assert data["tables"] == ["fact_daily_kpi"]
        assert data["row_counts"]["fact_daily_kpi"] == 300
        # AD-14 : performed_by = identite reelle du Bearer token.
        assert data["performed_by"] == "test-user"
        # Audit ecrit (au moins une fois pour la demande + une fois pour le resultat).
        assert audit_mock.call_count >= 1
        # review-19-3 F-6 (AD-14): l'audit row porte l'identite reelle, pas un
        # compte de service -- coherence reponse JSON <-> contenu de l'audit.
        assert any(
            c.kwargs.get("identity") == "test-user"
            for c in audit_mock.call_args_list
        ), "write_audit_row doit etre appele avec identity='test-user' (AD-14)"

    def test_performed_by_is_real_identity(self):
        """performed_by doit etre l'identite reelle, jamais 'system' (AD-14)."""
        rebuild_result = {"status": "ok", "tables": [], "row_counts": {}, "project_ids": []}
        resp, _ = self._post(rebuild_result)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("performed_by") not in (None, "system", "anonymous"), (
            "performed_by doit etre l'identite reelle du Bearer token, jamais 'system' (AD-14)"
        )

    def test_invariant_f_rebuild_failure_returns_clean_response(self):
        """Invariant f : un rebuild qui echoue retourne {"status":"failed"} sans 500 brut."""
        rebuild_result = {"status": "failed", "reason": "origin unreachable"}
        resp, _ = self._post(rebuild_result)
        # Ne doit JAMAIS etre 500 -- c'est le coeur de l'invariant f.
        assert resp.status_code == 200, (
            "invariant f viole : un rebuild qui echoue doit retourner 200 avec status='failed', "
            f"pas {resp.status_code}"
        )
        data = resp.json()
        assert data["status"] == "failed"
        assert "performed_by" in data

    def test_invariant_f_rebuild_raises_internally_returns_clean_response(self):
        """Invariant f : meme si rebuild_cache() leve une exception inattendue, pas de 500."""
        with (
            _auth_ok(),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}, clear=False),
            patch(
                "core.cache_warehouse.rebuild_cache",
                side_effect=RuntimeError("unexpected explosion"),
            ),
            patch("core.admin_api.write_audit_row"),
        ):
            client = _build_client()
            resp = client.post(
                "/api/admin/cache/rebuild",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200, (
            "invariant f : une exception inattendue dans rebuild_cache ne doit pas "
            f"produire un 500 brut -- recu {resp.status_code}"
        )
        data = resp.json()
        assert data["status"] == "failed"

    def test_audit_written_with_manual_trigger(self):
        """L'audit est ecrit avec trigger='manual' et performed_by=identite (AD-14)."""
        rebuild_result = {"status": "ok", "tables": [], "row_counts": {}, "project_ids": []}
        _resp, audit_mock = self._post(rebuild_result)
        # On verifie qu'au moins un appel audit contient trigger='manual'.
        audit_calls = audit_mock.call_args_list
        manual_calls = [
            c for c in audit_calls
            if c.kwargs.get("metadata", {}).get("trigger") in ("manual", "manual_result")
        ]
        assert len(manual_calls) >= 1, (
            "L'audit doit etre ecrit avec trigger='manual' (AD-14 On-Behalf-Of)"
        )
        # review-19-3 F-1 (AD-14): chaque audit manual porte l'identite reelle.
        assert all(
            c.kwargs.get("identity") == "test-user" for c in manual_calls
        ), "Les audits trigger=manual doivent porter performed_by=identite reelle"


# ---------------------------------------------------------------------------
# Preuve invalidity detection (invariant c via l'endpoint)
# ---------------------------------------------------------------------------


class TestCacheStaleProofViaEndpoint:
    """Prouve qu'un cache non reconstruit apres re-pull est detecte 'stale' (19.3 spec).

    Le nightly re-pull (7 j) met a jour les marts -> _cache_is_fresh retourne False
    -> l'endpoint expose cache_state='stale' -> les lectures bypassent (19.2).
    Ce test prouve le chemin observable : cache_built_at ANTERIEUR au dernier nightly
    attendu => _cache_is_fresh(False) => status='stale' sur l'endpoint.

    La regle _cache_is_fresh est REUTILISEE (pas dupliquee) -- prouvee dans
    test_state_stale_when_cache_is_stale ci-dessus par le mock de la meme fonction.
    """

    def test_stale_detected_after_nightly_reruns_without_rebuild(self):
        """Apres un nightly sans rebuild, l'endpoint expose 'stale' (invariant c).

        Simule : cache_built_at = hier, _cache_is_fresh = False (nightly a tourne).
        """
        old_manifest = {
            **_SAMPLE_MANIFEST,
            "cache_built_at": "2026-07-18T01:00:00+00:00",  # veille
        }
        with (
            patch(
                "core.admin_api._check_auth",
                new=AsyncMock(return_value=(True, "test-user")),
            ),
            patch.dict(os.environ, {"TOOROW_CACHE_ENABLED": "true"}, clear=False),
            patch("core.cache_warehouse.read_manifest", return_value=old_manifest),
            patch("core.warehouse.get_cache_stats", return_value={}),
            # _cache_is_fresh retourne False : le nightly a tourne depuis le build.
            patch("core.warehouse._cache_is_fresh", return_value=False),
        ):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            client = TestClient(build_asgi_app(), raise_server_exceptions=False)
            resp = client.get(
                "/api/admin/cache/status",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        # L'endpoint doit exposer 'stale', jamais 'fresh'.
        assert data["cache_state"] == "stale", (
            "invariant c : un cache anterieur au dernier nightly DOIT etre 'stale' "
            "sur l'endpoint -- jamais du frais mensonger"
        )
        # Les metadonnees du manifeste sont exposees pour le diagnostic.
        assert data["cache_built_at"] == "2026-07-18T01:00:00+00:00"
