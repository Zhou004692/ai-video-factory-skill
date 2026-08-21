from __future__ import annotations

from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))


class ServerRuntimeSecurityTests(unittest.TestCase):
    def server_environment(self, data_dir: Path, **overrides: str) -> dict[str, str]:
        values = {
            "FACTORY_RUNTIME": "server",
            "FACTORY_PUBLIC_BASE_URL": "https://video.example.com",
            "FACTORY_DATA_DIR": str(data_dir),
            "FACTORY_SESSION_SECRET": "s" * 32,
            "FACTORY_ADMIN_PASSWORD_HASH": "pbkdf2_sha256$1$salt$digest",
        }
        values.update(overrides)
        return values

    def test_server_profile_requires_admin_password_hash(self):
        from factory.runtime_profile import RuntimeProfileError, resolve_runtime_profile

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            RuntimeProfileError, "FACTORY_ADMIN_PASSWORD_HASH"
        ):
            values = self.server_environment(Path(tmp) / "data")
            values.pop("FACTORY_ADMIN_PASSWORD_HASH")
            resolve_runtime_profile(root=Path(tmp), environ=values)

    def test_password_hash_and_signed_session_do_not_store_plaintext(self):
        from factory.server_auth import ServerSessionManager, hash_password

        password_hash = hash_password("correct horse battery staple", iterations=10_000)
        self.assertNotIn("correct horse battery staple", password_hash)
        manager = ServerSessionManager(
            session_secret="s" * 32,
            password_hash=password_hash,
            public_base_url="https://video.example.com",
            session_ttl_sec=60,
        )
        issued = manager.login("correct horse battery staple", now=100)
        session = manager.authenticate(f"other=x; avf_session={issued.token}", now=120)

        self.assertIsNotNone(session)
        self.assertEqual(session.csrf_token, issued.csrf_token)
        self.assertIsNone(manager.authenticate(f"avf_session={issued.token}x", now=120))
        self.assertIsNone(manager.authenticate(f"avf_session={issued.token}", now=161))
        with self.assertRaisesRegex(ValueError, "用户名或密码错误"):
            manager.login("wrong password", now=100)

    def test_private_routes_require_session_and_mutations_require_csrf(self):
        from factory.server_auth import AuthRejected, ServerSessionManager, hash_password

        manager = ServerSessionManager(
            session_secret="s" * 32,
            password_hash=hash_password("secret", iterations=10_000),
            public_base_url="https://video.example.com",
        )
        issued = manager.login("secret")
        cookie = f"avf_session={issued.token}"

        with self.assertRaises(AuthRejected) as missing:
            manager.authorize("GET", "/api/videos", {"Host": "video.example.com"})
        self.assertEqual(missing.exception.status, 401)

        session = manager.authorize(
            "GET", "/api/videos",
            {"Host": "video.example.com", "Cookie": cookie},
        )
        self.assertEqual(session.csrf_token, issued.csrf_token)

        for headers, status in (
            ({"Host": "evil.example", "Cookie": cookie}, 403),
            ({"Host": "video.example.com", "Cookie": cookie}, 403),
            ({
                "Host": "video.example.com", "Cookie": cookie,
                "Origin": "https://evil.example",
                "X-CSRF-Token": issued.csrf_token,
            }, 403),
        ):
            with self.subTest(headers=headers), self.assertRaises(AuthRejected) as rejected:
                manager.authorize("POST", "/api/videos/J/produce", headers)
            self.assertEqual(rejected.exception.status, status)

        allowed = manager.authorize(
            "POST", "/api/videos/J/produce",
            {
                "Host": "video.example.com", "Cookie": cookie,
                "Origin": "https://video.example.com",
                "X-CSRF-Token": issued.csrf_token,
            },
        )
        self.assertEqual(allowed.csrf_token, issued.csrf_token)

    def test_only_minimal_health_login_and_oauth_callback_are_public(self):
        from factory.server_auth import ServerSessionManager, hash_password

        manager = ServerSessionManager(
            session_secret="s" * 32,
            password_hash=hash_password("secret", iterations=10_000),
            public_base_url="https://video.example.com",
        )
        for method, path in (
            ("GET", "/healthz"), ("GET", "/readyz"),
            ("GET", "/login"), ("POST", "/api/auth/login"),
            ("GET", "/api/integrations/douyin/oauth/callback"),
        ):
            with self.subTest(path=path):
                self.assertIsNone(manager.authorize(
                    method, path,
                    {
                        "Host": "video.example.com",
                        "Origin": "https://video.example.com",
                    },
                ))

    def test_console_runtime_uses_external_worker_and_server_auth(self):
        from management_server import configure_console_runtime

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "persistent"
            profile, manager = configure_console_runtime(
                root=Path(tmp), environ=self.server_environment(data_dir)
            )

        self.assertEqual(profile.mode, "server")
        self.assertEqual(profile.bind_host, "0.0.0.0")
        self.assertEqual(profile.worker_mode, "external")
        self.assertIsNotNone(manager)

    def test_management_ui_attaches_server_csrf_to_json_and_binary_mutations(self):
        script = (ROOT / "scripts" / "management_ui" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("ensureServerCsrf", script)
        self.assertIn("X-CSRF-Token", script)
        self.assertIn("/api/auth/session", script)
        self.assertGreaterEqual(script.count("setRequestHeader('X-CSRF-Token'"), 2)

    def test_external_worker_mode_never_spawns_from_web_service(self):
        from factory import db
        from factory.management_api import ManagementService

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"FACTORY_DB_PATH": str(Path(tmp) / "factory.db")}
        ):
            database = db.conn()
            database.execute(
                "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
                "VALUES('J-SERVER','T-SERVER','server job','PRODUCTION_QUEUED','{}')"
            )
            database.commit()
            service = ManagementService(
                database, ROOT, worker_mode="external"
            )
            with mock.patch("factory.management_api.subprocess.Popen") as popen:
                result = service.dispatch_production_queue()
            state = database.execute(
                "SELECT state FROM jobs WHERE job_id='J-SERVER'"
            ).fetchone()[0]
            database.close()

        popen.assert_not_called()
        self.assertEqual(state, "PRODUCTION_QUEUED")
        self.assertEqual(result["worker_mode"], "external")
        self.assertEqual(result["queued_jobs"], ["J-SERVER"])


