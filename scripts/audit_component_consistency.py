#!/usr/bin/env python3
"""Audit the console component library for internal consistency.

    pnpm --filter @toorow/admin dev          # then, in another shell:
    python scripts/audit_component_consistency.py [http://localhost:5173]

`measure_component_sheet.py` asks a different question: does each component
match the mockup rule it claims. This one asks whether the components agree
with EACH OTHER — the failure mode that produced 1 359 visual behaviours under
1 117 names, where no single screen was wrong and the set was incoherent.

It reads the source for what can only be seen there, and the rendered page for
what only exists after the cascade. Every finding names the file or the element
and the value, so it can be acted on without a second investigation.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "admin" / "src"
COMPONENTS = [UI / "components" / "ui", UI / "ui"]
URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5199"
SHEET = f"{URL.rstrip('/')}/debug/components"

# The ladders. A value outside one of these is either a mistake or a token that
# was never named — both worth reporting.
RADII = {"0px", "6px", "10px", "12px", "14px", "16px", "999px", "50%", "9999px", "3px", "4px"}
TYPE = {"12px", "13px", "14px", "15px", "16px", "20px", "24px", "30px", "10px", "11px"}
FAMILIES = {
    "Plus Jakarta Sans", "Plus Jakarta Sans Variable",
    "Lexend", "Lexend Variable",
    "JetBrains Mono", "JetBrains Mono Variable",
    "Geist", "Geist Variable",
}


def source_findings() -> list[str]:
    out: list[str] = []
    files = [f for d in COMPONENTS for f in sorted(d.glob("*.ts*"))]

    for f in files:
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(ROOT).as_posix()
        body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # ignore doc comments
        body = re.sub(r"//.*", "", body)

        for hexcol in set(re.findall(r"#[0-9A-Fa-f]{6}\b", body)):
            out.append(f"{rel}: literal colour {hexcol} — every colour is a token")
        for px in set(re.findall(r"text-\[(\d+)px\]", body)):
            out.append(f"{rel}: literal type size {px}px — use a --text-* step")
        # A focus ring written any way but the shared one.
        rings = set(re.findall(r"focus-visible:(?:ring-\[[^\]]+\]|ring-\d|border-ring)", body))
        for r in rings:
            out.append(f"{rel}: focus ring `{r}` — the library ring is outline-3/offset-2/outline-focus")
        # shadcn palette names that bypass our tokens.
        for name in sorted(set(re.findall(r"\b(?:bg|text|border)-(muted-foreground|accent-foreground|primary-foreground|destructive)\b", body))):
            out.append(f"{rel}: shadcn palette name `{name}` — map it onto a token")

    # One concept, one implementation.
    exported = defaultdict(list)
    for f in files:
        for name in re.findall(r"^export function (\w+)", f.read_text(encoding="utf-8"), flags=re.M):
            exported[name].append(f.relative_to(ROOT).as_posix())
    for name, where in sorted(exported.items()):
        if len(where) > 1:
            out.append(f"{name} is implemented {len(where)} times: {', '.join(where)}")
    return out


JS = r"""() => {
  const seen = { radius:{}, size:{}, family:{}, focus:{}, height:{} };
  const bump = (b,k,el) => { (b[k] ||= []).push(el); };
  const label = (el) => {
    const slot = el.getAttribute('data-slot');
    if (slot) return slot;
    const t = el.tagName.toLowerCase();
    const c = (el.className||'').toString().split(' ').slice(0,2).join('.');
    return c ? `${t}.${c}` : t;
  };
  for (const el of document.querySelectorAll('[data-slot], button, input, select, textarea, th, td')) {
    const c = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const radius = c.borderTopLeftRadius;
    if (radius && radius !== '0px') bump(seen.radius, radius, label(el));
    bump(seen.size, c.fontSize, label(el));
    bump(seen.family, (c.fontFamily.split(',')[0]||'').replace(/["']/g,''), label(el));
    if (el.matches('button, input, select, textarea, [role=button]')) {
      bump(seen.height, Math.round(r.height) + 'px', label(el));
    }
  }
  return seen;
}"""


def rendered_findings() -> tuple[list[str], dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ["playwright not installed — rendered checks skipped"], {}

    out: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(SHEET, wait_until="networkidle", timeout=20000)
        except Exception as exc:  # the sheet is a dev route; say so plainly
            browser.close()
            return [f"could not open {SHEET} — is `pnpm dev` running? ({exc})"], {}
        page.wait_for_timeout(900)
        seen = page.evaluate(JS)
        browser.close()

    for value, users in sorted(seen["radius"].items()):
        if value not in RADII:
            out.append(f"radius {value} is off the ladder — on {', '.join(sorted(set(users))[:4])}")
    for value, users in sorted(seen["size"].items()):
        if value not in TYPE:
            out.append(f"type size {value} is off the scale — on {', '.join(sorted(set(users))[:4])}")
    for value, users in sorted(seen["family"].items()):
        if value and value not in FAMILIES:
            out.append(f"font family `{value}` is not one of the four — on {', '.join(sorted(set(users))[:4])}")
    return out, seen


def main() -> int:
    src = source_findings()
    rendered, seen = rendered_findings()

    print("\n  Component library — consistency audit\n")
    print("  Source")
    if src:
        for f in src:
            print(f"    ! {f}")
    else:
        print("    no literal colours, no literal type sizes, one ring, one implementation each")

    print("\n  Rendered")
    if rendered:
        for f in rendered:
            print(f"    ! {f}")
    else:
        print("    every radius, type size and family on the page is on the scale")

    if seen:
        print("\n  What the page actually uses")
        for axis, title in (("radius", "radii"), ("size", "type sizes"), ("height", "control heights")):
            values = sorted(seen[axis].items(), key=lambda kv: -len(kv[1]))
            listed = "  ".join(f"{v} ({len(u)})" for v, u in values[:10])
            print(f"    {title:16s} {listed}")

    total = len(src) + len(rendered)
    print(f"\n  {total} inconsistenc{'y' if total == 1 else 'ies'}\n")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
