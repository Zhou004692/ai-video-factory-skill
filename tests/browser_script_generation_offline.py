"""Serve an isolated, zero-provider-call UI journey for manual/agent browser QA.

Only local fixture models are installed. No original management database or
credentials are read. Stop by creating OUTPUT/stop after the browser journey.
"""
from argparse import ArgumentParser
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from factory import db, script_sources, script_drafts
from factory.management_api import ManagementService
from tests.test_script_drafts import _brief_payload
from tests.test_script_generation import variant
with patch("dotenv.load_dotenv", return_value=False):
    from management_server import ConsoleHandler


def main():
    parser = ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "factory.db").exists():
        raise RuntimeError("Use a fresh QA output directory; existing fixtures are preserved")
    database = sqlite3.connect(output / "factory.db", check_same_thread=False)
    database.row_factory = sqlite3.Row
    database.executescript(db.DDL)
    source = script_sources.ingest_text(database, "\n".join([
        "离线回归夹具：设备租赁客户讲清工况、型号与履约规则；检查候选逐版保存和人工恢复，不代表真实创意验收。"
    ] * 15), title="离线回归：候选逐版恢复")
    brief_id = "BRIEF-OFFLINE-RUN"
    database.execute("INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json) VALUES(?,?,?,?)", (
        brief_id, "candidate", "离线恢复验收", json.dumps(_brief_payload(), ensure_ascii=False),
    ))
    database.execute("INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)", (brief_id, source.source_id))
    database.commit()
    calls = []
    counters = {"variants": 0, "third_failed": False}
    def generate(prompt, **kwargs):
        time.sleep(0.3)
        ranking = "RANKING_REQUEST" in prompt
        calls.append({"stage": "ranking" if ranking else "variant", "max_tokens": kwargs.get("max_tokens"), "strict_json": kwargs.get("strict_json")})
        (output / "fixture-model-calls.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
        if ranking:
            return {"recommended_index": 1, "recommendation_reason": "离线夹具第二版叙事更清晰"}, {"input_tokens": 2, "output_tokens": 3}
        if counters["variants"] == 2 and not counters["third_failed"]:
            counters["third_failed"] = True
            raise TimeoutError("offline simulated unknown third-version response")
        counters["variants"] += 1
        item = variant(source.source_id, f"离线候选第{counters['variants']}版")
        return {"variant": item}, {"input_tokens": 10, "output_tokens": 20}
    script_drafts.doubao_llm.complete_json = generate
    service = ManagementService(database, ROOT, worker_mode="external", data_dir=output, env_path=output / "absent.env")
    service._load_runtime_env = lambda: None
    lock = threading.RLock()
    service.bind_service_lock(lock)
    handler = type("OfflineScriptHandler", (ConsoleHandler,), {
        "service": service, "service_lock": lock, "runtime_mode": "desktop", "auth_manager": None,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    manifest = {"url": f"http://127.0.0.1:{server.server_port}/#overview", "source_id": source.source_id,
                "brief_id": brief_id, "paid_provider_calls": 0, "fixture_only": True}
    (output / "server.json").write_text(json.dumps(manifest), encoding="utf-8")
    def stop():
        while not (output / "stop").exists():
            time.sleep(0.3)
        server.shutdown()
    threading.Thread(target=stop, daemon=True).start()
    print(json.dumps(manifest), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        for worker in service._script_generation_workers.copy().values():
            worker.join(3)
        database.close()


if __name__ == "__main__":
    main()
