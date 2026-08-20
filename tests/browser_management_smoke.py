"""Read-only browser smoke check for the local management console."""
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT = ROOT / "data" / "management_console_p0_smoke.png"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" else None)
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")

    page.locator('.nav-item[data-page="projects"]').click()
    page.wait_for_selector("#page-projects.active")
    assert page.get_by_text("一条内容，一个完整生产档案").is_visible()
    assert page.locator(".project-row").count() > 0
    page.locator(".project-row").first.click()
    assert page.locator(".quality-card").is_visible()
    assert page.get_by_text("项目档案", exact=True).is_visible()
    page.locator("#drawer-close").click()
    page.locator('.nav-item[data-page="scripts"]').click()
    page.get_by_role("button", name="新增来源").click()
    source_type = page.locator("#source-type")
    source_type.select_option("video")
    assert page.locator("#source-file").is_visible()
    assert page.get_by_role("button", name="上传并解析").is_visible()
    page.locator("#modal-close").click()

    page.locator('.nav-item[data-page="videos"]').click()
    page.wait_for_selector("#page-videos.active")
    page.wait_for_selector("#video-table .table-head")
    assert page.locator("#video-table").is_visible()
    page.locator('#video-filters [data-state="STORYBOARDED"]').click()
    setup_job = page.locator("[data-video-id]").first
    if setup_job.count():
        setup_job.click()
        page.locator("#drawer-produce").click()
        page.wait_for_selector("#setting-orientation, #produce-confirm")
        if page.locator("#setting-orientation").count():
            assert page.locator("#setting-orientation").is_visible()
            assert page.locator("#setting-budget").is_visible()
        else:
            assert page.locator("#produce-confirm").is_visible()
        page.locator("#modal-close").click()
    page.locator('#video-filters [data-state="COMPOSED"]').click()
    review_job = page.locator("[data-video-id]").first
    if review_job.count():
        review_job.click()
        review_button = page.locator("#drawer-review-approve")
        if review_button.count():
            review_button.click()
            assert page.locator("[data-review-check]").count() == 6
            page.locator("#modal-close").click()
    page.locator('.nav-item[data-page="publishing"]').click()
    page.wait_for_selector("#page-publishing.active")
    assert page.get_by_text("混合发布模式", exact=True).is_visible()
    page.locator('.nav-item[data-page="config"]').click()
    page.wait_for_selector("#page-config.active")
    assert page.get_by_text("必要配置", exact=True).is_visible()

    page.screenshot(path=str(SCREENSHOT), full_page=True)
    browser.close()

assert not errors, errors
print(SCREENSHOT)
