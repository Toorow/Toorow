from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parents[4]
MODULE_DIR = ROOT / "server" / "modules" / "amazon-dsp"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_amazon_dsp", MODULE_DIR / "connector.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, content=b"", text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.content = content
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_product_is_standalone_and_both_ids_load():
    source = (MODULE_DIR / "connector.py").read_text()
    assert "amazon-ads" not in source
    assert "server.modules.amazon" not in source
    dsp = json.loads((MODULE_DIR / "manifest.json").read_text())
    sponsored = json.loads(
        (ROOT / "server" / "modules" / "amazon-ads" / "manifest.json").read_text()
    )
    assert {dsp["name"], sponsored["name"]} == {"amazon-dsp", "amazon-ads"}
    assert dsp["account_topology"]["levels"] != sponsored["account_topology"]["levels"]


def test_catalog_is_exactly_541_zero_planned_and_11_types():
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    report = json.loads((MODULE_DIR / "catalog_sources" / "fusion-report.json").read_text())
    sources = json.loads((MODULE_DIR / "catalog_sources" / "catalog_sources.json").read_text())
    assert len(catalog["fields"]) == 541
    assert report["official_total"] == 541
    assert set(report["exposure_counts"]) == {"exposed", "excluded"}
    assert report["exposure_counts"]["exposed"] + report["exposure_counts"]["excluded"] == 541
    assert len(sources["report_type_compatibility"]["report_types"]) == 11


def test_discovery_uses_dsp_account_header_topology_not_profiles(connector):
    client = MagicMock()
    client.request.return_value = Response(
        payload={
            "accounts": [
                {
                    "adsAccountId": "acct-1",
                    "advertisers": [
                        {"advertiserId": "adv-1", "name": "DSP advertiser", "timezone": "UTC"}
                    ],
                }
            ]
        }
    )
    result = connector.discover_accounts(
        "conn", regions=("EU",), _client=client, _token_value="token", _client_id_value="cid"
    )
    assert result[0]["ads_account_id"] == "acct-1"
    assert result[0]["advertiser_id"] == "adv-1"
    assert "Amazon-Advertising-API-Scope" not in client.request.call_args.kwargs["headers"]


def test_empty_dsp_seat_is_actionable(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"accounts": []})
    with pytest.raises(connector.AmazonDspOnboardingError, match="DSP seat"):
        connector.discover_accounts(
            "conn", regions=("NA",), _client=client, _token_value="t", _client_id_value="c"
        )


def test_default_request_is_valid_and_carries_full_routing_hash(connector):
    request = connector.build_report_request(
        "dspCampaign",
        "campaign",
        ["date", "impressions", "clicks"],
        "2026-07-01",
        "2026-07-02",
        "adv",
        region="EU",
        ads_account_id="acct",
    )
    assert request["configuration"]["adProduct"] == "AMAZON_DSP"
    assert request["routing"] == {"region": "EU", "adsAccountId": "acct", "advertiserId": "adv"}
    changed = json.loads(json.dumps(request))
    changed["routing"]["adsAccountId"] = "other"
    assert connector.canonical_request_hash(request) != connector.canonical_request_hash(changed)


def test_incompatible_shape_and_benchmark_fail_before_call(connector):
    with pytest.raises(connector.AmazonDspCompatibilityError, match="Illegal DSP shape"):
        connector.validate_selection("dspCampaign", "order", "DAILY", ["impressions"])
    with pytest.raises(connector.AmazonDspCompatibilityError, match="dedicated"):
        connector.validate_selection("dspBenchmarks", "brandCategoryBenchmarks", "DAILY", [])


def test_report_specific_window_splitting(connector):
    assert connector.split_date_windows("2026-01-01", "2026-02-01", 31) == [
        ("2026-01-01", "2026-01-31"),
        ("2026-02-01", "2026-02-01"),
    ]


def test_425_reuses_existing_report_and_mandatory_account_header(connector):
    client = MagicMock()
    client.request.return_value = Response(425, {"existingReportId": "r-existing"})
    flow = connector._DspReportFlow(
        client,
        connector.REGIONAL_HOSTS["EU"],
        connector._headers("token", "client", "acct"),
        {"configuration": {}, "routing": {"region": "EU", "adsAccountId": "acct"}},
    )
    assert flow.submit() == "r-existing"
    headers = client.request.call_args.kwargs["headers"]
    assert headers["Amazon-Ads-AccountId"] == "acct"


def test_poll_tolerates_two_401_then_maps_statuses(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(401),
        Response(401),
        Response(200, {"status": "PROCESSING"}),
        Response(200, {"status": "COMPLETED", "url": "https://signed.example/result"}),
    ]
    flow = connector._DspReportFlow(client, "https://host", {}, {})
    assert [flow.poll("r") for _ in range(4)] == ["pending", "pending", "processing", "completed"]
    assert flow.download_url == "https://signed.example/result"


