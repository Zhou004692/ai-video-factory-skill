"""Real queue dispatch and CLI processes over a persistent isolated replay DB."""
from __future__ import annotations

import base64
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db
from factory.management_api import ManagementService
from tests import test_strict_master_compose_journey as master_fixture
from tests.strict_replay_support import _SYSTEM_ENV_KEYS


class StrictProcessJourney:
    def __init__(self, *, work_root=None):
        self.work_root = Path(work_root).resolve() if work_root is not None else None

    def __enter__(self):
        self.stack = ExitStack()
        try:
            if self.work_root is not None:
                self.work_root.mkdir(parents=True, exist_ok=True)
            self.root = Path(self.stack.enter_context(
                tempfile.TemporaryDirectory(prefix="strict-process-", dir=self.work_root)
            )).resolve()
            (self.root / ".strict-replay-root").touch()
            hook = self.root / "bootstrap"
            hook.mkdir()
            (hook / "sitecustomize.py").write_text(
                "import os, traceback\n"
                "try:\n"
                "    from tests.strict_process_worker import install\n"
                "    install()\n"
                "except BaseException:\n"
                "    from pathlib import Path\n"
                "    Path(os.environ['STRICT_PROCESS_ROOT'], 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf-8')\n"
                "    os._exit(87)\n", encoding="utf-8",
            )
            env = {k: v for k, v in os.environ.items() if k.upper() in _SYSTEM_ENV_KEYS}
            env.update({
                "PYTHONPATH": os.pathsep.join((str(hook), str(ROOT))),
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "FACTORY_DB_PATH": str(self.root / "factory.db"),
                "FACTORY_DATA_DIR": str(self.root),
                "PROVIDER_RECONCILE_ENABLED": "false",
                "STRICT_PROCESS_ROOT": str(self.root),
                "STRICT_PROCESS_PHASE": "normal",
            })
            self.stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            self.stack.enter_context(mock.patch.object(db, "database_path", return_value=self.root / "factory.db"))
            self.database = db.conn()
            self.stack.callback(self.database.close)
            self.fixture = master_fixture.StrictMasterComposeJourneyTests()
            self.spec = self.fixture.prepare(SimpleNamespace(database=self.database))
            self.database.execute("UPDATE jobs SET state='PRODUCTION_QUEUED'")
            self.database.commit()
            self.service = ManagementService(self.database, ROOT, data_dir=self.root)
            self.service.env_path = self.root / "isolated.env"
            self.service.production_service.env_path = self.service.env_path
            connect = sqlite3.connect
            def isolated_connect(path, *args, **kwargs):
                if Path(path).resolve() != self.root / "factory.db" or kwargs.get("uri"):
                    raise AssertionError("supervisor database outside isolated root")
                return connect(path, *args, **kwargs)
            self.stack.enter_context(mock.patch.object(sqlite3, "connect", isolated_connect))
            self.records = []
            contract = self.fixture.contract()
            self.write_contract("normal", contract)
            corrupt = deepcopy(contract)
            body = b"authored corrupt master media"
            corrupt["steps"][3]["response"] = {
                "status_code": 200,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
            corrupt["steps"] = corrupt["steps"][:4]
            self.write_contract("corrupt", corrupt)
            interrupted = deepcopy(contract)
            interrupted["steps"] = interrupted["steps"][:4]
            self.write_contract("interrupted", interrupted)
            recover = deepcopy(contract)
            recover["steps"] = recover["steps"][2:]
            self.write_contract("recover", recover)
            self.stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("supervisor external socket denied")))
            self.stack.enter_context(mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("supervisor DNS denied")))
            original = subprocess.Popen.__init__
            self._approved_recovery_child = None
            self.worker_pids = []
            self.taskkill = str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32" / "taskkill.exe")

            def launch(process, args, *positional, **kwargs):
                if (self._approved_recovery_child is not None
                        and list(args) == self._approved_recovery_child
                        and not positional and not kwargs.get("shell")
                        and kwargs.get("executable") is None
                        and Path(kwargs.get("cwd", "")).resolve() == ROOT):
                    child_env = kwargs.get("env") or {}
                    if child_env.get("PYTHONPATH") != str(ROOT):
                        raise AssertionError("recovery child must bypass Worker bootstrap")
                    return original(process, args, **kwargs)
                cleanup_commands = [[self.taskkill, "/PID", str(pid), "/T", "/F"] for pid in self.worker_pids]
                if os.name == "nt" and list(args) in cleanup_commands and not positional and not kwargs.get("shell") and not kwargs.get("executable") and kwargs.get("env") is None:
                    return original(process, args, **kwargs)
                expected = self.service._production_worker_command(master_fixture.JOB_ID)
                if (list(args) != expected or positional or kwargs.get("shell")
                        or kwargs.get("env") is not None or kwargs.get("executable")
                        or Path(kwargs.get("cwd", "")).resolve() != ROOT):
                    raise AssertionError("unregistered supervisor child")
                output = self.stack.enter_context((self.root / "worker-output.txt").open("a", encoding="utf-8"))
                kwargs.update(stdout=output, stderr=subprocess.STDOUT)
                original(process, args, **kwargs)
                self.worker_pids.append(process.pid)

            self.stack.enter_context(mock.patch.object(subprocess.Popen, "__init__", launch))
            return self
        except BaseException:
            self.stack.close()
            raise

    def write_contract(self, phase, contract):
        (self.root / f"{phase}.json").write_text(json.dumps(contract), encoding="utf-8")

    def wait_worker(self):
        workers = list(self.service._production_workers.values())
        if not workers:
            raise AssertionError("formal supervisor did not dispatch a Worker")
        worker = workers[-1]
        try:
            code = worker.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.terminate_worker(worker)
            raise AssertionError("strict Worker timed out")
        known = {record["pid"] for record in self.records}
        paths = [p for p in self.root.glob("worker-*.json") if int(p.stem.split("-")[1]) not in known]
        if len(paths) != 1:
            error = self.root / "startup-error.txt"
            output = (self.root / "worker-output.txt").read_text(encoding="utf-8")
            raise AssertionError(error.read_text(encoding="utf-8") if error.exists() else f"Worker {worker.pid} exited {code} without evidence: {output}; files={list(self.root.iterdir())}")
        record = json.loads(paths[0].read_text(encoding="utf-8"))
        record["launcher_pid"] = worker.pid
        record["supervisor_pid"] = os.getpid()
        if record["pid"] != worker.pid and record["parent_pid"] != worker.pid:
            raise AssertionError("strict Worker failed: unexpected process ancestry")
        if record["phase"] != os.environ["STRICT_PROCESS_PHASE"] or record["http_requests_matched"] != record["expected_requests"]:
            raise AssertionError("strict Worker failed: evidence phase/contract mismatch")
        self.records.append(record)
        expected_exit = 89 if record.get("interrupted") and record["phase"] == "interrupted" else 0
        if code != expected_exit or record["status"] != "passed":
            raise AssertionError(f"strict Worker failed ({code}): {record}")
        self.service.dispatch_production_queue()
        return record

    def dispatch(self, phase="normal"):
        os.environ["STRICT_PROCESS_PHASE"] = phase
        result = self.service.dispatch_production_queue()
        if result["dispatched_jobs"] != [master_fixture.JOB_ID]:
            raise AssertionError(result)
        return self.wait_worker()

    def run_recovery_child(self, mode, task_id="strict-master-task-1", *, expected_exit=0):
        report = self.root / f"recovery-{mode}.json"
        command = [sys.executable, str(ROOT / "tests" / "process_recovery_child.py"),
                   mode, str(self.root / "factory.db"), str(self.root),
                   master_fixture.JOB_ID, task_id, str(report)]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        self._approved_recovery_child = command
        try:
            child = subprocess.run(command, cwd=ROOT, env=environment,
                                   capture_output=True, text=True, timeout=45)
        finally:
            self._approved_recovery_child = None
        if child.returncode != expected_exit:
            raise AssertionError(child.stdout + child.stderr)
        return json.loads(report.read_text(encoding="utf-8"))

    def __exit__(self, *exc):
        for worker in self.service._production_workers.values():
            if worker.poll() is None:
                self.terminate_worker(worker)
        self.stack.close()

    def terminate_worker(self, worker):
        if worker.pid not in self.worker_pids:
            raise AssertionError("refusing cleanup of unregistered process")
        if os.name == "nt":
            result = subprocess.run(
                [self.taskkill, "/PID", str(worker.pid), "/T", "/F"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=15,
            )
            if result.returncode and worker.poll() is None:
                raise AssertionError("strict Worker process-tree cleanup failed")
        else:
            worker.kill()
        worker.wait(timeout=10)
