"""Local-only HTTP server for the AI Video Factory management console."""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib import request
from urllib.parse import parse_qs, urlparse
import webbrowser

from dotenv import load_dotenv
from runtime_lock import exclusive_runtime_lock
from runtime_paths import environment_file, runtime_home

SERVER_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(environment_file(SERVER_ROOT, os.environ))

from factory import SKILL_ROOT
from factory.db import conn
from factory.management_api import (
    CUSTOM_CHARACTER_IMAGE_MAX_BYTES, ManagementService,
)
from factory.runtime_profile import RuntimeProfile, resolve_runtime_profile
from factory.server_auth import AuthRejected, COOKIE_NAME, ServerSessionManager


UI_ROOT = Path(__file__).resolve().parent / "management_ui"
MEDIA_UPLOAD_MAX_BYTES = 512 * 1024 * 1024
MEDIA_UPLOAD_CHUNK_BYTES = 1024 * 1024


def configure_console_runtime(
    *, root: Path = SKILL_ROOT, environ: dict[str, str] | None = None
) -> tuple[RuntimeProfile, ServerSessionManager | None]:
    values = dict(os.environ if environ is None else environ)
    profile = resolve_runtime_profile(root=root, environ=values)
    if profile.mode == "desktop":
        return profile, None
    return profile, ServerSessionManager(
        session_secret=str(values.get("FACTORY_SESSION_SECRET") or ""),
        password_hash=str(values.get("FACTORY_ADMIN_PASSWORD_HASH") or ""),
        public_base_url=str(profile.public_base_url),
        session_ttl_sec=int(values.get("FACTORY_SESSION_TTL_SEC") or 12 * 60 * 60),
    )


def existing_console_url(
    host: str,
    port: int,
    *,
    start_page: str = "overview",
) -> str | None:
    """Return the existing local console URL only when its API shape matches."""
    base_url = f"http://{host}:{port}"
    try:
        with request.urlopen(f"{base_url}/api/overview", timeout=0.75) as response:
            if response.status != HTTPStatus.OK:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not all(key in payload for key in ("scripts", "videos", "publishing")):
        return None
    page = start_page if start_page in {
        "overview", "projects", "scripts", "videos", "publishing", "characters", "config"
    } else "overview"
    return f"{base_url}/#{page}"


def run_queue_supervisor(
    service: ManagementService,
    service_lock: threading.RLock,
    stop_event: threading.Event,
    *,
    interval_sec: float = 2.0,
) -> None:
    """Continuously reconcile persisted queued work with local workers."""
    while not stop_event.wait(interval_sec):
        try:
            with service_lock:
                service.dispatch_production_queue()
        except Exception as exc:
            # Keep the console available; persist the cause before retrying.
            with service_lock:
                service.record_queue_supervisor_error(str(exc))
            continue


def run_source_feed_scheduler(
    service: ManagementService,
    service_lock: threading.RLock,
    stop_event: threading.Event,
    *,
    interval_sec: float = 300.0,
) -> None:
    """Collect enabled public feeds periodically, never during server startup."""
    while not stop_event.wait(interval_sec):
        try:
            with service_lock:
                service.collect_enabled_source_feeds()
        except Exception:
            # Individual feed failures are persisted by the scheduler service.
            # A process-level error must not take down the management console.
            continue


def run_provider_reconcile_sweeper(
    service: ManagementService,
    service_lock: threading.RLock,
    stop_event: threading.Event,
    *,
    interval_sec: float = 30.0,
    limit: int = 10,
) -> None:
    """Periodically inspect known remote tasks; never submit paid work."""
    while not stop_event.wait(max(0.0, float(interval_sec))):
        try:
            with service_lock:
                service.reconcile_provider_tasks(limit=limit)
        except Exception:
            # A sweep is advisory. Per-task errors are retained by the service,
            # and a process-level failure must not take down the local console.
            continue


def validate_local_bind_host(host: str) -> str:
    """Keep this credential-bearing console reachable from this machine only."""
    value = str(host or "").strip().lower()
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("管理台只允许绑定本机回环地址")
    return str(host).strip()


def _safe_upload_name(filename: str) -> str:
    leaf = Path(str(filename or "").replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(leaf).stem).strip("._")
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(leaf).suffix.lower())
    return f"{stem or 'media'}{suffix}"


def parse_byte_range(header: str, size: int) -> tuple[int, int]:
    """Parse one RFC 7233 byte range for local video streaming."""
    if size <= 0 or not str(header).startswith("bytes="):
        raise ValueError("无效的视频分段请求")
    value = str(header)[6:].strip()
    if "," in value or "-" not in value:
        raise ValueError("一次仅支持一个视频分段")
    start_text, end_text = value.split("-", 1)
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("无效的视频分段请求")
        return max(0, size - length), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("视频分段超出文件范围")
    return start, min(end, size - 1)


