"""Exercise shipped Skill entry points in isolated processes and state roots.

Prepare an offline wheelhouse first; no Provider credentials or paid requests
are used. The legacy package comes from an actual Git revision, not a changed
VERSION label. HTTP review is scripted and does not assert content quality.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from urllib.request import Request, urlopen
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.zero_cost_release_gate import _SECRET_MARKERS, _guard_source


def python_path(root):
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class DeliveryJourney:
    def __init__(self, workspace, wheelhouse, legacy_ref, legacy_expected_version=None):
        self.workspace = workspace.resolve()
        self.wheelhouse = wheelhouse.resolve()
        self.legacy_ref = legacy_ref
        self.legacy_expected_version = legacy_expected_version
        self.commands = []
        self.checks = []
        self.telemetry = self.workspace / "external-requests.jsonl"
        self.guard = self.workspace / "guard"
        self.guard.mkdir()
        (self.guard / "sitecustomize.py").write_text(_guard_source(), encoding="utf-8")
        self.host = self.workspace / "clean-host"
        venv.EnvBuilder(with_pip=True).create(self.host)
        self.host_python = python_path(self.host)

    def environment(self, home):
        env = {key: value for key, value in os.environ.items()
               if not any(marker in key.upper() for marker in _SECRET_MARKERS)
               and not key.startswith(("FACTORY_", "PIP_", "PYTHON"))}
        env.update({
            "PYTHONPATH": str(self.guard), "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
            "FACTORY_RUNTIME_HOME": str(home), "FACTORY_PROVIDER_MODE": "replay",
            "FACTORY_REPLAY_DISABLE_WORKERS": "1", "PROVIDER_RECONCILE_ENABLED": "false",
            "FACTORY_ZERO_COST_SOCKET_TELEMETRY": str(self.telemetry),
            "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(self.wheelhouse),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        })
        return env

    def run(self, args, *, cwd, home, expected=0, env=None):
        index = len(self.commands) + 1
        with (self.workspace / f"command-{index}.out.log").open("wb") as out, \
                (self.workspace / f"command-{index}.err.log").open("wb") as err:
            process = subprocess.Popen([str(a) for a in args], cwd=cwd,
                                       env=env or self.environment(home), stdout=out, stderr=err)
            try:
                code = process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                self.stop(process)
                raise
        self.commands.append({"pid": process.pid, "returncode": code,
                              "entry": str(args[1]) if len(args) > 1 else str(args[0])})
        if code != expected:
            detail = (self.workspace / f"command-{index}.err.log").read_text(
                encoding="utf-8", errors="replace")[-2500:]
            raise AssertionError(f"command {index}: exit {code}, expected {expected}: {detail}")
        return (self.workspace / f"command-{index}.out.log").read_text(
            encoding="utf-8", errors="replace")

    @staticmethod
    def stop(process):
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    def check(self, name, condition):
        if not condition:
            raise AssertionError(name)
        self.checks.append(name)
        print(f"P4_JOURNEY passed={name}", flush=True)

    def last_error(self):
        return (self.workspace / f"command-{len(self.commands)}.err.log").read_text(
            encoding="utf-8", errors="replace")

    @staticmethod
    def json_output(text):
        decoder = json.JSONDecoder()
        for pos in range(len(text)):
            if text[pos] != '{':
                continue
            try:
                value, end = decoder.raw_decode(text[pos:])
                if isinstance(value, dict) and not text[pos + end:].strip():
                    return value
            except ValueError:
                continue
        raise AssertionError("entry point returned no JSON report")

    def bootstrap(self, package, home, *, script=False):
        report = self.json_output(self.run(
            [self.host_python, package / "scripts/bootstrap.py", "--profile",
             "script" if script else "learning", "--json"], cwd=package, home=home,
            expected=1 if script else 0))
        self.check("bootstrap_configuration_required" if script else "bootstrap_ready",
                   report["status"] == ("configuration_required" if script else "ready")
                   and report["paid_provider_calls"] == 0)
        return report

    def extract(self, archive, parent):
        parent.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if not (parent / name).resolve().is_relative_to(parent.resolve()):
                    raise AssertionError("package path escapes extraction root")
            bundle.extractall(parent)
        return parent / "ai-video-factory"

    def update(self, staged, parent, home, *, expected=0, env=None):
        return self.run([self.host_python, staged / "scripts/distribute_skill.py",
                         "--output", parent], cwd=staged, home=home, expected=expected, env=env)

    @contextmanager
    def server(self, package, home):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log_path = self.workspace / f"server-{len(self.commands)}.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [str(python_path(package / ".venv")), str(package / "scripts/management_server.py"),
                 "--port", str(port), "--no-browser"], cwd=package,
                env=self.environment(home), stdout=log, stderr=log)
            self.commands.append({"pid": process.pid, "entry": "management_server.py"})
            base = f"http://127.0.0.1:{port}"
            try:
                deadline = time.monotonic() + 45
                while True:
                    if process.poll() is not None:
                        raise AssertionError(log_path.read_text(encoding="utf-8", errors="replace"))
                    try:
                        self.http(base, "/api/videos")
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("management service did not become ready")
                        time.sleep(.2)
                yield base
            finally:
                self.stop(process)

    @staticmethod
    def http(base, path, payload=None):
        request = Request(base + path,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json", "Origin": base})
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def review(self, base, job_id):
        result = self.http(base, f"/api/videos/{job_id}/review", {
            "decision": "approved", "notes": "scripted technical review",
            "checks": dict.fromkeys(("identity", "lip_sync", "subtitle_sync",
                                     "visual_integrity", "clean_frame", "editing_rhythm"), True),
        })
        self.check("historical_task_review_resumed", result["publish_unlocked"])

    def execute(self):
        fresh_home = self.workspace / "fresh-home"
        self.run([self.host_python, "-c",
                  "import importlib.util; assert all(importlib.util.find_spec(name) is None "
                  "for name in ('httpx','click','dotenv','yaml','tos','anthropic','PIL'))"],
                 cwd=self.workspace, home=fresh_home)
        self.check("clean_host_has_no_project_dependencies", True)
        release = self.json_output(self.run(
            [self.host_python, ROOT / "scripts/distribute_skill.py", "--output",
             self.workspace / "release"], cwd=ROOT, home=fresh_home))
        archive = Path(release["archive"])
        staged = self.extract(archive, self.workspace / "staged")
        fresh = self.extract(archive, self.workspace / "fresh")
        self.bootstrap(fresh, fresh_home, script=True)
        self.check("fresh_state_outside_skill", (fresh_home / "data/factory.db").is_file()
                   and (fresh_home / ".env").is_file() and not (fresh / ".env").exists()
                   and not (fresh / "data/factory.db").exists())
        self.bootstrap(fresh, fresh_home)
        fresh_version = digest(fresh / "VERSION")
        with self.server(fresh, fresh_home) as base:
            self.check("fresh_management_http_started", self.http(base, "/api/videos")["jobs"] == [])
            self.update(staged, fresh.parent, fresh_home, expected=1)
            self.check("live_service_blocks_code_replacement",
                       digest(fresh / "VERSION") == fresh_version and 'RuntimeStateBusy' in self.last_error())

        legacy_source = self.workspace / "legacy-source"
        legacy_source.mkdir()
        tar_path = self.workspace / "legacy.tar"
        self.run(["git", "archive", self.legacy_ref, "--output", tar_path], cwd=ROOT, home=fresh_home)
        with tarfile.open(tar_path) as bundle:
            bundle.extractall(legacy_source, filter="data")
        home = self.workspace / "legacy-home"
        legacy_report = self.json_output(self.run(
            [self.host_python, legacy_source / "scripts/distribute_skill.py", "--output",
             self.workspace / "installed"], cwd=legacy_source, home=home))
        package = Path(legacy_report["package"])
        legacy_version = (package / "VERSION").read_text().strip()
        old_source_sha = digest(package / 'scripts/factory/script_drafts.py')
        if self.legacy_expected_version is None:
            self.run([self.host_python, package / "scripts/install.py", "--no-venv", "--no-deps"],
                     cwd=package, home=home)
            legacy_data = package / "data"
            env_path = package / ".env"
        else:
            # Current previous main has the real external-runtime bootstrap;
            # exercise that old code, rather than pretend it writes in Skill.
            self.bootstrap(package, home)
            legacy_data = home / 'data'
            env_path = home / '.env'
        env_path.write_text("ARK_API_KEY=<required>\nP4_KEEP=preserved\n", encoding="utf-8")
        media = legacy_data / "assets/J-P4/final.mp4"
        media.parent.mkdir(parents=True)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required for a decodable historical media fixture")
        self.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                  "color=c=blue:s=160x120:d=0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", media],
                 cwd=package, home=home)
        archived_source = legacy_data / 'assets/J-P4/shots/review/repair_sources/historical.mp4'
        archived_source.parent.mkdir(parents=True)
        shutil.copyfile(media, archived_source)
        opaque_proof = json.dumps({'fixture':'historical opaque proof; no generated business-action claim',
                                   'source_sha256':digest(archived_source)},sort_keys=True)
        character = legacy_data / "character_assets/demo/anchor.png"
        character.parent.mkdir(parents=True)
        character.write_bytes(b"historical-character-artifact")
        with database(legacy_data / "factory.db") as db:
            db.execute("INSERT INTO jobs(job_id,topic_id,title,state,cost_cents,final_path) "
                       "VALUES('J-P4','T-P4','Historical delivery task','COMPOSED',944,?)", (str(media),))
            db.execute("INSERT INTO shots(job_id,idx,state,video_path) VALUES('J-P4',1,'OK',?)", (str(media),))
            db.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) "
                       "VALUES('paid-before-update','J-P4',1,'seedance','generate',944,'{\"status\":\"completed\"}')")
            db.execute("INSERT INTO events(job_id,step,level,message) VALUES('J-P4','historical_native_source_archive','info',?)",(opaque_proof,))
            db.execute("INSERT INTO platform_publications(publication_id,job_id,platform,item_id,title,published_at,metadata_json) "
                       "VALUES('PUB-P4','J-P4','douyin','old-offline-item','Historical publication','2026-10-01T00:00:00Z','{}')")
            db.execute("INSERT INTO performance_snapshots(snapshot_id,job_id,platform,captured_at,views,likes,metadata_json) "
                       "VALUES('SNAP-P4','J-P4','douyin','2026-10-02T00:00:00Z',7,0,'{}')")
        media_hash, character_hash, env_bytes = digest(media), digest(character), env_path.read_bytes()
        self.update(staged, package.parent, home)
        self.bootstrap(package, home)
        data = home / "data"
        migrated_media = data / "assets/J-P4/final.mp4"
        migrated_character = data / "character_assets/demo/anchor.png"
        migrated_archive = data / 'assets/J-P4/shots/review/repair_sources/historical.mp4'
        def historical_context_preserved(connection):
            return (digest(migrated_archive)==media_hash
                    and connection.execute("SELECT message FROM events WHERE step='historical_native_source_archive'").fetchone()[0]==opaque_proof
                    and connection.execute("SELECT item_id FROM platform_publications WHERE publication_id='PUB-P4'").fetchone()[0]=='old-offline-item'
                    and connection.execute("SELECT views,likes FROM performance_snapshots WHERE snapshot_id='SNAP-P4'").fetchone()==(7,0))
        old_version_matches = (legacy_version == "3.2.0.1" if self.legacy_expected_version is None
                               else legacy_version == self.legacy_expected_version)
        new_source_sha = digest(package / 'scripts/factory/script_drafts.py')
        self.check('actual_source_code_replaced', old_source_sha != new_source_sha
                   and new_source_sha == digest(ROOT / 'scripts/factory/script_drafts.py'))
        self.check("genuine_legacy_package_migrated", old_version_matches
                   and (home / ".env").read_bytes() == env_bytes
                   and digest(migrated_media) == media_hash
                   and digest(migrated_character) == character_hash)
        with database(data / "factory.db") as db:
            row = db.execute("SELECT cost_cents,final_path FROM jobs WHERE job_id='J-P4'").fetchone()
            self.check("historical_cost_and_media_binding_preserved",
                       row == (944, str(migrated_media)) and db.execute("PRAGMA integrity_check").fetchone()[0] == 'ok')
            self.check('source_archive_proof_and_feedback_preserved', historical_context_preserved(db))
        with self.server(package, home) as base:
            self.check("historical_task_loaded_by_new_service",
                       any(j['job_id'] == 'J-P4' and j['final_exists'] for j in self.http(base, '/api/videos')['jobs']))
            with urlopen(base + '/api/videos/J-P4/file', timeout=10) as response:
                self.check("historical_media_served", hashlib.sha256(response.read()).hexdigest() == media_hash)
            self.review(base, 'J-P4')
        self.update(staged, package.parent, home)
        self.bootstrap(package, home)
        with database(data / 'factory.db') as db:
            self.check('external_runtime_survives_repeat_replacement',
                       db.execute("SELECT state,cost_cents FROM jobs WHERE job_id='J-P4'").fetchone()
                       == ('READY_TO_PUBLISH', 944))
            self.check('repeat_keeps_source_archive_and_feedback', historical_context_preserved(db))
            db.execute("INSERT INTO jobs(job_id,topic_id,title,state,final_path) "
                       "VALUES('J-P4-ROLLBACK','T-P4','Rollback task','COMPOSED',?)", (str(migrated_media),))
        metadata = (data / 'runtime.json').read_bytes()
        original_db_code = (package / 'scripts/factory/db.py').read_bytes()
        original_version = (package / 'VERSION').read_bytes()
        failure_marker = self.workspace / 'migration-failure-observed.txt'
        (package / 'VERSION').write_text('p4-injected-failed-upgrade', encoding='utf-8')
        with (package / 'scripts/factory/db.py').open('a', encoding='utf-8') as stream:
            stream.write("\n_original_conn = conn\ndef conn():\n"
                         "    db = _original_conn()\n"
                         "    db.execute(\"UPDATE jobs SET cost_cents=999999 WHERE job_id='J-P4'\")\n"
                         "    db.commit()\n    db.close()\n"
                         "    (DATA_DIR / 'character_assets/demo/anchor.png').write_bytes(b'corrupted')\n"
                         "    Path(os.environ['FACTORY_ENV_FILE']).write_text('P4_KEEP=corrupted')\n"
                         f"    Path({str(failure_marker)!r}).write_text('P4 injected migration failure')\n"
                         "    raise RuntimeError('P4 injected migration failure')\n")
        self.run([python_path(package / '.venv'), package / 'scripts/install.py', '--no-venv', '--no-deps'],
                 cwd=package, home=home, expected=1)
        self.check('injected_migration_failure_observed', failure_marker.is_file()
                   and failure_marker.read_text() == 'P4 injected migration failure'
                   and 'CalledProcessError' in self.last_error())
        with database(data / 'factory.db') as db:
            self.check('failed_upgrade_restores_state',
                       db.execute("SELECT cost_cents FROM jobs WHERE job_id='J-P4'").fetchone()[0] == 944
                       and db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                       and (home / '.env').read_bytes() == env_bytes
                       and digest(migrated_character) == character_hash
                       and digest(migrated_media) == media_hash
                       and (data / 'runtime.json').read_bytes() == metadata)
            self.check('rollback_keeps_source_archive_and_feedback', historical_context_preserved(db))
        (package / 'scripts/factory/db.py').write_bytes(original_db_code)
        (package / 'VERSION').write_bytes(original_version)
        self.bootstrap(package, home)
        with self.server(package, home) as base:
            self.review(base, 'J-P4-ROLLBACK')

        crash_guard = self.workspace / 'crash-guard'
        crash_guard.mkdir()
        (crash_guard / 'sitecustomize.py').write_text(_guard_source() + "\n"
            "from pathlib import Path\n_original_replace = os.replace\n"
            "def _crash_replace(source, destination):\n"
            "    result = _original_replace(source, destination)\n"
            "    if Path(destination).name == 'previous':\n        os._exit(91)\n"
            "    return result\nos.replace = _crash_replace\n", encoding='utf-8')
        crash_env = self.environment(home)
        crash_env['PYTHONPATH'] = str(crash_guard)
        self.update(staged, package.parent, home, expected=91, env=crash_env)
        self.check('hard_exit_keeps_previous_code', not package.exists()
                   and len(list(package.parent.glob('.skill-replacement-*/previous/SKILL.md'))) == 1)
        self.update(staged, package.parent, home)
        self.bootstrap(package, home)
        with database(data / 'factory.db') as db:
            self.check('interrupted_update_recovers_without_rebilling',
                       db.execute("SELECT state,cost_cents FROM jobs WHERE job_id='J-P4'").fetchone()
                       == ('READY_TO_PUBLISH', 944)
                       and db.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0] == 1)
            self.check('interrupted_replace_keeps_source_archive_and_feedback', historical_context_preserved(db))
        external = len(self.telemetry.read_text().splitlines()) if self.telemetry.exists() else 0
        self.check('no_guarded_external_requests', external == 0)
        return {"status": "passed", "legacy_ref": self.legacy_ref, "legacy_version": legacy_version,
                "old_script_drafts_sha256": old_source_sha, "candidate_script_drafts_sha256": new_source_sha,
                "skill_archive_sha256": digest(archive), "host_platform": sys.platform,
                "version": original_version.decode().strip(), "checks": self.checks,
                "commands": self.commands, "paid_provider_calls": 0, "external_requests": external,
                "scope": "shipped_cli_and_http_with_synthetic_historical_media",
                "limitations": ["one genuine historical version on this host OS",
                                "offline dependencies sourced from a prepared wheelhouse",
                                "scripted final review does not measure content quality",
                                "Python socket guard is not OS process-tree network isolation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--legacy-ref', default='59556cf')
    parser.add_argument('--legacy-expected-version', default=None,
                        help='Explicit previous-main version; default preserves genuine 3.2.0.1 migration proof')
    parser.add_argument('--work-root', type=Path, required=True)
    args = parser.parse_args()
    args.work_root.mkdir(parents=True, exist_ok=False)
    report_path = args.work_root / 'report.json'
    journey = None
    try:
        journey = DeliveryJourney(args.work_root, args.wheelhouse, args.legacy_ref, args.legacy_expected_version)
        report = journey.execute()
    except Exception as error:
        report = {"status": "failed", "error": f"{type(error).__name__}: {error}",
                  "checks": journey.checks if journey else [],
                  "commands": journey.commands if journey else []}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({"status": report['status'], "report": str(report_path)}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
