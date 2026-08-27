from pathlib import Path
import os

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "full_studio_modules"
OUT.mkdir(parents=True, exist_ok=True)

MODULES = {
    "projects": ".project-archive-workbench",
    "scripts": ".script-module-workbench",
    "videos": ".video-module-workbench",
    "publishing": ".publish-module-workbench",
    "characters": ".character-module-workbench",
    "config": ".config-module-workbench",
}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=os.environ.get(
            "PLAYWRIGHT_BROWSER_PATH",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ),
    )
    page = browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=1)
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")

    def open_module(module: str) -> None:
        nav = page.locator(f'.nav-item[data-page="{module}"]')
        if nav.is_visible():
            nav.click()
            return
        page.locator("#open-advanced-nav").click()
        page.locator(f'[data-advanced-page="{module}"]').click()

    for module, selector in MODULES.items():
        open_module(module)
        page.wait_for_selector(f"#page-{module}.active {selector}")
        assert page.locator(f"{selector} > .module-rail").is_visible()
        assert page.locator(f"{selector} > .module-canvas").is_visible()
        assert page.locator(f"{selector} > .module-inspector").is_visible()
        page.screenshot(path=str(OUT / f"{module}.png"), full_page=False)

    open_module("scripts")
    page.locator('[data-script-stage="sources"]').click()
    assert page.locator('[data-stage-board="sources"]').is_visible()
    assert page.locator('[data-stage-board="briefs"]').is_hidden()

    open_module("videos")
    page.locator('#video-filters [data-state="COMPOSED"]').click()
    assert page.locator("#video-table").is_visible()

    open_module("characters")
    page.locator("#character-add").click()
    assert page.locator("#char-submit").is_visible()
    page.locator("#modal-close").click()

    open_module("config")
    assert page.locator("#save-config").is_visible()
    assert page.locator("#save-config-secondary").count() == 0
    assert not errors, errors
    browser.close()

print("FULL_STUDIO_MODULES_OK")
