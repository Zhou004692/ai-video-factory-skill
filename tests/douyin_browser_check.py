from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


base_url = os.environ.get("MANAGEMENT_URL", "http://127.0.0.1:8765")
output = Path(os.environ.get("BROWSER_ARTIFACT_DIR", "artifacts/browser"))
output.mkdir(parents=True, exist_ok=True)
console_errors: list[str] = []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=os.environ.get(
            "PLAYWRIGHT_BROWSER_PATH",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ),
    )
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.goto(base_url, wait_until="networkidle")

    page.locator('[data-page="publishing"]').click()
    page.wait_for_selector("#page-publishing.active")
    page.get_by_text("发布与绩效工作区", exact=True).wait_for()
    page.get_by_text("OpenAPI 直连", exact=False).wait_for()
    page.get_by_text("官方助手扫码登录", exact=False).wait_for()
    page.screenshot(path=str(output / "douyin-publishing.png"), full_page=True)

    page.locator('[data-page="config"]').click()
    page.wait_for_selector("#page-config.active")
    page.get_by_text("发布平台配置", exact=True).wait_for()
    page.get_by_text("OAuth 账号直连", exact=True).wait_for()
    if page.locator('[data-key="DOUYIN_CLIENT_KEY"]').count() != 1:
        raise AssertionError("Douyin settings must appear only in the channel card")
    page.locator('[data-platform-expand="douyin"]').click()
    page.locator('[data-platform-fields="douyin"]:not([hidden])').wait_for()
    douyin_panel = page.locator('[data-platform-fields="douyin"]')
    douyin_panel.locator('[data-key="DOUYIN_CLIENT_KEY"]').wait_for()
    douyin_panel.locator('[data-key="DOUYIN_REDIRECT_URI"]').wait_for()
    account_action = douyin_panel.locator("#bind-douyin, #disconnect-douyin")
    account_action.scroll_into_view_if_needed()
    account_action.wait_for()
    page.locator('[data-platform-expand="weixin_channels"]').click()
    weixin_panel = page.locator(
        '[data-platform-fields="weixin_channels"]:not([hidden])'
    )
    weixin_panel.wait_for()
    weixin_panel.get_by_text("登录状态由微信官方页面保管", exact=False).wait_for()
    assistant_link = weixin_panel.get_by_role("link", name="打开视频号助手")
    assistant_link.wait_for()
    if assistant_link.get_attribute("href") != "https://channels.weixin.qq.com/login":
        raise AssertionError("WeChat Channels must open the official assistant page")
    if page.locator("#bind-weixin, #disconnect-weixin").count():
        raise AssertionError("The console must not claim a local WeChat account binding")
    page.screenshot(path=str(output / "douyin-config.png"), full_page=True)

    browser.close()

if console_errors:
    raise AssertionError(f"browser console errors: {console_errors}")

print("DOUYIN_BROWSER_CHECK_OK")
