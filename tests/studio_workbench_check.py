from pathlib import Path
import os

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "studio_workbench_validation"
OUT.mkdir(parents=True, exist_ok=True)

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

    page.wait_for_selector("#studio-workbench")
    assert page.locator("#global-project-list [data-global-project]").count() > 0
    assert page.locator("#studio-project-title").inner_text() != "选择一个项目开始"
    assert page.locator("#studio-inspector-next").is_visible()
    page.screenshot(path=str(OUT / "studio-source.png"), full_page=False)

    for stage in ("brief", "script", "video", "publish"):
        button = page.locator(f'[data-global-stage="{stage}"]')
        if not button.is_disabled():
            button.click()
            page.wait_for_timeout(120)
            assert button.get_attribute("class") and "current" in button.get_attribute("class")
            page.screenshot(path=str(OUT / f"studio-{stage}.png"), full_page=False)

    second = page.locator("#global-project-list [data-global-project]").nth(1)
    if second.count():
        before = page.locator("#studio-project-title").inner_text()
        second.click()
        page.wait_for_timeout(120)
        assert page.locator("#studio-project-title").inner_text() != before

    assert not errors, errors
    browser.close()

print("STUDIO_WORKBENCH_OK")
