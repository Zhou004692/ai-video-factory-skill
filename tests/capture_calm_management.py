from pathlib import Path
import os

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8765"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "management_calm"
PAGES = ("overview", "projects", "scripts", "videos", "publishing", "characters", "config")


OUTPUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=os.environ.get(
            "PLAYWRIGHT_BROWSER_PATH",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ),
    )
    page = browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=1)
    page.goto(BASE_URL, wait_until="networkidle")
    for name in PAGES:
        page.locator(f'.nav-item[data-page="{name}"]').click()
        page.wait_for_timeout(450)
        page.screenshot(path=str(OUTPUT / f"{name}.png"), full_page=False)
    browser.close()

print(OUTPUT)
