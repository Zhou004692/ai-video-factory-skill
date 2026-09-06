"""Compile unrestricted creative intent into a frozen production contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal, Mapping

from . import master_dependencies, production_complexity
from .clients import music, seedance
from .creative_ir import (
    CreativeBeat, CreativeIR, SPEECH_MODES, explicit_visible_cast,
    lip_sync_required, normalize_speech_mode, on_screen_speakers,
    visible_cast as _beat_cast,
)
from .production_contract import ContractIssue


@dataclass(frozen=True)
class ProviderCapabilities:
    model: str
    max_request_duration_sec: int
    max_reference_images: int
    max_reference_videos: int
    native_audio: bool

    @classmethod
    def seedance_2_5(cls) -> "ProviderCapabilities":
        model = "doubao-seedance-2-5-260628"
        profile = seedance.capability_profile(model)
        return cls(
            model=model,
            max_request_duration_sec=(
                seedance.SEEDANCE_25_MAX_GENERATION_DURATION_SEC
            ),
            max_reference_images=int(
                profile["reference_limits"]["reference_image"]
            ),
            max_reference_videos=int(
                profile["reference_limits"]["reference_video"]
            ),
            native_audio=True,
        )


@dataclass(frozen=True)
class MasterSpecV2:
    idx: int
    scene_key: str
    editorial_duration_sec: float
    generation_duration_sec: float
    creative_beat_ids: tuple[str, ...]
    character_ids: tuple[str, ...]
    speaker_ids: tuple[str, ...]
    dialogue: str
    action: str
    preserved_requirements: tuple[str, ...]
    adaptation_actions: tuple[str, ...]
    quality_gates: tuple[str, ...]
    depends_on: tuple[int, ...]
    estimated_cost_cents: int

    @property
    def duration_sec(self) -> float:
        """Backward-compatible editorial duration used by existing callers."""
        return self.editorial_duration_sec


@dataclass(frozen=True)
class ProductionNodeSpec:
    node_id: str
    node_type: str
    paid: bool
    estimated_cost_cents: int
    input: Mapping[str, object]
    quality_gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductionEdgeSpec:
    from_node_id: str
    to_node_id: str
    edge_type: str


@dataclass(frozen=True)
class ProductionSpecV2:
    version: int
    job_id: str
    creative_ir_hash: str
    complexity: Mapping[str, object]
    mode: Literal["stable", "enhanced", "experimental"]
    reliability_commitment: Literal["standard", "assisted", "best_effort"]
    characters: tuple[Mapping[str, object], ...]
    masters: tuple[MasterSpecV2, ...]
    nodes: tuple[ProductionNodeSpec, ...]
    edges: tuple[ProductionEdgeSpec, ...]
    capabilities: ProviderCapabilities
    runtime_storyboard: Mapping[str, object]
    budget_cents: int
    estimated_cost_cents: int
    spec_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProductionCompilationV2:
    spec: ProductionSpecV2 | None
    issues: tuple[ContractIssue, ...]
    required_content: tuple[str, ...]

    @property
    def blockers(self) -> tuple[ContractIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "blocking")

    @property
    def decisions(self) -> tuple[ContractIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "decision")

    @property
    def compilable(self) -> bool:
        return self.spec is not None and not self.blockers

    @property
    def payable(self) -> bool:
        """Compatibility alias for callers shared with v1 compilation."""
        return self.compilable


@dataclass(frozen=True)
class _Unit:
    beat: CreativeBeat
    duration_sec: float
    scene_key: str
    character_ids: tuple[str, ...]
    dialogue: str
    adaptation_actions: tuple[str, ...]


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _speech_duration(dialogue: str) -> float:
    compact = re.sub(r"[\s，。！？、；：,.!?;:]", "", dialogue or "")
    return round(max(0.0, len(compact) / 4.2), 2)


def _scene_key(beat: CreativeBeat, index: int) -> str:
    explicit = str(
        beat.extensions.get("scene_id")
        or beat.extensions.get("scene_anchor_key")
        or ""
    ).strip().casefold()
    if explicit:
        return explicit
    scene = beat.scene.strip().casefold()
    return f"scene:{hashlib.sha256(scene.encode('utf-8')).hexdigest()[:12]}" if scene else f"scene:{index}"


def _reference_paths(beat: CreativeBeat) -> tuple[str, ...]:
    values: list[object] = []
    for key in ("reference_paths", "reference_images", "reference_videos"):
        raw = beat.extensions.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(raw)
    for key in ("reference_path", "reference_image_path", "reference_video_path"):
        if beat.extensions.get(key):
            values.append(beat.extensions[key])
    return _unique(str(value).strip() for value in values)


def _deterministic_issues(
    creative: CreativeIR, *, root: str | Path | None,
) -> list[ContractIssue]:
    aliases: set[str] = set()
    for character in creative.characters:
        for key in ("id", "name", "script_character_name"):
            value = str(character.get(key) or "").strip().casefold()
            if value:
                aliases.add(value)
    issues: list[ContractIssue] = []
    if not creative.beats:
        issues.append(ContractIssue(
            code="creative_empty",
            severity="blocking",
            stage="creative_ir",
            subject="beats",
            message="创作内容没有可生产的剧情节拍",
        ))
    root_path = Path(root).expanduser().resolve() if root is not None else None
    for index, beat in enumerate(creative.beats, 1):
        raw_mode = beat.extensions.get("speech_mode")
        explicit_mode = str(raw_mode).strip().lower() if raw_mode is not None else ""
        if explicit_mode and explicit_mode not in SPEECH_MODES:
            issues.append(ContractIssue(
                code="speech_mode_invalid",
                severity="blocking",
                stage="creative_ir",
                subject=beat.beat_id,
                message=(
                    f"创作节拍 {beat.beat_id} 的 speech_mode={explicit_mode!r} 无效；"
                    f"请使用 {', '.join(SPEECH_MODES)}"
                ),
            ))
        elif beat.dialogue and len({
            normalize_speech_mode(explicit_mode, speaker=speaker)
            for speaker in beat.speakers
        }) > 1:
            issues.append(ContractIssue(
                code="speech_mode_ambiguous",
                severity="blocking",
                stage="creative_ir",
                subject=beat.beat_id,
                message=(
                    f"创作节拍 {beat.beat_id} 的说话人包含不同语音模式；"
                    "请按说话轮次拆分节拍并分别指定 speech_mode，"
                    "或确认所有说话人使用同一模式后显式设置 speech_mode"
                ),
            ))
        for character in explicit_visible_cast(beat):
            if character and character.casefold() not in aliases:
                issues.append(ContractIssue(
                    code="character_unknown",
                    severity="blocking",
                    stage="creative_ir",
                    subject=f"{beat.beat_id}:{character}",
                    message=f"创作节拍 {beat.beat_id} 的出镜人物 {character} 未出现在人物表中",
                ))
        for speaker in on_screen_speakers(beat):
            if speaker.casefold() not in aliases:
                issues.append(ContractIssue(
                    code="speaker_unknown",
                    severity="blocking",
                    stage="creative_ir",
                    subject=beat.beat_id,
                    message=f"创作节拍 {beat.beat_id} 的说话人 {speaker} 未出现在人物表中",
                ))
        raw_duration = beat.extensions.get("duration_sec")
        if raw_duration is not None and beat.extensions.get("duration_locked") is True:
            try:
                locked_duration = float(raw_duration)
            except (TypeError, ValueError):
                locked_duration = 0
            if beat.dialogue and locked_duration + 0.25 < _speech_duration(beat.dialogue):
                issues.append(ContractIssue(
                    code="dialogue_timing_impossible",
                    severity="blocking",
                    stage="creative_ir",
                    subject=beat.beat_id,
                    message=f"创作节拍 {beat.beat_id} 锁定时长不足以完整说完台词",
                ))
        if root_path is not None:
            for raw_path in _reference_paths(beat):
                path = Path(raw_path).expanduser()
                resolved = path if path.is_absolute() else root_path / path
                if not resolved.is_file():
                    issues.append(ContractIssue(
                        code="reference_missing",
                        severity="blocking",
                        stage="creative_ir",
                        subject=f"{beat.beat_id}:{raw_path}",
                        message=f"创作节拍 {beat.beat_id} 引用文件不存在：{raw_path}",
                    ))
    return issues


def _allocated_durations(creative: CreativeIR) -> list[float]:
    fixed: list[float | None] = []
    minimums = [_speech_duration(beat.dialogue) for beat in creative.beats]
    for beat in creative.beats:
        raw = beat.extensions.get("duration_sec")
        try:
            fixed.append(float(raw) if raw is not None else None)
        except (TypeError, ValueError):
            fixed.append(None)
    target = float(creative.target_duration_sec or 0)
    known = sum(value for value in fixed if value is not None)
    missing = [index for index, value in enumerate(fixed) if value is None]
    remaining = max(0.0, target - known)
    weights = [max(1, len(creative.beats[index].dialogue)) for index in missing]
    weight_total = sum(weights) or len(missing) or 1
    result: list[float] = []
    for index, value in enumerate(fixed):
        if value is None:
            weight = max(1, len(creative.beats[index].dialogue))
            value = remaining * weight / weight_total if remaining else 4.0
        result.append(round(max(value, minimums[index], 0.5), 2))
    return result


def _split_dialogue(dialogue: str, count: int) -> list[str]:
    if count <= 1:
        return [dialogue]
    if not dialogue:
        return [""] * count
    chunks: list[str] = []
    cursor = 0
    for part in range(1, count):
        ideal = round(len(dialogue) * part / count)
        candidates = [
            index + 1 for index in range(cursor, len(dialogue))
            if dialogue[index] in "，。！？；、,!?;"
        ]
        cut = min(candidates, key=lambda value: abs(value - ideal)) if candidates else ideal
        cut = max(cursor + 1, min(cut, len(dialogue) - (count - part)))
        chunks.append(dialogue[cursor:cut])
        cursor = cut
    chunks.append(dialogue[cursor:])
    return chunks


def _units(
    creative: CreativeIR, capabilities: ProviderCapabilities,
) -> list[_Unit]:
    result: list[_Unit] = []
    for index, (beat, duration) in enumerate(
        zip(creative.beats, _allocated_durations(creative)), 1
    ):
        part_count = max(1, math.ceil(duration / capabilities.max_request_duration_sec))
        durations = [round(duration / part_count, 2)] * part_count
        durations[-1] = round(duration - sum(durations[:-1]), 2)
        dialogues = _split_dialogue(beat.dialogue, part_count)
        for part_index, (part_duration, dialogue) in enumerate(
            zip(durations, dialogues), 1
        ):
            actions = ["dialogue_time_allocated"]
            if part_count > 1:
                actions.append("provider_duration_split")
            result.append(_Unit(
                beat=beat,
                duration_sec=part_duration,
                scene_key=_scene_key(beat, index),
                character_ids=_beat_cast(beat),
                dialogue=dialogue,
                adaptation_actions=tuple(actions),
            ))
    return result


def _master_groups(
    units: list[_Unit], max_duration: float,
) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    current_duration = 0.0
    current_scene = ""
    for unit in units:
        scene_changed = bool(current and unit.scene_key != current_scene)
        overflow = bool(current and current_duration + unit.duration_sec > max_duration)
        if scene_changed or overflow:
            groups.append(current)
            current = []
            current_duration = 0.0
        if not current:
            current_scene = unit.scene_key
        current.append(unit)
        current_duration += unit.duration_sec
    if current:
        groups.append(current)
    return groups


def _runtime_performance_beats(group: list[_Unit]) -> list[dict[str, object]]:
    """Project compiled units into the legacy executor's beat contract."""
    result: list[dict[str, object]] = []
    start = 0.0
    for unit in group:
        beat = unit.beat
        payload = deepcopy(dict(beat.extensions))
        payload.pop("source_master_idx", None)
        payload.update({
            "beat": beat.kind,
            "source_beat_id": beat.beat_id,
            "speaker": beat.speakers[0] if beat.speakers else "",
            "speakers": list(beat.speakers),
            "start": round(start, 2),
            "duration": unit.duration_sec,
            "dialogue": unit.dialogue,
            "visual_intent": beat.action,
            "emotion": beat.emotion,
            "camera_intent": beat.camera_intent,
            "must_preserve": list(beat.must_preserve),
            "may_adapt": list(beat.may_adapt),
        })
        result.append(payload)
        start += unit.duration_sec
    return result


