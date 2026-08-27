"""Compile reviewed storyboards into deterministic production contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal

from . import (
    anchor_generation,
    character_assets,
    master_dependencies,
    production_config,
    production_planner,
)
from .clients import seedance


IssueSeverity = Literal["repair", "decision", "blocking"]


@dataclass(frozen=True)
class ContractIssue:
    code: str
    severity: IssueSeverity
    stage: str
    subject: str
    message: str
    repair: dict[str, object] | None = None


@dataclass(frozen=True)
class CharacterSpec:
    character_id: str
    display_name: str
    identity_key: str
    identity_type: str
    generation_mode: str
    asset_uri: str


@dataclass(frozen=True)
class AnchorSpec:
    kind: str
    key: str
    source: str
    generator: str
    model: str
    status: str
    estimated_cost_cents: int


@dataclass(frozen=True)
class MasterSpec:
    idx: int
    narrative_type: str
    editorial_duration_sec: float
    generation_duration_sec: float
    speaker_id: str
    scene_anchor_key: str
    camera: str
    depends_on: tuple[int, ...]
    estimated_cost_cents: int


@dataclass(frozen=True)
class ProviderProfile:
    video_model: str
    resolution: str
    output_format: str
    ratio: str
    audio_mode: str
    character_anchor_generator: str
    scene_anchor_generator: str
    max_concurrent_masters: int


@dataclass(frozen=True)
class ProductionSpec:
    version: int
    job_id: str
    characters: tuple[CharacterSpec, ...]
    anchors: tuple[AnchorSpec, ...]
    masters: tuple[MasterSpec, ...]
    dependency_waves: tuple[tuple[int, ...], ...]
    provider_profile: ProviderProfile
    budget_cents: int
    spec_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProductionCompilation:
    spec: ProductionSpec | None
    issues: tuple[ContractIssue, ...]

    @property
    def payable(self) -> bool:
        return self.spec is not None and not any(
            issue.severity == "blocking" for issue in self.issues
        )


def persist_production_spec(
    database: sqlite3.Connection,
    spec: object,
    *,
    commit: bool = True,
) -> None:
    """Persist the job-side frozen spec inside or outside a caller transaction."""
    database.execute(
        "UPDATE jobs SET production_spec_json=?,production_spec_hash=?,"
        "production_spec_version=? WHERE job_id=?",
        (
            json.dumps(
                spec.as_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ),
            spec.spec_hash,
            spec.version,
            spec.job_id,
        ),
    )
    if commit:
        database.commit()


def _character_aliases(characters: list[dict]) -> set[str]:
    aliases: set[str] = set()
    for character in characters:
        for key in ("id", "name", "script_character_name"):
            value = str(character.get(key) or "").strip().casefold()
            if value:
                aliases.add(value)
    return aliases


def _character_mapping_issues(characters: list[dict]) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for character in characters:
        identity_type = str(character.get("identity_type") or "").strip()
        needs_asset = identity_type in {"real_person", "virtual_person"}
        asset_uri = str(
            character.get("seedance_asset_uri")
            or character.get("asset_uri")
            or ""
        ).strip()
        if needs_asset and not asset_uri.startswith("asset://"):
            character_id = str(
                character.get("id") or character.get("name") or "未命名人物"
            ).strip()
            issues.append(ContractIssue(
                code="character_unmapped",
                severity="blocking",
                stage="character",
                subject=character_id,
                message=f"角色 {character_id} 尚未绑定可用的人物资产",
            ))
    return issues


def _reference_paths(master: dict) -> list[str]:
    values: list[object] = []
    for key in (
        "reference_paths", "reference_images", "reference_videos",
        "reference_audios",
    ):
        value = master.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(value)
    for key in (
        "reference_path", "first_frame_path", "last_frame_path",
        "reference_image_path", "reference_video_path", "reference_audio_path",
    ):
        if master.get(key):
            values.append(master[key])
    return [str(value).strip() for value in values if str(value or "").strip()]


def _missing_reference_issues(
    masters: list[dict], root: Path,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    seen: set[str] = set()
    for master in masters:
        idx = int(master.get("idx") or 0)
        for raw_path in _reference_paths(master):
            path = Path(raw_path).expanduser()
            resolved = path if path.is_absolute() else root / path
            if resolved.is_file() or raw_path in seen:
                continue
            seen.add(raw_path)
            issues.append(ContractIssue(
                code="reference_missing",
                severity="blocking",
                stage="reference",
                subject=f"master:{idx}:{raw_path}",
                message=f"母片 #{idx} 引用文件不存在：{raw_path}",
            ))
    return issues


def _speaker_issues(
    masters: list[dict], known_aliases: set[str],
) -> list[ContractIssue]:
    unknown: list[str] = []
    for master in masters:
        if str(master.get("type") or master.get("narrative_type") or "").casefold() != "dialogue":
            continue
        speakers = [master.get("speaker") or master.get("speaker_id")]
        speakers.extend(
            beat.get("speaker")
            for beat in (master.get("performance_beats") or [])
            if isinstance(beat, dict) and str(beat.get("dialogue") or "").strip()
        )
        for speaker in speakers:
            normalized = str(speaker or "").strip()
            if normalized and normalized.casefold() not in known_aliases and normalized not in unknown:
                unknown.append(normalized)
    return [
        ContractIssue(
            code="speaker_unknown",
            severity="blocking",
            stage="dialogue",
            subject=speaker,
            message=f"对白角色未出现在人物表中：{speaker}",
        )
        for speaker in unknown
    ]


def _duration_issues(masters: list[dict]) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for master in masters:
        idx = int(master.get("idx") or 0)
        raw = master.get("duration_sec", master.get("duration"))
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0 or duration > 30:
            issues.append(ContractIssue(
                code="duration_invalid",
                severity="blocking",
                stage="master",
                subject=f"master:{idx}",
                message=(
                    f"母片 #{idx} 时长必须大于 0 秒且不超过 30 秒 "
                    "(30 seconds)"
                ),
            ))
    return issues


def _safe_repairs(storyboard: dict) -> tuple[dict, list[ContractIssue]]:
    """Apply the small allowlist of deterministic repairs to a copy."""
    repaired = copy.deepcopy(storyboard)
    characters = [
        item for item in (repaired.get("characters") or [])
        if isinstance(item, dict)
    ]
    alias_to_id: dict[str, str] = {}
    for character in characters:
        character_id = str(character.get("id") or "").strip()
        if not character_id:
            continue
        for value in (
            character_id,
            character.get("name"),
            character.get("script_character_name"),
        ):
            alias = str(value or "").strip().casefold()
            if alias:
                alias_to_id[alias] = character_id

    issues: list[ContractIssue] = []
    for master in repaired.get("masters") or []:
        if not isinstance(master, dict):
            continue
        idx = int(master.get("idx") or 0)
        speaker_repaired = False
        for container, key in [(master, "speaker"), (master, "speaker_id")]:
            raw = str(container.get(key) or "")
            canonical = alias_to_id.get(raw.strip().casefold())
            if canonical and raw != canonical:
                container[key] = canonical
                speaker_repaired = True
        for beat in master.get("performance_beats") or []:
            if not isinstance(beat, dict):
                continue
            raw = str(beat.get("speaker") or "")
            canonical = alias_to_id.get(raw.strip().casefold())
            if canonical and raw != canonical:
                beat["speaker"] = canonical
                speaker_repaired = True
        if speaker_repaired:
            issues.append(ContractIssue(
                code="speaker_alias_normalized",
                severity="repair",
                stage="dialogue",
                subject=f"master:{idx}",
                message=f"母片 #{idx} 的人物别名已在生产规格中标准化",
                repair={"field": "speaker", "strategy": "canonical_character_id"},
            ))

        duration = float(master.get("duration_sec", master.get("duration")) or 0)
        normalized_duration = seedance.normalize_generation_duration(duration)
        if normalized_duration != duration:
            issues.append(ContractIssue(
                code="duration_normalized",
                severity="repair",
                stage="master",
                subject=f"master:{idx}",
                message=(
                    f"母片 #{idx} 的生成时长已按模型能力从 "
                    f"{duration:g} 秒规范为 {normalized_duration:g} 秒"
                ),
                repair={
                    "editorial_duration_sec": duration,
                    "generation_duration_sec": normalized_duration,
                },
            ))
        if not str(master.get("camera") or "").strip():
            master["camera"] = "中景，固定机位，保持自然表演"
            issues.append(ContractIssue(
                code="camera_defaulted",
                severity="repair",
                stage="master",
                subject=f"master:{idx}",
                message=f"母片 #{idx} 已补齐默认运镜描述",
                repair={"camera": master["camera"]},
            ))
    return repaired, issues


def _build_spec(
    database: sqlite3.Connection,
    *,
    job_id: str,
    storyboard: dict,
    current_cost_cents: int,
) -> ProductionSpec:
    source = copy.deepcopy(storyboard)
    plan = production_planner.analyze_storyboard(
        database,
        source,
        current_cost_cents=current_cost_cents,
    )
    snapshot = dict(plan["generation_snapshot"])
    graph = dict(plan["master_dependency_graph"])
    node_by_idx = {
        int(node["idx"]): dict(node) for node in (graph.get("nodes") or [])
    }
    paid_by_idx = {
        int(item["master_idx"]): dict(item)
        for item in plan["paid_masters"]
    }

    characters = tuple(
        CharacterSpec(
            character_id=str(item.get("id") or "").strip(),
            display_name=str(
                item.get("script_character_name")
                or item.get("name")
                or item.get("id")
                or ""
            ).strip(),
            identity_key=character_assets.character_identity_key(item),
            identity_type=str(item.get("identity_type") or "ai_generated"),
            generation_mode=str(item.get("generation_mode") or "ai_generated"),
            asset_uri=str(
                item.get("seedance_asset_uri") or item.get("asset_uri") or ""
            ).strip(),
        )
        for item in sorted(
            source.get("characters") or [],
            key=lambda value: str(value.get("id") or ""),
        )
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    )

    identity_anchors = [
        AnchorSpec(
            kind="character",
            key=str(item["identity_key"]),
            source=str(item.get("source") or ""),
            generator=str(item.get("generator") or ""),
            model=str(item.get("model") or ""),
            status=str(item.get("status") or "pending"),
            estimated_cost_cents=int(item.get("estimated_cost_cents") or 0),
        )
        for item in plan["character_makeup_plan"]
    ]
    scene_anchors = [
        AnchorSpec(
            kind="scene",
            key=str(item["anchor_key"]),
            source="generated_scene_anchor",
            generator=str(item.get("generator") or ""),
            model=str(item.get("model") or ""),
            status="pending",
            estimated_cost_cents=int(item.get("estimated_cost_cents") or 0),
        )
        for item in plan["paid_scene_anchors"]
    ]
    anchors = tuple(sorted(
        identity_anchors + scene_anchors,
        key=lambda item: (item.kind, item.key),
    ))

    masters: list[MasterSpec] = []
    for raw in sorted(
        (item for item in (source.get("masters") or []) if isinstance(item, dict)),
        key=lambda item: int(item.get("idx") or 0),
    ):
        idx = int(raw.get("idx") or 0)
        editorial_duration = float(raw.get("duration_sec", raw.get("duration")) or 0)
        paid = paid_by_idx.get(idx) or {}
        generation_duration = float(
            paid.get("generation_duration_sec")
            or seedance.normalize_generation_duration(editorial_duration)
        )
        node = node_by_idx.get(idx) or {}
        masters.append(MasterSpec(
            idx=idx,
            narrative_type=str(
                raw.get("type") or raw.get("narrative_type") or "unknown"
            ),
            editorial_duration_sec=editorial_duration,
            generation_duration_sec=generation_duration,
            speaker_id=str(raw.get("speaker") or raw.get("speaker_id") or ""),
            scene_anchor_key=str(raw.get("scene_anchor_key") or ""),
            camera=str(raw.get("camera") or ""),
            depends_on=tuple(int(value) for value in (node.get("depends_on") or [])),
            estimated_cost_cents=int(paid.get("estimated_cost_cents") or 0),
        ))

    payload: dict[str, object] = {
        "version": 1,
        "job_id": job_id,
        "characters": [asdict(item) for item in characters],
        "anchors": [asdict(item) for item in anchors],
        "masters": [asdict(item) for item in masters],
        "dependency_waves": [
            [int(value) for value in wave] for wave in (graph.get("waves") or [])
        ],
        "provider_profile": asdict(ProviderProfile(
            video_model=str(snapshot["model"]),
            resolution=str(snapshot["resolution"]),
            output_format=str(snapshot["output_format"]),
            ratio=str(snapshot["ratio"]),
            audio_mode=str(snapshot["audio_mode"]),
            character_anchor_generator=anchor_generation.selected_generator(
                source, "character"
            ),
            scene_anchor_generator=anchor_generation.selected_generator(
                source, "scene"
            ),
            max_concurrent_masters=int(snapshot["max_concurrent_masters"]),
        )),
        "budget_cents": int(plan["budget_cents"]),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    spec_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    profile = ProviderProfile(**payload["provider_profile"])
    return ProductionSpec(
        version=1,
        job_id=job_id,
        characters=characters,
        anchors=anchors,
        masters=tuple(masters),
        dependency_waves=tuple(
            tuple(int(value) for value in wave)
            for wave in payload["dependency_waves"]
        ),
        provider_profile=profile,
        budget_cents=int(payload["budget_cents"]),
        spec_hash=spec_hash,
    )


def compile_production_spec(
    database: sqlite3.Connection,
    job_id: str,
    root: str | Path,
    *,
    storyboard_override: dict | None = None,
    persist: bool = True,
    target_version: int | None = None,
) -> ProductionCompilation:
    """Return all deterministic blockers without creating paid work."""
    if target_version is None:
        try:
            target_version = int(os.environ.get("FACTORY_PRODUCTION_SPEC_VERSION", "1"))
        except ValueError:
            target_version = 1
    if target_version not in {1, 2}:
        raise ValueError(f"unsupported production spec version: {target_version}")
    row = database.execute(
        "SELECT storyboard,cost_cents,creative_ir_json FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"video job not found: {job_id}")
    if storyboard_override is not None:
        storyboard = copy.deepcopy(storyboard_override)
    else:
        try:
            storyboard = json.loads(row["storyboard"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"video job storyboard is invalid: {job_id}") from exc
    if target_version == 2:
        from . import creative_ir, production_compiler

        raw_ir = None
        if storyboard_override is None and row["creative_ir_json"]:
            try:
                raw_ir = json.loads(row["creative_ir_json"])
            except (TypeError, json.JSONDecodeError):
                raw_ir = None
        creative = (
            creative_ir.from_dict(raw_ir)
            if isinstance(raw_ir, dict)
            else creative_ir.from_storyboard(storyboard)
        )
        # Compatibility for jobs approved before the Creative IR hand-off was
        # derived from the production-ready storyboard.  Those rows can have
        # valid beat references but an empty or incomplete character table.
        # The runtime storyboard already contains the reviewed canonical
        # identities, so restoring only those exact entries is deterministic
        # and does not invent or remap creative intent.
        creative_characters = list(creative.characters)
        known_character_ids = {
            str(character.get("id") or "").strip().casefold()
            for character in creative_characters
            if str(character.get("id") or "").strip()
        }
        restored_characters = []
        for character in storyboard.get("characters") or []:
            if not isinstance(character, dict):
                continue
            character_id = str(character.get("id") or "").strip()
            if not character_id or character_id.casefold() in known_character_ids:
                continue
            restored = copy.deepcopy(character)
            creative_characters.append(restored)
            restored_characters.append(restored)
            known_character_ids.add(character_id.casefold())
        creative_repairs: list[ContractIssue] = []
        if restored_characters:
            creative = replace(
                creative,
                characters=tuple(creative_characters),
            )
            creative_repairs.append(ContractIssue(
                code="creative_characters_restored",
                severity="repair",
                stage="creative_ir",
                subject="characters",
                message=(
                    "已从审核后的运行时分镜恢复创作人物表："
                    + "、".join(
                        str(character.get("id") or "")
                        for character in restored_characters
                    )
                ),
                repair={
                    "character_ids": [
                        str(character.get("id") or "")
                        for character in restored_characters
                    ],
                    "strategy": "restore_from_reviewed_runtime_storyboard",
                },
            ))
        plan = production_planner.analyze_storyboard(
            database,
            copy.deepcopy(storyboard),
            current_cost_cents=int(row["cost_cents"] or 0),
        )
        compilation = production_compiler.compile_ir(
            creative,
            job_id=job_id,
            capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=int(plan["budget_cents"]),
            root=root,
            runtime_storyboard=storyboard,
        )
        if creative_repairs:
            compilation = replace(
                compilation,
                issues=tuple(creative_repairs) + tuple(compilation.issues),
            )
        if persist:
            if compilation.spec is None:
                database.execute(
                    "UPDATE jobs SET production_spec_json=NULL,"
                    "production_spec_hash=NULL,production_spec_version=0 "
                    "WHERE job_id=?",
                    (job_id,),
                )
            else:
                database.execute(
                    "UPDATE jobs SET production_spec_json=?,production_spec_hash=?,"
                    "production_spec_version=? WHERE job_id=?",
                    (
                        json.dumps(
                            compilation.spec.as_dict(), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"),
                        ),
                        compilation.spec.spec_hash,
                        compilation.spec.version,
                        job_id,
                    ),
                )
            database.commit()
        return compilation
    characters = [
        dict(item) for item in (storyboard.get("characters") or [])
        if isinstance(item, dict)
    ]
    masters = [
        dict(item) for item in (storyboard.get("masters") or [])
        if isinstance(item, dict)
    ]
    root_path = Path(root).expanduser().resolve()
    issues = (
        _character_mapping_issues(characters)
        + _missing_reference_issues(masters, root_path)
        + _speaker_issues(masters, _character_aliases(characters))
        + _duration_issues(masters)
    )
    if any(issue.severity == "blocking" for issue in issues):
        if persist:
            database.execute(
                "UPDATE jobs SET production_spec_json=NULL,"
                "production_spec_hash=NULL,production_spec_version=0 "
                "WHERE job_id=?",
                (job_id,),
            )
            database.commit()
        return ProductionCompilation(spec=None, issues=tuple(issues))

    compiled_storyboard, repair_issues = _safe_repairs(storyboard)
    spec = _build_spec(
        database,
        job_id=job_id,
        storyboard=compiled_storyboard,
        current_cost_cents=int(row["cost_cents"] or 0),
    )
    if persist:
        database.execute(
            "UPDATE jobs SET production_spec_json=?,production_spec_hash=?,"
            "production_spec_version=? WHERE job_id=?",
            (
                json.dumps(
                    spec.as_dict(), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                spec.spec_hash,
                spec.version,
                job_id,
            ),
        )
        database.commit()
    return ProductionCompilation(
        spec=spec,
        issues=tuple(issues + repair_issues),
    )
