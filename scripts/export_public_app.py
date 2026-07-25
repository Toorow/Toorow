"""Create a shareable application-only projection of the private monorepo."""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "distribution" / "public-app.toml"


def _matches(path: str, patterns: list[str]) -> bool:
    pure = PurePosixPath(path)
    return any(fnmatch.fnmatch(path, pattern) or pure.match(pattern) for pattern in patterns)


def _under(path: str, candidate: str) -> bool:
    return path == candidate or path.startswith(f"{candidate}/")


def _git_visible_files() -> list[str]:
    command = [
        "git",
        "-C",
        str(ROOT),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    result = subprocess.run(command, check=True, capture_output=True)
    return sorted(item for item in result.stdout.decode("utf-8").split("\0") if item)


def _load_config() -> dict[str, object]:
    with CONFIG_PATH.open("rb") as stream:
        return tomllib.load(stream)


def _select_files(config: dict[str, object]) -> tuple[list[str], list[str]]:
    root_files = set(config["root_files"])
    public_directories = list(config["public_directories"])
    private_paths = list(config["private_paths"])
    exclude_globs = list(config["exclude_globs"])
    allow_globs = list(config["allow_globs"])

    selected: list[str] = []
    errors: list[str] = []
    for path in _git_visible_files():
        included = path in root_files or any(_under(path, root) for root in public_directories)
        if not included:
            continue
        if any(_under(path, private) for private in private_paths):
            errors.append(f"private path selected: {path}")
            continue
        excluded = _matches(path, exclude_globs)
        allowed = _matches(path, allow_globs)
        if excluded and not allowed:
            continue

        source = ROOT / Path(path)
        if source.is_symlink():
            errors.append(f"symbolic link rejected: {path}")
        elif not source.is_file():
            errors.append(f"selected path is not a regular file: {path}")
        else:
            selected.append(path)

    return selected, errors


def _validate_destination(destination: Path) -> None:
    resolved = destination.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("the export destination must be outside the source repository")

    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError(f"the export destination is not empty: {resolved}")


def _export(files: list[str], destination: Path) -> None:
    _validate_destination(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for relative in files:
        source = ROOT / Path(relative)
        target = destination / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or export the public application projection."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="copy the projection to this new or empty directory (outside this repository)",
    )
    parser.add_argument("--list", action="store_true", help="print every selected path")
    args = parser.parse_args()

    config = _load_config()
    files, errors = _select_files(config)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if args.list:
        print("\n".join(files))

    if args.output:
        try:
            _export(files, args.output)
        except ValueError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        stream = sys.stderr if args.list else sys.stdout
        print(f"Exported {len(files)} files to {args.output.resolve()}", file=stream)
    elif not args.list:
        print(f"Public application boundary OK: {len(files)} files selected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