class ConsoleHandler(BaseHTTPRequestHandler):
    service: ManagementService
    queue_supervisor: threading.Thread | None = None
    service_lock = threading.RLock()
    runtime_mode = "desktop"
    auth_manager: ServerSessionManager | None = None

    def log_message(self, format: str, *args) -> None:
        return

    def _json(
        self, payload: dict, status: int = 200, *, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorize_server_request(self) -> bool:
        if self.runtime_mode != "server":
            self._authorized_session = None
            return True
        if self.auth_manager is None:
            self._json({"error": "服务端认证未初始化"}, 503)
            return False
        try:
            self._authorized_session = self.auth_manager.authorize(
                self.command,
                urlparse(self.path).path,
                dict(self.headers.items()),
            )
            return True
        except AuthRejected as exc:
            request_path = urlparse(self.path).path
            if exc.status == 401 and self.command == "GET" and not request_path.startswith("/api/"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/login")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return False
            self.close_connection = True
            self._json(
                {"error": str(exc)}, exc.status,
                headers={"Connection": "close"},
            )
            return False

    def _login_html(self) -> None:
        body = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>AI Video Factory Login</title></head><body>
<main><h1>AI Video Factory</h1><form id='login'><label>管理员密码
<input id='password' type='password' autocomplete='current-password' required></label>
<button>登录</button><p id='error' role='alert'></p></form></main>
<script>document.querySelector('#login').onsubmit=async(e)=>{e.preventDefault();
const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({password:document.querySelector('#password').value})});
const p=await r.json();if(r.ok){sessionStorage.setItem('avf_csrf',p.csrf_token);location.href='/';}
else document.querySelector('#error').textContent=p.error||'登录失败';};</script></body></html>""".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, title: str, message: str, status: int = 200) -> None:
        body = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>{escape(title)}</title><style>body{{font-family:system-ui;"
            "display:grid;place-items:center;min-height:100vh;margin:0;background:#f5f7f5;"
            "color:#22352b}.card{max-width:520px;padding:36px;border:1px solid #d9e1dc;"
            "border-radius:16px;background:white;box-shadow:0 18px 60px #20362b18}"
            "h1{font-size:24px}p{line-height:1.7;color:#627069}</style></head>"
            f"<body><main class='card'><h1>{escape(title)}</h1><p>{escape(message)}</p>"
            "<p>现在可以关闭此页面，返回 AI Video Factory 管理台。</p></main></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    def _post_request_error(
        self, *, binary_kind: str | None = None
    ) -> tuple[int, str] | None:
        origin = self.headers.get("Origin")
        if origin and self.runtime_mode != "server":
            parsed_origin = urlparse(origin)
            host = self.headers.get("Host", "")
            parsed_host = urlparse(f"//{host}")
            loopback = {"127.0.0.1", "localhost", "::1"}
            if (
                parsed_origin.scheme not in {"http", "https"}
                or parsed_origin.hostname not in loopback
                or parsed_host.hostname not in loopback
                or parsed_origin.netloc.casefold() != host.casefold()
            ):
                return HTTPStatus.FORBIDDEN, "拒绝跨来源 POST 请求"
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        media_type = media_type.strip().lower()
        if binary_kind == "media":
            accepted = (
                media_type == "application/octet-stream"
                or re.fullmatch(r"video/[^/\s]+", media_type)
                is not None
            )
        elif binary_kind == "character_image":
            accepted = (
                media_type == "application/octet-stream"
                or re.fullmatch(r"image/[^/\s]+", media_type) is not None
            )
        else:
            accepted = media_type == "application/json"
        if not accepted:
            return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "不支持的 Content-Type"
        return None

    def _media_upload(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        filename = _safe_upload_name(query.get("filename", [""])[0])
        title = query.get("title", [""])[0]
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("上传文件大小无效") from exc
        if length <= 0:
            raise ValueError("请选择需要上传的音视频文件")
        if length > MEDIA_UPLOAD_MAX_BYTES:
            raise OverflowError("上传文件过大，单个文件不能超过 512 MB")
        upload_root = self.service.data_dir / "management_uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="source-", dir=upload_root) as tmp:
            target = Path(tmp) / filename
            remaining = length
            with target.open("xb") as stream:
                while remaining:
                    chunk = self.rfile.read(min(MEDIA_UPLOAD_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("上传文件不完整，请重试")
                    stream.write(chunk)
                    remaining -= len(chunk)
            self._json(self.service.add_media_source(target, title=title))

    def _character_image_upload(self, character_id: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        filename = _safe_upload_name(query.get("filename", [""])[0])
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("角色图片大小无效") from exc
        if length <= 0:
            raise ValueError("请选择需要上传的角色图片")
        if length > CUSTOM_CHARACTER_IMAGE_MAX_BYTES:
            raise OverflowError("单张角色图片不能超过 20 MB")
        upload_root = self.service.data_dir / "management_uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="character-", dir=upload_root) as tmp:
            target = Path(tmp) / filename
            remaining = length
            with target.open("xb") as stream:
                while remaining:
                    chunk = self.rfile.read(min(MEDIA_UPLOAD_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("角色图片上传不完整，请重试")
                    stream.write(chunk)
                    remaining -= len(chunk)
            self._json(self.service.add_custom_character_image(
                character_id, target, original_name=filename,
            ))

    def do_GET(self) -> None:
        if not self._authorize_server_request():
            return
        with self.service_lock:
            return self._do_GET()

    def _serve_binary_file(self, path: Path, *, cache_control: str) -> None:
        size = path.stat().st_size
        byte_range = self.headers.get("Range")
        start, end = (0, size - 1)
        if byte_range:
            start, end = parse_byte_range(byte_range, size)
        length = end - start + 1
        media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(
            HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK
        )
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache_control)
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/healthz", "/readyz"}:
            return self._json({"ok": True, "runtime": self.runtime_mode})
        if parsed.path == "/login":
            return self._login_html()
        if parsed.path == "/api/auth/session":
            return self._json({
                "authenticated": True,
                "csrf_token": self._authorized_session.csrf_token,
            })
        if parsed.path == "/api/integrations/douyin/oauth/callback":
            query = parse_qs(parsed.query)
            if query.get("error"):
                return self._html(
                    "抖音绑定未完成",
                    query.get("error_description", query["error"])[0],
                    400,
                )
            try:
                status = self.service.complete_douyin_oauth(
                    code=query.get("code", [""])[0],
                    state=query.get("state", [""])[0],
                )
                return self._html(
                    "抖音账号绑定成功",
                    f"已绑定账号 {status.get('open_id_masked', '')}。",
                )
            except Exception as exc:
                return self._html("抖音绑定失败", str(exc), 400)
        if (parsed.path.startswith("/api/sources/")
                and parsed.path.endswith("/media")):
            try:
                source_id = parsed.path.split("/")[3]
                return self._serve_binary_file(
                    self.service.source_media_file(source_id),
                    cache_control="no-store",
                )
            except (IndexError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith("/api/sources/")
                and "/frames/" in parsed.path):
            try:
                parts = parsed.path.split("/")
                return self._serve_binary_file(
                    self.service.source_frame_file(parts[3], int(parts[5])),
                    cache_control="private, max-age=3600",
                )
            except (IndexError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith("/api/characters/")
                and "/images/" in parsed.path):
            try:
                parts = parsed.path.split("/")
                path = self.service.character_image_file(parts[3], parts[5])
                body = path.read_bytes()
                media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if parsed.path.startswith("/api/characters/") and parsed.path.endswith("/preview"):
            try:
                character_id = parsed.path.split("/")[3]
                path = self.service.character_preview_file(character_id)
                body = path.read_bytes()
                media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith("/api/videos/")
                and "/scenes/" in parsed.path
                and parsed.path.endswith("/preview")):
            try:
                parts = parsed.path.split("/")
                path = self.service.scene_anchor_preview_file(
                    parts[3], parts[5]
                )
                body = path.read_bytes()
                media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
                return
            except (IndexError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith("/api/videos/")
                and "/identities/" in parsed.path
                and parsed.path.endswith("/preview")):
            try:
                parts = parsed.path.split("/")
                path = self.service.character_identity_preview_file(
                    parts[3], parts[5]
                )
                body = path.read_bytes()
                media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
                return
            except (IndexError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith("/api/videos/")
                and parsed.path.endswith("/repair-source")
                and "/masters/" in parsed.path):
            try:
                parts = parsed.path.split("/")
                path = self.service.master_repair_source_file(
                    parts[3], int(parts[5])
                )
                size = path.stat().st_size
                byte_range = self.headers.get("Range")
                start, end = (0, size - 1)
                if byte_range:
                    start, end = parse_byte_range(byte_range, size)
                length = end - start + 1
                self.send_response(
                    HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK
                )
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if byte_range:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{size}"
                    )
                self.end_headers()
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except (IndexError, ValueError) as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith('/api/videos/') and parsed.path.endswith('/visual-repair') and '/masters/' in parsed.path):
            try:
                parts = parsed.path.split('/')
                source = parse_qs(parsed.query).get('source', ['candidate'])[0]
                return self._json(self.service.visual_repair_plan(parts[3], int(parts[5]), source))
            except (IndexError, ValueError) as exc:
                return self._json({'error': str(exc)}, 400)
        if (parsed.path.startswith('/api/videos/') and parsed.path.endswith('/content-review') and '/masters/' in parsed.path):
            try:
                parts = parsed.path.split('/')
                source = parse_qs(parsed.query).get('source', ['candidate'])[0]
                return self._json(self.service.content_review_detail(parts[3], int(parts[5]), source))
            except (IndexError, ValueError) as exc:
                return self._json({'error': str(exc)}, 400)
        if (parsed.path.startswith("/api/videos/")
                and (parsed.path.endswith("/candidate") or parsed.path.endswith('/content-preview'))
                and "/masters/" in parsed.path):
            try:
                parts = parsed.path.split("/")
                path = (self.service.content_review_source_file(parts[3], int(parts[5]),
                    parse_qs(parsed.query).get('source', ['candidate'])[0]) if parsed.path.endswith('/content-preview')
                    else self.service.master_candidate_file(parts[3], int(parts[5])))
                size = path.stat().st_size
                byte_range = self.headers.get("Range")
                start, end = (0, size - 1)
                if byte_range:
                    start, end = parse_byte_range(byte_range, size)
                length = end - start + 1
                self.send_response(
                    HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK
                )
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if byte_range:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{size}"
                    )
                self.end_headers()
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/cover"):
            try:
                job_id = parsed.path.split("/")[3]
                path = self.service.video_cover_file(job_id)
                body = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
                return
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if parsed.path.startswith("/api/videos/") and parsed.path.endswith("/file"):
            try:
                job_id = parsed.path.split("/")[3]
                path = self.service.video_file(job_id)
                download = parse_qs(parsed.query).get("download", ["0"])[0] == "1"
                size = path.stat().st_size
                byte_range = self.headers.get("Range")
                start, end = (0, size - 1)
                if byte_range:
                    start, end = parse_byte_range(byte_range, size)
                length = end - start + 1
                self.send_response(
                    HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK
                )
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if byte_range:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                if download:
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{job_id}.mp4"'
                    )
                self.end_headers()
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        endpoints = {
            "/api/overview": self.service.overview,
            "/api/todos": self.service.todo_center,
            "/api/scripts": self.service.scripts,
            "/api/videos": self.service.videos,
            "/api/publishing": self.service.publishing,
            "/api/config": self.service.config_status,
            "/api/providers": self.service.provider_statuses,
            "/api/projects": self.service.content_projects,
            "/api/integrations/douyin": self.service.douyin_account_status,
            "/api/production/health": lambda: self.service.production_health(
                supervisor_alive=bool(
                    self.queue_supervisor and self.queue_supervisor.is_alive()
                )
            ),
            "/api/source-feeds": self.service.source_feeds,
            "/api/voices": self.service.voice_catalog,
        }
        if parsed.path == "/api/events":
            limit = parse_qs(parsed.query).get("limit", ["40"])[0]
            return self._json(self.service.recent_events(int(limit)))
        if parsed.path == "/api/characters":
            query = parse_qs(parsed.query)
            return self._json(self.service.characters(
                keyword=query.get("keyword", [""])[0],
                identity_type=query.get("identity_type", [""])[0],
                status=query.get("status", [""])[0],
            ))
        if parsed.path == "/api/source-candidates":
            query = parse_qs(parsed.query)
            status = query.get("status", ["candidate"])[0]
            return self._json(self.service.source_candidates(status=status or None))
        if parsed.path == "/api/production/consistency":
            limit = parse_qs(parsed.query).get("limit", ["100"])[0]
            return self._json(self.service.production_consistency(limit=int(limit)))
        if (parsed.path.startswith("/api/videos/")
                and parsed.path.endswith("/production-graph")):
            job_id = parsed.path.split("/")[3]
            try:
                return self._json(self.service.production_graph_detail(job_id))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if (parsed.path.startswith("/api/videos/")
                and parsed.path.endswith("/doctor")):
            job_id = parsed.path.split("/")[3]
            try:
                return self._json(self.service.diagnose_video_task(job_id))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 404)
        if parsed.path in endpoints:
            try:
                return self._json(endpoints[parsed.path]())
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)
        self._static(parsed.path)

    def do_POST(self) -> None:
        if not self._authorize_server_request():
            return
        with self.service_lock:
            return self._do_POST()

    def _do_POST(self) -> None:
        try:
            request_path = urlparse(self.path).path
            character_upload = re.fullmatch(
                r"/api/characters/([^/]+)/images", request_path
            )
            binary_kind = (
                "media" if request_path == "/api/sources/media"
                else "character_image" if character_upload
                else None
            )
            rejection = self._post_request_error(binary_kind=binary_kind)
            if rejection is not None:
                status, message = rejection
                return self._json({"error": message}, status)
            if request_path == "/api/sources/media":
                return self._media_upload()
            if (request_path.startswith("/api/videos/")
                    and request_path.endswith("/compile")):
                job_id = request_path.split("/")[3]
                return self._json(self.service.compile_video_job(job_id))
            if character_upload:
                return self._character_image_upload(character_upload.group(1))
            payload = self._body()
            if request_path == "/api/auth/login" and self.runtime_mode == "server":
                issued = self.auth_manager.login(str(payload.get("password") or ""))
                return self._json(
                    {"authenticated": True, "csrf_token": issued.csrf_token},
                    headers={
                        "Set-Cookie": (
                            f"{COOKIE_NAME}={issued.token}; Path=/; HttpOnly; Secure; "
                            "SameSite=Lax"
                        )
                    },
                )
            if request_path == "/api/auth/logout" and self.runtime_mode == "server":
                return self._json(
                    {"authenticated": False},
                    headers={
                        "Set-Cookie": (
                            f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; "
                            "SameSite=Lax"
                        )
                    },
                )
            if request_path == "/api/integrations/douyin/oauth/start":
                result = self.service.start_douyin_oauth()
            elif request_path == "/api/quality-canary/jobs":
                result = self.service.create_quality_canary(payload)
            elif request_path == "/api/providers/seedance/clear-block":
                result = self.service.clear_seedance_provider_block()
            elif request_path == "/api/providers/seedance/transport-preflight":
                result = self.service.run_transport_preflight()
            elif request_path == "/api/integrations/douyin/disconnect":
                result = self.service.disconnect_douyin()
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/publish/douyin")):
                job_id = request_path.split("/")[3]
                result = self.service.publish_to_douyin(job_id, payload)
            elif self.path == "/api/config":
                result = self.service.update_config(payload.get("changes") or {})
            elif self.path == "/api/sources":
                result = self.service.add_source(
                    source_type=str(payload.get("source_type") or "manual"),
                    title=str(payload.get("title") or ""),
                    content=str(payload.get("content") or ""),
                )
            elif request_path == "/api/source-feeds":
                result = self.service.create_source_feed(payload)
            elif request_path == "/api/source-candidates/batch-delete":
                result = self.service.batch_delete_source_candidates(
                    payload.get("candidate_ids") or []
                )
            elif request_path == "/api/scripts/batch-reject":
                result = self.service.batch_reject_scripts(
                    payload.get("draft_ids") or []
                )
            elif request_path == "/api/videos/batch-preflight":
                result = self.service.batch_production_preflight(
                    payload.get("job_ids") or []
                )
            elif (request_path.startswith("/api/source-feeds/")
                  and request_path.endswith("/collect")):
                result = self.service.collect_source_feed(request_path.split("/")[3])
            elif (request_path.startswith("/api/source-feeds/")
                  and request_path.endswith("/settings")):
                result = self.service.update_source_feed(
                    request_path.split("/")[3], payload
                )
            elif (request_path.startswith("/api/source-candidates/")
                  and request_path.endswith("/promote")):
                result = self.service.promote_source_candidate(request_path.split("/")[3])
            elif (request_path.startswith("/api/source-candidates/")
                  and request_path.endswith("/reject")):
                result = self.service.reject_source_candidate(
                    request_path.split("/")[3], payload
                )
            elif (request_path.startswith("/api/sources/")
                  and request_path.endswith("/analyze")):
                if payload.get("confirmed") is not True:
                    raise ValueError("请确认后再调用 AI 解析视频内容")
                result = self.service.analyze_media_source(
                    request_path.split("/")[3], force=True
                )
            elif (request_path.startswith("/api/sources/")
                  and request_path.endswith("/performance-review")):
                result = self.service.review_source_performance(
                    request_path.split("/")[3], payload
                )
            elif self.path.endswith("/generate-brief") and self.path.startswith("/api/sources/"):
                if payload.get("confirmed") is not True:
                    raise ValueError("请确认后再调用 AI 生成选题简报")
                source_id = self.path.split("/")[3]
                result = self.service.generate_brief_from_source(
                    source_id, risk_confirmed=payload.get("risk_confirmed") is True
                )
            elif self.path.endswith("/generate-scripts") and self.path.startswith("/api/briefs/"):
                if payload.get("confirmed") is not True:
                    raise ValueError("请确认后再调用 AI 生成候选剧本")
                brief_id = self.path.split("/")[3]
                generation_preferences = {
                    key: payload.get(key)
                    for key in (
                        "suggested_duration_sec",
                        "suggested_shot_count",
                        "suggested_character_count",
                    )
                    if payload.get(key) not in (None, "")
                }
                result = self.service.confirm_brief_and_generate_scripts(
                    brief_id,
                    angle_index=int(payload.get("angle_index", -1)),
                    generation_preferences=generation_preferences,
                )
            elif request_path.endswith("/edit") and request_path.startswith("/api/briefs/"):
                brief_id = request_path.split("/")[3]
                result = self.service.update_brief(
                    brief_id, payload.get("brief") or {},
                    change_note=str(payload.get("change_note") or ""),
                )
            elif request_path.endswith("/edit") and request_path.startswith("/api/scripts/"):
                draft_id = request_path.split("/")[3]
                result = self.service.update_script_draft(
                    draft_id, payload.get("script") or {},
                    change_note=str(payload.get("change_note") or ""),
                    auto_adjust_durations=(
                        payload.get("auto_adjust_durations") is True
                    ),
                )
            elif self.path.endswith("/approve") and self.path.startswith("/api/scripts/"):
                draft_id = self.path.split("/")[3]
                result = self.service.approve_script(draft_id)
            elif self.path == "/api/characters":
                result = self.service.bind_character(
                    character_id=str(payload.get("character_id") or ""),
                    display_name=str(payload.get("display_name") or ""),
                    asset_id=str(payload.get("asset_id") or ""),
                    identity_type=str(
                        payload.get("identity_type") or "real_person"
                    ),
                    background_description=str(
                        payload.get("background_description") or ""
                    ),
                    gender=str(payload.get("gender") or ""),
                )
            elif self.path.endswith("/background") and self.path.startswith("/api/characters/"):
                character_id = self.path.split("/")[3]
                result = self.service.update_character_background(
                    character_id,
                    str(payload.get("background_description") or ""),
                )
            elif self.path.endswith("/status") and self.path.startswith("/api/characters/"):
                character_id = self.path.split("/")[3]
                result = self.service.set_character_status(
                    character_id, str(payload.get("status") or "")
                )
            elif self.path.endswith("/refresh") and self.path.startswith("/api/characters/"):
                character_id = self.path.split("/")[3]
                result = self.service.refresh_character(character_id)
            elif (request_path.startswith("/api/characters/")
                  and request_path.endswith("/voice/auto")):
                character_id = request_path.split("/")[3]
                result = self.service.auto_match_character_voice(character_id)
            elif (request_path.startswith("/api/characters/")
                  and request_path.endswith("/voice")):
                character_id = request_path.split("/")[3]
                result = self.service.set_character_voice(
                    character_id,
                    voice_type=str(payload.get("voice_type") or ""),
                    voice_name=str(payload.get("voice_name") or ""),
                    resource_id=str(
                        payload.get("resource_id") or "seed-tts-2.0"
                    ),
                    preview_url=str(payload.get("preview_url") or ""),
                )
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/anchors/approve")):
                job_id = request_path.split("/")[3]
                result = self.service.approve_continuity_anchors(
                    job_id, note=str(payload.get("note") or "")
                )
            elif self.path.endswith("/produce") and self.path.startswith("/api/videos/"):
                job_id = self.path.split("/")[3]
                result = self.service.start_video_job(
                    job_id,
                    confirmed=payload.get("confirmed") is True,
                    approved_budget_cents=payload.get("approved_budget_cents"),
                    high_risk_confirmed=(
                        payload.get("high_risk_confirmed") is True
                    ),
                )
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/budget-confirmation")):
                job_id = request_path.split("/")[3]
                result = self.service.confirm_video_budget(
                    job_id,
                    confirmed=payload.get("confirmed") is True,
                    approved_budget_cents=payload.get(
                        "approved_budget_cents"
                    ),
                    max_auto_retries=payload.get("max_auto_retries", 0),
                )
            elif (request_path.endswith("/upgrade-director")
                  and request_path.startswith("/api/videos/")):
                job_id = request_path.split("/")[3]
                result = self.service.upgrade_director_plan(job_id)
            elif (request_path.endswith("/rebuild-director")
                  and request_path.startswith("/api/videos/")):
                job_id = request_path.split("/")[3]
                result = self.service.rebuild_failed_director_plan(job_id)
            elif (request_path.endswith("/execution-contract/repair")
                  and request_path.startswith("/api/videos/")):
                job_id = request_path.split("/")[3]
                result = self.service.repair_execution_contract(job_id)
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/doctor/repair")):
                job_id = request_path.split("/")[3]
                requested_actions = payload.get("action_ids")
                result = self.service.repair_video_task(
                    job_id,
                    action_ids=(
                        [str(item) for item in requested_actions]
                        if isinstance(requested_actions, list) else None
                    ),
                )
            elif request_path.startswith("/api/videos/") and request_path.endswith("/retry-network-failures"):
                result = self.service.retry_network_failures(
                    request_path.split("/")[3], confirmed=payload.get("confirmed") is True,
                )
            elif self.path.endswith("/resume") and self.path.startswith("/api/videos/"):
                job_id = self.path.split("/")[3]
                result = self.service.resume_failed_video(job_id)
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/provider-task/reconcile")):
                job_id = request_path.split("/")[3]
                result = self.service.reconcile_provider_task(
                    job_id, str(payload.get("task_id") or "")
                )
            elif request_path == "/api/production/reconcile":
                result = self.service.reconcile_provider_tasks(
                    limit=int(payload.get("limit") or 10)
                )
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/regenerate")
                  and "/masters/" in request_path):
                parts = request_path.split("/")
                job_id = parts[3]
                master_idx = int(parts[5])
                result = self.service.prepare_master_regeneration(
                    job_id,
                    master_idx,
                    reason=str(payload.get("reason") or ""),
                )
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/lip-sync-repair")
                  and "/masters/" in request_path):
                parts = request_path.split("/")
                job_id = parts[3]
                master_idx = int(parts[5])
                result = self.service.prepare_master_lip_sync_repair(
                    job_id,
                    master_idx,
                    start_sec=float(payload.get("repair_start_sec")),
                    end_sec=float(payload.get("repair_end_sec")),
                    reason=str(payload.get("reason") or ""),
                )
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/extend")
                  and "/masters/" in request_path):
                parts = request_path.split("/")
                job_id = parts[3]
                master_idx = int(parts[5])
                result = self.service.prepare_master_extension(
                    job_id,
                    master_idx,
                    extension_sec=float(payload.get("extension_sec")),
                    direction=str(payload.get("direction") or ""),
                )
            elif (request_path.startswith('/api/videos/') and request_path.endswith('/align-local-timeline') and '/masters/' in request_path):
                parts = request_path.split('/')
                result = self.service.align_cached_visual_timeline(parts[3], int(parts[5]), payload)
            elif (request_path.startswith('/api/videos/') and request_path.endswith('/local-recut') and '/masters/' in request_path):
                parts = request_path.split('/')
                result = self.service.apply_visual_recut(parts[3], int(parts[5]), payload)
            elif (request_path.startswith('/api/videos/') and request_path.endswith('/content-review') and '/masters/' in request_path):
                parts = request_path.split('/')
                result = self.service.record_content_review(parts[3], int(parts[5]), payload)
            elif request_path.startswith('/api/videos/') and request_path.endswith('/resume-visual-content'):
                result = self.service.resume_visual_content(request_path.split('/')[3])
            elif (request_path.startswith("/api/videos/")
                  and request_path.endswith("/candidate-review")
                  and "/masters/" in request_path):
                parts = request_path.split("/")
                result = self.service.review_master_candidate(
                    parts[3], int(parts[5]),
                    decision=str(payload.get("decision") or ""),
                )
            elif self.path.endswith("/configure") and self.path.startswith("/api/videos/"):
                job_id = self.path.split("/")[3]
                result = self.service.configure_video(job_id, payload)
            elif self.path.endswith("/review") and self.path.startswith("/api/videos/"):
                job_id = self.path.split("/")[3]
                first_pass = payload.get("first_pass")
                result = self.service.review_video(
                    job_id,
                    decision=str(payload.get("decision") or ""),
                    manual_minutes=float(payload.get("manual_minutes") or 0),
                    notes=str(payload.get("notes") or ""),
                    first_pass=(first_pass if isinstance(first_pass, bool) else None),
                    checks=(payload.get("checks") if isinstance(payload.get("checks"), dict) else None),
                )
            elif self.path.endswith("/submit-publish") and self.path.startswith("/api/videos/"):
                job_id = self.path.split("/")[3]
                result = self.service.submit_for_publish(job_id)
            elif self.path == "/api/publications":
                result = self.service.register_publication(payload)
            elif self.path == "/api/publications/batch":
                result = self.service.register_publications(payload)
            elif (self.path.startswith("/api/publications/")
                  and self.path.endswith("/performance")):
                publication_id = self.path.split("/")[3]
                result = self.service.record_performance_snapshot(
                    publication_id, payload
                )
            else:
                return self._json({"error": "接口不存在"}, 404)
            return self._json(result)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except OverflowError as exc:
            return self._json({"error": str(exc)}, 413)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        if not self._authorize_server_request():
            return
        with self.service_lock:
            return self._do_DELETE()

    def _do_DELETE(self) -> None:
        try:
            if self.path.startswith("/api/source-feeds/"):
                result = self.service.delete_source_feed(self.path.split("/")[3])
            elif self.path.startswith("/api/source-candidates/"):
                result = self.service.delete_source_candidate(self.path.split("/")[3])
            elif self.path.startswith("/api/sources/"):
                result = self.service.delete_source(self.path.split("/")[3])
            elif self.path.startswith("/api/briefs/"):
                result = self.service.delete_brief(self.path.split("/")[3])
            elif self.path.startswith("/api/scripts/"):
                result = self.service.delete_script(self.path.split("/")[3])
            elif self.path.startswith("/api/videos/"):
                result = self.service.delete_video(self.path.split("/")[3])
            elif self.path.startswith("/api/characters/"):
                result = self.service.delete_character(self.path.split("/")[3])
            else:
                return self._json({"error": "接口不存在"}, 404)
            return self._json(result)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        target = (UI_ROOT / relative).resolve()
        if UI_ROOT.resolve() not in target.parents and target != UI_ROOT.resolve():
            return self._json({"error": "not found"}, 404)
        if not target.is_file():
            target = UI_ROOT / "index.html"
        body = target.read_bytes()
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type + ("; charset=utf-8" if media_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        # This local console is edited in place.  Never let a long-lived tab
        # keep stale filtering or workflow JavaScript after a server update.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def serve(
    host: str | None = None,
    port: int = 8765,
    *,
    open_browser: bool = True,
    start_page: str = "overview",
) -> None:
    with exclusive_runtime_lock(
        runtime_home(SKILL_ROOT, os.environ),
        purpose="启动管理服务",
    ):
        _serve_locked(
            host,
            port,
            open_browser=open_browser,
            start_page=start_page,
        )


def _serve_locked(
    host: str | None = None,
    port: int = 8765,
    *,
    open_browser: bool = True,
    start_page: str = "overview",
) -> None:
    profile, auth_manager = configure_console_runtime()
    requested_host = str(host or profile.bind_host)
    if profile.mode == "desktop":
        requested_host = validate_local_bind_host(requested_host)
    elif requested_host != profile.bind_host:
        raise ValueError(
            "server 模式监听地址必须由 FACTORY_BIND_HOST 统一配置"
        )
    host = requested_host
    ConsoleHandler.runtime_mode = profile.mode
    ConsoleHandler.auth_manager = auth_manager
    database = conn()
    ConsoleHandler.service = ManagementService(
        database,
        SKILL_ROOT,
        worker_mode=profile.worker_mode,
        data_dir=profile.data_dir,
        env_path=environment_file(SKILL_ROOT, os.environ),
        transport_preflight_enabled=True,
    )
    stop_event = threading.Event()
    supervisor = None
    ConsoleHandler.queue_supervisor = supervisor
    if profile.worker_mode == "child_process":
        supervisor = threading.Thread(
            target=run_queue_supervisor,
            args=(ConsoleHandler.service, ConsoleHandler.service_lock, stop_event),
            name="video-queue-supervisor",
            daemon=True,
        )
        ConsoleHandler.queue_supervisor = supervisor
        supervisor.start()
    source_scheduler = threading.Thread(
        target=run_source_feed_scheduler,
        args=(ConsoleHandler.service, ConsoleHandler.service_lock, stop_event),
        name="public-source-feed-scheduler",
        daemon=True,
    )
    source_scheduler.start()
    reconcile_sweeper = None
    if str(os.getenv("PROVIDER_RECONCILE_ENABLED", "true")).strip().casefold() \
            not in {"0", "false", "no", "off"}:
        try:
            reconcile_interval = max(
                1.0, float(os.getenv("PROVIDER_RECONCILE_INTERVAL_SEC", "30"))
            )
        except (TypeError, ValueError):
            reconcile_interval = 30.0
        try:
            reconcile_limit = max(
                1, min(50, int(os.getenv("PROVIDER_RECONCILE_BATCH_SIZE", "10")))
            )
        except (TypeError, ValueError):
            reconcile_limit = 10
        reconcile_sweeper = threading.Thread(
            target=run_provider_reconcile_sweeper,
            args=(ConsoleHandler.service, ConsoleHandler.service_lock, stop_event),
            kwargs={
                "interval_sec": reconcile_interval,
                "limit": reconcile_limit,
            },
            name="provider-reconcile-sweeper",
            daemon=True,
        )
        reconcile_sweeper.start()
    server = ThreadingHTTPServer((host, port), ConsoleHandler)
    start_page = start_page if start_page in {
        "overview", "projects", "scripts", "videos", "publishing", "characters", "config"
    } else "overview"
    url = (
        f"{profile.public_base_url}/#{start_page}"
        if profile.mode == "server"
        else f"http://{host}:{port}/#{start_page}"
    )
    print(f"AI Video Factory 管理台已启动：{url}")
    if open_browser and profile.mode == "desktop":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if supervisor is not None:
            supervisor.join(timeout=3)
        source_scheduler.join(timeout=3)
        if reconcile_sweeper is not None:
            reconcile_sweeper.join(timeout=3)
        ConsoleHandler.queue_supervisor = None
        server.server_close()
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--start-page",
        choices=("overview", "projects", "scripts", "videos", "publishing", "characters", "config"),
        default="overview",
    )
    args = parser.parse_args()
    profile, _ = configure_console_runtime()
    host = args.host or profile.bind_host
    if profile.mode == "desktop":
        existing = existing_console_url(
            host,
            args.port,
            start_page=args.start_page,
        )
        if existing:
            print(f"AI Video Factory 管理台已在运行：{existing}")
            if not args.no_browser:
                webbrowser.open(existing)
            return 0
    serve(
        host,
        args.port,
        open_browser=not args.no_browser,
        start_page=args.start_page,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
