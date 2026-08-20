"""Deterministic browser release gate for production failure decisions.

The check starts an ephemeral static server by default. Set ``MANAGEMENT_URL``
to exercise an already-running management UI instead. Every API route and media
request is intercepted; no production worker or provider can be contacted.

Stable release gate command (run at the repository root)::

    python tests/management_fault_browser_check.py --release-gate
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from playwright.sync_api import Route, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = PROJECT_ROOT / "scripts" / "management_ui"
UNIT_RELEASE_GATE_TESTS = (
    "tests.test_script_drafts",
    "tests.test_production_failure_matrix",
    "tests.test_video_upgrade.MusicFallbackTests."
    "test_explicit_library_missing_fails_closed_with_actionable_error",
    "tests.test_video_upgrade.MusicFallbackTests."
    "test_suno_post_ambiguity_keeps_gate_closed_for_second_run",
    "tests.test_video_upgrade.MusicFallbackTests."
    "test_s5_publish_db_failure_keeps_cost_journal_and_blocks_repost",
    "tests.test_management_console.ManagementServiceTests."
    "test_cost_reconciliation_separates_tracked_calls_from_legacy_total",
    "tests.test_management_console.ManagementServiceTests."
    "test_restart_recovers_accept_review_operation_at_every_crash_phase",
    "tests.test_management_console.ManagementServiceTests."
    "test_reconcile_second_phase_db_failure_replays_journal_without_inspect",
    "tests.test_management_console.ManagementMediaServerTests."
    "test_all_json_posts_reject_foreign_origin_and_wrong_content_type_before_produce",
    "tests.test_management_console.ManagementMediaServerTests."
    "test_binary_uploads_reject_foreign_origin_and_allow_same_origin",
)


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return


@contextmanager
def management_server():
    configured = os.environ.get("MANAGEMENT_URL", "").strip().rstrip("/")
    if configured:
        yield configured
        return
    handler = partial(QuietStaticHandler, directory=str(UI_ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "ephemeral management server did not stop"


def _preview_data_uri() -> str:
    return (
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
        "width='80' height='60'%3E%3Crect width='80' height='60' "
        "fill='%23dfe9e1'/%3E%3C/svg%3E"
    )


def base_job(job_id: str, state: str, *, title: str, error: str = "") -> dict:
    labels = {
        "PRODUCTION_REVIEW_REQUIRED": "母片待复核",
        "PROVIDER_ACTION_REQUIRED": "模型账号待处理",
        "REJECTED_PENDING_RECONCILIATION": "已终止，待成本核对",
        "FAILED": "失败",
    }
    actions = {
        "PRODUCTION_REVIEW_REQUIRED": {
            "id": "review_master_candidates", "label": "预览并处理候选母片",
        },
        "PROVIDER_ACTION_REQUIRED": {
            "id": "view_provider_account_guide", "label": "查看账号处理指引",
        },
        "REJECTED_PENDING_RECONCILIATION": {
            "id": "reconcile_rejected_provider_cost", "label": "核对终止任务成本",
        },
    }
    return {
        "job_id": job_id, "title": title, "state": state,
        "display_state": state, "display_state_label": labels[state],
        "next_action": actions.get(state), "storyboard": {},
        "characters": [], "character_makeup_plan": [],
        "scene_anchor_plan": [], "shots": [], "planned_masters": [],
        "director_timeline": [], "director_plan": {},
        "production_settings": {}, "unresolved_provider_tasks": [],
        "master_count": 0, "completed_master_count": 0,
        "total_master_duration_sec": 0, "cost_cents": 0,
        "final_exists": False, "resumable": False,
        "updated_at": "2026-08-18 12:00:00", "error": error,
    }


def review_job() -> dict:
    job = base_job(
        "J-BROWSER-REVIEW", "PRODUCTION_REVIEW_REQUIRED",
        title="确定性软质检复核", error="raw aggregate provider diagnostic",
    )
    preview = _preview_data_uri()
    job.update({
        "characters": [{"id": "lead", "name": "工程师"}],
        "character_makeup_plan": [{
            "identity_key": "lead", "display_name": "工程师",
            "preview_url": preview, "status": "ready",
            "approval_state": "APPROVED",
        }],
        "scene_anchor_plan": [{
            "anchor_key": "yard", "description": "设备停车场",
            "master_indexes": [2], "preview_url": preview, "status": "ready",
        }],
        "shots": [{
            "idx": 2, "state": "REVIEW_REQUIRED", "provider": "seedance",
            "duration_sec": 4, "cost_cents": 600,
            "manual_review_required": True,
            "quality_reasons": ["visual_mismatch", "missing_meaningful_action"],
            "quality_status": "manual_review_required",
            "candidate_preview_url": "/candidate.mp4",
            "previous_master_preview_url": None, "has_prior_master": False,
            "error": "SeedanceQualityError: safe visual mismatch detail",
        }],
        "planned_masters": [{"idx": 2, "type": "dialogue", "speaker": "lead"}],
        "master_count": 1, "total_master_duration_sec": 4, "cost_cents": 600,
    })
    return job


def provider_job(*, reconcilable: bool, rejected: bool = False) -> dict:
    state = "REJECTED_PENDING_RECONCILIATION" if rejected else "PROVIDER_ACTION_REQUIRED"
    job = base_job(
        "J-BROWSER-REJECTED" if rejected else (
            "J-BROWSER-PROVIDER" if reconcilable else "J-BROWSER-MANUAL"
        ),
        state, title="远端成本核对" if rejected else "模型任务核对",
        error="远端任务状态需要人工核对",
    )
    task_id = "remote-safe-rejected" if rejected else "remote-safe-active"
    job["unresolved_provider_tasks"] = [{
        "task_id": task_id if reconcilable else "local-submit-safe",
        "provider": "seedance", "intent": "generate", "cost_cents": 0,
        "reconcilable": reconcilable,
        **({} if reconcilable else {
            "action_message": "缺少远端任务 ID，请人工处置，禁止重新提交"
        }),
    }]
    return job


def provider_recovery_ready_job() -> dict:
    job = base_job(
        "J-BROWSER-PROVIDER-RECOVER", "PROVIDER_ACTION_REQUIRED",
        title="账号处理完成后恢复", error="Seedance 账号需要人工处理",
    )
    job.update({
        "next_action": {
            "id": "resume_failed_video", "label": "继续生成剩余母片",
        },
        "resumable": True,
        "shots": [{
            "idx": 1, "state": "FAILED", "provider": "seedance",
            "duration_sec": 4, "cost_cents": 0,
            "manual_review_required": False, "quality_reasons": [],
            "quality_status": "", "candidate_preview_url": None,
            "error": "Seedance 账号需要人工处理",
        }],
        "planned_masters": [{"idx": 1, "duration": 4, "type": "dialogue"}],
        "master_count": 1, "remaining_master_count": 1,
        "total_master_duration_sec": 4,
    })
    return job


def failed_job() -> dict:
    job = base_job(
        "J-BROWSER-FAILED", "FAILED", title="参数校验失败",
        error="SeedanceParameterError: duration maximum is 30 seconds",
    )
    job["shots"] = [{
        "idx": 1, "state": "FAILED", "provider": "seedance",
        "duration_sec": 36, "cost_cents": 0,
        "manual_review_required": False, "quality_reasons": [],
        "quality_status": "", "candidate_preview_url": None,
        "error": job["error"],
    }]
    job["planned_masters"] = [{"idx": 1, "duration": 36, "type": "dialogue"}]
    job["master_count"] = 1
    job["total_master_duration_sec"] = 36
    return job


def api_payload(path: str, job: dict) -> dict:
    if path == "/api/videos":
        return {"jobs": [job]}
    if path == "/api/overview":
        return {
            "scripts": {"sources": 0, "briefs": 0, "drafts": 0},
            "videos": {
                "jobs": 1, "active": 0, "completed": 0,
                "review_required": int(job["state"] == "PRODUCTION_REVIEW_REQUIRED"),
                "failed": int(job["state"] == "FAILED"),
                "cost_cents": job["cost_cents"],
                "tracked_call_cost_cents": job["cost_cents"],
                "unallocated_cost_cents": 0,
                "reconciliation_status": "reconciled",
            },
            "publishing": {"ready": 0, "mapped": 0, "published": 0},
            "configuration": {},
        }
    if path == "/api/projects":
        return {"projects": []}
    if path == "/api/production/health":
        return {"supervisor_alive": False, "running": [], "queued": []}
    if path == "/api/events":
        return {"events": []}
    if path == "/api/scripts":
        return {"sources": [], "briefs": [], "drafts": []}
    if path == "/api/publishing":
        return {"submissions": [], "publications": []}
    return {}


def scenario_page(browser, base_url: str, job: dict):
    posts: list[dict] = []
    errors: list[str] = []
    bad_responses: list[str] = []
    page = browser.new_page(viewport={"width": 1440, "height": 980})
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: bad_responses.append(
        f"{response.status} {response.url}"
    ) if response.status >= 400 else None)
    page.route("**/candidate.mp4*", lambda route: route.fulfill(
        status=200, content_type="video/mp4", body=b""
    ))
    page.route("**/favicon.ico", lambda route: route.fulfill(status=204, body=b""))

    def route_api(route: Route) -> None:
        provider_request = route.request
        path = urlsplit(provider_request.url).path
        if provider_request.method == "POST":
            body = json.loads(provider_request.post_data or "{}")
            posts.append({"path": path, "body": body})
            if path.endswith("/candidate-review"):
                result = {
                    "job_id": job["job_id"], "master_idx": 2,
                    "decision": body.get("decision"),
                    "state": "REJECTED" if body.get("decision") == "reject_job"
                    else "PRODUCTION_QUEUED",
                    "remaining_review_count": 0,
                    "remaining_generation_count": (
                        0 if body.get("decision") == "reject_job" else 1
                    ),
                    "dispatched_jobs": [],
                }
            elif path.endswith("/provider-task/reconcile"):
                result = {
                    "job_id": job["job_id"],
                    "state": "REJECTED" if job["state"].startswith("REJECTED")
                    else "PROVIDER_ACTION_REQUIRED",
                }
            elif path.endswith("/resume"):
                result = {
                    "job_id": job["job_id"], "state": "STORYBOARDED",
                    "preserved_masters": 0, "remaining_masters": 1,
                }
                job.update({
                    "state": "STORYBOARDED", "display_state": "STORYBOARDED",
                    "display_state_label": None, "next_action": None,
                    "resumable": False, "error": None,
                })
                for shot in job["shots"]:
                    shot["state"] = "PENDING"
                    shot["error"] = None
            else:
                result = {}
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(result, ensure_ascii=False),
            )
            return
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(api_payload(path, job), ensure_ascii=False),
        )

    page.route("**/api/**", route_api)
    page.goto(f"{base_url}/index.html#videos", wait_until="networkidle")
    return page, posts, errors, bad_responses


def assert_clean(page, errors: list[str], bad_responses: list[str]) -> None:
    try:
        assert not errors and not bad_responses, {
            "console": errors, "responses": bad_responses,
        }
    finally:
        page.close()


def run_review_decision(browser, base_url: str, decision: str) -> None:
    page, posts, errors, bad = scenario_page(browser, base_url, review_job())
    page.locator('[data-video-id="J-BROWSER-REVIEW"]').click()
    page.locator("#drawer-business-pause-action").click()
    assert page.get_by_text("候选与要求的差异", exact=True).is_visible()
    assert page.get_by_text("人物定妆", exact=True).is_visible()
    assert page.get_by_text("场景锚点", exact=True).is_visible()
    assert page.get_by_text("上一条连续母片：未提供", exact=True).is_visible()
    assert page.locator("[data-candidate-decision]").count() == 3
    page.locator(f'[data-candidate-decision="{decision}"]').click()
    page.wait_for_timeout(150)
    assert posts == [{
        "path": "/api/videos/J-BROWSER-REVIEW/masters/2/candidate-review",
        "body": {"decision": decision},
    }], posts
    assert page.locator("#modal").get_attribute("hidden") is not None
    assert_clean(page, errors, bad)


def run_provider_reconcile(browser, base_url: str, *, rejected: bool) -> None:
    job = provider_job(reconcilable=True, rejected=rejected)
    page, posts, errors, bad = scenario_page(browser, base_url, job)
    page.locator(f'[data-video-id="{job["job_id"]}"]').click()
    page.locator("#drawer-business-pause-action").click()
    task = job["unresolved_provider_tasks"][0]["task_id"]
    page.locator(f'[data-provider-task="{task}"]').click()
    page.wait_for_timeout(150)
    assert posts == [{
        "path": f'/api/videos/{job["job_id"]}/provider-task/reconcile',
        "body": {"task_id": task},
    }], posts
    assert_clean(page, errors, bad)


def run_provider_manual_only(browser, base_url: str) -> None:
    job = provider_job(reconcilable=False)
    page, posts, errors, bad = scenario_page(browser, base_url, job)
    page.locator(f'[data-video-id="{job["job_id"]}"]').click()
    page.locator("#drawer-business-pause-action").click()
    manual = page.locator(".provider-task-manual")
    assert manual.is_visible()
    assert "人工处置" in manual.inner_text()
    assert page.locator("[data-provider-task]").count() == 0
    manual.click()
    page.wait_for_timeout(100)
    assert posts == [], posts
    assert_clean(page, errors, bad)


def run_provider_recovery_ready(browser, base_url: str) -> None:
    job = provider_recovery_ready_job()
    page, posts, errors, bad = scenario_page(browser, base_url, job)
    page.locator(f'[data-video-id="{job["job_id"]}"]').click()
    assert page.locator("#drawer-resume-video").count() == 1
    assert page.locator("#drawer-business-pause-action").count() == 0
    assert page.locator("#drawer .drawer-footer").get_by_text(
        "继续生成剩余母片", exact=True
    ).count() == 1
    duplicate_ids = page.evaluate("""() => [...document.querySelectorAll('[id]')]
        .map(node => node.id).filter((id, index, ids) => ids.indexOf(id) !== index)""")
    assert duplicate_ids == [], duplicate_ids
    page.locator("#drawer-resume-video").click()
    page.wait_for_timeout(150)
    assert posts == [{
        "path": "/api/videos/J-BROWSER-PROVIDER-RECOVER/resume", "body": {},
    }], posts
    assert page.get_by_text("生产预检与成本确认", exact=True).is_visible()
    assert page.get_by_text("模型账号与远端任务待处理", exact=True).count() == 0
    assert_clean(page, errors, bad)


def run_failed_details(browser, base_url: str) -> None:
    job = failed_job()
    page, posts, errors, bad = scenario_page(browser, base_url, job)
    row = page.locator(f'[data-video-id="{job["job_id"]}"]')
    assert "查看失败原因" in row.inner_text()
    row.click()
    assert page.get_by_text(
        "任务存在异常，先查看原因再决定是否重试", exact=True
    ).is_visible()
    assert page.locator("#drawer .error-callout > p").filter(
        has_text="视频模型调用失败，请查看技术详情"
    ).is_visible()
    details = page.locator("#drawer details")
    assert details.count() == 1
    assert details.get_attribute("open") is None
    details.locator("summary").click()
    assert details.get_attribute("open") is not None
    technical = details.locator(".technical-error").inner_text()
    assert technical == job["error"]
    assert "Traceback" not in technical
    assert "Authorization" not in technical
    assert posts == [], posts
    assert_clean(page, errors, bad)


def run_browser_gate() -> None:
    with management_server() as base_url, sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
        edge = Path(
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        )
        launch = {"headless": True}
        if executable:
            launch["executable_path"] = executable
        elif edge.is_file():
            launch["executable_path"] = str(edge)
        browser = playwright.chromium.launch(**launch)
        try:
            for decision in (
                "accept_candidate", "regenerate_master", "reject_job",
            ):
                run_review_decision(browser, base_url, decision)
            run_provider_reconcile(browser, base_url, rejected=False)
            run_provider_manual_only(browser, base_url)
            run_provider_recovery_ready(browser, base_url)
            run_provider_reconcile(browser, base_url, rejected=True)
            run_failed_details(browser, base_url)
        finally:
            browser.close()


def run_unit_gate() -> None:
    subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *UNIT_RELEASE_GATE_TESTS],
        cwd=PROJECT_ROOT, check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release-gate", action="store_true",
        help="run the explicit unit regression manifest before browser checks",
    )
    args = parser.parse_args()
    if args.release_gate:
        run_unit_gate()
    run_browser_gate()
    print("management production-fault browser check: PASS (8 scenarios)")


if __name__ == "__main__":
    main()
