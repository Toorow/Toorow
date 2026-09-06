"""Render the G1 component sheet and capture it — full page plus each section."""
import pathlib
import sys
from playwright.sync_api import sync_playwright

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "scratch/g1")
OUT.mkdir(parents=True, exist_ok=True)
URL = "http://localhost:5199/debug/components"

errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(1200)

    page.screenshot(path=str(OUT / "00-full.png"), full_page=True)

    sections = page.locator("section:has(h2)")
    for i in range(sections.count()):
        s = sections.nth(i)
        title = s.locator("h2").first.inner_text().strip().lower()
        slug = "".join(c if c.isalnum() else "-" for c in title).strip("-")[:40]
        s.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        s.screenshot(path=str(OUT / f"{i+1:02d}-{slug}.png"))

    # Overlays, which only exist while open.
    page.get_by_role("button", name="Open dialog", exact=True).click()
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "90-dialog.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    page.get_by_role("button", name="Actions", exact=True).click()
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "91-dropdown.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    page.get_by_role("button", name="Hover me", exact=True).hover()
    page.wait_for_timeout(700)
    page.screenshot(path=str(OUT / "92-tooltip.png"))

    page.get_by_role("combobox").first.click()
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "93-select-open.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # A toast of each tone, all five at once.
    for tone in ["neutral", "success", "warning", "error", "info"]:
        page.locator("section:has(h2)", has_text="Status").get_by_role("button", name=tone, exact=True).click()
        page.wait_for_timeout(120)
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT / "94-toasts.png"))

    # Focus ring, on the first button.
    page.keyboard.press("Escape")
    page.locator("button", has_text="Publish").first.focus()
    page.wait_for_timeout(200)
    page.locator("section:has(h2)").first.screenshot(path=str(OUT / "95-focus-ring.png"))

    b.close()

print(f"wrote {len(list(OUT.glob('*.png')))} images to {OUT}")
if errors:
    print("\nBROWSER ERRORS:")
    for e in errors[:25]:
        print(" ", e)
else:
    print("no console or page errors")