def test_gzip_download_uses_presigned_url_without_amazon_headers(connector, monkeypatch):
    calls = []
    body = gzip.compress(json.dumps([{"date": "2026-07-01", "impressions": 1}]).encode())

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(200, headers={"Content-Type": "application/json"}, content=body)

    monkeypatch.setattr(connector.httpx, "get", fake_get)
    flow = connector._DspReportFlow(MagicMock(), "https://host", {"Authorization": "secret"}, {})
    flow.download_url = "https://signed.example/result"
    assert flow.download("r")[0]["impressions"] == 1
    assert calls == [("https://signed.example/result", {"timeout": 60})]


def test_429_uses_retry_after_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(429, {}, {"Retry-After": "9"}))
    assert raised.value.retry_after == 9


def _stub_report(connector, monkeypatch, captured):
    """Stub the whole async report flow: capture the request, land nothing."""

    def fake_run(connection_id, request, **kwargs):
        captured.append(request)
        return {"status": "completed", "rows": []}

    monkeypatch.setattr(connector, "run_dsp_report", fake_run)
    monkeypatch.setattr(connector, "_land", lambda rows, context: 0)


def test_the_manifest_declares_the_parameter_that_carries_the_account():
    """Le compte arrive par le nom que le manifeste declare, pas par `selection`.

    `selection` est la selection de RAPPORT (datastream-intent.schema.json,
    additionalProperties: false) : elle ne peut pas porter un advertiser.
    """
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    assert manifest["account_topology"]["pull_parameter"] == "advertiser_id"


def test_discovery_id_carries_the_full_routing(connector):
    """L'`id` d'un compte decouvert est ce que le coeur stocke, et RIEN d'autre.

    Le coeur persiste une seule chaine opaque (`app.connection_account_scope.
    account_id`) et la rend telle quelle au pull. Un `id` synthetique indexe
    (`amazon_dsp_selection_1`) ne permet ni de router la region, ni de poser
    l'en-tete `Amazon-Ads-AccountId` -- et change de cible si la decouverte
    reordonne. La recherche l'exige nommement : identifiant opaque portant
    assez d'information de region pour router sans redecouvrir.
    """
    client = MagicMock()
    client.request.return_value = Response(
        payload={
            "accounts": [
                {
                    "adsAccountId": "acct-1",
                    "advertisers": [{"advertiserId": "adv-1", "name": "DSP advertiser"}],
                }
            ]
        }
    )
    row = connector.discover_accounts(
        "conn", regions=("EU",), _client=client, _token_value="t", _client_id_value="c"
    )[0]
    assert row["id"] == "EU:acct-1:adv-1"


def test_pull_reads_the_account_from_its_declared_parameter(connector, monkeypatch):
    captured: list[dict] = []
    _stub_report(connector, monkeypatch, captured)
    connector.pull(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "proj_EXAMPLE",
        "pull-1",
        advertiser_id="EU:acct-1:adv-1",
    )
    assert captured[0]["routing"] == {
        "region": "EU",
        "adsAccountId": "acct-1",
        "advertiserId": "adv-1",
    }
    assert captured[0]["configuration"]["filters"] == [
        {"field": "advertiserId", "values": ["adv-1"]}
    ]


def test_pull_still_honours_a_report_selection_without_the_account(connector, monkeypatch):
    """`selection` garde ses usages legitimes : la forme du rapport."""
    captured: list[dict] = []
    _stub_report(connector, monkeypatch, captured)
    connector.pull_catalog_daily(
        "conn",
        "2026-07-01",
        "2026-07-02",
        "proj_EXAMPLE",
        "pull-1",
        selection={"columns": ["date", "impressions"], "group_by": "campaign"},
        advertiser_id="NA:acct-9:adv-9",
    )
    assert captured[0]["configuration"]["columns"] == ["date", "impressions"]
    assert captured[0]["routing"]["region"] == "NA"


def test_a_missing_account_is_typed_and_names_the_selection(connector):
    with pytest.raises(connector.AmazonDspOnboardingError, match="advertiser_id"):
        connector.pull("conn", "2026-07-01", "2026-07-02", "proj_EXAMPLE", "pull-1")


def test_an_unrouted_account_is_refused_before_any_call(connector):
    """Un advertiser nu ne peut pas router : ni region, ni Amazon-Ads-AccountId."""
    with pytest.raises(connector.AmazonDspOnboardingError, match="region"):
        connector.pull(
            "conn", "2026-07-01", "2026-07-02", "proj_EXAMPLE", "pull-1", advertiser_id="adv-1"
        )


def test_no_environment_fallback_for_the_account(connector, monkeypatch):
    """Aucun repli d'environnement : une variable est unique pour tout le deploiement."""
    for name in ("AMAZON_DSP_ADVERTISER_ID", "AMAZON_ADS_ACCOUNT_ID", "AMAZON_DSP_REGION"):
        monkeypatch.setenv(name, "leak")
    with pytest.raises(connector.AmazonDspOnboardingError):
        connector.pull("conn", "2026-07-01", "2026-07-02", "proj_EXAMPLE", "pull-1")


def test_refetch_and_non_additive_policy(connector):
    assert connector.REFETCH_DAYS == (3, 14, 45)
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    ratio_fields = [field for field in catalog["fields"] if "rate" in field["field_id"].lower()]
    assert ratio_fields
    assert all(field["exposure"] == "excluded" for field in ratio_fields)
