"""AD-2 source-agnostic boundary scanner (Epic 38, Story 38.1, AC5).

Asserts that ``server/core`` carries no inbound-provider vocabulary: provider
names (Mailgun, Cloudflare, ...) and the inbound package itself must live only
behind the ``ReceiptAdapter`` seam under ``server/inbound``. Follows the
scanner style of the conformance vocabulary tests (no module-specific strings
baked into core).

This is a static source scan (ast/text), not an import — it fails loudly if a
later change smuggles a provider name into the core boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]  # tests/inbound -> tests -> server -> repo root
_CORE_DIR = _REPO_ROOT / "server" / "core"

# Provider / inbound-package vocabulary that must NEVER appear in server/core.
# The guard's purpose (per the module docstring) is provider-NEUTRALITY: no
# provider names and no coupling to the internet-facing ``server/inbound`` package
# or its adapter seam. It is NOT meant to ban the provider-agnostic domain noun
# "inbound receipt": Story 38.8 legitimately introduces the durable receipt ledger
# (``app.inbound_receipts`` / core.inbound_receipts) in core -- it MUST live here
# because it needs the DB + operations seam, and it carries no provider vocabulary.
# The bare "inbound-receipt"/"inbound_receipt" tokens (added in 38.1, when core had
# nothing durable) are therefore intentionally omitted; provider-neutrality stays
# fully enforced by the provider + adapter + wire tokens below and by
# ``test_core_never_imports_inbound_package``.
_FORBIDDEN_TOKENS = [
    "mailgun",
    "cloudflare_worker",
    "cloudflareworker",
    "inbound.adapters",
    "receiptadapter",
    "inbounddelivery",
    "x-inbound-worker-secret",
]


def _core_py_files() -> list[Path]:
    return [p for p in _CORE_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_core_dir_exists():
    assert _CORE_DIR.is_dir(), f"expected core dir at {_CORE_DIR}"


@pytest.mark.parametrize("token", _FORBIDDEN_TOKENS)
def test_core_contains_no_inbound_provider_vocabulary(token: str):
    offenders: list[str] = []
    for path in _core_py_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if token in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"AD-2 violation: inbound-provider token {token!r} found in server/core "
        f"({offenders}). Provider vocabulary must stay behind the ReceiptAdapter "
        f"seam in server/inbound."
    )


def test_core_never_imports_inbound_package():
    offenders: list[str] = []
    for path in _core_py_files():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import inbound", "from inbound")):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {stripped}")
    assert not offenders, (
        f"AD-2 violation: server/core imports the inbound package ({offenders}). "
        f"Core must remain provider-agnostic."
    )
