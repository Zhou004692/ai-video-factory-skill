"""Writable, zero-cost browser regression for the complete management flow."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from factory import (  # noqa: E402
    db,
    full_flow_regression,
    production_graph_runner,
    provider_replay,
    script_drafts,
    topic_briefs,
)
from factory.clients import doubao_llm  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from tests.management_workflow_browser_check import (  # noqa: E402
    _edge_path,
    _free_port,
    _signature,
    _stop_process_tree,
    _wait_until_ready,
)


TITLE = "零费用回归：三人跨场景高空车抢修"
CONTENT = """白天的施工现场，一台高空作业车在升降前触发支腿告警。调度负责人林夏立即暂停作业，通过对讲机要求现场负责人周工隔离设备，并让维修工程师高远在设备场复检备用车。

林夏说：先停机，不要带故障强行升降。周工回答：现场已经拉起警戒线，我核对支腿和地面承载。高远说：备用车的传感器、液压和载荷测试全部通过，可以出场。

镜头从调度中心切到设备场，再回到白天施工现场。备用高空车抵达后，周工复核安全条件并恢复作业。三人分别完成调度、检修和现场交付，形成可追溯的复工闭环。"""
PREFERENCES = {
    "suggested_duration_sec": 24,
    "suggested_shot_count": 6,
    "suggested_character_count": 3,
}


def _write_combined_manifest(root: Path, operations: list[tuple[dict, dict]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "fixture_schema_version": provider_replay.FIXTURE_SCHEMA_VERSION,
        "fixture_id": "management-writable-full-flow-v1",
        "operations": [
            {
                "operation": "doubao.llm.complete_json",
                "request_hash": provider_replay.fingerprint(
                    "doubao.llm.complete_json", request
                ),
                "responses": [{
                    "status": "succeeded",
                    "result": result,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }],
            }
            for request, result in operations
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prepare_manifest(planning_db: Path, fixture_root: Path) -> None:
    previous_db = os.environ.get("FACTORY_DB_PATH")
    os.environ["FACTORY_DB_PATH"] = str(planning_db)
    database = db.conn()
    try:
        service = ManagementService(database, ROOT, worker_mode="external")
        source = service.add_source(
            source_type="manual", title=TITLE, content=CONTENT
        )
        source_id = str(source["source_id"])
        source_record = full_flow_regression._source_record(database, source_id)

        brief_request = doubao_llm.request_payload(
            topic_briefs._brief_prompt([source_record]), max_tokens=3000
        )
        brief_result = full_flow_regression._brief_result(source_record)
        generated_brief = topic_briefs.generate_brief(
            database,
            [source_id],
            generate_json=lambda *_args, **_kwargs: (
                brief_result, {"input_tokens": 0, "output_tokens": 0}
            ),
        )
        brief_id = str(generated_brief["brief_id"])
        topic_briefs.approve_brief(
            database,
            brief_id,
            topic_id="TOPIC-" + brief_id.removeprefix("BRIEF-"),
            angle_index=0,
        )
        context, sources = script_drafts._load_context(database, brief_id)
        script_request = doubao_llm.request_payload(
            script_drafts._prompt(context, sources, PREFERENCES),
            max_tokens=6000,
        )
        script_result = full_flow_regression._script_result(source_id)
        _write_combined_manifest(
            fixture_root,
            [(brief_request, brief_result), (script_request, script_result)],
        )
    finally:
        database.close()
        if previous_db is None:
            os.environ.pop("FACTORY_DB_PATH", None)
        else:
            os.environ["FACTORY_DB_PATH"] = previous_db


def _open_advanced_page(page, name: str) -> None:
    page.locator("#open-advanced-nav").click()
    page.locator(f'[data-advanced-page="{name}"]').click()
    page.wait_for_selector(f"#page-{name}.active")


def _open_job(page, job_id: str) -> None:
    _open_advanced_page(page, "videos")
    page.wait_for_selector(f'[data-video-id="{job_id}"]')
    page.locator(f'[data-video-id="{job_id}"]').click()
    page.wait_for_selector("#page-overview.active")
    page.wait_for_function(
        "jobId => state.videos?.jobs?.some(item => item.job_id === jobId)",
        arg=job_id,
    )
    page.locator("#studio-primary-action").click()


def _job_id(database) -> str:
    row = database.execute(
        "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise AssertionError("approved script did not create a video job")
    return str(row["job_id"])


def _run_graph(database, job_id: str, media_root: Path) -> str:
    handlers = full_flow_regression.build_zero_cost_handlers(
        database, media_root=media_root
    )
    def execute():
        return asyncio.run(production_graph_runner.run_graph_job(
            database, job_id, configured_limit=4, handlers=handlers
        ))

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(execute).result()
    return str(result.job_state)


def main() -> int:
    production_db = ROOT / "data" / "factory.db"
    production_assets = ROOT / "data" / "assets"
    before = (_signature(production_db), _signature(production_assets))
    non_local_requests: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ai-video-browser-full-flow-") as temp:
        temp_root = Path(temp)
        target_db = temp_root / "factory.db"
        fixture_root = temp_root / "replay"
        media_root = temp_root / "assets"
        _prepare_manifest(temp_root / "planning.db", fixture_root)

        environment = os.environ.copy()
        environment.update({
            "FACTORY_DB_PATH": str(target_db),
            "FACTORY_DATA_DIR": str(temp_root),
            "FACTORY_PROVIDER_MODE": "replay",
            "FACTORY_REPLAY_FIXTURE_ROOT": str(fixture_root),
            "FACTORY_REPLAY_DISABLE_WORKERS": "1",
            "PRODUCTION_EXECUTION_ENGINE": "graph_v2",
            "PROVIDER_RECONCILE_ENABLED": "false",
        })
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "management_server.py"),
                "--host", "127.0.0.1",
                "--port", str(port),
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
            os.environ["FACTORY_DB_PATH"] = str(target_db)
            database = db.conn()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True, executable_path=_edge_path()
                    )
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 980},
                        device_scale_factor=1,
                    )
                    browser_errors: list[str] = []
                    page.on(
                        "pageerror", lambda error: browser_errors.append(str(error))
                    )

                    def local_only(route) -> None:
                        host = (urlsplit(route.request.url).hostname or "").casefold()
                        if host not in {"127.0.0.1", "localhost"}:
                            non_local_requests.append(route.request.url)
                            route.abort()
                            return
                        route.continue_()

                    page.route("**/*", local_only)
                    page.goto(base_url, wait_until="networkidle")
                    _open_advanced_page(page, "scripts")

                    page.locator("#new-source").click()
                    page.locator("#source-title").fill(TITLE)
                    page.locator("#source-type").select_option("manual")
                    page.locator("#source-content").fill(CONTENT)
                    page.locator("#source-submit").click()
                    page.wait_for_selector('#drawer[aria-hidden="false"]')
                    page.locator("#drawer-generate-brief").click()
                    page.locator("#confirm-generate-brief").click()
                    page.wait_for_selector("#drawer-generate-scripts")

                    page.locator("#drawer-edit-brief").click()
                    page.wait_for_selector("#save-brief-edit")
                    page.locator("#edit-brief-note").fill("零费用回归：确认人工可编辑")
                    page.locator("#save-brief-edit").click()
                    page.wait_for_selector("#drawer-generate-scripts")
                    page.locator("#drawer-generate-scripts").click()
                    page.locator("#script-suggest-duration").fill("24")
                    page.locator("#script-suggest-shots").fill("6")
                    page.locator("#script-suggest-characters").fill("3")
                    page.locator("#confirm-generate-scripts").click()
                    page.wait_for_selector("[data-draft-id]")

                    page.locator("[data-approve-script]").first.click()
                    page.wait_for_selector("#drawer-edit-script")
                    page.locator("#drawer-edit-script").click()
                    page.wait_for_selector("#save-script-edit")
                    page.locator("#edit-script-note").fill("零费用回归：确认分镜可编辑")
                    page.locator("#save-script-edit").click()
                    page.wait_for_selector("#drawer-approve")
                    page.locator("#drawer-approve").click()
                    page.wait_for_function(
                        "() => state.scripts?.drafts?.some(item => item.status === 'approved')"
                    )

                    job_id = _job_id(database)
                    _open_job(page, job_id)
                    page.wait_for_selector("#save-production-settings")
                    assert page.locator("[data-speaker-mapping]").count() == 3
                    page.locator("#setting-anchor-review-mode").select_option("manual")
                    page.locator("#setting-asr").uncheck()
                    page.locator("#setting-budget").fill("100")
                    page.locator("#save-production-settings").click()
                    page.wait_for_selector("#produce-confirm")
                    risk = page.locator("#high-risk-master-confirm")
                    if risk.count():
                        risk.check()
                    page.locator("#produce-confirm").click()
                    page.wait_for_function(
                        "jobId => state.videos?.jobs?.some(item => item.job_id === jobId && item.state === 'PRODUCTION_QUEUED')",
                        arg=job_id,
                    )

                    assert _run_graph(database, job_id, media_root) == "ANCHOR_REVIEW_REQUIRED"
                    page.reload(wait_until="networkidle")
                    _open_job(page, job_id)
                    page.wait_for_selector("#anchor-generation-status")
                    page.locator("#drawer-next-action").click()
                    page.wait_for_timeout(300)

                    from factory.steps import s6_compose
                    previous_assets = s6_compose.ASSETS_DIR
                    s6_compose.ASSETS_DIR = media_root
                    try:
                        assert _run_graph(database, job_id, media_root) == "COMPOSED"
                    finally:
                        s6_compose.ASSETS_DIR = previous_assets

                    page.reload(wait_until="networkidle")
                    _open_job(page, job_id)
                    page.wait_for_selector("#review-check-all")
                    page.locator("#review-check-all").click()
                    page.locator("#review-first-pass").check()
                    page.locator("#review-notes").fill("零费用回归验收通过")
                    page.locator("#review-submit").click()
                    page.wait_for_selector("#drawer-next-action")
                    page.locator("#drawer-next-action").click()
                    page.wait_for_function(
                        "jobId => state.videos?.jobs?.some(item => item.job_id === jobId && item.state === 'READY_TO_PUBLISH')",
                        arg=job_id,
                    )

                    row = database.execute(
                        "SELECT state,cost_cents,final_path FROM jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                    assert row["state"] == "READY_TO_PUBLISH", dict(row)
                    assert int(row["cost_cents"] or 0) == 0, dict(row)
                    assert Path(str(row["final_path"])).is_file(), dict(row)

                    # A changed provider request must fail closed and remain
                    # recoverable from the same visible action, in Chinese.
                    _open_advanced_page(page, "scripts")
                    page.locator("#new-source").click()
                    page.locator("#source-title").fill("零费用回放缺失夹具演练")
                    page.locator("#source-content").fill(
                        CONTENT + "\n补充一个会改变请求指纹的新事实。"
                    )
                    page.locator("#source-submit").click()
                    page.wait_for_selector("#drawer-generate-brief")
                    page.locator("#drawer-generate-brief").click()
                    page.locator("#confirm-generate-brief").click()
                    page.wait_for_selector("#toast.error.show")
                    assert "零费用回放未找到匹配夹具" in page.locator(
                        "#toast span"
                    ).inner_text()
                    assert "未调用外部模型" in page.locator(
                        "#toast span"
                    ).inner_text()
                    assert page.locator("#confirm-generate-brief").is_enabled()
                    assert not browser_errors, browser_errors
                    browser.close()
            finally:
                database.close()
                os.environ.pop("FACTORY_DB_PATH", None)
        finally:
            _stop_process_tree(server)

    assert not non_local_requests, non_local_requests
    assert (_signature(production_db), _signature(production_assets)) == before
    print("MANAGEMENT_FULL_FLOW_BROWSER_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
