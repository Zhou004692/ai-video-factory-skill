from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib import request
from urllib.parse import parse_qs, urlparse

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from factory import db  # noqa: E402
from factory.clients.douyin import DouyinClient, DouyinError  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from management_server import ConsoleHandler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript(db.DDL)
    return database


class DouyinClientTests(unittest.TestCase):
    def test_authorization_url_contains_required_oauth_parameters(self):
        url = DouyinClient.authorization_url(
            client_key="client-key",
            redirect_uri="https://publish.example.com/oauth/douyin/callback",
            state="csrf-state",
        )

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "open.douyin.com")
        self.assertEqual(query["client_key"], ["client-key"])
        self.assertEqual(query["redirect_uri"], ["https://publish.example.com/oauth/douyin/callback"])
        self.assertEqual(query["state"], ["csrf-state"])
        self.assertIn("video.create.bind", query["scope"][0].split(","))

    def test_authorization_url_rejects_non_https_redirect(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            DouyinClient.authorization_url(
                client_key="client-key",
                redirect_uri="http://127.0.0.1:8765/oauth/douyin/callback",
                state="csrf-state",
            )

    def test_exchange_upload_and_create_follow_official_contract(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/oauth/access_token/":
                return httpx.Response(200, json={"data": {
                    "error_code": 0,
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "open_id": "open-id",
                    "expires_in": 1200,
                    "refresh_expires_in": 2400,
                    "scope": "video.create.bind",
                }})
            if request.url.path.endswith("/upload_video/"):
                return httpx.Response(200, json={"data": {
                    "error_code": 0,
                    "video": {"video_id": "encrypted-video-id"},
                }})
            if request.url.path.endswith("/create_video/"):
                return httpx.Response(200, json={"data": {
                    "error_code": 0,
                    "item_id": "encrypted-item-id",
                    "video_id": "public-video-id",
                }})
            raise AssertionError(request.url)

        client = DouyinClient(transport=httpx.MockTransport(handler))
        token = client.exchange_code(
            client_key="client-key", client_secret="client-secret", code="oauth-code"
        )
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "final.mp4"
            video.write_bytes(b"fake-mp4")
            uploaded = client.upload_video(
                access_token=token["access_token"], open_id=token["open_id"], path=video
            )
        created = client.create_video(
            access_token=token["access_token"],
            open_id=token["open_id"],
            video_id=uploaded["video_id"],
            text="标题 #设备租赁",
            cover_tsp=1.5,
        )
        client.close()

        self.assertEqual(created["item_id"], "encrypted-item-id")
        self.assertEqual(requests[0].headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(requests[1].headers["access-token"], "access-token")
        self.assertEqual(parse_qs(requests[1].url.query.decode())["open_id"], ["open-id"])
        self.assertEqual(json.loads(requests[2].content)["cover_tsp"], 1.5)

    def test_refresh_token_uses_form_contract(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": {
                "error_code": 0,
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "open_id": "open-id",
                "expires_in": 1200,
                "refresh_expires_in": 2400,
                "scope": "video.create.bind",
            }})

        with DouyinClient(transport=httpx.MockTransport(handler)) as client:
            token = client.refresh_token(
                client_key="client-key", refresh_token="old-refresh"
            )

        self.assertEqual(token["access_token"], "new-access")
        self.assertEqual(seen[0].url.path, "/oauth/refresh_token/")
        self.assertEqual(
            parse_qs(seen[0].content.decode()),
            {
                "client_key": ["client-key"],
                "refresh_token": ["old-refresh"],
                "grant_type": ["refresh_token"],
            },
        )

    def test_platform_error_is_raised_with_description(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"data": {"error_code": 28001018, "description": "应用未获得该能力"}}
        ))

        with DouyinClient(transport=transport) as client:
            with self.assertRaisesRegex(DouyinError, "应用未获得该能力"):
                client.exchange_code(
                    client_key="client-key", client_secret="secret", code="code"
                )


class DouyinManagementServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "templates").mkdir()
        (self.root / "templates" / "env.example").write_text("", encoding="utf-8")
        (self.root / ".env").write_text(
            "DOUYIN_CLIENT_KEY=client-key\n"
            "DOUYIN_CLIENT_SECRET=client-secret\n"
            "DOUYIN_REDIRECT_URI=https://publish.example.com/oauth/douyin/callback\n",
            encoding="utf-8",
        )
        self.database = _database()
        self.service = ManagementService(self.database, self.root)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def _ready_video(self, job_id: str = "J-READY") -> Path:
        target = self.root / "data" / "jobs" / job_id / "final.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,final_path) VALUES(?,?,?,?,?)",
            (job_id, "T-1", "待发布视频", "READY_TO_PUBLISH", str(target)),
        )
        self.database.commit()
        return target

    def test_oauth_start_persists_state_without_returning_secrets(self):
        result = self.service.start_douyin_oauth()

        self.assertTrue(result["authorization_url"].startswith("https://open.douyin.com/"))
        self.assertNotIn("client-secret", json.dumps(result))
        row = self.database.execute(
            "SELECT platform,redirect_uri,used_at FROM platform_oauth_states WHERE state=?",
            (result["state"],),
        ).fetchone()
        self.assertEqual(row["platform"], "douyin")
        self.assertIsNone(row["used_at"])

    @mock.patch("factory.management_api.douyin.DouyinClient")
    def test_oauth_callback_consumes_state_and_hides_tokens(self, client_class):
        client_class.return_value.exchange_code.return_value = {
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "open_id": "open-id-123456789",
            "scope": "video.create.bind",
            "expires_in": 1200,
            "refresh_expires_in": 2400,
        }
        started = self.service.start_douyin_oauth()

        result = self.service.complete_douyin_oauth(code="code", state=started["state"])
        status = self.service.douyin_account_status()

        self.assertTrue(result["connected"])
        self.assertNotIn("private-access", json.dumps(result))
        self.assertNotIn("private-refresh", json.dumps(status))
        self.assertEqual(status["open_id_masked"], "open…6789")
        with self.assertRaisesRegex(ValueError, "已使用"):
            self.service.complete_douyin_oauth(code="code", state=started["state"])

    @mock.patch("factory.management_api.douyin.DouyinClient")
    def test_direct_publish_uploads_then_creates_and_registers_evidence(self, client_class):
        final_path = self._ready_video()
        self.service._save_douyin_account({
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "open_id": "open-id",
            "scope": "video.create.bind",
            "expires_in": 3600,
            "refresh_expires_in": 7200,
        })
        client = client_class.return_value
        client.upload_video.return_value = {"video_id": "uploaded-video-id"}
        client.create_video.return_value = {
            "item_id": "item-id", "video_id": "public-video-id"
        }

        result = self.service.publish_to_douyin(
            "J-READY", {"confirmed": True, "text": "正式标题 #设备租赁", "cover_tsp": 1.2}
        )

        client.upload_video.assert_called_once()
        upload_args = client.upload_video.call_args.kwargs
        self.assertEqual(upload_args["access_token"], "access-token")
        self.assertEqual(upload_args["open_id"], "open-id")
        self.assertTrue(upload_args["path"].samefile(final_path))
        client.create_video.assert_called_once()
        self.assertEqual(result["status"], "submitted_for_review")
        publication = self.database.execute(
            "SELECT platform,item_id,metadata_json FROM platform_publications WHERE job_id='J-READY'"
        ).fetchone()
        self.assertEqual(publication["platform"], "douyin")
        self.assertEqual(publication["item_id"], "item-id")
        self.assertEqual(json.loads(publication["metadata_json"])["review_status"], "pending")
        self.assertEqual(
            self.database.execute(
                "SELECT state FROM jobs WHERE job_id='J-READY'"
            ).fetchone()["state"],
            "READY_TO_PUBLISH",
        )
        pending = self.service.publishing()
        self.assertEqual(pending["ready"][0]["pending_platforms"], ["douyin"])
        self.assertEqual(
            pending["publications"][0]["verification_status"], "pending_review"
        )

    @mock.patch("factory.management_api.douyin.DouyinClient")
    def test_direct_publish_requires_confirmation_and_never_records_failed_call(self, client_class):
        self._ready_video()
        self.service._save_douyin_account({
            "access_token": "access-token", "refresh_token": "refresh-token",
            "open_id": "open-id", "scope": "video.create.bind",
            "expires_in": 3600, "refresh_expires_in": 7200,
        })
        with self.assertRaisesRegex(ValueError, "确认"):
            self.service.publish_to_douyin("J-READY", {"confirmed": False, "text": "标题"})

        client_class.return_value.upload_video.side_effect = DouyinError("上传失败")
        with self.assertRaisesRegex(DouyinError, "上传失败"):
            self.service.publish_to_douyin(
                "J-READY", {"confirmed": True, "text": "标题"}
            )
        count = self.database.execute(
            "SELECT COUNT(*) FROM platform_publications WHERE job_id='J-READY'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    @mock.patch("factory.management_api.douyin.DouyinClient")
    def test_verified_registration_promotes_pending_submission_to_published(self, client_class):
        self._ready_video()
        self.service._save_douyin_account({
            "access_token": "access-token", "refresh_token": "refresh-token",
            "open_id": "open-id", "scope": "video.create.bind",
            "expires_in": 3600, "refresh_expires_in": 7200,
        })
        client = client_class.return_value
        client.upload_video.return_value = {"video_id": "uploaded-video-id"}
        client.create_video.return_value = {
            "item_id": "verified-item-id", "video_id": "public-video-id"
        }
        self.service.publish_to_douyin(
            "J-READY", {"confirmed": True, "text": "正式标题"}
        )

        result = self.service.register_publications({
            "job_id": "J-READY",
            "channels": [{
                "platform": "douyin",
                "item_id": "verified-item-id",
                "title": "正式标题",
                "published_at": "2026-08-16T10:00:00+00:00",
                "metadata": {"review_status": "published", "publish_source": "openapi"},
            }],
        })

        submission = self.database.execute(
            "SELECT state FROM platform_submissions "
            "WHERE job_id='J-READY' AND platform='douyin'"
        ).fetchone()
        self.assertEqual(result["state"], "READY_TO_PUBLISH")
        self.assertEqual(submission["state"], "published")
        self.assertEqual(
            self.service.publishing()["publications"][0]["verification_status"],
            "verified",
        )

    @mock.patch("factory.management_api.douyin.DouyinClient")
    def test_retry_after_create_failure_reuses_uploaded_video(self, client_class):
        self._ready_video()
        self.service._save_douyin_account({
            "access_token": "access-token", "refresh_token": "refresh-token",
            "open_id": "open-id", "scope": "video.create.bind",
            "expires_in": 3600, "refresh_expires_in": 7200,
        })
        client = client_class.return_value
        client.upload_video.return_value = {"video_id": "upload-existing"}
        client.create_video.side_effect = [
            DouyinError("temporary create failure"),
            {"item_id": "item-after-retry", "video_id": "public-id"},
        ]

        with self.assertRaisesRegex(DouyinError, "temporary"):
            self.service.publish_to_douyin(
                "J-READY", {"confirmed": True, "text": "正式标题"}
            )
        result = self.service.publish_to_douyin(
            "J-READY", {"confirmed": True, "text": "正式标题"}
        )

        client.upload_video.assert_called_once()
        self.assertEqual(client.create_video.call_count, 2)
        self.assertEqual(result["verification_status"], "pending_review")
        submission = self.database.execute(
            "SELECT state,upload_id,item_id FROM platform_submissions "
            "WHERE job_id='J-READY' AND platform='douyin'"
        ).fetchone()
        self.assertEqual(dict(submission), {
            "state": "pending_review",
            "upload_id": "upload-existing",
            "item_id": "item-after-retry",
        })

    @mock.patch("factory.management_api.douyin.DouyinClient")
    def test_expired_oauth_error_requires_rebind_without_exposing_token(self, client_class):
        self._ready_video()
        self.service._save_douyin_account({
            "access_token": "private-access-token", "refresh_token": "private-refresh-token",
            "open_id": "open-id", "scope": "video.create.bind",
            "expires_in": 3600, "refresh_expires_in": 7200,
        })
        client_class.return_value.upload_video.side_effect = DouyinError(
            "expired access_token private-access-token"
        )

        with self.assertRaisesRegex(ValueError, "授权已失效，请重新绑定") as raised:
            self.service.publish_to_douyin(
                "J-READY", {"confirmed": True, "text": "正式标题"}
            )

        self.assertNotIn("private-access-token", str(raised.exception))
        self.assertNotIn("private-access-token", json.dumps(
            self.service.douyin_account_status(), ensure_ascii=False
        ))

    def test_config_uses_oauth_binding_instead_of_manual_access_token(self):
        status = self.service.config_status()
        fields = {field["key"]: field for field in status["fields"]}
        platform = next(item for item in status["publish_platforms"] if item["id"] == "douyin")

        self.assertIn("DOUYIN_REDIRECT_URI", fields)
        self.assertNotIn("DOUYIN_ACCESS_TOKEN", fields)
        self.assertEqual(platform["mode"], "oauth")
        self.assertFalse(platform["connected"])


class DouyinManagementServerTests(unittest.TestCase):
    def setUp(self):
        self.service = mock.Mock()
        ConsoleHandler.service = self.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, path: str, payload: dict) -> dict:
        response = request.urlopen(request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        ))
        return json.loads(response.read().decode("utf-8"))

    def test_oauth_status_and_start_routes(self):
        self.service.douyin_account_status.return_value = {
            "connected": False, "status": "unbound"
        }
        self.service.start_douyin_oauth.return_value = {
            "authorization_url": "https://open.douyin.com/authorize", "state": "state"
        }

        status = json.loads(request.urlopen(
            self.base + "/api/integrations/douyin"
        ).read().decode("utf-8"))
        started = self._post("/api/integrations/douyin/oauth/start", {})

        self.assertFalse(status["connected"])
        self.assertIn("authorization_url", started)
        self.service.start_douyin_oauth.assert_called_once_with()

    def test_oauth_callback_completes_binding_and_returns_success_page(self):
        self.service.complete_douyin_oauth.return_value = {
            "connected": True, "open_id_masked": "open…6789"
        }

        response = request.urlopen(
            self.base + "/api/integrations/douyin/oauth/callback?code=code&state=state"
        )
        body = response.read().decode("utf-8")

        self.assertIn("绑定成功", body)
        self.service.complete_douyin_oauth.assert_called_once_with(
            code="code", state="state"
        )

    def test_direct_publish_route_delegates_to_service(self):
        self.service.publish_to_douyin.return_value = {
            "status": "submitted_for_review", "item_id": "item-id"
        }

        result = self._post(
            "/api/videos/J-READY/publish/douyin",
            {"confirmed": True, "text": "标题"},
        )

        self.assertEqual(result["status"], "submitted_for_review")
        self.service.publish_to_douyin.assert_called_once_with(
            "J-READY", {"confirmed": True, "text": "标题"}
        )


class DouyinManagementUiTests(unittest.TestCase):
    def test_ui_exposes_oauth_binding_and_direct_publish_without_manual_token(self):
        js = (ROOT / "scripts" / "management_ui" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "scripts" / "management_ui" / "index.html").read_text(encoding="utf-8")

        self.assertIn("DOUYIN_REDIRECT_URI", js)
        self.assertNotIn("DOUYIN_ACCESS_TOKEN:'抖音 OAuth Access Token'", js)
        self.assertIn("/api/integrations/douyin/oauth/start", js)
        self.assertIn("/publish/douyin", js)
        self.assertIn("绑定抖音账号", js)
        self.assertIn("审核中", js)
        self.assertIn("请勿重复提交", js)
        self.assertIn("直连发布与结果回流", html)
        self.assertNotIn("本管理台不直接发布到平台", html)


if __name__ == "__main__":
    unittest.main()