def _compiled_runtime_shots(
    runtime_masters: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Project the compiled masters into a self-contained local edit plan."""
    shots: list[dict[str, object]] = []
    previous_master_idx: int | None = None
    previous_scene_key = ""
    for master in runtime_masters:
        master_idx = int(master.get("idx") or 0)
        scene_key = str(master.get("scene_anchor_key") or "")
        beats = [
            deepcopy(dict(item))
            for item in (master.get("performance_beats") or [])
            if isinstance(item, Mapping)
        ]
        if not beats:
            beats = [{
                "beat": "scene",
                "source_beat_id": f"master-{master_idx}",
                "duration": float(
                    master.get("duration") or master.get("duration_sec") or 0
                ),
                "dialogue": str(master.get("voiceover") or ""),
                "visual_intent": str(
                    master.get("action") or master.get("scene") or ""
                ),
            }]
        cursor = 0.0
        for beat in beats:
            duration = float(beat.get("duration") or 0)
            if duration <= 0:
                continue
            shot = deepcopy(beat)
            shot_idx = len(shots) + 1
            first_in_master = previous_master_idx != master_idx
            scene_changed = bool(
                shots and first_in_master and scene_key != previous_scene_key
            )
            shot.update({
                "idx": shot_idx,
                "source_beat_id": str(
                    beat.get("source_beat_id") or f"beat-{shot_idx}"
                ),
                "beat": str(beat.get("beat") or "scene"),
                "duration": duration,
                "video_master_idx": master_idx,
                "audio_master_idx": master_idx,
                "video_start": round(cursor, 3),
                "audio_start": round(cursor, 3),
                "subtitle": str(beat.get("dialogue") or "").strip() or None,
                "visual_intent": str(beat.get("visual_intent") or ""),
                "continuity_relation": (
                    "opening" if not shots
                    else "scene_switch" if scene_changed
                    else "same_master_continuous" if not first_in_master
                    else "same_scene_master_continuation"
                ),
                "transition_type": "dip_to_black" if scene_changed else "none",
                "transition_duration_sec": 0.15 if scene_changed else 0.0,
                "preserve_audio_continuity": not first_in_master,
            })
            shots.append(shot)
            cursor = round(cursor + duration, 3)
            previous_master_idx = master_idx
            previous_scene_key = scene_key
    return shots


def _remap_source_director_shots(
    source: Mapping[str, object],
    groups: list[list[_Unit]],
    masters: tuple[MasterSpecV2, ...],
) -> list[dict[str, object]] | None:
    """Keep an existing dual-track edit plan when compilation is one-to-one.

    The source storyboard may deliberately place a B-roll master over an
    earlier dialogue master and then return to a later CTA. Rebuilding shots
    by iterating compiled masters destroys that narrative order. Preserve the
    authored timeline only when every source master maps unambiguously to one
    compiled master and all referenced ranges still fit the compiled output;
    otherwise the compiler must fall back to its self-contained projection.
    """
    raw_shots = [
        deepcopy(dict(item))
        for item in (source.get("shots") or [])
        if isinstance(item, Mapping)
    ]
    if not raw_shots:
        return None

    compiled_by_source: dict[int, int] = {}
    duration_by_compiled = {
        int(master.idx): float(master.editorial_duration_sec)
        for master in masters
    }
    for spec, group in zip(masters, groups):
        source_indexes = tuple(dict.fromkeys(
            int(unit.beat.extensions.get("source_master_idx") or 0)
            for unit in group
            if int(unit.beat.extensions.get("source_master_idx") or 0) > 0
        ))
        if len(source_indexes) != 1 or source_indexes[0] in compiled_by_source:
            return None
        compiled_by_source[source_indexes[0]] = int(spec.idx)

    remapped: list[dict[str, object]] = []
    for shot_index, raw_shot in enumerate(raw_shots, 1):
        source_video_idx = int(raw_shot.get("video_master_idx") or 0)
        source_audio_idx = int(
            raw_shot.get("audio_master_idx") or source_video_idx
        )
        video_idx = compiled_by_source.get(source_video_idx)
        audio_idx = compiled_by_source.get(source_audio_idx)
        if video_idx is None or audio_idx is None:
            return None
        try:
            duration = float(
                raw_shot.get("duration")
                or raw_shot.get("duration_sec")
                or 0
            )
            video_start = float(raw_shot.get("video_start") or 0)
            audio_start = float(raw_shot.get("audio_start") or 0)
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        tolerance = 0.05
        if (
            video_start < 0
            or audio_start < 0
            or video_start + duration
            > duration_by_compiled.get(video_idx, 0) + tolerance
            or audio_start + duration
            > duration_by_compiled.get(audio_idx, 0) + tolerance
        ):
            return None
        shot = deepcopy(raw_shot)
        shot.update({
            "idx": shot_index,
            "duration": duration,
            "video_master_idx": video_idx,
            "audio_master_idx": audio_idx,
            "video_start": round(video_start, 3),
            "audio_start": round(audio_start, 3),
        })
        remapped.append(shot)
    return remapped


def ensure_director_timeline(storyboard: Mapping[str, object]) -> dict[str, object]:
    """Return a runnable director storyboard without dangling master refs."""
    runtime = deepcopy(dict(storyboard))
    if runtime.get("production_mode") != "native_master_local_director":
        return runtime
    masters = [
        deepcopy(dict(item))
        for item in (runtime.get("masters") or [])
        if isinstance(item, Mapping)
    ]
    master_indexes = {int(item.get("idx") or 0) for item in masters}
    shots = [
        item for item in (runtime.get("shots") or [])
        if isinstance(item, Mapping)
    ]
    valid = bool(shots) and all(
        int(item.get("video_master_idx") or 0) in master_indexes
        and int(
            item.get("audio_master_idx")
            or item.get("video_master_idx")
            or 0
        ) in master_indexes
        for item in shots
    )
    if not valid:
        runtime["shots"] = _compiled_runtime_shots(masters)
    return runtime


def _compiled_runtime_storyboard(
    source: Mapping[str, object],
    groups: list[list[_Unit]],
    masters: tuple[MasterSpecV2, ...],
) -> dict[str, object]:
    """Make the immutable compiler output the sole executable storyboard.

    Rich continuity metadata is inherited from the first source master in the
    same compiled scene. Fields that define execution identity, timing and
    dialogue are always rebuilt from the compiler output.
    """
    storyboard = deepcopy(dict(source))
    raw_masters = {
        int(item.get("idx") or index): deepcopy(dict(item))
        for index, item in enumerate(source.get("masters") or [], 1)
        if isinstance(item, Mapping)
    }
    runtime_masters: list[dict[str, object]] = []
    for spec, group in zip(masters, groups):
        source_indexes = tuple(dict.fromkeys(
            int(unit.beat.extensions.get("source_master_idx") or 0)
            for unit in group
            if int(unit.beat.extensions.get("source_master_idx") or 0) > 0
        ))
        base = deepcopy(raw_masters.get(source_indexes[0], {})) if source_indexes else {}
        base.pop("site_anchor_key", None)
        base.pop("site_anchor_description", None)
        scenes = _unique(unit.beat.scene for unit in group)
        actions = _unique(unit.beat.action for unit in group)
        cameras = _unique(unit.beat.camera_intent for unit in group)
        continuity_contract = base.get("continuity_contract")
        continuity_scene = (
            continuity_contract.get("scene")
            if isinstance(continuity_contract, Mapping)
            else None
        )
        spatial_anchor_description = str(
            continuity_scene.get("description")
            if isinstance(continuity_scene, Mapping)
            else ""
        ).strip() or "；".join(scenes)
        has_dialogue = bool(spec.dialogue.strip())
        performance_beats = _runtime_performance_beats(group)
        for performance_beat in performance_beats:
            performance_beat.pop("site_anchor_key", None)
            performance_beat.pop("site_anchor_description", None)
        base.update({
            "idx": spec.idx,
            "type": "dialogue" if has_dialogue else "broll",
            "narrative_type": "dialogue" if has_dialogue else "broll",
            "speaker": spec.speaker_ids[0] if spec.speaker_ids else "",
            "speakers": list(spec.speaker_ids),
            "audio_speakers": list(_unique(
                speaker
                for unit in group
                for speaker in unit.beat.speakers
            )),
            "cast": list(spec.character_ids),
            "scene_anchor_key": spec.scene_key,
            "scene_anchor_description": spatial_anchor_description,
            "scene": "；".join(_unique((*scenes, *actions))),
            "action": spec.action,
            "camera": "；".join(cameras) or str(base.get("camera") or ""),
            "duration": spec.editorial_duration_sec,
            "duration_sec": spec.editorial_duration_sec,
            "generation_duration": spec.generation_duration_sec,
            "voiceover": spec.dialogue,
            "subtitle": spec.dialogue,
            "performance_beats": performance_beats,
            "source_beat_ids": list(spec.creative_beat_ids),
            "source_master_indexes": list(source_indexes),
            "depends_on": list(spec.depends_on),
            "preserved_requirements": list(spec.preserved_requirements),
            "adaptation_actions": list(spec.adaptation_actions),
            "quality_gates": list(spec.quality_gates),
            "render_mode": (
                str(base.get("render_mode") or "cinematic_character")
                if has_dialogue else "broll"
            ),
        })
        runtime_masters.append(base)
    storyboard["masters"] = runtime_masters
    storyboard["shots"] = (
        _remap_source_director_shots(source, groups, masters)
        or _compiled_runtime_shots(runtime_masters)
    )
    director_plan = deepcopy(dict(storyboard.get("director_plan") or {}))
    dialogue_master_count = sum(
        1 for master in runtime_masters if master.get("type") == "dialogue"
    )
    broll_master_count = sum(
        1 for master in runtime_masters if master.get("type") == "broll"
    )
    director_plan.update({
        "strategy": (
            "dialogue_plus_broll"
            if broll_master_count else "cinematic_dialogue_only"
        ),
        "dialogue_master_count": dialogue_master_count,
        "broll_master_count": broll_master_count,
        "paid_master_count": len(runtime_masters),
    })
    if not broll_master_count:
        director_plan["broll_beats"] = []
    storyboard["director_plan"] = director_plan
    storyboard["master_dependency_graph"] = (
        master_dependencies.build_master_dependency_graph(runtime_masters)
    )
    return storyboard


def _quality_gates(
    mode: str, has_dialogue: bool, *, requires_lip_sync: bool,
) -> tuple[str, ...]:
    gates = ["unexpected_readable_text", "duration_complete"]
    if has_dialogue:
        gates.append("transcript_complete")
        if requires_lip_sync:
            gates.append("lip_sync")
        gates.append("subtitle_sync")
    if mode in {"enhanced", "experimental"}:
        gates.extend(("character_consistency", "scene_continuity", "staging"))
    if mode == "experimental":
        gates.append("mandatory_human_review")
    return tuple(gates)


def _build_nodes(
    creative: CreativeIR, masters: tuple[MasterSpecV2, ...], *,
    mode: str, capabilities: ProviderCapabilities, music_provider: str,
) -> tuple[tuple[ProductionNodeSpec, ...], tuple[ProductionEdgeSpec, ...]]:
    nodes: list[ProductionNodeSpec] = []
    edges: list[ProductionEdgeSpec] = []
    for character in creative.characters:
        character_id = str(character.get("id") or "").strip()
        if not character_id:
            continue
        identity_type = str(character.get("identity_type") or "ai_generated")
        asset_uri = str(
            character.get("seedance_asset_uri")
            or character.get("asset_uri")
            or ""
        ).strip()
        reuses_provider_asset = (
            identity_type in {"real_person", "virtual_person"}
            and asset_uri.startswith("asset://")
        )
        nodes.append(ProductionNodeSpec(
            node_id=f"character_anchor:{character_id}",
            node_type="character_anchor",
            paid=not reuses_provider_asset,
            estimated_cost_cents=(
                0 if reuses_provider_asset
                else seedance.estimate_cost_cents(4, "480p")
            ),
            input={
                "character_id": character_id,
                "identity_type": identity_type,
                "asset_uri": asset_uri,
            },
            quality_gates=("single_identity", "no_readable_text"),
        ))
    scene_keys = tuple(dict.fromkeys(master.scene_key for master in masters))
    recurring = {
        key for key in scene_keys
        if sum(master.scene_key == key for master in masters) > 1
    }
    if mode != "stable":
        recurring.update(scene_keys)
    for scene_key in scene_keys:
        if scene_key not in recurring:
            continue
        nodes.append(ProductionNodeSpec(
            node_id=f"scene_anchor:{scene_key}",
            node_type="scene_anchor",
            paid=True,
            estimated_cost_cents=seedance.estimate_cost_cents(4, "480p"),
            input={"scene_key": scene_key},
            quality_gates=("empty_scene", "no_readable_text"),
        ))
    for master in masters:
        node_id = f"master:{master.idx}"
        nodes.append(ProductionNodeSpec(
            node_id=node_id,
            node_type="master",
            paid=True,
            estimated_cost_cents=master.estimated_cost_cents,
            input={
                "master_idx": master.idx,
                "creative_beat_ids": master.creative_beat_ids,
                "scene_key": master.scene_key,
            },
            quality_gates=master.quality_gates,
        ))
        for character_id in master.character_ids:
            edges.append(ProductionEdgeSpec(
                f"character_anchor:{character_id}", node_id, "identity_reference",
            ))
        if scene_key := next((
            key for key in recurring if key == master.scene_key
        ), ""):
            edges.append(ProductionEdgeSpec(
                f"scene_anchor:{scene_key}", node_id, "scene_reference",
            ))
        for dependency in master.depends_on:
            edges.append(ProductionEdgeSpec(
                f"master:{dependency}", node_id, "continuity",
            ))
    music_paid = music.may_submit_paid_work(music_provider)
    nodes.extend((
        ProductionNodeSpec(
            "bgm", "bgm", music_paid,
            music.estimate_cost_cents(60, provider=music_provider),
            {"provider": music_provider},
        ),
        ProductionNodeSpec("local_compose", "local_compose", False, 0, {}),
        ProductionNodeSpec(
            "final_review", "final_review", False, 0, {},
            ("human_acceptance",),
        ),
    ))
    for master in masters:
        edges.append(ProductionEdgeSpec(
            f"master:{master.idx}", "local_compose", "media_input",
        ))
        edges.append(ProductionEdgeSpec(
            f"master:{master.idx}", "bgm", "timeline_input",
        ))
    edges.append(ProductionEdgeSpec(
        "bgm", "local_compose", "audio_input",
    ))
    edges.append(ProductionEdgeSpec(
        "local_compose", "final_review", "review_input",
    ))
    return tuple(nodes), tuple(edges)


def compile_ir(
    creative: CreativeIR,
    *,
    job_id: str,
    capabilities: ProviderCapabilities,
    budget_cents: int,
    root: str | Path | None = None,
    runtime_storyboard: Mapping[str, object] | None = None,
) -> ProductionCompilationV2:
    """Compile creative intent without submitting or mutating paid work."""
    required_content = _unique(
        requirement
        for beat in creative.beats
        for requirement in beat.must_preserve
    )
    issues = _deterministic_issues(creative, root=root)
    frozen_storyboard = deepcopy(dict(runtime_storyboard or {}))
    settings = frozen_storyboard.get("production_settings") or {}
    configured_music_provider = str(
        settings.get("music_provider")
        or (frozen_storyboard.get("bgm") or {}).get("provider")
        or "silent"
    ).strip()
    try:
        music_provider = music.resolve_provider(
            explicit=configured_music_provider
        )
    except (TypeError, ValueError) as exc:
        music_provider = "silent"
        issues.append(ContractIssue(
            code="music_provider_invalid",
            severity="blocking",
            stage="audio",
            subject=job_id,
            message=str(exc),
        ))
    blockers = [issue for issue in issues if issue.severity == "blocking"]
    if blockers:
        return ProductionCompilationV2(None, tuple(issues), required_content)

    complexity = production_complexity.analyze(creative)
    units = _units(creative, capabilities)
    groups = _master_groups(units, capabilities.max_request_duration_sec)
    masters: list[MasterSpecV2] = []
    scene_count = len({unit.scene_key for unit in units})
    previous_cast: set[str] = set()
    previous_scene = ""
    for idx, group in enumerate(groups, 1):
        cast = _unique(character for unit in group for character in unit.character_ids)
        speakers = _unique(
            speaker
            for unit in group
            if unit.dialogue.strip()
            for speaker in on_screen_speakers(unit.beat)
        )
        requirements = _unique(
            requirement
            for unit in group
            for requirement in unit.beat.must_preserve
        )
        actions = list(_unique(
            action for unit in group for action in unit.adaptation_actions
        ))
        if len(group) > 1:
            actions.append("continuous_scene_grouping")
        if scene_count > 1:
            actions.append("scene_split")
        if len(cast) > 2:
            actions.append("multi_character_staging")
        depends_on: tuple[int, ...] = ()
        if masters and (
            group[0].scene_key == previous_scene
            or bool(set(cast) & previous_cast)
        ):
            depends_on = (masters[-1].idx,)
        editorial_duration = round(sum(unit.duration_sec for unit in group), 2)
        generation_duration = seedance.normalize_generation_duration(
            editorial_duration
        )
        has_dialogue = any(unit.dialogue.strip() for unit in group)
        masters.append(MasterSpecV2(
            idx=idx,
            scene_key=group[0].scene_key,
            editorial_duration_sec=editorial_duration,
            generation_duration_sec=generation_duration,
            creative_beat_ids=_unique(unit.beat.beat_id for unit in group),
            character_ids=cast,
            speaker_ids=speakers,
            dialogue="".join(unit.dialogue for unit in group),
            action="；".join(_unique(unit.beat.action for unit in group)),
            preserved_requirements=requirements,
            adaptation_actions=_unique(actions),
            quality_gates=_quality_gates(
                complexity.mode, has_dialogue,
                requires_lip_sync=any(
                    unit.dialogue.strip() and lip_sync_required(unit.beat) for unit in group
                ),
            ),
            depends_on=depends_on,
            estimated_cost_cents=seedance.estimate_cost_cents(
                generation_duration, "480p"
            ),
        ))
        previous_cast = set(cast)
        previous_scene = group[0].scene_key

    master_tuple = tuple(masters)
    frozen_storyboard = _compiled_runtime_storyboard(
        frozen_storyboard, groups, master_tuple,
    )
    nodes, edges = _build_nodes(
        creative, master_tuple, mode=complexity.mode,
        capabilities=capabilities, music_provider=music_provider,
    )
    estimated_cost = sum(node.estimated_cost_cents for node in nodes)
    if estimated_cost > int(budget_cents):
        issues.append(ContractIssue(
            code="budget_confirmation_required",
            severity="decision",
            stage="budget",
            subject=job_id,
            message=(
                f"预计生产成本 {estimated_cost} 分超过当前预算 {int(budget_cents)} 分"
            ),
        ))
    creative_hash = _hash(creative.as_dict())
    payload = {
        "version": 2,
        "job_id": job_id,
        "creative_ir_hash": creative_hash,
        "complexity": complexity.as_dict(),
        "mode": complexity.mode,
        "reliability_commitment": {
            "stable": "standard",
            "enhanced": "assisted",
            "experimental": "best_effort",
        }[complexity.mode],
        "characters": creative.characters,
        "masters": tuple(asdict(master) for master in master_tuple),
        "nodes": tuple(asdict(node) for node in nodes),
        "edges": tuple(asdict(edge) for edge in edges),
        "capabilities": asdict(capabilities),
        "runtime_storyboard": frozen_storyboard,
        "budget_cents": int(budget_cents),
        "estimated_cost_cents": estimated_cost,
    }
    spec = ProductionSpecV2(
        version=2,
        job_id=job_id,
        creative_ir_hash=creative_hash,
        complexity=complexity.as_dict(),
        mode=complexity.mode,
        reliability_commitment=payload["reliability_commitment"],
        characters=creative.characters,
        masters=master_tuple,
        nodes=nodes,
        edges=edges,
        capabilities=capabilities,
        runtime_storyboard=frozen_storyboard,
        budget_cents=int(budget_cents),
        estimated_cost_cents=estimated_cost,
        spec_hash=_hash(payload),
    )
    return ProductionCompilationV2(spec, tuple(issues), required_content)
