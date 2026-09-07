"""Test-only startup hook for an unchanged, formally dispatched CLI Worker.

Installed through a temporary PYTHONPATH/sitecustomize, never imported by
production. Only transport, clock/UUID and isolated storage seams change.
"""
from __future__ import annotations

import atexit
import asyncio
import json
import os
import platform
from pathlib import Path
import sys
import traceback
from unittest import mock


def install() -> None:
    root = Path(os.environ["STRICT_PROCESS_ROOT"]).resolve()
    phase = os.environ["STRICT_PROCESS_PHASE"]
    if phase not in {"normal", "corrupt", "recover", "interrupted"}:
        raise RuntimeError("unknown strict Worker phase")
    repository = Path(__file__).resolve().parents[1]
    if (
        Path(sys.argv[0]).resolve() != repository / "scripts" / "cli.py"
        or sys.argv[1:] != ["produce-graph", "J-strict-master", "--dispatched"]
        or not (root / ".strict-replay-root").is_file()
    ):
        raise RuntimeError("strict Worker startup boundary mismatch")
    sys.path.insert(0, str(repository / "scripts"))
    # TOS constructs its User-Agent from cached OS metadata. Windows may use
    # `cmd /c ver` when WMI is unavailable; resolve it before the deny guard.
    platform.uname()
    from tests.strict_handler_support import StrictMasterComposeReplay

    contract = json.loads((root / (phase + ".json")).read_text(encoding="utf-8"))
    replay = StrictMasterComposeReplay(
        contract=contract, work_root=root,
    )
    # Allocate the proactor's private wakeup socket before the network deny
    # guard, just as IsolatedAsyncioTestCase does for the in-process journeys.
    loop = asyncio.new_event_loop()
    # A real installation may have credentials in its .env. The CLI still
    # loads configuration normally in production; this test uses only the
    # explicit replay environment and never opens the installation's file.
    config_patch = mock.patch("dotenv.load_dotenv", return_value=False)
    config_patch.start()
    try:
        replay.__enter__()
    except BaseException:
        config_patch.stop()
        raise
    replay._stack.callback(config_patch.stop)
    replay._stack.enter_context(mock.patch.object(asyncio.events, "new_event_loop", return_value=loop))
    # cli.py uses its normal build_handlers branch, never high-level fixtures.
    if os.environ.get("FACTORY_FLOW_GUARD_ZERO_COST_WORKER"):
        raise RuntimeError("strict Worker must retain production handlers")

    def finish(*, interrupted=False):
        failure = None
        job_state = replay.database.execute("SELECT state FROM jobs WHERE job_id='J-strict-master'").fetchone()[0]
        nodes = [dict(row) for row in replay.database.execute(
            "SELECT node_id,node_type,state,provider_task_id,actual_cost_cents FROM production_nodes WHERE job_id='J-strict-master' ORDER BY node_id"
        )]
        if not interrupted:
            try:
                replay.__exit__(None, None, None)
            except BaseException:
                failure = traceback.format_exc()
        record = {
            **replay.evidence(), "pid": os.getpid(), "parent_pid": os.getppid(), "phase": phase,
            "status": "failed" if failure else "passed",
            "error": failure, "requests": replay.requests,
            "media_processes": replay.processes,
            "worker_entrypoint": "cli.py produce-graph --dispatched",
            "interrupted": interrupted,
            "job_state": job_state, "graph_nodes": nodes,
        }
        (root / f"worker-{os.getpid()}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        if failure:
            os._exit(86)

    atexit.register(finish)
    if phase == "interrupted":
        respond = replay._respond
        def crash_after_download(request):
            response = respond(request)
            if replay._position == 4:
                # Emulate abrupt death at the transport return boundary, before
                # local staging/accounting completion. No cleanup/atexit runs.
                finish(interrupted=True)
                os._exit(89)
            return response
        replay._respond = crash_after_download
