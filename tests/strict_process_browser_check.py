"""Browser → real HTTP API → supervisor → strict CLI recovery → final review.

The authored review clicks prove decision propagation, not video quality.
Browser HTTP routes are local-only; this is not an OS/network sandbox.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright
with mock.patch("dotenv.load_dotenv", return_value=False):
    from management_server import ConsoleHandler
from tests.strict_process_support import StrictProcessJourney, master_fixture
from tests.test_strict_process_recovery import StrictProcessRecoveryTests


def run_browser_recovery(work_root=None):
    blocked = []
    posts = []
    errors = []
    media_responses = []
    # Playwright allocates its private IPC loop before the supervisor's guard.
    with sync_playwright() as playwright:
        edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
        browser = playwright.chromium.launch(
            headless=True, executable_path=str(edge),
            args=["--disable-background-networking", "--disable-component-update", "--no-first-run"],
        )
        try:
            context = browser.new_context(service_workers="block")
            with StrictProcessJourney(work_root=work_root) as journey:
                journey.dispatch("corrupt")
                os.environ["STRICT_PROCESS_PHASE"] = "recover"
                class Handler(ConsoleHandler):
                    service = journey.service
                    service_lock = threading.RLock()
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    def local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.scheme != "http" or parsed.netloc != f"127.0.0.1:{server.server_port}":
                            blocked.append(route.request.url)
                            route.abort()
                        else:
                            if route.request.method == "POST":
                                posts.append(parsed.path)
                            route.continue_()
                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("response", lambda response: media_responses.append(response.status) if urlsplit(response.url).path == f"/api/videos/{master_fixture.JOB_ID}/file" else None)

                    def open_job():
                        page.goto(base, wait_until="networkidle")
                        page.locator("#open-advanced-nav").click()
                        page.locator('[data-advanced-page="videos"]').click()
                        page.locator(f'[data-video-id="{master_fixture.JOB_ID}"]').click()

                    open_job()
                    page.locator("#drawer-next-action").click()
                    page.wait_for_function("() => state.videos?.jobs?.some(j => j.state === 'PRODUCTION_QUEUED')")
                    assert page.locator("#produce-confirm").count() == 0, "recovery incorrectly requested paid generation"
                    assert f"/api/videos/{master_fixture.JOB_ID}/resume" in posts
                    page.reload(wait_until="networkidle")
                    assert page.locator("#produce-confirm").count() == 0, "refresh lost persisted recovery decision"
                    # Execute the same formal supervisor sweep used by the server.
                    journey.dispatch("recover")
                    open_job()
                    page.wait_for_function("() => [...document.querySelectorAll('video')].some(v => v.readyState >= 1 && v.videoWidth > 0)")
                    page.locator("#drawer-next-action").click()
                    page.locator("#review-check-all").click()
                    page.locator("#review-notes").fill("Authored transport replay review; not real content acceptance")
                    page.locator("#review-submit").click()
                    page.wait_for_function("() => state.videos?.jobs?.some(j => j.state === 'READY_TO_PUBLISH')")
                    assert f"/api/videos/{master_fixture.JOB_ID}/review" in posts
                    assert not any(p.endswith(("/produce", "/publish")) for p in posts), posts
                    assert not blocked, blocked
                    assert not errors, errors
                    assert media_responses and all(status in (200, 206) for status in media_responses), media_responses
                    StrictProcessRecoveryTests().assert_paid_once(journey)
                    return {
                        "status": "passed", "scope": "strict_master_download_browser_recovery",
                        "ui_actions": ["resume_failed_video", "review_final_video"],
                        "posts": posts, "workers": journey.records,
                        "final_state": "READY_TO_PUBLISH", "media_http_statuses": media_responses,
                        "simulated_cost_cents": 7,
                        "paid_provider_calls": 0, "external_requests": len(blocked),
                        "recognition_accuracy": "not_run", "actual_content_quality": "not_run",
                    }
                finally:
                    context.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args()
    print("STRICT_PROCESS_BROWSER_EVIDENCE=" + json.dumps(run_browser_recovery(args.work_root), ensure_ascii=False))
