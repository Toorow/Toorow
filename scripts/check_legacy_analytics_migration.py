#!/usr/bin/env python3
"""Validate the whole-class Story 65.11 legacy analytics inventory.

The inventory is not a retirement switch.  Its independent census makes every
current path visible, while ``--retirement-gate`` refuses until Story 65.10 G10
and the exact qualification commit are pinned.  Historical evidence remains a
protected input under every disposition.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "scripts" / "legacy_analytics_migration_inventory.json"

CLASSES = frozenset(
    {
        "producer-tool",
        "mcp-binding",
        "client-semantic-aggregator",
        "ai-path-projection",
        "feedback-door",
        "historical-reader",
        "renderer",
    }
)
DISPOSITIONS = frozenset({"migrated", "retained", "blocked"})
RETIREMENT_MODES = frozenset({"removed", "delegated", "retained"})
TOP_KEYS = frozenset(
    {
        "schema_version",
        "story",
        "qualification_baseline",
        "historical_evidence",
        "entries",
    }
)
ENTRY_KEYS = frozenset(
    {
        "id",
        "class",
        "user_door",
        "replacement_id",
        "expected_locators",
        "disposition",
        "retirement_mode",
        "owner",
        "reason",
        "replacement_proof",
        "qualification_evidence",
        "required_shared_authority_locators",
        "forbidden_legacy_locators",
    }
)
G10_KEYS = frozenset(
    {
        "decision_ledger",
        "evidence_path",
        "replacement_id",
        "canonical_target",
        "evidence_sha256",
        "host",
    }
)
DECISION_KEYS = frozenset(
    {
        "schema_version",
        "replacement_id",
        "decision",
        "host",
        "canonical_target",
        "evidence_sha256",
        "reason",
        "recorded_at",
        "attempt_ordinal",
        "predecessor_evidence_sha256",
    }
)
QUALIFICATION_KEYS = frozenset(
    {"baseline_commit", "replacement_id", "canonical_target", "evidence_sha256"}
)


@dataclass(frozen=True)
class InventorySummary:
    paths: int
    by_disposition: dict[str, int]
    by_mode: dict[str, int]
    retirement_ready: bool

    def verdict(self) -> str:
        return "READY" if self.retirement_ready else "REFUSED pending Story 65.10 G10"

    def stable_line(self) -> str:
        """The census as it BLOCKS: a verdict, never a count.

        `line()` embeds four live counters, so every movement of the census
        minted a brand-new finding identity: the gate demanded a fresh
        exemption for work that was ADVANCING, and `known-debt.json` had
        accumulated EIGHT variants of this one sentence before the class was
        named. What is owed here is the refused retirement, and that sentence
        does not move until Story 65.10 G10 qualifies it. The counters stay
        visible in the report note, where a number belongs.
        """
        return f"legacy analytics inventory: retirement {self.verdict()}"

    def line(self) -> str:
        dispositions = ", ".join(
            f"{key}={self.by_disposition[key]}" for key in sorted(DISPOSITIONS)
        )
        modes = ", ".join(f"{key}={self.by_mode[key]}" for key in sorted(RETIREMENT_MODES))
        return (
            f"legacy analytics inventory: {self.paths} stable locator memberships; "
            f"{dispositions}; "
            f"{modes}; retirement {self.verdict()}"
        )


def _production_files(root: Path, bases: Iterable[str], suffixes: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    for relative in bases:
        base = root / relative
        if not base.exists():
            continue
        for current, directories, files in os.walk(base):
            directories[:] = [
                name for name in directories
                if name not in {"__tests__", "node_modules", "dist", "build", "__pycache__"}
            ]
            for name in files:
                path = Path(current) / name
                if path.suffix in suffixes and not name.endswith((".test.ts", ".test.tsx")):
                    paths.append(path)
    return sorted(set(paths))


def _python_symbols(text: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, parents: tuple[str, ...] = ()) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = ".".join((*parents, child.name))
                symbols.append((child.lineno, child.end_lineno or child.lineno, qualified))
                visit(child, (*parents, child.name))
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        qualified = ".".join((*parents, target.id))
                        symbols.append(
                            (child.lineno, child.end_lineno or child.lineno, qualified)
                        )
                visit(child, parents)
            else:
                visit(child, parents)

    visit(tree)
    return symbols


_TS_SYMBOL = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)|"
    r"^[ \t]*export\s+const\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.M,
)


def _ts_symbol_name(match: re.Match[str]) -> str:
    return match.group(1) or match.group(2)


def _without_python_prose(text: str) -> str:
    """Blank Python comments and docstrings while preserving every offset.

    Executable string literals (notably SQL assigned to a name) remain searchable.
    The census must inventory code paths, not a sentence explaining why a legacy
    path was refused.
    """
    chars = list(text)
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    def blank(
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        utf8_columns: bool = False,
    ) -> None:
        if not offsets:
            return
        start_line, start_column = start
        end_line, end_column = end
        if utf8_columns:
            start_column = len(
                lines[start_line - 1].encode("utf-8")[:start_column].decode("utf-8")
            )
            end_column = len(
                lines[end_line - 1].encode("utf-8")[:end_column].decode("utf-8")
            )
        begin = offsets[start_line - 1] + start_column
        finish = offsets[end_line - 1] + end_column
        for index in range(begin, min(finish, len(chars))):
            if chars[index] not in "\r\n":
                chars[index] = " "

    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                blank(token.start, token.end)
    except (IndentationError, tokenize.TokenError):
        pass

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "".join(chars)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        blank(
            (first.lineno, first.col_offset),
            (first.end_lineno or first.lineno, first.end_col_offset or first.col_offset),
            utf8_columns=True,
        )
    return "".join(chars)


def _occurrence_locator(path: Path, text: str, match: re.Match[str], root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    matched_line = text.count("\n", 0, match.start()) + 1
    literal_window = text[match.start() : match.end() + 240]
    resource = re.search(r"ui://[A-Za-z0-9_./{}:-]+", literal_window)
    if resource:
        return f"{relative}#resource:{resource.group(0)}"
    route = re.search(r"/api/[A-Za-z0-9_./{}:-]*feedback[A-Za-z0-9_./{}:-]*", literal_window)
    if route:
        return f"{relative}#route:{route.group(0)}"
    if path.suffix == ".py":
        enclosing = [
            symbol for start, end, symbol in _python_symbols(text) if start <= matched_line <= end
        ]
        if enclosing:
            return f"{relative}#symbol:{max(enclosing, key=lambda value: value.count('.'))}"
    if path.suffix in {".ts", ".tsx"}:
        preceding = [item for item in _TS_SYMBOL.finditer(text) if item.start() <= match.start()]
        if preceding:
            return f"{relative}#symbol:{_ts_symbol_name(preceding[-1])}"
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    normalized = " ".join(text[line_start : None if line_end < 0 else line_end].split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    token = re.sub(r"[^A-Za-z0-9_]+", "_", match.group(0)).strip("_") or "literal"
    return f"{relative}#symbol:module.{path.stem}.{token}.{digest}"


def _matching_locators(
    paths: Iterable[Path],
    pattern: re.Pattern[str],
    root: Path,
    *,
    ignore_python_prose: bool = False,
) -> set[str]:
    found: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        searched = text
        if path.suffix == ".py" and ignore_python_prose:
            searched = _without_python_prose(text)
        elif path.suffix in {".ts", ".tsx"}:
            searched = re.sub(
                r"/\*.*?\*/|^[ \t]*//[^\r\n]*",
                lambda item: re.sub(r"[^\r\n]", " ", item.group(0)),
                text,
                flags=re.S | re.M,
            )
        for match in pattern.finditer(searched):
            found.add(_occurrence_locator(path, text, match, root))
    return found


def _profiled_tool_locators(paths: Iterable[Path], root: Path) -> set[str]:
    found: set[str] = set()
    for path in paths:
        if path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if name != "register_profiled" or len(node.args) < 2:
                continue
            handler = node.args[1]
            if isinstance(handler, ast.Name):
                relative = path.relative_to(root).as_posix()
                found.add(f"{relative}#tool:{handler.id}")
    return found


_RENDERER_DECLARATION = re.compile(r"\bdeclare\(\s*[\"']([^\"']+)[\"']")


def _renderer_locators(paths: Iterable[Path], root: Path) -> set[str]:
    found: set[str] = set()
    for path in paths:
        if path.suffix not in {".ts", ".tsx"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        found.update(
            f"{relative}#renderer:{match.group(1)}"
            for match in _RENDERER_DECLARATION.finditer(text)
        )
    return found


def discover_inventory_locators(root: Path = ROOT) -> dict[str, set[str]]:
    """Independently enumerate stable path#symbol/route/resource identities."""
    server = _production_files(root, ("server/core", "server/modules"), (".py", ".json"))
    ui = _production_files(
        root,
        (
            "ui/admin/src",
            "ui/cards",
            "ui/widgets",
        ),
        (".ts", ".tsx"),
    )

    producer_candidates = [
        path for path in server
        if path.suffix == ".py"
        and (
            ("server" in path.parts and "modules" in path.parts and path.name == "connector.py")
            or any(
                token in path.stem
                for token in (
                    "analyze", "answer", "card", "daily_insights", "envelope", "main",
                    "metric", "model_channel", "report", "result", "summarizer",
                )
            )
        )
    ]
    producer = _matching_locators(
        producer_candidates,
        re.compile(r"@(?:mcp\.)?tool\b|\bToolResult\s*\(|structuredContent|structured_content"),
        root,
    )
    producer.update(_profiled_tool_locators(server, root))
    bindings = _matching_locators(
        server,
        re.compile(r"widget_ref|resourceUri|resource_uri|@mcp\.resource\(\s*[\"']ui://"),
        root,
    )
    aggregator_candidates = [
        path
        for path in ui
        if path.name.lower() not in {"fixture.ts", "fixture.tsx"}
        and "__fixtures__" not in path.parts
    ]
    aggregators = _matching_locators(
        aggregator_candidates,
        re.compile(r"aggregation_rule|\brollup\b|\bgroupBy\b|\.reduce\s*\(|\baggregateRows\b"),
        root,
    )
    ai_paths = _matching_locators(
        [*server, *ui],
        re.compile(r"\bai_path(?:_id)?\b|AI Path"),
        root,
    )
    feedback_candidates = [
        *ui,
        *(path for path in server if "feedback" in path.stem or path.name == "main.py"),
    ]
    feedback = _matching_locators(
        feedback_candidates,
        re.compile(
            r"submit_feedback|submit_analyze_feedback|/test/feedback|"
            r"render-shares/session/feedback|/api/[A-Za-z0-9_./{}:-]*feedback|"
            r"(?:INSERT\s+INTO|UPDATE)\s+app\.[A-Za-z0-9_]*feedback"
        ),
        root,
    )
    historical = _matching_locators(
        server,
        re.compile(
            r"render_snapshots|app\.notebooks|app\.reports|/api/feedback|[\"']legacy[\"']\s*:\s*True"
        ),
        root,
        ignore_python_prose=True,
    )
    renderers = _renderer_locators(ui, root)
    return {
        "producer-tool": producer,
        "mcp-binding": bindings,
        "client-semantic-aggregator": aggregators,
        "ai-path-projection": ai_paths,
        "feedback-door": feedback,
        "historical-reader": historical,
        "renderer": renderers,
    }


