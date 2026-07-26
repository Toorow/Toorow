"""Trusted operator CLI for minting one self-hosted instance setup link."""

from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mint and persist a single-use self-hosted instance setup capability."
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Public console origin, for example https://toorow.example.com.",
    )
    parser.add_argument(
        "--expires-in-minutes",
        type=int,
        default=30,
        help="Capability lifetime (1 to 10080 minutes; default: 30).",
    )
    return parser


def _validated_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("--base-url must be an HTTPS origin (except localhost)")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.expires_in_minutes <= 10_080:
        raise SystemExit("--expires-in-minutes must be between 1 and 10080")

    base_url = _validated_base_url(args.base_url)

    from core.deployment_mode import deployment_mode

    if deployment_mode() != "self_hosted":
        raise SystemExit("TOOROW_DEPLOYMENT_MODE must be self_hosted")

    from core.db import get_connection
    from core.self_hosted_instance_claim import provision_bootstrap_capability

    bearer = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=args.expires_in_minutes)
    with get_connection() as conn:
        capability = provision_bootstrap_capability(
            conn,
            deployment_mode="self_hosted",
            bearer=bearer,
            expires_at=expires_at,
        )
        conn.commit()

    setup_url = f"{base_url}/setup#bootstrap={quote(bearer, safe='')}"
    print("One-time setup URL (shown once; do not log or share it):")
    print(setup_url)
    print(f"Expires at: {capability.expires_at.isoformat()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
