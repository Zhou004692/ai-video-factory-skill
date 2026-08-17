"""Small server-side client for Douyin OAuth and video publishing APIs."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import httpx


BASE_URL = "https://open.douyin.com"
PUBLISH_SCOPE = "video.create.bind"


class DouyinError(RuntimeError):
    """Raised when Douyin returns a transport or business error."""


def _data(response: httpx.Response) -> dict:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DouyinError(f"抖音开放平台请求失败：{exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise DouyinError("抖音开放平台返回格式异常")
    error_code = int(data.get("error_code") or 0)
    if error_code:
        description = str(data.get("description") or payload.get("message") or "未知错误")
        raise DouyinError(f"抖音开放平台错误 {error_code}：{description}")
    return data


class DouyinClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL, transport=transport, timeout=timeout, follow_redirects=True
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "DouyinClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def authorization_url(
        *, client_key: str, redirect_uri: str, state: str,
        scope: str = PUBLISH_SCOPE,
    ) -> str:
        if not str(redirect_uri).strip().lower().startswith("https://"):
            raise ValueError("抖音 OAuth 回调地址必须使用 HTTPS")
        query = urlencode({
            "client_key": str(client_key).strip(),
            "response_type": "code",
            "scope": str(scope).strip(),
            "redirect_uri": str(redirect_uri).strip(),
            "state": str(state).strip(),
        })
        return f"{BASE_URL}/platform/oauth/connect/?{query}"

    def exchange_code(self, *, client_key: str, client_secret: str, code: str) -> dict:
        response = self._http.post(
            "/oauth/access_token/",
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _data(response)

    def refresh_token(self, *, client_key: str, refresh_token: str) -> dict:
        response = self._http.post(
            "/oauth/refresh_token/",
            data={
                "client_key": client_key,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _data(response)

    def upload_video(self, *, access_token: str, open_id: str, path: Path) -> dict:
        target = Path(path)
        if not target.is_file():
            raise DouyinError("待发布视频文件不存在")
        with target.open("rb") as stream:
            response = self._http.post(
                "/api/douyin/v1/video/upload_video/",
                params={"open_id": open_id},
                headers={"access-token": access_token},
                files={"video": (target.name, stream, "video/mp4")},
            )
        data = _data(response)
        video = data.get("video")
        if not isinstance(video, dict) or not video.get("video_id"):
            raise DouyinError("抖音上传成功但未返回 video_id")
        return video

    def create_video(
        self, *, access_token: str, open_id: str, video_id: str,
        text: str, cover_tsp: float | None = None,
    ) -> dict:
        body: dict[str, object] = {"video_id": video_id, "text": text}
        if cover_tsp is not None:
            body["cover_tsp"] = float(cover_tsp)
        response = self._http.post(
            "/api/douyin/v1/video/create_video/",
            params={"open_id": open_id},
            headers={"access-token": access_token, "Content-Type": "application/json"},
            json=body,
        )
        data = _data(response)
        if not data.get("item_id"):
            raise DouyinError("抖音投稿成功但未返回 item_id")
        return data