def discover_inventory_paths(root: Path = ROOT) -> dict[str, set[str]]:
    return {
        kind: {locator.split("#", 1)[0] for locator in locators}
        for kind, locators in discover_inventory_locators(root).items()
    }


def _locator_exists_in_text(path: Path, text: str, identity: str) -> bool:
    if identity.startswith("symbol:"):
        symbol = identity.removeprefix("symbol:")
        if symbol.startswith("module."):
            _module, _stem, token, digest = symbol.split(".", 3)
            for line in text.splitlines():
                normalized = " ".join(line.split())
                if token not in re.sub(r"[^A-Za-z0-9_]+", "_", normalized):
                    continue
                if hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] == digest:
                    return True
            return False
        if path.suffix == ".py":
            return symbol in {item[2] for item in _python_symbols(text)}
        return any(_ts_symbol_name(match) == symbol for match in _TS_SYMBOL.finditer(text))
    if identity.startswith("route:"):
        return identity.removeprefix("route:") in text
    if identity.startswith("resource:"):
        return identity.removeprefix("resource:") in text
    if identity.startswith("tool:"):
        tool = identity.removeprefix("tool:")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        return any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "register_profiled")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "register_profiled")
            )
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == tool
            for node in ast.walk(tree)
        )
    if identity.startswith("renderer:"):
        family = identity.removeprefix("renderer:")
        return family in {match.group(1) for match in _RENDERER_DECLARATION.finditer(text)}
    return False


