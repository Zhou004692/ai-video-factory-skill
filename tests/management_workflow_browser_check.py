"""Deterministic end-to-end check for the local management console."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib import request

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db  # noqa: E402
from tests.fixtures.management_factory import (  # noqa: E402
    FIXTURE_IDS,
    seed_management_factory,
    seed_management_scale,
)


def _signature(path: Path) -> tuple[int, int, str] | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    if path.is_file():
        files = (path,)
        relative_root = path.parent
    else:
        files = tuple(sorted(
            (item for item in path.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(path).as_posix(),
        ))
        relative_root = path
    total_size = 0
    newest_mtime_ns = path.stat().st_mtime_ns
    for item in files:
        relative = item.relative_to(relative_root).as_posix()
        stat = item.stat()
        total_size += stat.st_size
        newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return total_size, newest_mtime_ns, digest.hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_ready(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"management server exited early: {output}")
        try:
            with request.urlopen(f"{url}/api/overview", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("management server did not become ready")


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _edge_path() -> str:
    configured = os.environ.get("PLAYWRIGHT_BROWSER_PATH", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Microsoft Edge executable was not found")


def _contrast_ratio(page) -> float:
    return float(page.locator("#toast").evaluate(
        """element => {
          const parse = value => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
          const luminance = color => {
            const channels = parse(color).map(value => value / 255).map(
              value => value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4)
            );
            return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
          };
          const style = getComputedStyle(element);
          const foreground = luminance(style.color);
          const background = luminance(style.backgroundColor);
          return (Math.max(foreground, background) + 0.05) /
            (Math.min(foreground, background) + 0.05);
        }"""
    ))


def main() -> int:
    production_db = ROOT / "data" / "factory.db"
    production_before = _signature(production_db)
    blocked_writes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ai-video-management-e2e-") as temp:
        isolated_db = Path(temp) / "factory-e2e.db"
        previous_db = os.environ.get("FACTORY_DB_PATH")
        os.environ["FACTORY_DB_PATH"] = str(isolated_db)
        try:
            database = db.conn()
            try:
                seed_management_factory(database)
                seed_management_scale(database)
                database.execute(
                    "UPDATE jobs SET execution_engine='graph_v2' "
                    "WHERE job_id='J-E2E-RUNNING'"
                )
                graph_nodes = (
                    ("character_anchor:lead", "character_anchor", "SUCCEEDED"),
                    ("scene_anchor:yard", "scene_anchor", "SUCCEEDED"),
                    ("master:1", "master", "RUNNING"),
                    ("local_compose", "local_compose", "PENDING"),
                    ("final_review", "final_review", "PENDING"),
                )
                for node_id, node_type, state in graph_nodes:
                    database.execute(
                        "INSERT INTO production_nodes("
                        "job_id,node_id,node_type,paid,state,spec_hash,"
                        "input_json,idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            "J-E2E-RUNNING", node_id, node_type,
                            int(node_type in {
                                "character_anchor", "scene_anchor", "master",
                            }),
                            state, "browser-spec", "{}", f"browser-{node_id}",
                        ),
                    )
                database.commit()
            finally:
                database.close()

            port = _free_port()
            base_url = f"http://127.0.0.1:{port}"
            environment = os.environ.copy()
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "management_server.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-browser",
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
            try:
                _wait_until_ready(base_url, server)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True, executable_path=_edge_path()
                    )
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 980},
                        device_scale_factor=1,
                    )
                    errors: list[str] = []
                    page.on(
                        "console",
                        lambda message: errors.append(message.text)
                        if message.type == "error"
                        else None,
                    )
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def guard_writes(route) -> None:
                        if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                            blocked_writes.append(
                                f"{route.request.method} {route.request.url}"
                            )
                            route.fulfill(
                                status=418,
                                content_type="application/json",
                                body='{"error":"write blocked by e2e gate"}',
                            )
                            return
                        route.continue_()

                    page.route("**/api/**", guard_writes)
                    page.goto(base_url, wait_until="networkidle")

                    def open_advanced_page(page_name: str) -> None:
                        page.locator("#open-advanced-nav").click()
                        page.locator(
                            f'[data-advanced-page="{page_name}"]'
                        ).click()

                    page.locator('.nav-item[data-page="todos"]').click()
                    page.wait_for_selector("#page-todos.active")
                    page.wait_for_selector("#reliability-overview", state="visible")
                    reliability = page.locator("#reliability-overview")
                    assert reliability.is_visible()
                    assert not reliability.evaluate("element => element.open")
                    collapsed_height = reliability.evaluate(
                        "element => element.getBoundingClientRect().height"
                    )
                    assert collapsed_height <= 70, collapsed_height
                    reliability.locator("summary").click()
                    rollout = page.locator(".v2-rollout-card")
                    assert rollout.is_visible()
                    assert "/ 10" in rollout.locator("b").inner_text()
                    reliability.locator("summary").click()
                    page.locator('.nav-item[data-page="overview"]').click()
                    page.wait_for_selector("#page-overview.active")
                    workbench = page.locator("#studio-workbench")
                    assert workbench.evaluate(
                        "element => element.getBoundingClientRect().bottom"
                    ) <= page.locator("#page-overview").evaluate(
                        "element => element.getBoundingClientRect().bottom + 1"
                    )
                    stage_body = page.locator(".studio-stage-body")
                    assert stage_body.evaluate(
                        "element => getComputedStyle(element).overflowY"
                    ) in {"auto", "scroll"}
                    open_advanced_page("projects")
                    page.wait_for_selector("#page-projects.active")
                    page.wait_for_selector(".project-row")
                    assert page.locator(".project-row").count() >= 6

                    expected_actions = {
                        "source": ("生成选题简报",),
                        "brief": ("使用当前角度生成剧本", "确认选题并生成剧本"),
                        "script": ("审核当前版本", "审核剧本版本"),
                        "setup": ("完成制作设置",),
                        "running": ("查看当前母片", "查看生成进度"),
                        "review": ("验收成片", "处理当前问题"),
                    }
                    for stage, action_labels in expected_actions.items():
                        page.locator(
                            f'[data-project-id="{FIXTURE_IDS[stage]}"]'
                        ).click()
                        page.wait_for_selector("#page-overview.active")
                        page.wait_for_function(
                            "projectId => document.querySelector('#studio-project-code')?.textContent.includes(projectId)",
                            arg=FIXTURE_IDS[stage],
                        )
                        next_action = page.locator("#studio-primary-action")
                        assert next_action.is_visible()
                        actual_action = next_action.inner_text()
                        assert actual_action.startswith(action_labels), (
                            stage, action_labels, actual_action
                        )
                        next_action.click()
                        if stage == "setup":
                            page.wait_for_selector("#setting-orientation")
                            assert page.locator("[data-speaker-mapping]").count() == 1
                            assert page.locator("#setting-budget").is_visible()
                            page.locator("#modal-close").click()
                            open_advanced_page("projects")
                            page.wait_for_selector("#page-projects.active")
                            continue
                        page.wait_for_selector('#drawer[aria-hidden="false"]')
                        if stage == "source":
                            assert page.get_by_text(
                                "来源具体内容", exact=True
                            ).is_visible()
                            drawer_body = page.locator(".drawer-body")
                            assert drawer_body.evaluate(
                                "element => getComputedStyle(element).overflowY"
                            ) in {"auto", "scroll"}
                        elif stage == "brief":
                            assert page.locator(".brief-angle").count() == 3
                        elif stage == "script":
                            assert page.get_by_text("完整剧本", exact=True).is_visible()
                        else:
                            assert page.get_by_text("生产信息", exact=True).is_visible()
                        page.locator("#drawer-close").click()
                        open_advanced_page("projects")
                        page.wait_for_selector("#page-projects.active")

                    open_advanced_page("scripts")
                    page.wait_for_selector("#page-scripts.active")
                    page.wait_for_function(
                        "() => document.querySelector('#source-count')?.textContent === '50'"
                    )
                    assert page.locator("#source-count").inner_text() == "50"
                    assert page.locator("#brief-count").inner_text() == "20"
                    assert page.locator("#draft-count").inner_text() == "20"
                    assert page.locator("#source-list [data-source-id]").count() == 50
                    assert page.locator("#source-list").evaluate(
                        "element => element.scrollHeight > element.clientHeight"
                    )
                    page.locator('[data-script-stage="candidates"]').click()
                    candidate = page.locator(
                        '[data-candidate-id="CAND-E2E-HOTSPOT"]'
                    )
                    assert candidate.is_visible()
                    candidate.click()
                    assert page.get_by_text("候选摘要", exact=True).is_visible()
                    assert page.locator("#drawer-promote-candidate").is_visible()
                    page.locator("#drawer-close").click()

                    open_advanced_page("videos")
                    page.wait_for_selector("#page-videos.active")
                    page.wait_for_function(
                        "() => document.querySelectorAll('[data-video-id]').length === 30"
                    )
                    assert page.locator("[data-video-id]").count() == 30
                    page.locator('#video-filters [data-state="in_progress"]').click()
                    visible_jobs = page.locator("[data-video-id]")
                    assert visible_jobs.count() == 1
                    assert visible_jobs.first.get_attribute("data-video-id") == "J-E2E-RUNNING"
                    browser_stages = page.evaluate(
                        "state.videos.jobs.find(job => job.job_id === 'J-E2E-RUNNING')?.production_stages || []"
                    )
                    assert len(browser_stages) == 5, browser_stages
                    visible_jobs.first.click()
                    page.wait_for_selector("#page-overview.active")
                    page.wait_for_function(
                        "projectId => document.querySelector('#studio-project-code')?.textContent.includes(projectId)",
                        arg=FIXTURE_IDS["running"],
                    )
                    page.locator("#studio-primary-action").click()
                    page.wait_for_selector('#drawer[aria-hidden="false"]')
                    page.wait_for_function(
                        "() => document.querySelectorAll('.production-stage-card').length === 5"
                    )
                    assert page.locator(".production-stage-card").count() == 5
                    technical = page.locator(".production-technical")
                    assert technical.is_visible()
                    assert not technical.evaluate("element => element.open")
                    page.locator("#drawer-close").click()

                    open_advanced_page("publishing")
                    page.wait_for_selector("#page-publishing.active")
                    page.locator('[data-go-config="douyin"]').click()
                    page.wait_for_selector("#page-config.active")
                    platform_fields = page.locator('[data-platform-fields="douyin"]')
                    assert not platform_fields.get_attribute("hidden")
                    assert platform_fields.is_visible()

                    page.evaluate("toast('端到端错误提示', true)")
                    assert page.locator("#toast.error.show").is_visible()
                    assert _contrast_ratio(page) >= 4.5

                    mobile = browser.new_page(
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=1,
                    )
                    mobile.route("**/api/**", guard_writes)
                    mobile.goto(base_url, wait_until="networkidle")
                    mobile.locator("#open-advanced-nav").click()
                    mobile.locator('[data-advanced-page="videos"]').click()
                    mobile.wait_for_selector("#page-videos.active")
                    assert mobile.evaluate(
                        "document.documentElement.scrollWidth <= "
                        "document.documentElement.clientWidth"
                    )
                    assert not errors, errors
                    browser.close()
            finally:
                _stop_process_tree(server)
        finally:
            if previous_db is None:
                os.environ.pop("FACTORY_DB_PATH", None)
            else:
                os.environ["FACTORY_DB_PATH"] = previous_db

    assert blocked_writes == [], blocked_writes
    assert _signature(production_db) == production_before
    print("MANAGEMENT_WORKFLOW_BROWSER_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
