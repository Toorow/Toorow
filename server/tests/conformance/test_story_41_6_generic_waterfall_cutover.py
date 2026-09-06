"""Story 41.6 cutover guard: Waterfall is a generic Result/Visual path only."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def test_bespoke_tax_read_and_card_artifacts_are_retired() -> None:
    retired = (
        "server/core/fee_tax_api.py",
        "ui/cards/fee-tax-bridge",
        "ui/cards/shell/src/Waterfall.tsx",
        "ui/cards/shell/src/__tests__/Waterfall.test.tsx",
    )
    assert all(not (_ROOT / relative).exists() for relative in retired)


def test_bespoke_tax_read_and_card_surfaces_are_not_mounted() -> None:
    mounted_sources = "\n".join(
        _read(relative)
        for relative in (
            "server/core/admin_api.py",
            "server/core/main.py",
            "server/core/cards.py",
            "server/core/narrative.py",
            "ui/cards/shell/src/index.ts",
            "ui/pnpm-lock.yaml",
            ".github/workflows/ci.yml",
        )
    )
    forbidden = (
        "FEE_TAX_ROUTES",
        "_register_fee_tax_mcp",
        "card-fee-tax-bridge",
        "fee-tax-bridge",
        'id="fee_tax_bridge"',
        '"fee_tax_bridge": build_fee_tax_bridge_comment',
        'from "./Waterfall"',
    )
    for token in forbidden:
        assert token not in mounted_sources


def test_waterfall_is_delivered_by_the_generic_runtime_contract() -> None:
    result_shapes = _read("server/core/result_shapes.py")
    families = _read("server/core/visualization_families.py")
    renderer = _read("ui/cards/shell/src/viz/renderers/index.ts")
    migration = _read("infra/nango/migrations/191_waterfall_visual_family.sql")

    assert 'WATERFALL_SHAPE = "waterfall_v1"' in result_shapes
    assert "WATERFALL_FIELDS" in result_shapes
    assert "serialize_waterfall_v1" in result_shapes

    assert 'id="waterfall"' in families
    assert 'result_shape="waterfall_v1"' in families
    assert "WATERFALL_RESULT_BINDINGS" in families

    assert (
        'declare("waterfall", "toorow-echarts-waterfall"'
        in renderer
    )
    assert "ck_visualization_spec_versions_family" in migration
    assert "ck_renderer_runtime_builds_family" in migration
    assert "toorow-echarts-waterfall@1.0.0" in migration
