"""Resolve fixed real-person assets without browser automation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3

from .clients.volc_assets import (
    AssetApiError,
    AssetApiUnavailable,
    VolcAssetClient,
)


_ASSET_ID = re.compile(r"^asset-[A-Za-z0-9][A-Za-z0-9._-]*$")
_USER_PROMPT = (
    "没有通过火山资产 API 唯一定位到该真人角色。"
    "请把火山真人资产详情里的 assetId 发给我（形如 asset-...；"
    "也可以直接粘贴完整资产 URI）"
)


@dataclass(frozen=True)
class AssetResolution:
    status: str
    character_id: str
    display_name: str
    asset_id: str | None = None
    asset_uri: str | None = None
    source: str | None = None
    reason: str | None = None
    user_prompt: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "character_id": self.character_id,
            "display_name": self.display_name,
            "asset_id": self.asset_id,
            "asset_uri": self.asset_uri,
            "source": self.source,
            "reason": self.reason,
            "user_prompt": self.user_prompt,
        }


def ensure_schema(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS character_asset_bindings (
            character_id   TEXT PRIMARY KEY,
            display_name   TEXT NOT NULL,
            provider       TEXT NOT NULL DEFAULT 'volcengine_ark',
            asset_id       TEXT NOT NULL,
            asset_uri      TEXT NOT NULL,
            asset_group_id TEXT,
            source         TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'Active',
            metadata_json  TEXT NOT NULL DEFAULT '{}',
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_character_asset_id
            ON character_asset_bindings(asset_id);
        """
    )
    database.commit()


def normalize_asset_id(value: str) -> str:
    candidate = str(value or "").strip()
    if candidate.startswith("asset://"):
        candidate = candidate[len("asset://") :]
    if not _ASSET_ID.fullmatch(candidate):
        raise ValueError("assetId 格式无效，应为 asset-... 或 asset://asset-...")
    return candidate


def get_binding(database: sqlite3.Connection, character_id: str):
    ensure_schema(database)
    return database.execute(
        "SELECT * FROM character_asset_bindings WHERE character_id=?",
        (character_id,),
    ).fetchone()


def apply_cached_bindings(
    database: sqlite3.Connection, characters: list[dict]
) -> bool:
    """Inject locally bound asset URIs into runtime characters."""
    changed = False
    for character in characters:
        if character.get("seedance_asset_uri"):
            continue
        binding = get_binding(database, str(character.get("id") or ""))
        if binding and binding["status"] == "Active":
            character["seedance_asset_uri"] = binding["asset_uri"]
            changed = True
    return changed


def _save_binding(
    database: sqlite3.Connection,
    *,
    character_id: str,
    display_name: str,
    asset_id: str,
    source: str,
    group_id: str | None = None,
    metadata: dict | None = None,
) -> AssetResolution:
    asset_id = normalize_asset_id(asset_id)
    asset_uri = f"asset://{asset_id}"
    ensure_schema(database)
    database.execute(
        "INSERT INTO character_asset_bindings("
        "character_id,display_name,asset_id,asset_uri,asset_group_id,source,"
        "status,metadata_json) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(character_id) DO UPDATE SET "
        "display_name=excluded.display_name,asset_id=excluded.asset_id,"
        "asset_uri=excluded.asset_uri,asset_group_id=excluded.asset_group_id,"
        "source=excluded.source,status=excluded.status,"
        "metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
        (
            character_id,
            display_name,
            asset_id,
            asset_uri,
            group_id,
            source,
            "Active",
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    database.commit()
    return AssetResolution(
        status="resolved",
        character_id=character_id,
        display_name=display_name,
        asset_id=asset_id,
        asset_uri=asset_uri,
        source=source,
    )


def bind_manual_asset(
    database: sqlite3.Connection,
    *,
    character_id: str,
    display_name: str,
    asset_id: str,
) -> AssetResolution:
    return _save_binding(
        database,
        character_id=character_id,
        display_name=display_name,
        asset_id=asset_id,
        source="manual",
    )


def resolve_character_asset(
    database: sqlite3.Connection,
    *,
    character_id: str,
    display_name: str,
    api_client: VolcAssetClient | None,
) -> AssetResolution:
    """Use a cached binding, otherwise API discovery, otherwise ask once."""
    cached = get_binding(database, character_id)
    if cached and cached["status"] == "Active":
        return AssetResolution(
            status="resolved",
            character_id=character_id,
            display_name=display_name,
            asset_id=cached["asset_id"],
            asset_uri=cached["asset_uri"],
            source=cached["source"],
        )

    if api_client is None:
        return AssetResolution(
            status="asset_id_required",
            character_id=character_id,
            display_name=display_name,
            reason="api_unavailable",
            user_prompt=_USER_PROMPT,
        )
    try:
        matches = api_client.find_real_person_assets(display_name)
    except (AssetApiUnavailable, AssetApiError):
        return AssetResolution(
            status="asset_id_required",
            character_id=character_id,
            display_name=display_name,
            reason="api_unavailable",
            user_prompt=_USER_PROMPT,
        )

    if len(matches) == 1:
        match = matches[0]
        return _save_binding(
            database,
            character_id=character_id,
            display_name=display_name,
            asset_id=str(match.get("Id") or ""),
            group_id=str(match.get("GroupId") or "") or None,
            source="api",
            metadata={
                "provider_name": match.get("Name"),
                "asset_type": match.get("AssetType"),
            },
        )

    reason = "ambiguous_api_match" if len(matches) > 1 else "api_no_match"
    return AssetResolution(
        status="asset_id_required",
        character_id=character_id,
        display_name=display_name,
        reason=reason,
        user_prompt=_USER_PROMPT,
    )
