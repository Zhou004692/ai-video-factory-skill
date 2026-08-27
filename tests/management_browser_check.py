import os
from pathlib import Path
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "management_console_validation"
OUT.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("MANAGEMENT_URL", "http://127.0.0.1:8765")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    page = browser.new_page(viewport={"width": 1440, "height": 980}, device_scale_factor=1)
    errors = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(BASE_URL, wait_until="networkidle")
    assert page.locator("#studio-workbench").is_visible()
    assert not page.locator('.nav-item[data-page="scripts"]').is_visible()
    assert page.locator("#open-advanced-nav").is_visible()
    assert page.locator("#command-palette").count() == 0
    assert page.locator("#command-panel").count() == 0
    first_project = page.locator("[data-global-project]").first
    if first_project.count():
        first_project.click()
        assert page.locator("#studio-primary-action").is_enabled()
        assert page.locator("#studio-primary-action").inner_text().strip()
        layout = page.evaluate(
            """() => {
                const rect = selector => document.querySelector(selector).getBoundingClientRect();
                const workbench = rect('#studio-workbench');
                const projectHead = rect('.studio-project-head');
                const projectTitle = rect('#studio-project-title');
                const stages = rect('#studio-stages');
                const canvas = rect('.studio-canvas');
                const inspector = rect('.studio-inspector');
                const inspectorHead = rect('.studio-inspector > header');
                const stageStatus = rect('#studio-stage-status');
                return {
                    viewportWidth: document.documentElement.clientWidth,
                    workbenchRight: workbench.right,
                    projectHeadTop: projectHead.top,
                    projectHeadBottom: projectHead.bottom,
                    projectHeadRight: projectHead.right,
                    projectTitleWidth: projectTitle.width,
                    stagesRight: stages.right,
                    canvasTop: canvas.top,
                    inspectorTop: inspector.top,
                    inspectorHeadTop: inspectorHead.top,
                    stageStatusRight: stageStatus.right,
                };
            }"""
        )
        assert layout["workbenchRight"] <= layout["viewportWidth"], layout
        assert layout["projectTitleWidth"] >= 40, layout
        assert abs(layout["projectHeadRight"] - layout["workbenchRight"]) < 1, layout
        assert abs(layout["stagesRight"] - layout["workbenchRight"]) < 1, layout
        assert abs(layout["canvasTop"] - layout["inspectorTop"]) < 1, layout
        assert layout["stageStatusRight"] <= layout["workbenchRight"] - 12, layout
    page.screenshot(path=str(OUT / "desktop-overview.png"), full_page=True)

    def open_module(target_page: str) -> None:
        nav = page.locator(f'.nav-item[data-page="{target_page}"]')
        if nav.is_visible():
            nav.click()
            return
        page.locator("#open-advanced-nav").click()
        page.locator(f'[data-advanced-page="{target_page}"]').click()

    for label, heading in (
        ("内容与剧本", "两条入口，一套剧本标准"),
        ("视频制作", "原生母片生成 + 本地导演剪辑"),
        ("发布与数据", "直连发布与结果回流"),
        ("配置中心", "先完成当前方案所需配置"),
    ):
        target_page=({"内容与剧本":"scripts","视频制作":"videos","发布与数据":"publishing","配置中心":"config"})[label]
        open_module(target_page)
        page.wait_for_timeout(150)
        assert page.locator(".page.active h2").filter(has_text=heading).first.is_visible(), {
            "label": label,
            "active": page.locator(".page.active").get_attribute("id"),
            "errors": errors,
        }
        module_layout = page.evaluate(
            """() => {
                const box = selector => document.querySelector(selector)?.getBoundingClientRect();
                const main = box('main');
                const topbar = box('.topbar');
                const intro = box('.page.active .section-intro');
                const module = box('.page.active .module-workbench');
                const rail = box('.page.active .module-rail');
                const inspector = box('.page.active .module-inspector');
                return {main, topbar, intro, module, rail, inspector};
            }"""
        )
        assert abs(module_layout["topbar"]["height"] - 70) < 1, module_layout
        assert abs(module_layout["intro"]["height"] - 86) < 1, module_layout
        assert abs(module_layout["module"]["left"] - module_layout["main"]["left"] - 16) < 1, module_layout
        assert abs(module_layout["main"]["right"] - module_layout["module"]["right"] - 16) < 1, module_layout
        assert abs(module_layout["rail"]["width"] - 220) < 1, module_layout
        assert abs(module_layout["inspector"]["width"] - 270) < 1, module_layout
        if label == "内容与剧本":
            page.locator("#new-source").click()
            assert page.locator('#source-type option[value="video"]').count() == 1
            assert page.locator('#source-type option[value="audio"]').count() == 1
            page.locator("#source-type").select_option("video")
            assert page.locator("#source-file").is_visible()
            assert ".mp4" in (page.locator("#source-file").get_attribute("accept") or "")
            page.locator("#modal-close").click()
            if page.locator("[data-source-id]").count():
                page.locator("[data-source-id]").first.click()
                page.wait_for_selector("#page-overview.active")
                page.wait_for_selector('[data-studio-stage="source"].active')
                page.locator("#studio-open-source").click()
                assert page.locator("#drawer").get_attribute("aria-hidden") == "false"
                assert page.locator("#drawer").get_by_text("来源信息", exact=True).is_visible()
                assert page.locator("#drawer").get_by_text("来源具体内容", exact=True).or_(
                    page.locator("#drawer").get_by_text("视频剧本解析", exact=True)
                ).is_visible()
                page.locator("#drawer-close").click()
                open_module("scripts")
            if page.locator("[data-draft-id]").count():
                page.locator("[data-draft-id]").first.click()
                page.wait_for_selector("#page-overview.active")
                page.wait_for_selector('[data-studio-stage="script"].active')
                page.locator("#studio-open-script").click()
                assert page.get_by_text("完整剧本", exact=True).is_visible()
                page.locator("#drawer-close").click()
                open_module("scripts")
            if page.locator("[data-brief-id]").count():
                page.locator("[data-brief-id]").first.click()
                page.wait_for_selector("#page-overview.active")
                page.wait_for_selector('[data-studio-stage="brief"].active')
                page.locator("#studio-open-brief").click()
                assert page.get_by_text("创意角度", exact=True).is_visible()
                assert page.locator(".brief-angle").count() > 0
                page.screenshot(path=str(OUT / "desktop-brief-drawer.png"), full_page=True)
                page.locator("#drawer-close").click()
                open_module("scripts")
            page.screenshot(path=str(OUT / "desktop-scripts.png"), full_page=True)
        if label == "视频制作":
            target = page.locator("[data-video-id]").first
            if target.count():
                target.click()
                page.wait_for_timeout(250)
                if page.locator("#page-overview.active").count():
                    if page.locator('[data-studio-stage="video"].active').count():
                        page.locator("#studio-open-video").click()
                    else:
                        page.locator("#studio-review-video").click()
                assert page.locator("#drawer").get_attribute("aria-hidden") == "false"
                assert page.get_by_text("任务信息", exact=True).or_(
                    page.get_by_text("生产信息", exact=True)
                ).is_visible()
                if page.locator(".video-preview").count():
                    assert page.get_by_text("下载 MP4", exact=True).is_visible()
                if page.locator(".director-cue").count():
                    assert page.get_by_text("导演时间线", exact=True).is_visible()
                    assert page.locator(".director-cue").count() > 0
                if page.locator("[data-regenerate-master]").count():
                    page.locator("[data-regenerate-master]").first.click()
                    assert page.get_by_text("只重新生成当前母片", exact=True).is_visible()
                    assert page.get_by_text("追加成本", exact=True).is_visible()
                    page.screenshot(
                        path=str(OUT / "desktop-master-regeneration.png"),
                        full_page=True,
                    )
                    page.locator("#modal-close").click()
                page.screenshot(path=str(OUT / "desktop-video-drawer.png"), full_page=True)
                page.locator("#drawer-close").click()
                open_module("videos")
        if label == "发布与数据":
            assert page.locator("#page-publishing").get_by_text("发布渠道", exact=True).is_visible()
            assert page.get_by_text("混合发布模式", exact=True).is_visible()
            assert page.get_by_role("heading", name="历史状态待核实").is_visible()
            if page.locator("[data-publish-job]").count():
                page.locator("[data-publish-job]").first.click()
                page.wait_for_timeout(150)
                assert page.locator("#page-overview.active").is_visible()
                page.wait_for_selector('[data-studio-stage="publish"].active')
                open_module("publishing")
            if page.locator("[data-publication-id]").count():
                page.locator("[data-publication-id]").first.click()
                page.wait_for_selector("#page-overview.active")
                page.wait_for_selector('[data-studio-stage="publish"].active')
                assert page.locator(".studio-publish-focus").is_visible()
                open_module("publishing")
        page.screenshot(
            path=str(
                OUT
                / {
                    "内容与剧本": "desktop-scripts-page.png",
                    "视频制作": "desktop-videos-page.png",
                    "发布与数据": "desktop-publishing-page.png",
                    "配置中心": "desktop-config-page.png",
                }[label]
            ),
            full_page=False,
        )

    assert "secret-value" not in page.content()
    assert page.get_by_text("必要配置", exact=True).is_visible()
    assert page.get_by_text("可选配置", exact=True).is_visible()
    assert page.get_by_text("发布平台配置", exact=True).is_visible()
    assert page.locator(".required-config-card").count() >= 3
    assert not page.locator("#advanced-config").get_attribute("open")
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.screenshot(path=str(OUT / "desktop-config.png"), full_page=True)

    for page_name, screenshot_name in (
        ("characters", "desktop-characters-unified.png"),
        ("todos", "desktop-todos-unified.png"),
    ):
        page.locator(f'.nav-item[data-page="{page_name}"]').click()
        page.wait_for_timeout(180)
        assert page.locator(f"#page-{page_name}").is_visible()
        shell = page.evaluate(
            """() => {
                const rail = document.querySelector('.sidebar').getBoundingClientRect();
                const main = document.querySelector('main').getBoundingClientRect();
                return {
                    railWidth: rail.width,
                    mainLeft: main.left,
                    railBackground: getComputedStyle(document.querySelector('.sidebar')).backgroundColor,
                };
            }"""
        )
        assert abs(shell["railWidth"] - 64) < 1, shell
        assert abs(shell["mainLeft"] - 64) < 1, shell
        assert shell["railBackground"] == "rgb(23, 35, 30)", shell
        if page_name == "characters":
            module = page.locator("#page-characters .module-workbench").bounding_box()
            assert module and abs(module["x"] - 80) < 1, module
        page.screenshot(path=str(OUT / screenshot_name), full_page=False)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.goto(BASE_URL, wait_until="networkidle")
    mobile.locator("#open-advanced-nav").click()
    mobile.locator('[data-advanced-page="videos"]').click()
    assert mobile.locator(".page.active h2").filter(
        has_text="原生母片生成 + 本地导演剪辑"
    ).first.is_visible()
    assert mobile.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    mobile.screenshot(path=str(OUT / "mobile-video.png"), full_page=True)
    assert not errors, errors
    browser.close()

print("BROWSER_CHECK_OK")
