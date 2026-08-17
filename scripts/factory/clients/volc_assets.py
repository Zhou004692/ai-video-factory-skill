"""Read-only Volcengine Ark Assets API client.

The client deliberately exposes only discovery and validation. Creation and
real-person verification stay in the Volcengine console. Requests use the
official Volcengine Python SDK for HMAC-SHA256 signing.
"""
from __future__ import annotations

import json
import os
from typing import Any

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import httpx


class AssetApiUnavailable(RuntimeError):
    """The account cannot use the Assets API or cannot be authenticated."""


class AssetApiError(RuntimeError):
    """An Assets API request failed for a reason other than availability."""


_VERSION = "2024-01-01"
def _normal_name(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


class VolcAssetClient:
    """Minimal signed client for account-scoped Ark portrait assets."""

    def __init__(
        self,
        *,
        ak: str | None = None,
        sk: str | None = None,
        region: str | None = None,
        endpoint: str | None = None,
        project_name: str | None = None,
    ) -> None:
        self.ak = ak or os.getenv("VOLC_AK")
        self.sk = sk or os.getenv("VOLC_SK")
        self.region = region or os.getenv("VOLC_ASSETS_REGION", "cn-beijing")
        self.endpoint = endpoint or os.getenv(
            "VOLC_ASSETS_ENDPOINT", "open.volcengineapi.com"
        )
        self.project_name = project_name or os.getenv(
            "VOLC_ASSETS_PROJECT", "default"
        )
        if not (self.ak and self.sk):
            raise AssetApiUnavailable("VOLC_AK / VOLC_SK are not configured")

        self._http = httpx.Client(timeout=15)

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _hmac(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    def _signed_headers(self, query: dict, body: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = x_date[:8]
        body_hash = hashlib.sha256(body).hexdigest()
        canonical_query = urlencode(sorted(query.items()), quote_via=quote)
        canonical_headers = (
            "content-type:application/json\n"
            f"host:{self.endpoint}\n"
            f"x-content-sha256:{body_hash}\n"
            f"x-date:{x_date}\n"
        )
        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical_request = "\n".join(
            (
                "POST",
                "/",
                canonical_query,
                canonical_headers,
                signed_headers,
                body_hash,
            )
        )
        scope = f"{date}/{self.region}/ark/request"
        string_to_sign = "\n".join(
            (
                "HMAC-SHA256",
                x_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        key_date = self._hmac(self.sk.encode("utf-8"), date)
        key_region = self._hmac(key_date, self.region)
        key_service = self._hmac(key_region, "ark")
        signing_key = self._hmac(key_service, "request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "Host": self.endpoint,
            "X-Content-Sha256": body_hash,
            "X-Date": x_date,
            "Authorization": (
                f"HMAC-SHA256 Credential={self.ak}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def _call(self, action: str, payload: dict) -> dict:
        try:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            query = {"Action": action, "Version": _VERSION}
            response_http = self._http.post(
                f"https://{self.endpoint}/",
                params=query,
                content=body,
                headers=self._signed_headers(query, body),
            )
            response_http.raise_for_status()
            response = response_http.json()
        except Exception as exc:
            message = str(exc)
            lowered = message.casefold()
            unavailable_markers = (
                "unauthorized",
                "accessdenied",
                "permission",
                "forbidden",
                "invalidaccesskey",
                "signature",
                "entitlement",
                "not subscribed",
            )
            if any(marker in lowered for marker in unavailable_markers):
                raise AssetApiUnavailable(message[:500]) from exc
            raise AssetApiError(message[:500]) from exc

        error = (response.get("ResponseMetadata") or {}).get("Error") or {}
        if error:
            code = str(error.get("Code") or "AssetApiError")
            message = str(error.get("Message") or code)
            lowered = f"{code} {message}".casefold()
            if any(
                marker in lowered
                for marker in (
                    "unauthorized",
                    "accessdenied",
                    "permission",
                    "forbidden",
                    "entitlement",
                    "not subscribed",
                )
            ):
                raise AssetApiUnavailable(f"{code}: {message}")
            raise AssetApiError(f"{code}: {message}")
        return response.get("Result") or {}

    def list_assets(
        self,
        *,
        name: str | None = None,
        group_ids: list[str] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        asset_filter: dict[str, Any] = {
            "GroupType": "LivenessFace",
            "Statuses": ["Active"],
        }
        if name:
            asset_filter["Name"] = name
        if group_ids:
            asset_filter["GroupIds"] = group_ids
        result = self._call(
            "ListAssets",
            {
                "Filter": asset_filter,
                "PageNumber": 1,
                "PageSize": min(100, max(1, int(page_size))),
                "SortBy": "UpdateTime",
                "SortOrder": "Desc",
                "ProjectName": self.project_name,
            },
        )
        return list(result.get("Items") or [])

    def list_real_person_groups(self, *, name: str | None = None) -> list[dict]:
        group_filter: dict[str, Any] = {"GroupType": "LivenessFace"}
        if name:
            group_filter["Name"] = name
        result = self._call(
            "ListAssetGroups",
            {
                "Filter": group_filter,
                "PageNumber": 1,
                "PageSize": 100,
                "SortBy": "UpdateTime",
                "SortOrder": "Desc",
                "ProjectName": self.project_name,
            },
        )
        return list(result.get("Items") or [])

    def get_asset(self, asset_id: str) -> dict:
        return self._call(
            "GetAsset",
            {"Id": asset_id, "ProjectName": self.project_name},
        )

    def get_asset_group(self, group_id: str) -> dict:
        """Return the asset group that carries the portrait's catalog profile."""
        return self._call(
            "GetAssetGroup",
            {"Id": group_id, "ProjectName": self.project_name},
        )

    def find_real_person_assets(self, display_name: str) -> list[dict]:
        """Return exact-name active candidates, searching assets then groups."""
        target = _normal_name(display_name)
        direct = [
            item
            for item in self.list_assets(name=display_name)
            if _normal_name(item.get("Name")) == target
            and str(item.get("Status") or "Active") == "Active"
        ]
        if direct:
            return direct

        groups = [
            item
            for item in self.list_real_person_groups(name=display_name)
            if _normal_name(item.get("Name") or item.get("Title")) == target
        ]
        group_ids = [str(item.get("Id")) for item in groups if item.get("Id")]
        if not group_ids:
            return []
        return [
            item
            for item in self.list_assets(group_ids=group_ids)
            if str(item.get("Status") or "") == "Active"
        ]


def from_environment() -> VolcAssetClient:
    return VolcAssetClient()
