"""Browser contract for brief and script revision flows."""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "management_console_validation" / "content-revisions"
OUT.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("MANAGEMENT_URL", "http://127.0.0.1:8765")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=(
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        ),
    )
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    errors: list[str] = []
    page.on(
        "console",
        lambda message: errors.append(message.text)
        if message.type == "error" else None,
    )
    page.on("pageerror", lambda exception: errors.append(str(exception)))

    page.goto(BASE_URL, wait_until="networkidle")
    page.locator("#open-advanced-nav").click()
    page.locator('[data-advanced-page="scripts"]').click()
    page.wait_for_selector("[data-brief-id]")

    candidate_brief = page.locator('[data-brief-id]').filter(
        has=page.locator('.tag.candidate')
    ).first
    if candidate_brief.count() == 0:
        candidate_brief = page.locator('[data-brief-id]').first
    assert candidate_brief.count() == 1, "没有可用于交互验收的选题简报"
    candidate_brief.click()
    page.wait_for_selector('[data-studio-stage="brief"].active')
    assert page.get_by_text("候选角度", exact=True).is_visible()
    page.locator("#studio-open-brief").click()
    assert page.locator("#drawer-body").get_by_text("创意角度").first.is_visible()
    edit_brief = page.locator("#drawer-edit-brief")
    if edit_brief.count():
        edit_brief.click()
        assert page.get_by_role("heading", name="编辑选题简报").is_visible()
        assert page.locator('[data-edit-brief-angle]').count() > 0
        assert page.locator("#edit-brief-note").is_visible()
        modal_body = page.locator("#modal-body")
        assert modal_body.evaluate(
            "element => element.scrollHeight > element.clientHeight"
        )
        page.screenshot(path=str(OUT / "brief-editor.png"), full_page=True)
        page.locator("#modal-close").click()
    else:
        page.locator("#drawer-close").click()

    page.locator("#open-advanced-nav").click()
    page.locator('[data-advanced-page="scripts"]').click()
    page.wait_for_selector("[data-draft-id]")
    candidate_script = page.locator('[data-draft-id]').filter(
        has=page.locator('.tag.candidate')
    ).first
    assert candidate_script.count() == 1, "没有可用于交互验收的候选剧本"
    candidate_script.click()
    page.wait_for_selector('[data-studio-stage="script"].active')
    page.locator("#studio-open-script").click()
    edit_script = page.locator("#drawer-edit-script")
    assert edit_script.count() == 1, "候选剧本缺少编辑入口"
    edit_script.click()
    assert page.get_by_role("heading", name="编辑候选剧本").is_visible()
    assert page.locator('[data-edit-script-beat]').count() > 0
    assert page.locator("#edit-script-note").is_visible()
    modal_body = page.locator("#modal-body")
    assert modal_body.evaluate(
        "element => element.scrollHeight > element.clientHeight"
    )
    page.screenshot(path=str(OUT / "script-editor.png"), full_page=True)

    assert not errors, errors
    browser.close()

print("CONTENT_REVISION_BROWSER_CHECK_OK")
