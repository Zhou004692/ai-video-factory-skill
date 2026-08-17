from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "ui-readability-20260816"
BASE_URL = "http://127.0.0.1:8765/"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PAGES = ("overview", "projects", "scripts", "videos", "publishing", "characters", "config")


def px(value: str) -> float:
    return float(value.removesuffix("px"))


def computed_size(page, selector: str) -> float:
    locator = page.locator(selector).filter(visible=True).first
    assert locator.count(), f"missing visible selector: {selector}"
    return px(locator.evaluate("node => getComputedStyle(node).fontSize"))


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, dict] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(CHROME),
        )
        for viewport_name, viewport in {
            "laptop": {"width": 1440, "height": 900},
            "desktop": {"width": 1920, "height": 1080},
        }.items():
            page = browser.new_page(viewport=viewport)
            page.goto(BASE_URL, wait_until="networkidle")
            for name in PAGES:
                page.locator(f'.nav-item[data-page="{name}"]').click()
                page.wait_for_function(f'document.body.dataset.page === "{name}"')
                page.wait_for_function(
                    f'!document.querySelector("#page-{name} .page-load-state")'
                )
                page.wait_for_timeout(150)
                page.screenshot(
                    path=str(ARTIFACTS / f"{name}-{viewport_name}.png"),
                    full_page=False,
                )
                metrics[f"{name}-{viewport_name}"] = page.evaluate(
                    """() => {
                      const visible = node => {
                        const style = getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const nodes = [...document.querySelectorAll('body *')].filter(visible);
                      return {
                        below12: nodes.filter(node => parseFloat(getComputedStyle(node).fontSize) < 12 && node.textContent.trim()).length,
                        clipped: nodes.filter(node => node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflowX !== 'auto').length,
                      };
                    }"""
                )
            page.close()

        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script(
            """window.__coldLoadLayoutShifts = [];
            new PerformanceObserver(list => {
              for (const entry of list.getEntries()) {
                if (!entry.hadRecentInput) window.__coldLoadLayoutShifts.push(entry.value);
              }
            }).observe({type: 'layout-shift', buffered: true});"""
        )
        page.goto(f"{BASE_URL}#overview", wait_until="networkidle")
        cold_load_shift = page.evaluate(
            "window.__coldLoadLayoutShifts.reduce((total, value) => total + value, 0)"
        )
        assert cold_load_shift < 0.01, cold_load_shift
        assert page.locator(".brand-mark svg").count() == 1
        assert page.locator(".brand-mark").inner_text().strip() == ""
        assert computed_size(page, ".global-recent-projects b") >= 13
        assert computed_size(page, ".global-recent-projects small") >= 11
        assert computed_size(page, ".studio-inspector-section .detail-row") >= 13
        assert page.locator("#page-overview").evaluate(
            "node => getComputedStyle(node).animationName"
        ) == "none"

        # Silent polling must not insert loading UI into the document flow.
        # Observe one complete five-second refresh cycle after the first paint.
        page.evaluate(
            """() => {
              window.__postLoadLayoutShifts = [];
              window.__postLoadLayoutObserver = new PerformanceObserver(list => {
                for (const entry of list.getEntries()) {
                  if (!entry.hadRecentInput) window.__postLoadLayoutShifts.push(entry.value);
                }
              });
              window.__postLoadLayoutObserver.observe({type: 'layout-shift'});
            }"""
        )
        page.wait_for_timeout(5_500)
        layout_shift = page.evaluate(
            """() => {
              window.__postLoadLayoutObserver.disconnect();
              return window.__postLoadLayoutShifts.reduce((total, value) => total + value, 0);
            }"""
        )
        assert layout_shift < 0.001, layout_shift

        # The main workbench keeps project context. Operational clicks open an
        # overlay in place; only explicit module navigation changes the page.
        page.locator("#studio-inspector-next").click()
        page.wait_for_function(
            "document.querySelector('#drawer').classList.contains('open') || !document.querySelector('#modal').hidden"
        )
        assert page.locator("body").get_attribute("data-page") == "overview"
        assert page.url.endswith("#overview")
        if page.locator("#drawer").get_attribute("class") and "open" in page.locator("#drawer").get_attribute("class"):
            page.locator("#drawer-close").click()
        elif not page.locator("#modal").is_hidden():
            page.locator("#modal-close").click()

        page.locator("#global-new-project").click()
        page.wait_for_function("!document.querySelector('#modal').hidden")
        assert page.locator("body").get_attribute("data-page") == "overview"
        page.locator("#modal-close").click()

        page.locator('.nav-item[data-page="videos"]').click()
        page.wait_for_function('document.body.dataset.page === "videos"')
        page.wait_for_function(
            '!document.querySelector("#page-videos .page-load-state")'
        )
        assert computed_size(page, "#video-table .table-row") >= 14
        assert computed_size(page, "#video-table .tag") >= 12
        job_id = page.locator("#video-table .video-job-id").first
        assert job_id.count()
        assert job_id.evaluate("node => getComputedStyle(node).whiteSpace") == "nowrap"

        page.evaluate("location.hash = '#characters'")
        page.wait_for_function('document.body.dataset.page === "characters"')
        page.wait_for_function(
            '!document.querySelector("#page-characters .page-load-state")'
        )
        assert computed_size(page, ".character-card-title strong") >= 16
        assert computed_size(page, ".character-card-body>p") >= 13
        preview = page.locator(".character-preview:has(img)").first
        assert preview.count()
        preview_box = preview.bounding_box()
        image_box = preview.locator("img").bounding_box()
        assert preview_box and image_box
        ratio = preview_box["width"] / preview_box["height"]
        assert 0.78 <= ratio <= 0.82, ratio
        assert image_box["height"] <= preview_box["height"] + 1
        assert image_box["width"] <= preview_box["width"] + 1
        preview.click()
        page.wait_for_selector(".character-detail-preview img, .character-gallery img")
        detail_image = page.locator(
            ".character-detail-preview img, .character-gallery img"
        ).first
        assert detail_image.evaluate(
            "node => getComputedStyle(node).objectFit"
        ) == "contain"
        gallery = page.locator(".character-gallery")
        if gallery.count():
            gallery_box = gallery.bounding_box()
            assert gallery_box
            for button in gallery.locator("button").all():
                button_box = button.bounding_box()
                assert button_box
                assert button_box["x"] + button_box["width"] <= gallery_box["x"] + gallery_box["width"] + 1
        page.screenshot(
            path=str(ARTIFACTS / "character-detail-laptop.png"),
            full_page=False,
        )
        page.close()
        browser.close()

    (ARTIFACTS / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("UI_READABILITY_CHECK_OK")


if __name__ == "__main__":
    main()