class ServerHttpSecurityTests(unittest.TestCase):
    def setUp(self):
        from factory.server_auth import ServerSessionManager, hash_password
        from management_server import ConsoleHandler

        self.handler = ConsoleHandler
        self.previous = (
            getattr(ConsoleHandler, "runtime_mode", "desktop"),
            getattr(ConsoleHandler, "auth_manager", None),
            getattr(ConsoleHandler, "service", None),
        )
        service = Mock()
        service.overview.return_value = {
            "scripts": {}, "videos": {}, "publishing": {},
        }
        service.clear_seedance_provider_block.return_value = {
            "provider": {"status": "available"}, "message": "cleared",
        }
        ConsoleHandler.runtime_mode = "server"
        ConsoleHandler.auth_manager = ServerSessionManager(
            session_secret="s" * 32,
            password_hash=hash_password("secret", iterations=10_000),
            public_base_url="https://video.example.com",
        )
        ConsoleHandler.service = service
        self.service = service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.handler.runtime_mode, self.handler.auth_manager, self.handler.service = self.previous

    def request(self, method: str, path: str, *, headers=None, body: bytes | None = None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = {"Host": "video.example.com", **(headers or {})}
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def login(self) -> tuple[str, str]:
        status, headers, body = self.request(
            "POST", "/api/auth/login",
            headers={
                "Origin": "https://video.example.com",
                "Content-Type": "application/json",
            },
            body=b'{"password":"secret"}',
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        return cookie, payload["csrf_token"]

    def test_server_http_requires_login_and_csrf_before_side_effects(self):
        health, _, _ = self.request("GET", "/healthz")
        private, _, _ = self.request("GET", "/api/overview")
        cookie, csrf = self.login()
        allowed, _, body = self.request(
            "GET", "/api/overview", headers={"Cookie": cookie}
        )
        missing_csrf, _, _ = self.request(
            "POST", "/api/providers/seedance/clear-block",
            headers={
                "Cookie": cookie, "Origin": "https://video.example.com",
                "Content-Type": "application/json",
            }, body=b"{}",
        )
        changed, _, _ = self.request(
            "POST", "/api/providers/seedance/clear-block",
            headers={
                "Cookie": cookie, "Origin": "https://video.example.com",
                "X-CSRF-Token": csrf, "Content-Type": "application/json",
            }, body=b"{}",
        )

        self.assertEqual(health, 200)
        self.assertEqual(private, 401)
        self.assertEqual(allowed, 200)
        self.assertEqual(json.loads(body)["scripts"], {})
        self.assertEqual(missing_csrf, 403)
        self.assertEqual(changed, 200)
        self.service.clear_seedance_provider_block.assert_called_once_with()

    def test_server_http_rejects_untrusted_host_before_service(self):
        status, _, _ = self.request(
            "GET", "/api/overview", headers={"Host": "evil.example"}
        )
        self.assertEqual(status, 403)
        self.service.overview.assert_not_called()

    def test_unauthenticated_browser_is_redirected_to_local_login_page(self):
        status, headers, _ = self.request("GET", "/")
        login_status, _, login_body = self.request("GET", "/login")

        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login")
        self.assertEqual(login_status, 200)
        self.assertIn("管理员密码", login_body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
