"""Resolve fixed real-person assets without browser automation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import re
import sqlite3

from .clients.volc_assets import (
    AssetApiError,
    AssetApiUnavailable,
    VolcAssetClient,
)


_ASSET_ID = re.compile(r"^asset-[A-Za-z0-9][A-Za-z0-9._-]*$")
IDENTITY_TYPES = {"real_person", "virtual_person", "custom"}
_USER_PROMPT = (
    "没有通过火山资产 API 唯一定位到该真人角色。"
    "请把火山真人资产详情里的 assetId 发给我（形如 asset-...；"
    "也可以直接粘贴完整资产 URI）"
)


def _canonical_identity_name(character: dict) -> str:
    value = (
        character.get("script_character_name")
        or character.get("name")
        or character.get("id")
        or "unnamed-character"
    )
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def character_identity_key(character: dict) -> str:
    """Return the stable person identity shared by aliases and later shots.

    Runtime speaker ids often contain shot-specific visual descriptions.  They
    are not a reliable identity boundary.  Authorized Volc assets are keyed by
    their asset URI; custom and AI characters are keyed by the canonical
    script role instead.
    """
    asset_uri = str(
        character.get("seedance_asset_uri")
        or character.get("asset_uri")
        or ""
    ).strip()
    if asset_uri.startswith("asset://"):
        return f"volc:{asset_uri}"
    custom_id = str(character.get("asset_character_id") or "").strip()
    if custom_id:
        return f"custom:{custom_id.casefold()}"
    return f"script:{_canonical_identity_name(character)}"


def identity_token(identity_key: str) -> str:
    """Filesystem-safe stable token for one identity anchor."""
    return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:20]


def requires_generated_identity_anchor(character: dict) -> bool:
    """Whether Seedance needs a native identity master before scene work."""
    asset_uri = str(
        character.get("seedance_asset_uri")
        or character.get("asset_uri")
        or ""
    ).strip()
    identity_type = str(character.get("identity_type") or "").strip()
    return not (
        identity_type in {"real_person", "virtual_person"}
        and asset_uri.startswith("asset://")
    )


def character_makeup_plan(characters: list[dict]) -> list[dict]:
    """Build one zero-side-effect makeup entry per unique person identity."""
    grouped: dict[str, dict] = {}
    for character in characters:
        character_id = str(character.get("id") or "").strip()
        if not character_id:
            continue
        key = character_identity_key(character)
        entry = grouped.get(key)
        if entry is not None:
            entry["character_ids"].append(character_id)
            continue
        generated = requires_generated_identity_anchor(character)
        static_ai_identity = (
            generated
            and str(character.get("generation_mode") or "") == "ai_generated"
            and str(character.get("identity_type") or "") != "custom"
        )
        if not generated:
            source = "volc_asset"
            anchor_mode = "volc_asset"
        elif static_ai_identity:
            source = "seedream_identity_image"
            anchor_mode = "static_image"
        else:
            source = "seedance_identity_master"
            anchor_mode = "dynamic_video"
        grouped[key] = {
            "identity_key": key,
            "identity_token": identity_token(key),
            "character_ids": [character_id],
            "display_name": str(
                character.get("script_character_name")
                or character.get("name")
                or character_id
            ),
            "source": source,
            "anchor_mode": anchor_mode,
            "status": "pending" if generated else "ready",
            "requires_paid_call": generated,
            "asset_uri": str(
                character.get("seedance_asset_uri")
                or character.get("asset_uri")
                or ""
            ),
            "duration_sec": 0 if anchor_mode != "dynamic_video" else 4,
        }
    return list(grouped.values())


@dataclass(frozen=True)
class AssetResolution:
    status: str
    character_id: str
    display_name: str
    identity_type: str = "real_person"
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
            "identity_type": self.identity_type,
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
            identity_type  TEXT NOT NULL DEFAULT 'real_person',
            gender         TEXT NOT NULL DEFAULT '',
            provider       TEXT NOT NULL DEFAULT 'volcengine_ark',
            asset_id       TEXT NOT NULL,
            asset_uri      TEXT NOT NULL,
            asset_group_id TEXT,
            source         TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'Active',
            background_description TEXT NOT NULL DEFAULT '',
            background_description_source TEXT NOT NULL DEFAULT '',
            metadata_json  TEXT NOT NULL DEFAULT '{}',
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_character_asset_id
            ON character_asset_bindings(asset_id);
        """
    )
    columns = {
        row[1] for row in database.execute(
            "PRAGMA table_info(character_asset_bindings)"
        ).fetchall()
    }
    if "identity_type" not in columns:
        database.execute(
            "ALTER TABLE character_asset_bindings ADD COLUMN identity_type "
            "TEXT NOT NULL DEFAULT 'real_person'"
        )
    if "gender" not in columns:
        database.execute(
            "ALTER TABLE character_asset_bindings ADD COLUMN gender "
            "TEXT NOT NULL DEFAULT ''"
        )
    if "background_description" not in columns:
        database.execute(
            "ALTER TABLE character_asset_bindings ADD COLUMN "
            "background_description TEXT NOT NULL DEFAULT ''"
        )
    if "background_description_source" not in columns:
        database.execute(
            "ALTER TABLE character_asset_bindings ADD COLUMN "
            "background_description_source TEXT NOT NULL DEFAULT ''"
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
        if character.get("generation_mode") == "ai_generated":
            continue
        if character.get("seedance_asset_uri"):
            continue
        binding = get_binding(database, str(character.get("id") or ""))
        if binding and binding["status"] == "Active":
            if binding["identity_type"] != "custom":
                character["seedance_asset_uri"] = binding["asset_uri"]
            character["identity_type"] = binding["identity_type"]
            character["background_description"] = binding["background_description"]
            changed = True
    return changed


def _save_binding(
    database: sqlite3.Connection,
    *,
    character_id: str,
    display_name: str,
    asset_id: str,
    identity_type: str = "real_person",
    source: str,
    group_id: str | None = None,
    metadata: dict | None = None,
    background_description: str = "",
    background_description_source: str = "",
) -> AssetResolution:
    if identity_type not in IDENTITY_TYPES:
        raise ValueError("identity_type must be real_person, virtual_person or custom")
    asset_id = normalize_asset_id(asset_id)
    asset_uri = f"asset://{asset_id}"
    ensure_schema(database)
    database.execute(
        "INSERT INTO character_asset_bindings("
        "character_id,display_name,identity_type,asset_id,asset_uri,asset_group_id,source,"
        "status,background_description,background_description_source,metadata_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(character_id) DO UPDATE SET "
        "display_name=excluded.display_name,identity_type=excluded.identity_type,"
        "asset_id=excluded.asset_id,"
        "asset_uri=excluded.asset_uri,asset_group_id=excluded.asset_group_id,"
        "source=excluded.source,status=excluded.status,"
        "background_description=excluded.background_description,"
        "background_description_source=excluded.background_description_source,"
        "metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP",
        (
            character_id,
            display_name,
            identity_type,
            asset_id,
            asset_uri,
            group_id,
            source,
            "Active",
            str(background_description or "").strip(),
            str(background_description_source or "").strip(),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    database.commit()
    return AssetResolution(
        status="resolved",
        character_id=character_id,
        display_name=display_name,
        identity_type=identity_type,
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
    identity_type: str = "real_person",
    metadata: dict | None = None,
    background_description: str = "",
    background_description_source: str = "",
    group_id: str | None = None,
) -> AssetResolution:
    return _save_binding(
        database,
        character_id=character_id,
        display_name=display_name,
        asset_id=asset_id,
        identity_type=identity_type,
        source="manual",
        group_id=group_id,
        metadata=metadata,
        background_description=background_description,
        background_description_source=background_description_source,
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
            identity_type=cached["identity_type"],
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
