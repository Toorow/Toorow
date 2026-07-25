"""Sync the public application projection into the public repository checkout.

This is the "publish" half of the two-remote model (see distribution/README.md):

  private monorepo  --export (allow-list)-->  public checkout  --push-->  GitHub

Mechanically it is a one-way projection, not a git sync: git history is
all-or-nothing, so pushing the monorepo would leak private paths and their whole
history. Instead we copy only the allow-listed files into a separate public
repository that keeps its own clean history.

Usage (run from the monorepo root):

    # Stage the latest projection into ../toorow-public and show the diff.
    # Does NOT commit or push -- review + run a secret scanner first.
    python scripts/publish_public_app.py

    # After review, commit and push to github.com/Toorow/Toorow.
    python scripts/publish_public_app.py --push -m "chore: sync public app"

The public checkout defaults to a sibling directory outside this repository and
is cloned on first run.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "scripts" / "export_public_app.py"
DEFAULT_CHECKOUT = ROOT.parent / "toorow-public"
PUBLIC_REMOTE = "https://github.com/Toorow/Toorow.git"


def _run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _ensure_checkout(checkout: Path) -> None:
    if (checkout / ".git").is_dir():
        print(f"Using existing public checkout: {checkout}")
        _run(["git", "-C", str(checkout), "pull", "--ff-only"])
        return
    if checkout.exists() and any(checkout.iterdir()):
        raise SystemExit(
            f"ERROR: {checkout} exists, is not empty, and is not a git checkout."
        )
    print(f"Cloning {PUBLIC_REMOTE} into {checkout} ...")
    _run(["git", "clone", PUBLIC_REMOTE, str(checkout)])


def _clear_tracked(checkout: Path) -> None:
    """Remove everything except the .git directory so deletions propagate."""
    for entry in checkout.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _export_into(checkout: Path) -> None:
    with tempfile.TemporaryDirectory(dir=checkout.parent) as tmp:
        staging = Path(tmp) / "export"
        result = subprocess.run(
            [sys.executable, str(EXPORTER), "--output", str(staging)],
        )
        if result.returncode != 0:
            raise SystemExit("ERROR: export failed; public checkout left untouched.")
        _clear_tracked(checkout)
        for item in staging.iterdir():
            target = checkout / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkout",
        type=Path,
        default=DEFAULT_CHECKOUT,
        help=f"public repository checkout (default: {DEFAULT_CHECKOUT})",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="commit and push to the public remote (default: stage + diff only)",
    )
    parser.add_argument(
        "-m",
        "--message",
        default="chore: sync public application projection",
        help="commit message when --push is set",
    )
    args = parser.parse_args()

    checkout = args.checkout.resolve()
    _ensure_checkout(checkout)
    _export_into(checkout)
    _run(["git", "-C", str(checkout), "add", "-A"])

    print("\n--- Staged public projection ---")
    _run(["git", "-C", str(checkout), "status", "--short"])

    if not args.push:
        print(
            "\nReview the diff and run a secret scanner in the checkout, then re-run"
            "\nwith --push to publish:\n"
            f"    git -C {checkout} diff --cached\n"
            f"    python scripts/publish_public_app.py --push -m \"<message>\"\n"
        )
        return 0

    # Nothing staged -> nothing to publish.
    status = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        print("\nNo changes to publish; public repository already up to date.")
        return 0

    _run(["git", "-C", str(checkout), "commit", "-m", args.message])
    _run(["git", "-C", str(checkout), "push", "origin", "HEAD"])
    print("\nPublished to https://github.com/Toorow/Toorow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