def _locator_exists(root: Path, locator: str) -> bool:
    if "#" not in locator:
        return False
    relative, identity = locator.split("#", 1)
    path = root / relative
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return _locator_exists_in_text(path, text, identity)


def _locator_existed_at_head(root: Path, locator: str) -> bool:
    if "#" not in locator:
        return False
    relative, identity = locator.split("#", 1)
    completed = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        return False
    return _locator_exists_in_text(
        Path(relative), completed.stdout.decode("utf-8", errors="replace"), identity
    )


def load_inventory(path: Path = INVENTORY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _paths(value: object) -> list[str]:
    return value if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def _bounded_text(
    value: object, label: str, errors: list[str], *, maximum: int, pattern: str | None = None
) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        errors.append(f"{label} must be non-empty and at most {maximum} characters")
        return False
    if pattern is not None and re.fullmatch(pattern, value) is None:
        errors.append(f"{label} has an invalid identifier shape")
        return False
    return True


def _repo_path(root: Path, relative: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        errors.append(f"{label} must be a non-empty repository-relative POSIX path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        errors.append(f"{label} escapes the repository: {relative}")
        return None
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        errors.append(f"{label} escapes the repository: {relative}")
        return None
    return resolved


def _locator_path(root: Path, locator: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(locator, str) or "#" not in locator:
        errors.append(f"{label} must be a path#symbol/route/resource identity")
        return None
    relative, identity = locator.split("#", 1)
    if not identity.startswith(("symbol:", "route:", "resource:", "tool:", "renderer:")):
        errors.append(f"{label} must use symbol, route, resource, tool or renderer identity")
        return None
    return _repo_path(root, relative, label, errors)


def _valid_decision_row(row: object, label: str) -> list[str]:
    if not isinstance(row, dict) or set(row) != DECISION_KEYS:
        return [f"{label} is not the closed g10-qualification-decision.v1 shape"]
    errors: list[str] = []
    if row.get("schema_version") != "g10-qualification-decision.v1":
        errors.append(f"{label}.schema_version is not g10-qualification-decision.v1")
    if not isinstance(row.get("replacement_id"), str) or not row["replacement_id"]:
        errors.append(f"{label}.replacement_id must be non-empty")
    if row.get("decision") not in {"qualified", "blocked"}:
        errors.append(f"{label}.decision must be qualified or blocked")
    host = row.get("host")
    if (
        not isinstance(host, dict)
        or set(host) != {"name", "profile", "version"}
        or not all(isinstance(host.get(key), str) and host[key] for key in host)
    ):
        errors.append(f"{label}.host must have non-empty name, profile and version")
    if row.get("canonical_target") != "https://app.toorow.com":
        errors.append(f"{label}.canonical_target must be https://app.toorow.com")
    if not isinstance(row.get("evidence_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", row["evidence_sha256"]
    ):
        errors.append(f"{label}.evidence_sha256 must be a lowercase SHA-256")
    if row.get("reason") is not None and not isinstance(row.get("reason"), str):
        errors.append(f"{label}.reason must be a string or null")
    ordinal = row.get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        errors.append(f"{label}.attempt_ordinal must be a positive integer")
    predecessor = row.get("predecessor_evidence_sha256")
    if predecessor is not None and (
        not isinstance(predecessor, str) or not re.fullmatch(r"[0-9a-f]{64}", predecessor)
    ):
        errors.append(f"{label}.predecessor_evidence_sha256 must be null or lowercase SHA-256")
    recorded_at = row.get("recorded_at")
    try:
        if not isinstance(recorded_at, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            recorded_at,
        ) is None:
            raise ValueError
        timestamp = (
            f"{recorded_at[:-1]}+00:00"
            if recorded_at.endswith("Z")
            else recorded_at
        )
        parsed_at = datetime.fromisoformat(timestamp)
        if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{label}.recorded_at must be timezone-qualified RFC3339")
    return errors


def _git_commit_exists(root: Path, commit: str) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return completed.returncode == 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _baseline_qualification(
    root: Path,
    baseline: object,
    commit_verifier: Callable[[Path, str], bool] | None = None,
    attestation_key: bytes | None = None,
) -> tuple[bool, list[str], dict | None]:
    errors: list[str] = []
    if not isinstance(baseline, dict) or set(baseline) != {"story", "commit", "gates"}:
        return False, ["qualification_baseline must have exactly story, commit and gates"], None
    if baseline.get("story") != "65.10":
        errors.append("qualification_baseline.story must be 65.10")
    commit = baseline.get("commit")
    commit_valid = isinstance(commit, str) and bool(re.fullmatch(r"[0-9a-f]{40}", commit))
    if commit is not None and not commit_valid:
        errors.append("qualification_baseline.commit must be null or a lowercase 40-character SHA")
    verifier = commit_verifier or _git_commit_exists
    if commit_valid and not verifier(root, commit):
        errors.append("qualification_baseline.commit does not resolve to a Git commit")
        commit_valid = False
    gates = baseline.get("gates")
    if not isinstance(gates, dict) or set(gates) != {"G10"}:
        errors.append("qualification_baseline.gates must contain exactly G10")
        return False, errors, None
    g10 = gates["G10"]
    if (commit is None) != (g10 is None):
        errors.append("qualification_baseline.commit and G10 must be pinned together")
    if g10 is None:
        return False, errors, None
    if not isinstance(g10, dict) or set(g10) != G10_KEYS:
        keys = ", ".join(sorted(G10_KEYS))
        errors.append(f"qualification_baseline.gates.G10 must have exactly: {keys}")
        return False, errors, None
    if g10.get("canonical_target") != "https://app.toorow.com":
        errors.append("qualification_baseline.gates.G10.canonical_target must be https://app.toorow.com")
    host = g10.get("host")
    if (
        not isinstance(host, dict)
        or set(host) != {"name", "profile", "version"}
        or not all(isinstance(host.get(key), str) and host[key] for key in host)
    ):
        errors.append(
            "qualification_baseline.gates.G10.host must pin non-empty name, profile and version"
        )
    evidence_path = g10.get("evidence_path")
    evidence = _repo_path(
        root,
        evidence_path,
        "qualification_baseline.gates.G10.evidence_path",
        errors,
    )
    evidence_json: dict | None = None
    evidence_hash: str | None = None
    if evidence is None or not evidence.is_file():
        errors.append(
            f"G10 retained evidence is missing or escapes the repository: {evidence_path}"
        )
    else:
        try:
            evidence_hash = _sha256_file(evidence)
        except OSError as exc:
            errors.append(f"G10 retained evidence is unreadable: {exc}")
        else:
            if evidence_hash != g10.get("evidence_sha256"):
                errors.append("qualification_baseline.gates.G10 evidence hash does not match bytes")
        try:
            loaded_evidence = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"G10 retained evidence is not readable JSON: {exc}")
        else:
            if isinstance(loaded_evidence, dict):
                evidence_json = loaded_evidence
                try:
                    from e2e.host_qualification import load_and_validate

                    evidence_json = load_and_validate(evidence, repo_root=root)
                except ValueError as exc:
                    errors.append(f"qualified G10 evidence contract is invalid: {exc}")
                    evidence_json = None
            else:
                errors.append("G10 retained evidence root must be an object")
    ledger_path = g10.get("decision_ledger")
    if not isinstance(ledger_path, str) or not ledger_path:
        errors.append("qualification_baseline.gates.G10.decision_ledger must be non-empty")
        return False, errors, None
    ledger = _repo_path(
        root,
        ledger_path,
        "qualification_baseline.gates.G10.decision_ledger",
        errors,
    )
    if ledger is None or not ledger.is_file():
        errors.append(f"G10 decision ledger is missing or escapes the repository: {ledger_path}")
        return False, errors, None
    rows: list[dict] = []
    try:
        for line_number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"G10 decision ledger line {line_number} is invalid JSON: {exc.msg}")
                continue
            row_errors = _valid_decision_row(row, f"G10 decision ledger line {line_number}")
            errors.extend(row_errors)
            if not row_errors:
                rows.append(row)
    except OSError as exc:
        errors.append(f"G10 decision ledger is unreadable: {exc}")
        return False, errors, None
    heads: dict[str, dict] = {}
    for row in rows:
        previous = heads.get(row["replacement_id"])
        expected_ordinal = 1 if previous is None else previous["attempt_ordinal"] + 1
        expected_predecessor = None if previous is None else previous["evidence_sha256"]
        if row["attempt_ordinal"] != expected_ordinal:
            errors.append(
                "G10 decision attempts must be contiguous and ordered per replacement_id"
            )
        if row["predecessor_evidence_sha256"] != expected_predecessor:
            errors.append("G10 decision attempt does not reference its current predecessor")
        heads[row["replacement_id"]] = row
    replacement_id = g10.get("replacement_id")
    decision = heads.get(replacement_id) if isinstance(replacement_id, str) else None
    if decision is None:
        errors.append("G10 decision ledger has no head for the pinned replacement_id")
        return False, errors, None
    for key in ("canonical_target", "evidence_sha256", "host"):
        if decision[key] != g10.get(key):
            errors.append(f"qualification_baseline.gates.G10.{key} does not match the decision row")
    if evidence_json is not None and evidence_hash is not None:
        from e2e.host_qualification import qualification_decision

        projected = qualification_decision(evidence_json, evidence_hash=evidence_hash)
        observed_projection = {key: decision.get(key) for key in projected}
        if observed_projection != projected:
            errors.append("G10 decision head does not match the validated evidence projection")
    if decision["decision"] == "qualified":
        key = attestation_key
        if key is None:
            configured = os.getenv("TOOROW_QA_G10_ATTESTATION_KEY")
            key = configured.encode("utf-8") if configured is not None else None
        if key is None:
            errors.append("qualified G10 evidence requires TOOROW_QA_G10_ATTESTATION_KEY")
        elif evidence_json is not None:
            try:
                from e2e.host_qualification import verify_attestation

                verify_attestation(evidence_json, key=key)
            except ValueError as exc:
                errors.append(f"qualified G10 evidence attestation is invalid: {exc}")
    return commit_valid and decision["decision"] == "qualified" and not errors, errors, decision


def validate_inventory(
    *,
    root: Path = ROOT,
    inventory_path: Path = INVENTORY,
    data: dict | None = None,
    commit_verifier: Callable[[Path, str], bool] | None = None,
    attestation_key: bytes | None = None,
    legacy_locator_verifier: Callable[[Path, str], bool] | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        inventory = data if data is not None else load_inventory(inventory_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read {inventory_path}: {exc}"]
    if not isinstance(inventory, dict):
        return ["inventory root must be an object"]
    unknown = set(inventory) - TOP_KEYS
    missing = TOP_KEYS - set(inventory)
    if unknown:
        errors.append(f"inventory has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        errors.append(f"inventory is missing keys: {', '.join(sorted(missing))}")
    if inventory.get("schema_version") != "legacy-analytics-migration-inventory.v1":
        errors.append("schema_version must be legacy-analytics-migration-inventory.v1")
    if inventory.get("story") != "65.11":
        errors.append("story must be 65.11")

    baseline = inventory.get("qualification_baseline")
    qualified, qualification_errors, decision = _baseline_qualification(
        root, baseline, commit_verifier, attestation_key
    )
    errors.extend(qualification_errors)

    historical = inventory.get("historical_evidence")
    if not isinstance(historical, dict) or set(historical) != {"policy", "protected_paths"}:
        errors.append("historical_evidence must have exactly policy and protected_paths")
        historical = {}
    if historical.get("policy") != "supersede-never-delete":
        errors.append("historical evidence policy must be supersede-never-delete")
    protected = _paths(historical.get("protected_paths"))
    if not protected:
        errors.append("historical evidence must protect at least one path")
    for path in protected:
        resolved = _repo_path(root, path, "historical_evidence.protected_paths", errors)
        if resolved is not None and not resolved.is_file():
            errors.append(f"protected historical evidence is missing: {path}")

    entries = inventory.get("entries")
    if not isinstance(entries, list):
        return errors + ["entries must be an array"]
    discovered = discover_inventory_locators(root)
    current_locators = set().union(*discovered.values())
    historical_locator = legacy_locator_verifier or _locator_existed_at_head
    seen_ids: set[str] = set()
    replacements: dict[str, str] = {}
    inventoried: dict[str, set[str]] = {kind: set() for kind in CLASSES}
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(entry) != ENTRY_KEYS:
            errors.append(f"{label} must have exactly: {', '.join(sorted(ENTRY_KEYS))}")
        entry_id = entry.get("id")
        if not _bounded_text(
            entry_id, f"{label}.id", errors, maximum=100, pattern=r"[a-z0-9][a-z0-9-]*"
        ):
            pass
        elif entry_id in seen_ids:
            errors.append(f"duplicate inventory id: {entry_id}")
        else:
            seen_ids.add(entry_id)
        _bounded_text(entry.get("user_door"), f"{label}.user_door", errors, maximum=300)
        replacement_id = entry.get("replacement_id")
        if _bounded_text(
            replacement_id,
            f"{label}.replacement_id",
            errors,
            maximum=200,
            pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]*",
        ) and isinstance(entry_id, str):
            replacements[entry_id] = replacement_id
        kind = entry.get("class")
        if kind not in CLASSES:
            errors.append(f"{label}.class is not closed")
            continue
        locators = _paths(entry.get("expected_locators"))
        if not locators or locators != sorted(set(locators)) or any(
            "#" not in locator for locator in locators
        ):
            errors.append(
                f"{label}.expected_locators must be non-empty, sorted, unique path#identities"
            )
        overlap = inventoried[kind].intersection(locators)
        if overlap:
            errors.append(f"{label} duplicates locators in {kind}: {', '.join(sorted(overlap))}")
        inventoried[kind].update(locators)
        for locator in locators:
            path = _locator_path(root, locator, f"{label}.expected_locators", errors)
            if (
                path is not None
                and entry.get("retirement_mode") != "removed"
                and not path.is_file()
            ):
                errors.append(f"active inventory path is missing: {locator.split('#', 1)[0]}")
        disposition = entry.get("disposition")
        mode = entry.get("retirement_mode")
        if disposition not in DISPOSITIONS:
            errors.append(f"{label}.disposition is not closed")
        if mode not in RETIREMENT_MODES:
            errors.append(f"{label}.retirement_mode is not closed")
        required = _paths(entry.get("required_shared_authority_locators"))
        forbidden = _paths(entry.get("forbidden_legacy_locators"))
        if required != sorted(set(required)) or forbidden != sorted(set(forbidden)):
            errors.append(f"{label} authority locators must be sorted and unique")
        if mode in {"delegated", "removed"} and (not required or not forbidden):
            errors.append(
                f"{label} delegated/removed mode requires shared-authority and forbidden locators"
            )
        if mode in {"delegated", "removed"} and disposition != "migrated":
            errors.append(f"{label} delegated/removed mode requires migrated disposition")
        if disposition == "migrated" and mode not in {"delegated", "removed"}:
            errors.append(f"{label} migrated disposition requires delegated or removed mode")
        if mode == "retained" and (required or forbidden):
            errors.append(f"{label} retained mode must not claim retirement authority locators")
        for locator in required:
            _locator_path(root, locator, f"{label}.required_shared_authority_locators", errors)
            if locator not in current_locators or not _locator_exists(root, locator):
                errors.append(f"required shared authority locator is missing: {locator}")
        for locator in forbidden:
            _locator_path(root, locator, f"{label}.forbidden_legacy_locators", errors)
            if _locator_exists(root, locator):
                errors.append(f"forbidden legacy authority locator is present: {locator}")
            if not historical_locator(root, locator):
                errors.append(f"forbidden legacy locator has no historical identity: {locator}")
        _bounded_text(entry.get("owner"), f"{label}.owner", errors, maximum=200)
        _bounded_text(entry.get("reason"), f"{label}.reason", errors, maximum=1000)
        proof = entry.get("replacement_proof")
        proof_paths = _paths(proof.get("paths")) if isinstance(proof, dict) else []
        if not isinstance(proof, dict) or set(proof) != {"paths", "claim"} or not proof_paths:
            errors.append(f"{label}.replacement_proof requires claim and non-empty paths")
        else:
            _bounded_text(
                proof.get("claim"), f"{label}.replacement_proof.claim", errors, maximum=1000
            )
            for path in proof_paths:
                resolved = _repo_path(
                    root, path, f"{label}.replacement_proof.paths", errors
                )
                if resolved is not None and not resolved.is_file():
                    errors.append(f"replacement proof is missing: {path}")
        evidence = entry.get("qualification_evidence")
        evidence_passes = (
            isinstance(evidence, dict)
            and set(evidence) == QUALIFICATION_KEYS
            and isinstance(baseline, dict)
            and evidence.get("baseline_commit") == baseline.get("commit")
            and evidence.get("replacement_id") == replacement_id
            and decision is not None
            and all(
                evidence.get(key) == decision.get(key)
                for key in ("replacement_id", "canonical_target", "evidence_sha256")
            )
        )
        if disposition == "migrated" and (not qualified or not evidence_passes):
            errors.append(
                f"{label} cannot be migrated without pinned 65.10 G10 and passing evidence"
            )
        if mode in {"removed", "delegated"} and (not qualified or not evidence_passes):
            errors.append(
                f"{label} cannot be {mode} without pinned 65.10 G10 and passing evidence"
            )
        if disposition in {"retained", "blocked"} and evidence is not None:
            errors.append(f"{label} must not attach passing evidence to a non-migrated disposition")

    for start in sorted(replacements):
        chain: list[str] = []
        current = start
        while current in replacements:
            if current in chain:
                cycle = " -> ".join((*chain[chain.index(current) :], current))
                errors.append(f"replacement cycle detected: {cycle}")
                break
            chain.append(current)
            current = replacements[current]
    for kind in sorted(CLASSES):
        missing_paths = discovered[kind] - inventoried[kind]
        stale_paths = inventoried[kind] - discovered[kind]
        if missing_paths:
            missing_text = ", ".join(sorted(missing_paths))
            errors.append(f"{kind} census has unclassified locators: {missing_text}")
        if stale_paths:
            stale_text = ", ".join(sorted(stale_paths))
            errors.append(f"{kind} inventory locators no longer match the census: {stale_text}")
    return errors


def inventory_summary(
    data: dict,
    root: Path = ROOT,
    commit_verifier: Callable[[Path, str], bool] | None = None,
    attestation_key: bytes | None = None,
) -> InventorySummary:
    dispositions = {key: 0 for key in DISPOSITIONS}
    modes = {key: 0 for key in RETIREMENT_MODES}
    total = 0
    for entry in data.get("entries", []):
        count = len(_paths(entry.get("expected_locators"))) if isinstance(entry, dict) else 0
        total += count
        if entry.get("disposition") in dispositions:
            dispositions[entry["disposition"]] += count
        if entry.get("retirement_mode") in modes:
            modes[entry["retirement_mode"]] += count
    baseline = data.get("qualification_baseline", {})
    qualified, _errors, _decision = _baseline_qualification(
        root, baseline, commit_verifier, attestation_key
    )
    ready = (
        total > 0
        and dispositions["blocked"] == 0
        and dispositions["migrated"] + dispositions["retained"] == total
        and qualified
    )
    return InventorySummary(total, dispositions, modes, ready)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retirement-gate", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    errors = validate_inventory()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    data = load_inventory()
    summary = inventory_summary(data)
    print(summary.line())
    if args.retirement_gate and not summary.retirement_ready:
        print(
            "RETIREMENT REFUSED: Story 65.10 G10 and exact per-path evidence are not pinned.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
