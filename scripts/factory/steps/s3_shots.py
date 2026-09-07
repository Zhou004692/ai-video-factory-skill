"""s3: 用固定火山声线驱动 Seedance 2.5 画面与口型。

- presenter dialogue → 角色固定 TTS 音轨 + Seedance 条件画面/口型
- scene dialogue → 无人脸场景 + 角色固定画外音
- broll → 仅画面，不生成随机人声
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil

from .. import (
    ASSETS_DIR, SKILL_ROOT, anchor_generation, asset_library,
    character_assets, character_voices, master_dependencies,
    creative_ir,
    production_config, provider_capacity, provider_health,
    provider_executor as provider_tasks,
    reference_attention, scene_assets, subtitles,
)
from ..db import conn
from ..clients import (
    bigasr,
    ffmpeg,
    seedance,
    seedream_45,
    tos,
    video_qa,
    volc_assets,
    volc_tts,
)
from ..orchestrator import MAX_CONCURRENT_SHOTS
from ..scene_voiceover import faceless_visual_text, non_text_visual_text
from ..video_routing import (
    AUDIO_MODE_FIXED_TTS, VideoRoute, normalize_audio_mode,
    precision_motion_reason, select_route,
)
from ..review_candidates import controlled_candidate_path


@dataclass(frozen=True)
class SeedanceGenerationResult:
    video: bytes
    video_path: Path
    video_url: str
    qa_note: str
    native_audio: bool
    model: str
    audio_path: Path | None
    cost_cents: int
    provider_calls: tuple[dict, ...]
    transcription: bigasr.Transcription = bigasr.Transcription("")


@dataclass(frozen=True)
class DialogueAnchor:
    """A reusable identity reference for later Seedance masters."""

    video_url: str = ""
    duration_sec: float = 0
    image_reference: str | None = None
    anchor_mode: str = "dynamic_video"
    chain_depth: int = 0
    master_idx: int = 0


@dataclass(frozen=True)
class NativeVoiceAnchor:
    """A verified Seedance-native audio track for faceless voice-over."""

    audio_path: Path


@dataclass(frozen=True)
class FixedVoiceSegment:
    """One role-owned line on a master-relative speech timeline."""

    speaker_id: str
    voice_type: str
    text: str
    start_ms: int
    duration_ms: int


@dataclass(frozen=True)
class FixedVoiceTrack:
    audio_path: Path
    transcription: bigasr.Transcription
    voice_types: tuple[str, ...]
    duration_sec: float


def _fixed_voice_segments(
    shot: dict, characters: dict[str, dict]
) -> tuple[FixedVoiceSegment, ...]:
    """Resolve every spoken beat to an already-persisted fixed voice."""
    beats = [
        item for item in (shot.get("performance_beats") or [])
        if str(item.get("dialogue") or "").strip()
    ]
    if not beats:
        beats = [{
            "speaker": shot.get("speaker") or shot.get("speaker_id"),
            "start": 0,
            "duration": shot.get("duration_sec") or 0,
            "dialogue": shot.get("voiceover") or "",
        }]
    segments: list[FixedVoiceSegment] = []
    for beat in beats:
        text = str(beat.get("dialogue") or "").strip()
        if not text:
            continue
        speaker_id = str(
            beat.get("speaker")
            or shot.get("speaker")
            or shot.get("speaker_id")
            or ""
        ).strip()
        character = characters.get(speaker_id) or {}
        voice_type = str(
            character.get("voice_type")
            or (character.get("voice_binding") or {}).get("voice_type")
            or ""
        ).strip()
        if not voice_type:
            display_name = str(
                character.get("script_character_name")
                or character.get("name")
                or speaker_id
                or "未命名角色"
            ).strip()
            raise character_voices.VoiceBindingRequiredError(
                f"角色“{display_name}”尚未绑定固定声线，已在 Seedance "
                "付费调用前停止"
            )
        segments.append(FixedVoiceSegment(
            speaker_id=speaker_id,
            voice_type=voice_type,
            text=text,
            start_ms=max(0, round(float(beat.get("start") or 0) * 1000)),
            duration_ms=max(
                0, round(float(beat.get("duration") or 0) * 1000)
            ),
        ))
    if not segments:
        raise character_voices.VoiceBindingRequiredError(
            "对白母片没有可合成的角色台词"
        )
    return tuple(segments)


def _resolve_speaking_voice_bindings(
    database,
    characters: list[dict],
    generation_units: list[dict],
    *,
    client=None,
) -> bool:
    """Bind every speaking script role once, before any video paid call."""
    character_by_id = {
        str(item.get("id") or "").strip(): item for item in characters
        if str(item.get("id") or "").strip()
    }
    speaking_ids: list[str] = []
    for unit in generation_units:
        narrative_type = str(
            unit.get("narrative_type") or unit.get("type") or ""
        ).casefold()
        if narrative_type != "dialogue":
            continue
        values = list(unit.get("speakers") or [])
        values.append(unit.get("speaker") or unit.get("speaker_id"))
        values.extend(
            beat.get("speaker")
            for beat in (unit.get("performance_beats") or [])
            if isinstance(beat, dict)
        )
        for value in values:
            character_id = str(value or "").strip()
            if character_id and character_id not in speaking_ids:
                speaking_ids.append(character_id)
    created_client = None
    changed = False
    try:
        for character_id in speaking_ids:
            character = character_by_id.get(character_id)
            if character is None:
                raise character_voices.VoiceBindingRequiredError(
                    f"对白角色“{character_id}”不在人物设定中，无法匹配固定声线"
                )
            binding = character_voices.get_binding(database, character_id)
            if binding is None:
                if client is None:
                    created_client = created_client or volc_tts.VolcSpeechClient()
                binding = character_voices.resolve_voice_binding(
                    database, character, client=client or created_client
                )
            new_values = {
                "voice_type": binding["voice_type"],
                "voice_name": binding["voice_name"],
                "voice_resource_id": binding["resource_id"],
                "voice_preview_url": binding["preview_url"],
                "voice_binding_source": binding["source"],
            }
            if any(character.get(key) != value for key, value in new_values.items()):
                character.update(new_values)
                changed = True
        return changed
    finally:
        if created_client is not None:
            created_client.close()


def _timestamp_value(item: dict, *keys: str, default: int = 0) -> int:
    for key in keys:
        try:
            if item.get(key) is not None:
                return max(0, int(float(item[key])))
        except (TypeError, ValueError):
            continue
    return max(0, int(default))


async def _synthesize_fixed_voice_track(
    database,
    *,
    job_id: str,
    shot: dict,
    characters: dict[str, dict],
    paid_batch_id: str | None = None,
) -> FixedVoiceTrack:
    """Create and audit the one exact soundtrack Seedance must follow."""
    segments = _fixed_voice_segments(shot, characters)
    root = ASSETS_DIR / job_id / "voices" / f"master-{int(shot['idx']):02d}"
    root.mkdir(parents=True, exist_ok=True)
    track_path = root / "fixed-voice.wav"
    metadata_path = root / "fixed-voice.json"
    if track_path.is_file() and metadata_path.is_file():
        cached = json.loads(metadata_path.read_text(encoding="utf-8"))
        utterances = tuple(
            bigasr.TimedUtterance(
                str(item["text"]), int(item["start_ms"]), int(item["end_ms"])
            )
            for item in (cached.get("utterances") or [])
        )
        return FixedVoiceTrack(
            track_path,
            bigasr.Transcription(
                str(cached.get("text") or ""),
                min((item.start_ms for item in utterances), default=None),
                max((item.end_ms for item in utterances), default=None),
                utterances,
            ),
            tuple(cached.get("voice_types") or ()),
            float(cached.get("duration_sec") or shot.get("duration_sec") or 0),
        )

    clips: list[tuple[Path, int]] = []
    utterances: list[bigasr.TimedUtterance] = []
    voice_types: list[str] = []
    speech_end_ms = 0
    for index, segment in enumerate(segments, 1):
        character = characters.get(segment.speaker_id) or {}
        intent = f"fixed_voice_tts:{index}"
        safe_speaker = re.sub(
            r"[^A-Za-z0-9_.-]+", "-", segment.speaker_id
        ).strip("-") or "role"
        clip_path = root / f"{index:02d}-{safe_speaker}.mp3"
        segment_metadata_path = root / f"{index:02d}-{safe_speaker}.json"
        reservation_id = ""
        result = None
        try:
            segment_metadata = None
            if clip_path.is_file() and segment_metadata_path.is_file():
                segment_metadata = json.loads(
                    segment_metadata_path.read_text(encoding="utf-8")
                )
                actual_hash = hashlib.sha256(clip_path.read_bytes()).hexdigest()
                expected_identity = {
                    "speaker_id": segment.speaker_id,
                    "voice_type": segment.voice_type,
                    "text": segment.text,
                    "start_ms": segment.start_ms,
                }
                if (
                    any(
                        segment_metadata.get(key) != value
                        for key, value in expected_identity.items()
                    )
                    or segment_metadata.get("audio_sha256") != actual_hash
                    or int(segment_metadata.get("audio_bytes") or 0)
                    != clip_path.stat().st_size
                    or not clip_path.stat().st_size
                ):
                    raise volc_tts.VolcTTSResponseError(
                        "固定声线片段的本地审计证据不一致，已阻止自动重发"
                    )
                reservation_id = str(
                    segment_metadata.get("provider_task_id") or ""
                ).strip()
                if not reservation_id:
                    raise volc_tts.VolcTTSResponseError(
                        "固定声线片段缺少计费任务标识，已阻止自动重发"
                    )
                result = volc_tts.TTSResult(
                    b"",
                    int(segment_metadata.get("duration_ms") or 0),
                    tuple(segment_metadata.get("timestamps") or ()),
                    str(segment_metadata.get("provider_request_id") or ""),
                    dict(segment_metadata.get("usage") or {}),
                )
            else:
                reservation_id = provider_tasks.reserve_paid_submit(
                    database,
                    job_id=job_id,
                    shot_idx=int(shot["idx"]),
                    provider="volc_tts",
                    intent=intent,
                    authorized_batch_id=paid_batch_id,
                    estimated_cost_cents=volc_tts.estimate_cost_cents(
                        segment.text
                    ),
                    budget_limit_cents=0,
                )
                result = await volc_tts.synthesize(
                    text=segment.text,
                    voice_type=segment.voice_type,
                    resource_id=str(
                        character.get("voice_resource_id") or "seed-tts-2.0"
                    ),
                    voice_instruction=str(
                        character.get("voice_directive") or ""
                    ),
                )
                temporary = clip_path.with_suffix(".tmp")
                temporary.write_bytes(result.audio)
                temporary.replace(clip_path)
                segment_metadata = {
                    "speaker_id": segment.speaker_id,
                    "voice_type": segment.voice_type,
                    "text": segment.text,
                    "start_ms": segment.start_ms,
                    "duration_ms": result.duration_ms,
                    "timestamps": list(result.timestamps),
                    "provider_task_id": reservation_id,
                    "provider_request_id": result.request_id,
                    "usage": _safe_provider_usage(result.usage),
                    "audio_bytes": clip_path.stat().st_size,
                    "audio_sha256": hashlib.sha256(
                        clip_path.read_bytes()
                    ).hexdigest(),
                }
                metadata_temporary = segment_metadata_path.with_suffix(".tmp")
                metadata_temporary.write_text(
                    json.dumps(segment_metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                metadata_temporary.replace(segment_metadata_path)
            clips.append((clip_path, segment.start_ms))
            voice_types.append(segment.voice_type)
            timestamps = list(result.timestamps)
            if not timestamps:
                timestamps = [{
                    "text": segment.text,
                    "start_time": 0,
                    "end_time": result.duration_ms or segment.duration_ms,
                }]
            for item in timestamps:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                start_ms = segment.start_ms + _timestamp_value(
                    item, "start_time", "start_ms", "start"
                )
                end_ms = segment.start_ms + _timestamp_value(
                    item,
                    "end_time", "end_ms", "end",
                    default=result.duration_ms or segment.duration_ms,
                )
                if end_ms <= start_ms:
                    end_ms = start_ms + max(
                        1, result.duration_ms or segment.duration_ms
                    )
                utterances.append(
                    bigasr.TimedUtterance(text, start_ms, end_ms)
                )
                speech_end_ms = max(speech_end_ms, end_ms)
            usage = {
                **_safe_provider_usage(result.usage),
                "provider_request_id": result.request_id,
                "voice_type": segment.voice_type,
                "text_chars": len(segment.text),
            }
            delta = provider_tasks.upsert_final_call(
                database,
                task_id=reservation_id,
                job_id=job_id,
                shot_idx=int(shot["idx"]),
                provider="volc_tts",
                intent=intent,
                status="completed",
                usage=usage,
                cost_cents=volc_tts.estimate_cost_cents(segment.text),
            )
            database.execute(
                "UPDATE shots SET cost_cents=cost_cents+? "
                "WHERE job_id=? AND idx=?",
                (delta, job_id, int(shot["idx"])),
            )
            database.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (delta, job_id),
            )
            database.commit()
        except BaseException as exc:
            if reservation_id and (
                result is not None or isinstance(exc, asyncio.CancelledError) or bool(
                getattr(exc, "submit_result_unknown", False)
                )
            ):
                provider_tasks.mark_action_required(
                    database, reservation_id, message=str(exc)
                )
            elif reservation_id:
                provider_tasks.release_reservation(database, reservation_id)
            raise

    duration_sec = max(
        float(shot.get("duration_sec") or 0),
        speech_end_ms / 1000 + 0.25,
    )
    await ffmpeg.assemble_timed_audio(
        clips, track_path, duration_sec=duration_sec
    )
    utterances.sort(key=lambda item: (item.start_ms, item.end_ms))
    transcription = bigasr.Transcription(
        "".join(item.text for item in utterances),
        min((item.start_ms for item in utterances), default=None),
        max((item.end_ms for item in utterances), default=None),
        tuple(utterances),
    )
    metadata = {
        "text": transcription.text,
        "utterances": [
            {"text": item.text, "start_ms": item.start_ms,
             "end_ms": item.end_ms}
            for item in utterances
        ],
        "voice_types": voice_types,
        "duration_sec": duration_sec,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return FixedVoiceTrack(
        track_path, transcription, tuple(voice_types), duration_sec
    )


class SeedanceQualityError(RuntimeError):
    """A failed quality gate that still carries billable provider usage."""

    non_retryable = True

    def __init__(self, message: str, *, cost_cents: int,
                 provider_calls: tuple[dict, ...],
                 reasons: tuple[str, ...] = (),
                 candidate_path: Path | None = None,
                 candidate_url: str | None = None):
        super().__init__(message)
        self.cost_cents = cost_cents
        self.provider_calls = provider_calls
        self.reasons = reasons
        self.candidate_path = candidate_path
        self.candidate_url = candidate_url
        if candidate_path is not None and Path(candidate_path).is_file():
            self.pause_state = "PRODUCTION_REVIEW_REQUIRED"


class SeedanceBudgetError(RuntimeError):
    """The next paid provider call would exceed the confirmed job budget."""

    non_retryable = True
    pause_state = "BUDGET_CONFIRMATION_REQUIRED"
    stable_code = "budget_confirmation_required"

    def __init__(
        self, message: str, *, current_cost_cents: int = 0,
        pending_cost_cents: int = 0, next_estimate_cents: int = 0,
        budget_limit_cents: int = 0,
    ):
        super().__init__(message)
        if not any((
            current_cost_cents, pending_cost_cents,
            next_estimate_cents, budget_limit_cents,
        )):
            parsed = re.search(
                r"current=(\d+),\s*(?:pending|reserved)=(\d+),\s*"
                r"next_estimate=(\d+),\s*limit=(\d+)\s*cents",
                str(message or ""),
                flags=re.IGNORECASE,
            )
            if parsed:
                current_cost_cents = int(parsed.group(1))
                pending_cost_cents = int(parsed.group(2))
                next_estimate_cents = int(parsed.group(3))
                budget_limit_cents = int(parsed.group(4))
        self.current_cost_cents = max(0, int(current_cost_cents or 0))
        self.pending_cost_cents = max(0, int(pending_cost_cents or 0))
        self.next_estimate_cents = max(0, int(next_estimate_cents or 0))
        self.budget_limit_cents = max(0, int(budget_limit_cents or 0))


class SeedanceManualReviewPendingError(RuntimeError):
    """An already-paid candidate is waiting for a human decision."""

    non_retryable = True
    pause_state = "PRODUCTION_REVIEW_REQUIRED"


class ContinuityAnchorReviewPendingError(RuntimeError):
    """Pause production after paid anchors are ready for human approval."""

    non_retryable = True
    pause_state = "ANCHOR_REVIEW_REQUIRED"
    stable_code = "continuity_anchor_review_pending"


def _assert_continuity_anchors_approved(database, job_id: str) -> None:
    pending = database.execute(
        "SELECT display_name AS label FROM character_identity_anchors "
        "WHERE job_id=? AND source_type!='volc_asset' "
        "AND approval_state!='APPROVED' "
        "UNION ALL "
        "SELECT description AS label FROM scene_visual_anchors "
        "WHERE job_id=? AND approval_state!='APPROVED'",
        (job_id, job_id),
    ).fetchall()
    if not pending:
        return
    labels = [str(row["label"] or "未命名锚点") for row in pending]
    raise ContinuityAnchorReviewPendingError(
        "人物与场景锚点等待审核：" + "、".join(labels)
    )


def _apply_anchor_review_mode(database, job_id: str, mode: str) -> int:
    """Auto-approve generated READY anchors or retain the manual review gate."""
    normalized = normalize_anchor_review_mode(mode)
    if normalized == "manual":
        return 0
    character = database.execute(
        "UPDATE character_identity_anchors SET approval_state='APPROVED',"
        "approved_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='READY' "
        # A privacy fallback changes identity method and buys another artifact;
        # ordinary auto-review preference cannot approve this replacement.
        "AND approval_state!='APPROVED' AND source_type!='seedance_identity_fallback'",
        (job_id,),
    )
    scene = database.execute(
        "UPDATE scene_visual_anchors SET approval_state='APPROVED',"
        "approved_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='READY' "
        "AND approval_state!='APPROVED'",
        (job_id,),
    )
    database.commit()
    return int(character.rowcount or 0) + int(scene.rowcount or 0)


def normalize_anchor_review_mode(value: object) -> str:
    """Default generated continuity anchors to explicit human confirmation."""
    normalized = str(value or "manual").strip().casefold()
    if normalized not in {"auto", "manual"}:
        raise ValueError("anchor_review_mode must be auto or manual")
    return normalized


def _is_manual_review_pending(shot: dict) -> bool:
    try:
        qa = json.loads(shot.get("video_qa") or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return qa.get("status") == "manual_review_required"


def _appearance_only(description: str) -> str:
    """Keep visual facts while dropping a custom asset's source-story lore."""
    text = str(description or "").strip()
    for marker in ("真人形象设定为", "形象设定为", "外观设定为"):
        if marker in text:
            return text.split(marker, 1)[1].strip(" ：:；;")
    return re.sub(r"《[^》]+》", "", text).strip()


def _prompt_character_profile(character: dict) -> tuple[str, str, str]:
    """Return script-role identity, production look and safe context."""
    name = str(
        character.get("script_character_name")
        or character.get("name")
        or character.get("id")
        or "角色"
    ).strip()
    fixed = (
        character.get("generation_mode") == "fixed_asset"
        and bool(character.get("script_character_name"))
    )
    if not fixed:
        return (
            name,
            str(character.get("look") or "").strip(),
            str(character.get("background_description") or "").strip(),
        )
    if (
        character.get("identity_type") == "custom"
        or character.get("seedance_reference_mode") == "description_only"
    ):
        appearance = _appearance_only(
            character.get("background_description")
            or character.get("look")
            or ""
        )
        return name, appearance or "按本剧本角色设定生成虚构外貌", ""
    return (
        name,
        "保持授权角色素材中的外貌身份，服装、神态与职业状态服从本剧本场景",
        "",
    )


_HARD_RETRY_REASONS = frozenset({
    "low_confidence",
    "unexpected_speech",
    "transcript_mismatch",
    "lip_sync_mismatch",
    "unexpected_person",
    "unexpected_readable_text",
    "qa_error",
    "identity_drift",
    "scene_drift",
    "blocking_drift",
    "wardrobe_drift",
    "prop_state_mismatch",
})


def _apply_master_transcription(
        storyboard: dict, master_idx: int,
        transcription: bigasr.Transcription) -> bool:
    """Replace estimated edit subtitles with master-relative ASR cues."""
    return subtitles.apply_master_transcription(
        storyboard, master_idx, transcription
    )


def _persist_master_transcription(
        job_id: str, master_idx: int,
        transcription: bigasr.Transcription) -> bool:
    """Persist generated-media timing as the source of truth for editing."""
    db = conn()
    row = db.execute(
        "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not row or not row["storyboard"]:
        return False
    storyboard = json.loads(row["storyboard"])
    if not _apply_master_transcription(
            storyboard, master_idx, transcription):
        return False
    encoded = json.dumps(storyboard, ensure_ascii=False)
    db.execute(
        "UPDATE jobs SET storyboard=? WHERE job_id=?", (encoded, job_id)
    )
    db.commit()
    out_path = ASSETS_DIR / job_id / "storyboard.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


def _env_nonnegative_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _estimated_request_cost_cents(request_kwargs: dict) -> int:
    """Conservative pre-submit estimate from measured Seedance 2.5 calls."""
    if str(request_kwargs.get("task_intent") or "generate") == "edit":
        duration = max(
            1, int(math.ceil(float(
                request_kwargs.get("reference_video_duration_sec") or 30
            )))
        )
    else:
        duration = seedance.normalize_generation_duration(
            request_kwargs.get("duration")
        )
    resolution = str(request_kwargs.get("resolution") or "720p").lower()
    cents_per_second = seedance.estimated_cost_rate_cents_per_second(
        resolution
    )
    reference_duration = 0.0
    if request_kwargs.get("reference_videos"):
        reference_duration = max(
            duration,
            float(request_kwargs.get("reference_video_duration_sec") or 0),
        )
    return round((duration + reference_duration) * cents_per_second)


def _job_budget_cents(storyboard: dict) -> int:
    settings = storyboard.get("production_settings") or {}
    configured = settings.get("budget_cents", settings.get("job_budget_cents"))
    return _env_nonnegative_int(
        "SEEDANCE_JOB_BUDGET_CENTS",
        2500,
    ) if configured is None else max(0, int(configured))


def _assert_paid_call_within_job_budget(
    job_id: str | None,
    *,
    spent_cost_cents: int,
    request_kwargs: dict,
) -> None:
    if not job_id:
        return
    row = conn().execute(
        "SELECT cost_cents, storyboard FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return
    values = dict(row)
    storyboard = json.loads(values.get("storyboard") or "{}")
    limit = _job_budget_cents(storyboard)
    current = max(0, int(values.get("cost_cents") or 0))
    pending = max(0, int(spent_cost_cents or 0))
    estimate = _estimated_request_cost_cents(request_kwargs)
    if limit and current + pending + estimate > limit:
        raise SeedanceBudgetError(
            "Seedance job budget exceeded before paid call: "
            f"current={current}, pending={pending}, "
            f"next_estimate={estimate}, limit={limit} cents",
            current_cost_cents=current,
            pending_cost_cents=pending,
            next_estimate_cents=estimate,
            budget_limit_cents=limit,
        )


def _auto_retry_denial(reasons: tuple[str, ...],
                       projected_retry_cost_cents: int,
                       generation_snapshot: dict | None = None) -> str | None:
    max_auto_retries = (
        int(generation_snapshot["max_auto_retries"])
        if generation_snapshot else _env_nonnegative_int(
            "SEEDANCE_MAX_AUTO_RETRIES", 1
        )
    )
    if max_auto_retries == 0:
        return "automatic retries disabled"
    if not (_HARD_RETRY_REASONS & set(reasons)):
        return "manual review required for soft-only visual mismatch"
    retry_budget = (
        int(generation_snapshot["auto_retry_budget_cents"])
        if generation_snapshot else _env_nonnegative_int(
            "SEEDANCE_AUTO_RETRY_BUDGET_CENTS", 800
        )
    )
    if retry_budget and projected_retry_cost_cents > retry_budget:
        return (
            "retry budget exceeded: "
            f"next_estimate={projected_retry_cost_cents}, "
            f"limit={retry_budget} cents"
        )
    return None


def _aggregate_shot_failures(
        failed: list[tuple[int, BaseException]]) -> RuntimeError:
    msgs = "; ".join(f"#{idx}: {error}" for idx, error in failed)
    aggregate = RuntimeError(f"{len(failed)} shots failed: {msgs}")
    if any(bool(getattr(error, "non_retryable", False))
           for _, error in failed):
        aggregate.non_retryable = True
    stable_codes = {
        str(getattr(error, "stable_code", "") or "").strip()
        for _, error in failed
    } - {""}
    if len(stable_codes) == 1:
        aggregate.stable_code = next(iter(stable_codes))
    pause_states = [
        str(getattr(error, "pause_state", "") or "")
        for _, error in failed
    ]
    if "PROVIDER_ACTION_REQUIRED" in pause_states:
        aggregate.pause_state = "PROVIDER_ACTION_REQUIRED"
    elif pause_states and all(
        state == "BUDGET_CONFIRMATION_REQUIRED" for state in pause_states
    ):
        aggregate.pause_state = "BUDGET_CONFIRMATION_REQUIRED"
        budget_errors = [
            error for _, error in failed
            if str(getattr(error, "pause_state", "") or "")
            == "BUDGET_CONFIRMATION_REQUIRED"
        ]
        if budget_errors:
            aggregate.current_cost_cents = max(
                int(getattr(error, "current_cost_cents", 0) or 0)
                for error in budget_errors
            )
            aggregate.pending_cost_cents = sum(
                int(getattr(error, "pending_cost_cents", 0) or 0)
                for error in budget_errors
            )
            aggregate.next_estimate_cents = sum(
                int(getattr(error, "next_estimate_cents", 0) or 0)
                for error in budget_errors
            )
            aggregate.budget_limit_cents = max(
                int(getattr(error, "budget_limit_cents", 0) or 0)
                for error in budget_errors
            )
    elif pause_states and all(
        state == "PRODUCTION_REVIEW_REQUIRED" for state in pause_states
    ):
        aggregate.pause_state = "PRODUCTION_REVIEW_REQUIRED"
    elif pause_states and all(
        state == "ANCHOR_REVIEW_REQUIRED" for state in pause_states
    ):
        aggregate.pause_state = "ANCHOR_REVIEW_REQUIRED"
    return aggregate


def _attach_provider_usage(exc: BaseException, *, cost_cents: int,
                           provider_calls: list[dict]) -> None:
    """Carry already-paid usage across any exception leaving generation."""
    if not hasattr(exc, "cost_cents"):
        exc.cost_cents = max(0, int(cost_cents or 0))
    if not hasattr(exc, "provider_calls"):
        exc.provider_calls = tuple(provider_calls)


def _safe_provider_usage(usage: object) -> dict:
    """Keep billing counters while dropping provider-controlled payload data."""
    if not isinstance(usage, dict):
        return {}
    allowed = {
        "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
        "duration", "duration_sec", "cost_cents",
    }
    return {
        str(key): value
        for key, value in usage.items()
        if str(key) in allowed
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    }


def _record_provider_calls(db, job_id: str, idx: int, provider: str,
                           provider_calls: tuple[dict, ...],
                           *, fallback_cost_cents: int = 0) -> int:
    """Upsert pending/final provider usage and return only the cost delta."""
    if not provider_calls:
        return max(0, int(fallback_cost_cents or 0))

    new_cost = 0
    for call in provider_calls:
        cost = max(0, int(call.get("cost_cents") or 0))
        task_id = str(call.get("task_id") or "").strip()
        if not task_id:
            # Pre-task failures and test doubles have no stable provider id.
            new_cost += cost
            continue
        usage = call.get("usage") or {}
        new_cost += provider_tasks.upsert_final_call(
            db,
            task_id=task_id,
            job_id=job_id,
            shot_idx=idx,
            provider=provider,
            intent=call.get("intent"),
            status=str(usage.get("status") or "completed"),
            usage=usage,
            cost_cents=cost,
        )
    return new_cost


def _mark_shot_generating(job_id: str, idx: int,
                          provider: str = "seedance") -> None:
    """Expose the active provider call instead of leaving the shot pending."""
    database = conn()
    database.execute(
        "UPDATE shots SET state='GENERATING',provider=?,error=NULL,"
        "video_qa=NULL WHERE job_id=? AND idx=?",
        (provider, job_id, idx),
    )
    database.execute(
        "UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
        (job_id,),
    )
    database.commit()


def _record_failed_generation(db, job_id: str, idx: int,
                              exc: BaseException) -> None:
    error = f"{type(exc).__name__}: {str(exc)[:900]}"
    provider_calls = tuple(getattr(exc, "provider_calls", ()) or ())
    fallback_cost_cents = max(0, int(getattr(exc, "cost_cents", 0) or 0))
    cost = 0
    if provider_calls or fallback_cost_cents:
        cost = _record_provider_calls(
            db, job_id, idx, "seedance", provider_calls,
            fallback_cost_cents=fallback_cost_cents,
        )
    cost += max(
        0, int(getattr(exc, "pre_recorded_provider_cost_cents", 0) or 0)
    )
    candidate_path = controlled_candidate_path(
        ASSETS_DIR, job_id, getattr(exc, "candidate_path", None)
    )
    candidate_url = str(getattr(exc, "candidate_url", None) or "").strip()
    has_candidate = candidate_path is not None
    if isinstance(exc, SeedanceQualityError) and not has_candidate:
        try:
            delattr(exc, "pause_state")
        except AttributeError:
            pass
    if isinstance(exc, SeedanceQualityError) and has_candidate:
        qa_note = json.dumps({
            "status": "manual_review_required",
            "reason": str(exc),
            "reasons": exc.reasons,
            "candidate_path": (
                str(candidate_path) if candidate_path else None
            ),
            "candidate_url": candidate_url or None,
            "provider_calls": exc.provider_calls,
        }, ensure_ascii=False)
        db.execute(
            "UPDATE shots SET state='REVIEW_REQUIRED', error=?, provider='seedance', "
            "video_qa=?, cost_cents=cost_cents+? WHERE job_id=? AND idx=?",
            (error, qa_note, cost, job_id, idx),
        )
        db.execute(
            "UPDATE jobs SET cost_cents=cost_cents+?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (cost, job_id),
        )
    else:
        stable_code = str(getattr(exc, "stable_code", "") or "").strip()
        if stable_code not in {
            "request_invalid", "input_privacy_rejected",
            "output_policy_rejected", "output_copyright_rejected",
            "account_unavailable", "transient_network",
        }:
            stable_code = ""
        failure_note = (
            json.dumps({
                "status": "generation_failed", "failure_code": stable_code,
            }, ensure_ascii=False)
            if stable_code else None
        )
        db.execute(
            "UPDATE shots SET state='FAILED', error=?, video_qa=?, "
            "cost_cents=cost_cents+? WHERE job_id=? AND idx=?",
            (error, failure_note, cost, job_id, idx),
        )
        db.execute(
            "UPDATE jobs SET cost_cents=cost_cents+?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (cost, job_id),
        )
    db.commit()


def _commit_generated_shot(
    job_id: str,
    idx: int,
    result: SeedanceGenerationResult,
) -> None:
    """Atomically finalize provider, shot, and job cost, then close the writer."""
    database = conn()
    try:
        cost = _record_provider_calls(
            database,
            job_id,
            idx,
            "seedance",
            result.provider_calls,
            fallback_cost_cents=result.cost_cents,
        )
        database.execute(
            "UPDATE shots SET video_path=?, video_url=?, state='OK', "
            "provider='seedance', video_model=?, native_audio=?, "
            "video_qa=?, cost_cents=cost_cents+?, error=NULL "
            "WHERE job_id=? AND idx=?",
            (
                str(result.video_path), result.video_url, result.model,
                int(result.native_audio), result.qa_note, cost, job_id, idx,
            ),
        )
        database.execute(
            "UPDATE jobs SET cost_cents=cost_cents+?,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (cost, job_id),
        )
        database.commit()
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def _record_failed_generation_independently(
    job_id: str,
    idx: int,
    exc: BaseException,
) -> None:
    database = conn()
    try:
        _record_failed_generation(database, job_id, idx, exc)
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def _identity_anchor_prompt(character: dict) -> str:
    name = str(
        character.get("script_character_name")
        or character.get("name")
        or character.get("id")
        or "character"
    ).strip()
    appearance = str(
        character.get("look")
        or character.get("background_description")
        or "a distinctive fictional person"
    ).strip()
    return "\n".join([
        "CHARACTER MAKEUP / IDENTITY MASTER.",
        f"Create exactly one fictional person named {name}.",
        f"Locked appearance: {appearance}",
        "Neutral three-quarter medium portrait, then a subtle natural head turn.",
        "Show the same face, age, hairstyle, skin tone, body build and wardrobe "
        "throughout the clip.",
        "Plain softly lit studio background. No other people, no cuts, no text, "
        "no logo, no watermark, no speech and no background music.",
        "This clip is an identity reference for later cinematic scenes, not a "
        "finished story shot.",
    ])


def _identity_portrait_prompt(character: dict) -> str:
    """Build the Seedream 5.0 identity sheet used by every scene master."""
    name = str(
        character.get("script_character_name")
        or character.get("name")
        or character.get("id")
        or "character"
    ).strip()
    appearance = str(
        character.get("look")
        or character.get("background_description")
        or "a distinctive fictional person"
    ).strip()
    return "\n".join([
        "CHARACTER MAKEUP / STATIC IDENTITY REFERENCE.",
        f"Create exactly one fictional person named {name}.",
        f"Locked appearance: {appearance}",
        "Photorealistic vertical casting portrait, neutral three-quarter "
        "standing pose, head to knees, both hands visible.",
        "Preserve one unambiguous face, age, hairstyle, skin tone, body build "
        "and one complete wardrobe design.",
        "Soft neutral studio lighting and plain light-grey background.",
        "No other people, no collage, no split screen, no text, no logo, no "
        "watermark, no props covering the face or hands.",
        "This is a consistency reference for later cinematic scenes, not a "
        "finished story image.",
    ])


def _assert_fixed_cost_within_job_budget(
    job_id: str,
    estimate_cents: int,
) -> None:
    row = conn().execute(
        "SELECT cost_cents, storyboard FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None:
        return
    values = dict(row)
    limit = _job_budget_cents(json.loads(values.get("storyboard") or "{}"))
    current = max(0, int(values.get("cost_cents") or 0))
    if limit and current + estimate_cents > limit:
        raise SeedanceBudgetError(
            "identity image budget exceeded before paid call: "
            f"current={current}, next_estimate={estimate_cents}, "
            f"limit={limit} cents",
            current_cost_cents=current,
            pending_cost_cents=0,
            next_estimate_cents=estimate_cents,
            budget_limit_cents=limit,
        )


async def _submit_paid_image(
    database,
    *,
    job_id: str,
    shot_idx: int,
    provider: str,
    intent: str,
    task_id: str,
    prompt: str,
    resolution: str,
    model: str,
    usage: dict,
    cost_cents: int,
) -> tuple[bytes, str]:
    """Gate one paid image POST and leave its bytes in recoverable staging."""
    provider_health.assert_available(database, "seedance")
    reservation_id = provider_tasks.reserve_paid_submit(
        database,
        job_id=job_id,
        shot_idx=shot_idx,
        provider=provider,
        intent=intent,
    )
    try:
        image = await seedance.text_to_image(
            prompt,
            resolution=resolution,
            model=model,
        )
    except BaseException as exc:
        submit_is_uncertain = (
            isinstance(exc, asyncio.CancelledError)
            or (
                isinstance(exc, seedance.SeedanceSubmissionUnknownError)
                and bool(getattr(exc, "submit_result_unknown", True))
            )
        )
        if submit_is_uncertain:
            provider_tasks.mark_action_required(
                database, reservation_id, message=str(exc)
            )
        else:
            provider_tasks.release_reservation(database, reservation_id)
        if isinstance(exc, seedance.SeedanceAccountUnavailableError):
            provider_health.block(
                database,
                "seedance",
                "account_unavailable",
                "Seedance 账号不可用，请处理账号问题后人工解除本地阻断。",
            )
        raise
    provider_tasks.mark_action_required(
        database,
        reservation_id,
        message="image response received; local asset commit pending",
    )
    return image, reservation_id


def _finalize_paid_image(
    database,
    *,
    reservation_id: str,
    task_id: str,
    job_id: str,
    shot_idx: int,
    provider: str,
    intent: str,
    model: str,
    usage: dict,
    cost_cents: int,
    persist_cost,
) -> int:
    """Atomically commit image billing with its owning anchor/job rows."""
    try:
        delta = provider_tasks.upsert_final_call(
            database,
            task_id=task_id,
            job_id=job_id,
            shot_idx=shot_idx,
            provider=provider,
            intent=intent,
            status="completed",
            usage={"model": model, **dict(usage or {})},
            cost_cents=cost_cents,
        )
        persist_cost(delta)
        provider_tasks.release_reservation(database, reservation_id)
        return delta
    except BaseException as exc:
        database.rollback()
        provider_tasks.mark_action_required(
            database, reservation_id, message=f"local asset commit failed: {exc}"
        )
        raise


async def _extract_identity_preview(video_path: Path, output_dir: Path) -> Path:
    """Only an extracted JPEG may make a newly generated identity reviewable."""
    try:
        previews = await ffmpeg.extract_frames(video_path, output_dir, [1.0])
        preview = Path(previews[0]) if previews else None
        if preview is None or not preview.is_file():
            raise RuntimeError("missing extracted JPEG")
        with preview.open("rb") as preview_file:
            if preview_file.read(3) != b"\xff\xd8\xff":
                raise RuntimeError("empty or invalid extracted JPEG")
        return preview
    except Exception as preview_exc:
        raise RuntimeError(
            f"character identity preview extraction failed: {preview_exc}"
        ) from preview_exc


async def _ensure_character_identity_anchors(
    job_id: str,
    characters: list[dict],
    *,
    storyboard: dict | None = None,
    existing_only: bool = False,
) -> dict[str, DialogueAnchor]:
    """Prepare every non-Volc character before any paid scene generation.

    Authorized real/virtual portrait-library assets are already identity
    anchors. AI-created roles use the task-snapshotted Seedream 5.0 static
    portrait or Seedance 2.5 dynamic identity route. Local custom roles retain
    the privacy-safe dynamic Seedance route. The table makes this stage
    resumable and prevents a second purchase after a worker restart.
    """
    database = conn()
    anchors: dict[str, DialogueAnchor] = {}
    if storyboard is None:
        row = database.execute(
            "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        try:
            storyboard = json.loads(row["storyboard"] or "{}") if row else {}
        except json.JSONDecodeError:
            storyboard = {}
    generation_snapshot = production_config.generation_snapshot(storyboard)
    makeup_plan = anchor_generation.apply_character_generator(
        character_assets.character_makeup_plan(characters),
        storyboard,
        resolution=generation_snapshot["resolution"],
    )
    characters_by_id = {
        str(character.get("id") or ""): character
        for character in characters
    }
    for ordinal, entry in enumerate(makeup_plan, start=1):
        identity_key = entry["identity_key"]
        character_id = entry["character_ids"][0]
        character = characters_by_id[character_id]
        existing = database.execute(
            "SELECT * FROM character_identity_anchors "
            "WHERE job_id=? AND identity_key=?",
            (job_id, identity_key),
        ).fetchone()
        existing_generator = str(existing["generator"] or "") if existing else ""
        if not existing_generator and existing:
            existing_generator = (
                anchor_generation.SEEDREAM_5
                if str(existing["source_type"]).startswith("seedream")
                else anchor_generation.SEEDANCE_25
                if str(existing["source_type"]).startswith("seedance")
                else "volc_asset"
            )
        generator_matches = (
            not entry["requires_paid_call"]
            or existing_generator == entry["generator"]
        )
        if existing and existing["source_type"] == "seedance_identity_fallback":
            compatible_fallback = bool(
                existing["state"] == "READY"
                and existing_generator == anchor_generation.SEEDANCE_25
                and existing["model"] == generation_snapshot["model"]
                and existing["anchor_mode"] == "dynamic_video"
                and existing["video_url"] and existing["video_path"]
                and Path(existing["video_path"]).is_file()
            )
            if compatible_fallback and existing["approval_state"] != "APPROVED":
                raise ContinuityAnchorReviewPendingError(
                    f"人物替换锚点等待审核：{existing['display_name']}"
                )
            # Only this explicitly approved, compatible replacement may
            # override a frozen Seedream generator; generic mismatches cannot.
            generator_matches = compatible_fallback and existing["approval_state"] == "APPROVED"
        if existing and existing["state"] == "READY" and generator_matches:
            if (
                existing["anchor_mode"] == "static_image"
                and existing["image_url"]
                and existing["image_path"]
                and Path(existing["image_path"]).is_file()
            ):
                anchors[identity_key] = DialogueAnchor(
                    image_reference=str(existing["image_url"]),
                    anchor_mode="static_image",
                )
                continue
            if (
                existing["video_url"]
                and existing["video_path"]
                and Path(existing["video_path"]).is_file()
            ):
                anchors[identity_key] = DialogueAnchor(
                    str(existing["video_url"]),
                    float(existing["duration_sec"] or 4),
                    anchor_mode="dynamic_video",
                )
                continue
            if not entry["requires_paid_call"]:
                continue

        if existing_only:
            raise RuntimeError(
                f"frozen character anchor is not ready: {identity_key}"
            )

        if not entry["requires_paid_call"]:
            database.execute(
                "INSERT INTO character_identity_anchors("
                "job_id,identity_key,character_id,display_name,source_type,"
                "state,asset_uri,anchor_mode,duration_sec,approval_state,error) "
                "VALUES(?,?,?,?,?,?,?,?,?,'APPROVED',NULL) "
                "ON CONFLICT(job_id,identity_key) DO UPDATE SET "
                "character_id=excluded.character_id,display_name=excluded.display_name,"
                "source_type=excluded.source_type,state='READY',"
                "asset_uri=excluded.asset_uri,anchor_mode=excluded.anchor_mode,"
                "approval_state='APPROVED',approved_at=CURRENT_TIMESTAMP,error=NULL,"
                "updated_at=CURRENT_TIMESTAMP",
                (
                    job_id, identity_key, character_id, entry["display_name"],
                    entry["source"], "READY", entry["asset_uri"],
                    entry["anchor_mode"], 0,
                ),
            )
            database.commit()
            continue

        database.execute(
            "INSERT INTO character_identity_anchors("
            "job_id,identity_key,character_id,display_name,source_type,state,"
            "anchor_mode,duration_sec,approval_state,error) "
            "VALUES(?,?,?,?,?,'GENERATING',? ,?,'PENDING_REVIEW',NULL) "
            "ON CONFLICT(job_id,identity_key) DO UPDATE SET "
            "character_id=excluded.character_id,display_name=excluded.display_name,"
            "source_type=excluded.source_type,anchor_mode=excluded.anchor_mode,"
            "state='GENERATING',approval_state='PENDING_REVIEW',approved_at=NULL,"
            "review_note='',error=NULL,"
            "updated_at=CURRENT_TIMESTAMP",
            (
                job_id, identity_key, character_id, entry["display_name"],
                entry["source"], entry["anchor_mode"], entry["duration_sec"],
            ),
        )
        database.commit()
        database.execute(
            "UPDATE character_identity_anchors SET generator=?,model=? "
            "WHERE job_id=? AND identity_key=?",
            (entry["generator"], entry["model"], job_id, identity_key),
        )
        database.commit()

        anchor_dir = (
            ASSETS_DIR / job_id / "character_identity"
            / entry["identity_token"]
        )
        anchor_dir.mkdir(parents=True, exist_ok=True)
        if entry["anchor_mode"] == "static_image":
            out_path = anchor_dir / "identity.png"
            image_cost = seedream_45.estimate_cost_cents()
            try:
                reusable_image = bool(
                    existing
                    and existing_generator == entry["generator"]
                    and existing["anchor_mode"] == "static_image"
                    and existing["image_path"]
                    and Path(existing["image_path"]).is_file()
                    and int(existing["cost_cents"] or 0) > 0
                )
                if reusable_image:
                    out_path = Path(existing["image_path"])
                else:
                    _assert_fixed_cost_within_job_budget(job_id, image_cost)
                    identity_image_model = os.getenv(
                        "ARK_IDENTITY_IMAGE_MODEL",
                        "doubao-seedream-5-0-260128",
                    )
                    image, reservation_id = await _submit_paid_image(
                        database,
                        job_id=job_id,
                        shot_idx=-1000 - ordinal,
                        provider="seedream_identity",
                        intent="character_identity_anchor",
                        task_id=(
                            f"seedream-identity:{job_id}:"
                            f"{entry['identity_token']}"
                        ),
                        prompt=_identity_portrait_prompt(character),
                        resolution=os.getenv(
                            "ARK_IDENTITY_IMAGE_SIZE", "1440x2560"
                        ),
                        model=identity_image_model,
                        usage={"anchor_mode": "static_image"},
                        cost_cents=image_cost,
                    )
                    out_path.write_bytes(image)

                    def persist_identity_cost(delta: int) -> None:
                        database.execute(
                            "UPDATE character_identity_anchors SET "
                            "anchor_mode='static_image',image_path=?,"
                            "preview_path=?,duration_sec=0,"
                            "cost_cents=cost_cents+?,error=NULL,"
                            "updated_at=CURRENT_TIMESTAMP "
                            "WHERE job_id=? AND identity_key=?",
                            (
                                str(out_path), str(out_path), delta,
                                job_id, identity_key,
                            ),
                        )
                        database.execute(
                            "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                            (delta, job_id),
                        )

                    _finalize_paid_image(
                        database,
                        reservation_id=reservation_id,
                        task_id=(
                            f"seedream-identity:{job_id}:"
                            f"{entry['identity_token']}"
                        ),
                        job_id=job_id,
                        shot_idx=-1000 - ordinal,
                        provider="seedream_identity",
                        intent="character_identity_anchor",
                        model=identity_image_model,
                        usage={"anchor_mode": "static_image"},
                        cost_cents=image_cost,
                        persist_cost=persist_identity_cost,
                    )
                image_url = tos.upload(
                    out_path,
                    key_hint=f"{job_id}-identity-{entry['identity_token']}",
                )
                database.execute(
                    "UPDATE character_identity_anchors SET state='READY',"
                    "anchor_mode='static_image',image_path=?,image_url=?,"
                    "preview_path=?,video_path=NULL,video_url=NULL,"
                    "duration_sec=0,error=NULL,"
                    "generator=?,model=?,"
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND identity_key=?",
                    (
                        str(out_path), image_url, str(out_path),
                        entry["generator"], entry["model"], job_id, identity_key,
                    ),
                )
                database.commit()
                anchors[identity_key] = DialogueAnchor(
                    image_reference=image_url,
                    anchor_mode="static_image",
                )
            except BaseException as exc:
                database.execute(
                    "UPDATE character_identity_anchors SET state='FAILED',"
                    "error=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND identity_key=?",
                    (str(exc), job_id, identity_key),
                )
                database.commit()
                raise
            continue

        out_path = anchor_dir / "identity.mp4"
        try:
            result = await _generate_seedance_video(
                prompt=_identity_anchor_prompt(character),
                route=VideoRoute(
                    model=generation_snapshot["model"],
                    tier="seedance25",
                    generate_audio=False,
                    qa_required=False,
                    fallback_model=None,
                ),
                request_kwargs={
                    "duration": 4,
                    "resolution": generation_snapshot["resolution"],
                    "output_format": generation_snapshot["output_format"],
                    "ratio": generation_snapshot["ratio"],
                    "camera_fixed": True,
                    "motion_strength": 0.15,
                },
                out_path=out_path,
                key_hint=f"{job_id}-identity-{entry['identity_token']}",
                job_id=job_id,
                shot_idx=-1000 - ordinal,
                billing_provider="seedance_identity",
                expected_text=None,
            )
            cost = _record_provider_calls(
                database,
                job_id,
                -1000 - ordinal,
                "seedance_identity",
                result.provider_calls,
                fallback_cost_cents=result.cost_cents,
            )
            database.execute(
                "UPDATE character_identity_anchors SET state='GENERATING',"
                "video_path=?,video_url=?,preview_path=NULL,duration_sec=4,"
                "cost_cents=cost_cents+?,generator=?,model=?,error=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND identity_key=?",
                (
                    str(result.video_path), result.video_url, cost,
                    entry["generator"], result.model, job_id, identity_key,
                ),
            )
            database.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id),
            )
            database.commit()
            # A charged remote task is durable even if local decoding fails.
            # Never publish a reviewable anchor before its real frame exists.
            preview = await _extract_identity_preview(
                result.video_path, anchor_dir / "preview",
            )
            database.execute(
                "UPDATE character_identity_anchors SET state='READY',preview_path=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
                (str(preview), job_id, identity_key),
            )
            database.commit()
            anchors[identity_key] = DialogueAnchor(
                result.video_url, 4, anchor_mode="dynamic_video"
            )
        except BaseException as exc:
            database.execute(
                "UPDATE character_identity_anchors SET state='FAILED',error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
                (str(exc), job_id, identity_key),
            )
            database.commit()
            raise
    return anchors


async def _fallback_static_identity_anchors(
    job_id: str,
    characters: list[dict],
    identity_anchors: dict[str, DialogueAnchor],
) -> None:
    """Replace static identities with native clips after a privacy rejection.

    Seedance rejects an ordinary reference image before creating the paid
    scene task. The fallback therefore buys one four-second, non-speaking
    native identity master and waits for explicit review before scene retry.
    """
    database = conn()
    job_row = database.execute(
        "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    try:
        storyboard = json.loads(job_row["storyboard"] or "{}") if job_row else {}
    except json.JSONDecodeError:
        storyboard = {}
    generation_snapshot = production_config.generation_snapshot(storyboard)
    seen: set[str] = set()
    for ordinal, character in enumerate(characters, start=1):
        identity_key = character_assets.character_identity_key(character)
        if identity_key in seen:
            continue
        seen.add(identity_key)
        current = identity_anchors.get(identity_key)
        if current is None or current.anchor_mode != "static_image":
            continue
        # The rejected static reference must not survive as usable state if
        # replacement generation or preview extraction fails.
        identity_anchors.pop(identity_key)
        token = character_assets.identity_token(identity_key)
        anchor_dir = ASSETS_DIR / job_id / "character_identity" / token
        anchor_dir.mkdir(parents=True, exist_ok=True)
        out_path = anchor_dir / "identity-fallback.mp4"
        database.execute(
            "UPDATE character_identity_anchors SET state='GENERATING',"
            "source_type='seedance_identity_fallback',"
            "anchor_mode='dynamic_video',approval_state='PENDING_REVIEW',"
            "approved_at=NULL,review_note='',preview_path=NULL,"
            "image_path=NULL,image_url=NULL,generator=?,model=?,error=NULL,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
            (anchor_generation.SEEDANCE_25, generation_snapshot["model"], job_id, identity_key),
        )
        database.commit()
        try:
            result = await _generate_seedance_video(
                prompt=_identity_anchor_prompt(character),
                route=VideoRoute(
                    model=generation_snapshot["model"],
                    tier="seedance25",
                    generate_audio=False,
                    qa_required=False,
                    fallback_model=None,
                ),
                request_kwargs={
                    "duration": 4,
                    "resolution": generation_snapshot["resolution"],
                    "output_format": generation_snapshot["output_format"],
                    "ratio": generation_snapshot["ratio"],
                    "camera_fixed": True,
                    "motion_strength": 0.15,
                },
                out_path=out_path,
                key_hint=f"{job_id}-identity-fallback-{token}",
                job_id=job_id,
                shot_idx=-2000 - ordinal,
                billing_provider="seedance_identity_fallback",
                expected_text=None,
            )
            cost = _record_provider_calls(
                database,
                job_id,
                -2000 - ordinal,
                "seedance_identity_fallback",
                result.provider_calls,
                fallback_cost_cents=result.cost_cents,
            )
            database.execute(
                "UPDATE character_identity_anchors SET state='GENERATING',"
                "source_type='seedance_identity_fallback',"
                "anchor_mode='dynamic_video',video_path=?,video_url=?,"
                "duration_sec=4,cost_cents=cost_cents+?,model=?,error=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND identity_key=?",
                (
                    str(result.video_path), result.video_url, cost, result.model,
                    job_id, identity_key,
                ),
            )
            database.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id),
            )
            database.execute(
                "INSERT INTO events(job_id,step,level,message) "
                "VALUES(?,?,?,?)",
                (
                    job_id, "character_identity_fallback", "warning",
                    f"{character.get('name') or character.get('id')} 的静态定妆照"
                    "触发隐私策略，已切换为 Seedance 动态身份母片",
                ),
            )
            database.commit()
            preview = await _extract_identity_preview(
                result.video_path, anchor_dir / "fallback-preview",
            )
            database.execute(
                "UPDATE character_identity_anchors SET state='READY',preview_path=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
                (str(preview), job_id, identity_key),
            )
            database.commit()
            identity_anchors[identity_key] = DialogueAnchor(
                result.video_url,
                4,
                anchor_mode="dynamic_video",
            )
        except BaseException as exc:
            database.execute(
                "UPDATE character_identity_anchors SET state='FAILED',error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
                (str(exc), job_id, identity_key),
            )
            database.commit()
            raise


async def _ensure_scene_visual_anchors(
    job_id: str,
    storyboard: dict,
    *,
    anchor_keys: set[str] | None = None,
    existing_only: bool = False,
) -> dict[str, str]:
    """Generate the empty-location anchors required by this execution.

    Legacy callers without ``anchor_keys`` retain the conservative repeated-
    scene plan.  A compiled graph passes explicit keys; those are frozen
    execution requirements and must be materialized even for a single master.
    Seedream returns the image directly. Seedance creates a four-second silent
    location clip and the canonical image contract uses its extracted frame.
    """
    database = conn()
    generation_snapshot = production_config.generation_snapshot(storyboard)
    resolved: dict[str, str] = {}
    normalized_keys = {
        str(value or "").strip().casefold() for value in (anchor_keys or set())
        if str(value or "").strip()
    }
    anchor_specs = (
        scene_assets.required_scene_anchors(storyboard, normalized_keys)
        if anchor_keys is not None
        else scene_assets.recurring_scene_anchors(storyboard)
    )
    if anchor_keys is not None and normalized_keys != {spec["anchor_key"] for spec in anchor_specs}:
        raise ValueError("required scene anchor keys do not resolve in storyboard")
    for ordinal, spec in enumerate(anchor_specs, start=1):
        spec = anchor_generation.decorate_scene_anchor(
            spec,
            storyboard,
            resolution=generation_snapshot["resolution"],
        )
        key = spec["anchor_key"]
        existing = database.execute(
            "SELECT * FROM scene_visual_anchors WHERE job_id=? AND anchor_key=?",
            (job_id, key),
        ).fetchone()
        existing_generator = str(existing["generator"] or "") if existing else ""
        if not existing_generator and existing:
            existing_generator = anchor_generation.SEEDREAM_5
        if (
            existing and existing["state"] == "READY"
            and existing_generator == spec["generator"]
            and existing["image_url"] and existing["image_path"]
            and Path(existing["image_path"]).is_file()
        ):
            resolved[key] = str(existing["image_url"])
            continue
        if existing_only:
            raise RuntimeError(f"frozen scene anchor is not ready: {key}")
        database.execute(
            "INSERT INTO scene_visual_anchors("
            "job_id,anchor_key,description,master_indexes,state,approval_state,error) "
            "VALUES(?,?,?,?, 'GENERATING','PENDING_REVIEW',NULL) "
            "ON CONFLICT(job_id,anchor_key) DO UPDATE SET "
            "description=excluded.description,master_indexes=excluded.master_indexes,"
            "state='GENERATING',approval_state='PENDING_REVIEW',approved_at=NULL,"
            "review_note='',error=NULL,updated_at=CURRENT_TIMESTAMP",
            (
                job_id, key, spec["description"],
                json.dumps(spec["master_indexes"]),
            ),
        )
        database.commit()
        database.execute(
            "UPDATE scene_visual_anchors SET generator=?,model=? "
            "WHERE job_id=? AND anchor_key=?",
            (spec["generator"], spec["model"], job_id, key),
        )
        database.commit()
        anchor_dir = ASSETS_DIR / job_id / "scene_identity"
        anchor_dir.mkdir(parents=True, exist_ok=True)
        out_path = anchor_dir / f"{scene_assets.anchor_token(key)}.png"
        anchor_cost = spec["estimated_cost_cents"]
        try:
            reusable = bool(
                existing and existing["image_path"]
                and existing_generator == spec["generator"]
                and Path(existing["image_path"]).is_file()
                and int(existing["cost_cents"] or 0) > 0
            )
            if reusable:
                out_path = Path(existing["image_path"])
            elif spec["generator"] == anchor_generation.SEEDANCE_25:
                video_path = anchor_dir / f"{scene_assets.anchor_token(key)}.mp4"
                result = await _generate_seedance_video(
                    prompt=scene_assets.scene_anchor_prompt(spec),
                    route=VideoRoute(
                        model=spec["model"], tier="seedance25",
                        generate_audio=False, qa_required=False,
                        fallback_model=None,
                    ),
                    request_kwargs={
                        "duration": 4,
                        "resolution": generation_snapshot["resolution"],
                        "output_format": generation_snapshot["output_format"],
                        "ratio": generation_snapshot["ratio"], "camera_fixed": True,
                        "motion_strength": 0.1,
                    },
                    out_path=video_path,
                    key_hint=f"{job_id}-scene-video-{scene_assets.anchor_token(key)}",
                    job_id=job_id,
                    shot_idx=-2000 - ordinal,
                    billing_provider="seedance_scene",
                    forbid_people=True,
                    expected_text=None,
                )
                frames = await ffmpeg.extract_frames(
                    result.video_path, anchor_dir / f"{scene_assets.anchor_token(key)}-preview",
                    [1.0],
                )
                if not frames:
                    raise RuntimeError("Seedance 场景锚点母片未能提取稳定参考帧")
                out_path = Path(frames[0])
                cost = _record_provider_calls(
                    database, job_id, -2000 - ordinal, "seedance_scene",
                    result.provider_calls, fallback_cost_cents=result.cost_cents,
                )
                database.execute(
                    "UPDATE scene_visual_anchors SET video_path=?,video_url=?,"
                    "image_path=?,preview_path=?,cost_cents=cost_cents+?,"
                    "generator=?,model=? WHERE job_id=? AND anchor_key=?",
                    (
                        str(result.video_path), result.video_url,
                        str(out_path), str(out_path), cost,
                        spec["generator"], result.model, job_id, key,
                    ),
                )
                database.execute(
                    "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                    (cost, job_id),
                )
                database.commit()
            else:
                _assert_fixed_cost_within_job_budget(job_id, anchor_cost)
                image, reservation_id = await _submit_paid_image(
                    database,
                    job_id=job_id,
                    shot_idx=-2000 - ordinal,
                    provider="seedream_scene",
                    intent="scene_visual_anchor",
                    task_id=f"seedream-scene:{job_id}:{key}",
                    prompt=scene_assets.scene_anchor_prompt(spec),
                    resolution=os.getenv("ARK_SCENE_IMAGE_SIZE", "1440x2560"),
                    model=spec["model"],
                    usage={"anchor_key": key},
                    cost_cents=anchor_cost,
                )
                out_path.write_bytes(image)

                def persist_scene_cost(delta: int) -> None:
                    database.execute(
                        "UPDATE scene_visual_anchors SET image_path=?,preview_path=?,"
                        "cost_cents=cost_cents+? WHERE job_id=? AND anchor_key=?",
                        (str(out_path), str(out_path), delta, job_id, key),
                    )
                    database.execute(
                        "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                        (delta, job_id),
                    )

                _finalize_paid_image(
                    database,
                    reservation_id=reservation_id,
                    task_id=f"seedream-scene:{job_id}:{key}",
                    job_id=job_id,
                    shot_idx=-2000 - ordinal,
                    provider="seedream_scene",
                    intent="scene_visual_anchor",
                    model=spec["model"],
                    usage={"anchor_key": key},
                    cost_cents=anchor_cost,
                    persist_cost=persist_scene_cost,
                )
            image_url = tos.upload(
                out_path,
                key_hint=f"{job_id}-scene-{scene_assets.anchor_token(key)}",
            )
            database.execute(
                "UPDATE scene_visual_anchors SET state='READY',image_path=?,"
                "image_url=?,preview_path=?,generator=?,model=?,error=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND anchor_key=?",
                (
                    str(out_path), image_url, str(out_path), spec["generator"],
                    spec["model"], job_id, key,
                ),
            )
            database.commit()
            resolved[key] = image_url
        except BaseException as exc:
            database.execute(
                "UPDATE scene_visual_anchors SET state='FAILED',error=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND anchor_key=?",
                (str(exc), job_id, key),
            )
            database.commit()
            raise
    return resolved


def _apply_dialogue_reference_manifest(
    request: dict,
    *,
    shot: dict,
    snapshot: dict,
    voice_reference: Path | str | None,
    character: dict | None,
    cast_characters: list[dict] | None,
    identity_anchors: dict[str, DialogueAnchor],
    scene_reference: str | None,
    scene_video_anchor: DialogueAnchor | None,
) -> None:
    candidates: list[dict] = []
    if scene_video_anchor is not None and scene_video_anchor.video_url:
        candidates.append(reference_attention.candidate(
            scene_video_anchor.video_url,
            kind="video",
            duty="scene_continuity",
            duration_sec=scene_video_anchor.duration_sec,
        ))
    if scene_reference:
        candidates.append(reference_attention.candidate(
            scene_reference,
            kind="image",
            duty=(
                "site_continuity"
                if shot.get("_spatial_anchor_scope") == "site"
                else "scene_continuity"
            ),
        ))

    def add_identity(member: dict, duty: str) -> None:
        identity_key = character_assets.character_identity_key(member)
        identity = identity_anchors.get(identity_key)
        if identity is not None and identity.image_reference:
            candidates.append(reference_attention.candidate(
                identity.image_reference, kind="image", duty=duty,
                identity_key=identity_key,
            ))
            return
        if identity is not None and identity.video_url:
            candidates.append(reference_attention.candidate(
                identity.video_url, kind="video", duty=duty,
                identity_key=identity_key, duration_sec=identity.duration_sec,
            ))
            return
        asset_uri = str(
            member.get("seedance_asset_uri")
            or member.get("asset_uri")
            or _roster_character(member.get("id")).get("seedance_asset_uri")
            or ""
        ).strip()
        if asset_uri.startswith("asset://"):
            candidates.append(reference_attention.candidate(
                asset_uri, kind="image", duty=duty, identity_key=identity_key,
            ))

    if character:
        add_identity(character, "speaker_identity")
    for cast_character in cast_characters or []:
        add_identity(cast_character, "supporting_identity")
    if voice_reference is not None:
        candidates.append(reference_attention.candidate(
            str(voice_reference), kind="audio", duty="voice_continuity"
        ))

    known = {item["uri"] for item in candidates}
    for uri in request.get("reference_videos") or []:
        if uri not in known:
            candidates.append(reference_attention.candidate(
                uri, kind="video", duty="style_continuity",
                duration_sec=float(request.get("reference_video_duration_sec") or 0),
                required=False,
            ))
    for uri in request.get("references") or []:
        if uri not in known:
            candidates.append(reference_attention.candidate(
                uri, kind="image", duty="speaker_identity"
            ))
    for uri in request.get("reference_audios") or []:
        if uri not in known:
            candidates.append(reference_attention.candidate(
                uri, kind="audio", duty="voice_continuity"
            ))

    manifest = reference_attention.select_manifest(
        candidates,
        max_video_references=production_reference_video_budget(snapshot),
    )
    request["reference_manifest"] = manifest
    videos = [item for item in manifest if item["kind"] == "video"]
    images = [item for item in manifest if item["kind"] == "image"]
    audios = [item for item in manifest if item["kind"] == "audio"]
    if videos:
        request["reference_videos"] = [item["uri"] for item in videos]
        request["reference_video_duration_sec"] = sum(
            float(item["duration_sec"] or 0) for item in videos
        )
    else:
        request.pop("reference_videos", None)
        request.pop("reference_video_duration_sec", None)
    if images:
        request["references"] = [item["uri"] for item in images]
    else:
        request.pop("references", None)
    if audios:
        request["reference_audios"] = [item["uri"] for item in audios]
    else:
        request.pop("reference_audios", None)


def _dialogue_request_kwargs(
    shot: dict,
    anchor: DialogueAnchor | None,
    *,
    fixed_voice_audio: Path | None = None,
    native_voice_anchor: NativeVoiceAnchor | None = None,
    character: dict | None = None,
    cast_characters: list[dict] | None = None,
    identity_anchors: dict[str, DialogueAnchor] | None = None,
    scene_reference: str | None = None,
    scene_video_anchor: DialogueAnchor | None = None,
) -> dict:
    snapshot = production_config.snapshot_from_unit(shot) or {}
    request = {
        "duration": seedance.normalize_generation_duration(
            shot["duration_sec"]
        ),
        "resolution": snapshot.get("resolution") or os.getenv(
            "ARK_VIDEO_RESOLUTION", "720p"
        ),
        "output_format": snapshot.get("output_format", "mp4"),
        "ratio": snapshot.get("ratio", "9:16"),
        "camera_fixed": False,
        "motion_strength": 0.4,
    }
    voice_reference = (
        fixed_voice_audio
        if fixed_voice_audio is not None
        else native_voice_anchor.audio_path if native_voice_anchor else None
    )
    if voice_reference is not None:
        request["reference_audios"] = [str(voice_reference)]
    video_references: list[DialogueAnchor] = []
    asset_references: list[str] = []
    identity_anchors = identity_anchors or {}
    if scene_reference:
        asset_references.append(scene_reference)

    if scene_video_anchor is not None:
        video_references.append(scene_video_anchor)

    if character:
        identity_anchor = identity_anchors.get(
            character_assets.character_identity_key(character)
        )
        if identity_anchor is not None:
            if identity_anchor.image_reference:
                asset_references.append(identity_anchor.image_reference)
            elif identity_anchor.video_url:
                video_references.append(identity_anchor)
        elif character.get("generation_mode") == "ai_generated":
            pass
        else:
            roster_character = _roster_character(character.get("id"))
            identity_type = (
                character.get("identity_type")
                or roster_character.get("identity_type")
            )
            asset_uri = (
                character.get("seedance_asset_uri")
                or roster_character.get("seedance_asset_uri")
            )
            if identity_type in {"real_person", "virtual_person"}:
                if not str(asset_uri or "").startswith("asset://"):
                    asset_kind = (
                        "virtual-portrait" if identity_type == "virtual_person"
                        else "real-person"
                    )
                    raise SeedanceIdentityAuthorizationError(
                        f"{asset_kind} fixed character requires an authorized "
                        "Seedance portrait-library asset:// URI before any paid "
                        "generation"
                    )
                asset_references.append(str(asset_uri))

    for cast_character in cast_characters or []:
        cast_anchor = identity_anchors.get(
            character_assets.character_identity_key(cast_character)
        )
        if cast_anchor is not None:
            if cast_anchor.image_reference:
                asset_references.append(cast_anchor.image_reference)
            elif cast_anchor.video_url:
                video_references.append(cast_anchor)
            continue
        asset_uri = str(
            cast_character.get("seedance_asset_uri")
            or cast_character.get("asset_uri")
            or ""
        ).strip()
        if asset_uri.startswith("asset://"):
            asset_references.append(asset_uri)

    deduped_videos: list[DialogueAnchor] = []
    seen_video_urls: set[str] = set()
    for reference in video_references:
        if not reference.video_url:
            continue
        if reference.video_url in seen_video_urls:
            continue
        seen_video_urls.add(reference.video_url)
        deduped_videos.append(reference)
    if deduped_videos:
        reference_budget = production_reference_video_budget(snapshot)
        if len(deduped_videos) > reference_budget:
            raise SeedanceIdentityAuthorizationError(
                "one scene exceeds the product attention budget of "
                f"{reference_budget} reference videos; Seedance 2.5 supports "
                "a higher provider limit, but this scene must be split to "
                "avoid identity and continuity dilution before paid generation"
            )
        request["reference_videos"] = [
            reference.video_url for reference in deduped_videos
        ]
        request["reference_video_duration_sec"] = sum(
            reference.duration_sec for reference in deduped_videos
        )
    if asset_references:
        request["references"] = list(dict.fromkeys(asset_references))

    if character and not (
        request.get("reference_videos") or request.get("references")
    ):
        if character.get("generation_mode") == "ai_generated":
            return request
        roster_character = _roster_character(character.get("id"))
        identity_type = (
            character.get("identity_type")
            or roster_character.get("identity_type")
        )
        asset_uri = (
            character.get("seedance_asset_uri")
            or roster_character.get("seedance_asset_uri")
        )
        if identity_type in {"real_person", "virtual_person"}:
            if not str(asset_uri or "").startswith("asset://"):
                asset_kind = (
                    "virtual-portrait" if identity_type == "virtual_person"
                    else "real-person"
                )
                raise SeedanceIdentityAuthorizationError(
                    f"{asset_kind} fixed character requires an authorized "
                    "Seedance portrait-library asset:// URI before any paid "
                    "generation"
                )
            request["references"] = [str(asset_uri)]
            return request

        # Local custom-character photos stay in the asset library, but an
        # ordinary reference_image containing a photorealistic face is not an
        # authorized Seedance portrait asset. Build the first native master
        # from the reviewed role description; later masters reuse that clip.
        if identity_type == "custom":
            return request

        portrait = character.get("image_path")
        reference_image = (
            character.get("reference_image")
            or roster_character.get("reference_image")
        )
        if not portrait and reference_image:
            candidate = Path(str(reference_image))
            portrait = candidate if candidate.is_absolute() else SKILL_ROOT / candidate
        uploaded_references = [
            str(path) for path in character.get("reference_images", [])
            if Path(str(path)).exists()
        ]
        if uploaded_references:
            request["references"] = uploaded_references[:9]
        elif portrait and Path(portrait).exists():
            request["references"] = [str(portrait)]
    _apply_dialogue_reference_manifest(
        request,
        shot=shot,
        snapshot=snapshot,
        voice_reference=voice_reference,
        character=character,
        cast_characters=cast_characters,
        identity_anchors=identity_anchors,
        scene_reference=scene_reference,
        scene_video_anchor=scene_video_anchor,
    )
    return request


def _scene_cast_characters(
    shot: dict,
    characters: dict[str, dict],
    speaker_id: str,
) -> list[dict]:
    """Resolve only the people who actually appear in this master.

    When a storyboard does not carry a structured cast list, the source-video
    restoration path still names people in ``scene``/``action``.  Matching
    those names is safer than the previous behaviour of injecting every story
    character into every shot.
    """
    background_voiceover = shot.get("render_mode") == "background_voiceover"
    explicit = list(shot.get("cast") or [])
    if not background_voiceover:
        explicit.extend(shot.get("speakers") or [])
        for block in shot.get("dialogue_blocks") or []:
            if isinstance(block, dict) and block.get("speaker"):
                explicit.append(block["speaker"])
    explicit_names: set[str] = set()
    for item in explicit:
        if isinstance(item, dict):
            explicit_names.update(
                str(item.get(field) or "").strip().casefold()
                for field in ("id", "name", "script_character_name")
                if str(item.get(field) or "").strip()
            )
        elif str(item or "").strip():
            explicit_names.add(str(item).strip().casefold())

    haystack = " ".join(
        str(shot.get(field) or "")
        for field in ("scene", "action", "scene_dynamics", "visual_intent")
    ).casefold()
    resolved: list[dict] = []
    seen: set[str] = set()
    explicit_on_screen = {
        str(beat.get("speaker") or "").strip().casefold()
        for beat in (shot.get("performance_beats") or [])
        if isinstance(beat, dict)
        and str(beat.get("speaker") or "").strip()
        and creative_ir.normalize_speech_mode(
            beat.get("speech_mode"),
            speaker=str(beat.get("speaker") or "").strip(),
        ) == "on_screen"
    }
    if background_voiceover:
        # Compiled visibility is authoritative even for names such as narrator.
        explicit_on_screen.update(explicit_names)
    for character_id, character in characters.items():
        if character_id == speaker_id:
            continue
        if (
            character_id.casefold() not in explicit_on_screen
            and creative_ir.normalize_speech_mode(
                None, speaker=character_id
            ) != "on_screen"
        ):
            continue
        names = {
            str(value).strip().casefold()
            for value in (
                character_id,
                character.get("name"),
                character.get("script_character_name"),
            )
            if str(value or "").strip()
        }
        included = bool(names & explicit_names) if explicit_names or background_voiceover else any(
            name in haystack for name in names
        )
        if not included:
            continue
        identity_key = character_assets.character_identity_key(character)
        if identity_key in seen:
            continue
        seen.add(identity_key)
        resolved.append(character)
    return resolved


class SeedanceIdentityAuthorizationError(RuntimeError):
    """A fixed real person has not been authorized in Seedance's portrait library."""


def _roster_character(character_id: str | None) -> dict:
    """Load optional fixed-character metadata without making YAML mandatory."""
    if not character_id:
        return {}
    try:
        from ..prompts import character_roster
        return character_roster().get(character_id, {})
    except (OSError, ValueError, TypeError):
        return {}


def _resolve_real_person_bindings(database, characters: list[dict]) -> bool:
    """Resolve every missing real-person binding before a paid call."""
    pending: list[dict] = []
    for character in characters:
        if character.get("generation_mode") == "ai_generated":
            continue
        roster = _roster_character(character.get("id"))
        identity_type = character.get("identity_type") or roster.get(
            "identity_type"
        )
        asset_uri = character.get("seedance_asset_uri") or roster.get(
            "seedance_asset_uri"
        )
        if identity_type == "real_person" and not str(asset_uri or "").startswith(
            "asset://"
        ):
            pending.append(character)
    if not pending:
        return False

    try:
        api_client = volc_assets.from_environment()
    except volc_assets.AssetApiUnavailable:
        api_client = None

    changed = False
    for character in pending:
        result = character_assets.resolve_character_asset(
            database,
            character_id=str(character.get("id") or ""),
            display_name=str(character.get("name") or character.get("id") or ""),
            api_client=api_client,
        )
        if result.status != "resolved":
            raise SeedanceIdentityAuthorizationError(
                result.user_prompt or "real-person assetId is required"
            )
        character["seedance_asset_uri"] = result.asset_uri
        changed = True
    return changed


def _dialogue_anchor_from_completed_shot(
        shot: dict) -> DialogueAnchor | None:
    video_url = str(shot.get("video_url") or "").strip()
    video_path = shot.get("video_path")
    if not video_url or not video_path:
        return None
    if not Path(video_path).exists():
        return None
    return DialogueAnchor(
        video_url,
        seedance.normalize_generation_duration(shot.get("duration_sec")),
        chain_depth=1,
        master_idx=int(shot.get("idx") or 0),
    )


def _continuity_reset_interval() -> int:
    try:
        return max(1, int(os.getenv("SEEDANCE_CONTINUITY_RESET_INTERVAL", "3")))
    except (TypeError, ValueError):
        return 3


def _incremental_anchor(
        anchor: DialogueAnchor | None) -> DialogueAnchor | None:
    """Limit incremental video chaining so visual errors cannot compound."""
    if anchor is None:
        return None
    if anchor.chain_depth >= _continuity_reset_interval():
        return None
    return anchor


def _continuity_qa_context(
    shot: dict,
    character: dict,
    *,
    cast_characters: list[dict] | None = None,
    identity_anchors: dict[str, DialogueAnchor] | None = None,
    scene_reference: str | None = None,
    scene_video_anchor: DialogueAnchor | None = None,
    speaker_anchor: DialogueAnchor | None = None,
) -> dict | None:
    """Build QA evidence with canonical refs taking precedence over history."""
    identity_anchors = identity_anchors or {}
    image_urls: list[str] = []
    for member in [character, *(cast_characters or [])]:
        identity = identity_anchors.get(
            character_assets.character_identity_key(member)
        )
        image_url = str(
            identity.image_reference if identity is not None else ""
        ).strip()
        if image_url.startswith(("https://", "http://")):
            image_urls.append(image_url)
    image_urls = list(dict.fromkeys(image_urls))
    scene_url = str(scene_reference or "").strip()
    if shot.get("_spatial_anchor_scope") == "site":
        scene_url = ""
    previous = scene_video_anchor or speaker_anchor
    contract = shot.get("continuity_contract") or {}
    if not contract and not image_urls and not scene_url and previous is None:
        return None
    return {
        "contract": contract,
        "identity_image_urls": image_urls,
        "scene_image_url": (
            scene_url if scene_url.startswith(("https://", "http://")) else None
        ),
        "previous_video_url": (
            previous.video_url if previous and previous.video_url else None
        ),
    }


def _voice_anchor_from_completed_shot(
        shot: dict) -> NativeVoiceAnchor | None:
    video_path = shot.get("video_path")
    if not video_path:
        return None
    audio_path = Path(video_path).with_suffix(".wav")
    return NativeVoiceAnchor(audio_path) if audio_path.exists() else None


def _scene_voiceover_request_kwargs(
        shot: dict, anchor: NativeVoiceAnchor | None) -> dict:
    snapshot = production_config.snapshot_from_unit(shot) or {}
    request = {
        "duration": seedance.normalize_generation_duration(
            shot["duration_sec"]
        ),
        "resolution": snapshot.get("resolution") or os.getenv(
            "ARK_VIDEO_RESOLUTION", "720p"
        ),
        "output_format": snapshot.get("output_format", "mp4"),
        "ratio": "9:16",
    }
    if anchor is not None:
        request["reference_audios"] = [str(anchor.audio_path)]
    return request


def production_reference_video_budget(snapshot: dict | None = None) -> int:
    """Return the product attention budget, capped by provider capability.

    Seedance 2.5 accepts up to ten reference videos.  That provider maximum is
    not a recommended prompt size: identity, voice, scene and camera references
    compete for attention.  Production keeps a smaller configurable guard and
    reports it honestly as a product budget.
    """
    raw = (
        snapshot.get("reference_video_budget")
        if snapshot else os.getenv("SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET", "3")
    )
    try:
        requested = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET must be an integer"
        ) from exc
    if requested <= 0:
        raise ValueError(
            "SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET must be positive"
        )
    provider_limit = int(
        seedance.capability_profile(seedance.DEFAULT_MODEL)[
            "reference_limits"
        ]["reference_video"]
    )
    return min(requested, provider_limit)


def _broll_request_kwargs(
    shot: dict,
    *,
    cast_characters: list[dict] | None = None,
    identity_anchors: dict[str, DialogueAnchor] | None = None,
    scene_reference: str | None = None,
) -> dict:
    snapshot = production_config.snapshot_from_unit(shot) or {}
    request = {
        "duration": seedance.normalize_generation_duration(
            shot["duration_sec"]
        ),
        "resolution": snapshot.get("resolution") or os.getenv(
            "ARK_VIDEO_RESOLUTION", "720p"
        ),
        "output_format": snapshot.get("output_format", "mp4"),
        "ratio": snapshot.get("ratio", "9:16"),
    }
    image_references: list[str] = []
    video_references: list[DialogueAnchor] = []
    if scene_reference:
        image_references.append(scene_reference)
    for character in cast_characters or []:
        anchor = (identity_anchors or {}).get(
            character_assets.character_identity_key(character)
        )
        if anchor is not None:
            if anchor.image_reference:
                image_references.append(anchor.image_reference)
            elif anchor.video_url:
                video_references.append(anchor)
            continue
        asset_uri = str(
            character.get("seedance_asset_uri")
            or character.get("asset_uri")
            or ""
        ).strip()
        if asset_uri.startswith("asset://"):
            image_references.append(asset_uri)
    deduped_videos: list[DialogueAnchor] = []
    seen_videos: set[str] = set()
    for anchor in video_references:
        if anchor.video_url and anchor.video_url not in seen_videos:
            seen_videos.add(anchor.video_url)
            deduped_videos.append(anchor)
    reference_budget = production_reference_video_budget(snapshot)
    if len(deduped_videos) > reference_budget:
        raise SeedanceIdentityAuthorizationError(
            "one B-roll scene exceeds the product attention budget of "
            f"{reference_budget} reference videos; split the scene before "
            "paid generation"
        )
    if deduped_videos:
        request["reference_videos"] = [
            anchor.video_url for anchor in deduped_videos
        ]
        request["reference_video_duration_sec"] = sum(
            anchor.duration_sec for anchor in deduped_videos
        )
    if image_references:
        request["references"] = list(dict.fromkeys(image_references))
    candidates: list[dict] = []
    if scene_reference:
        candidates.append(reference_attention.candidate(
            scene_reference,
            kind="image",
            duty=(
                "site_continuity"
                if shot.get("_spatial_anchor_scope") == "site"
                else "scene_continuity"
            ),
        ))
    for character in cast_characters or []:
        identity_key = character_assets.character_identity_key(character)
        anchor = (identity_anchors or {}).get(identity_key)
        if anchor is not None and anchor.image_reference:
            candidates.append(reference_attention.candidate(
                anchor.image_reference, kind="image",
                duty="supporting_identity", identity_key=identity_key,
            ))
        elif anchor is not None and anchor.video_url:
            candidates.append(reference_attention.candidate(
                anchor.video_url, kind="video",
                duty="supporting_identity", identity_key=identity_key,
                duration_sec=anchor.duration_sec,
            ))
        else:
            asset_uri = str(
                character.get("seedance_asset_uri")
                or character.get("asset_uri") or ""
            ).strip()
            if asset_uri.startswith("asset://"):
                candidates.append(reference_attention.candidate(
                    asset_uri, kind="image", duty="supporting_identity",
                    identity_key=identity_key,
                ))
    manifest = reference_attention.select_manifest(
        candidates, max_video_references=reference_budget
    )
    request["reference_manifest"] = manifest
    videos = [item for item in manifest if item["kind"] == "video"]
    images = [item for item in manifest if item["kind"] == "image"]
    if videos:
        request["reference_videos"] = [item["uri"] for item in videos]
        request["reference_video_duration_sec"] = sum(
            float(item["duration_sec"] or 0) for item in videos
        )
    if images:
        request["references"] = [item["uri"] for item in images]
    return request


def _lip_sync_edit_request_kwargs(shot: dict) -> dict:
    """Build a Seedance 2.5 edit request around one existing paid master."""
    source = str(shot.get("repair_source_path") or "").strip()
    if not source:
        raise ValueError("口型局部修复缺少原始母片")
    source_url = str(shot.get("repair_source_url") or "").strip()
    reference = source_url if source_url.startswith("https://") else source
    duration = max(0.1, float(shot.get("duration_sec") or 0))
    snapshot = production_config.snapshot_from_unit(shot) or {}
    request = {
        "duration": -1,
        "resolution": snapshot.get("resolution") or os.getenv(
            "ARK_VIDEO_RESOLUTION", "720p"
        ),
        "ratio": "adaptive",
        "task_intent": "edit",
        # Seedance recommends MOV for edit/extend continuity. The managed
        # pipeline converts it to a browser-safe MP4 before QA and preview.
        "output_format": "mov",
        "reference_videos": [reference],
        "reference_video_duration_sec": duration,
    }
    return request


def _extend_request_kwargs(shot: dict) -> dict:
    """Build a Seedance 2.5 continuation request for one controlled master."""
    source = str(shot.get("repair_source_path") or "").strip()
    if not source:
        raise ValueError("延长母片缺少受控原始母片")
    try:
        extension = float(shot.get("extend_duration_sec") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("延长秒数必须在 4 到 30 秒之间") from exc
    if extension < 4 or extension > 30:
        raise ValueError("延长秒数必须在 4 到 30 秒之间")
    source_duration = max(0.1, float(shot.get("duration_sec") or 0.1))
    snapshot = production_config.snapshot_from_unit(shot) or {}
    return {
        "duration": seedance.normalize_generation_duration(extension),
        "resolution": snapshot.get("resolution") or os.getenv(
            "ARK_VIDEO_RESOLUTION", "720p"
        ),
        "output_format": "mov",
        "ratio": "adaptive",
        "task_intent": "extend",
        "reference_videos": [source],
        "reference_video_duration_sec": source_duration,
        "reference_manifest": reference_attention.select_manifest([
            reference_attention.candidate(
                source, kind="video", duty="scene_continuity",
                duration_sec=source_duration,
            )
        ], max_video_references=1),
    }


def _build_extend_prompt(shot: dict) -> str:
    direction = str(shot.get("extend_direction") or "").strip()
    if not direction:
        raise ValueError("请填写母片延长后的明确动作、运镜和结束状态")
    duration = seedance.normalize_generation_duration(
        shot.get("extend_duration_sec") or 0
    )
    return "\n".join([
        "延长现有母片，不重拍、不改写原片。",
        "素材职责：视频1只负责承接原片末帧的场景、人物身份、站位、动作惯性、光线和镜头轴线。",
        f"延长方向：{direction}",
        f"0-{duration:g}秒：从原片最后一帧无跳切继续动作和运镜，并自然收束。",
        "不得复述原片台词，不得新增旁白、第二人声、字幕、文字、标识或水印。",
        "人物五官、服装、场景布局、光线方向和摄影风格必须与原片连续。",
    ])


def _display_second(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _build_lip_sync_edit_prompt(shot: dict) -> str:
    """Preserve a cinematic master while editing only one lip-sync interval."""
    duration = max(0.1, float(shot.get("duration_sec") or 0))
    start = max(0.0, float(shot.get("repair_start_sec") or 0))
    end = min(duration, float(shot.get("repair_end_sec") or duration))
    if end <= start:
        raise ValueError("口型修复结束时间必须晚于开始时间")
    dialogue = str(
        shot.get("repair_dialogue") or shot.get("voiceover") or ""
    ).strip()
    if not dialogue:
        raise ValueError("口型局部修复缺少目标台词")
    reason = str(shot.get("repair_direction") or "口型与原生语音不同步").strip()
    material_duties = (
        "素材职责：@视频1同时提供原画面与唯一原声音轨，禁止替换或重构声音。"
    )
    return "\n".join([
        f"仅编辑@视频1中{_display_second(start)}-{_display_second(end)}秒的人物嘴部动作。",
        material_duties,
        f"目标台词只用于口型校准：{{台词：{dialogue}}}",
        "嘴唇、牙齿、舌位和下颌必须与唯一时间基准中的中文语音逐字同步，句末自然闭嘴。",
        "保留@视频1的原始声音、音色、语速、表情、身体动作、运镜、人物身份、"
        "服装、场景、灯光、构图、时长和镜头切换，其余均保持不变。",
        "不得重新配音，不得改变人物走位或摄影机运动，不得新增字幕、文字、人物或声音。",
        f"修复原因：{reason}。",
    ])


def _native_audio_prompt(prompt: str, enabled: bool,
                         expected_text: str | None = None) -> str:
    visual_contract = (
        "\n\n画面文字通道关闭：画面中不得生成黄色或其他颜色的悬浮文案、"
        "字幕、标题、贴纸、标牌、数字、字母、界面或水印；"
        "所有台词只进入原生音轨，任何可读文字都属于失败结果。"
    )
    hardened_prompt = f"{prompt}{visual_contract}"
    if not enabled or expected_text:
        return hardened_prompt
    return (
        f"{hardened_prompt}\n\n音频要求：只生成与画面动作匹配的环境音和物件音效；"
        "不得出现任何人声、对白、旁白、哼唱或背景音乐。"
    )


def _quality_retry_prompt(prompt: str, reasons: tuple[str, ...]) -> str:
    if "unexpected_readable_text" not in set(reasons):
        return prompt
    return (
        f"{prompt}\n\n定向返修：上一次画面出现了不应存在的可读文字。"
        "本次必须移除全部悬浮字幕、黄色文案、标题、标牌、数字、字母、"
        "水印与界面元素；相关区域改为纯色无字或符合场景的自然纹理，"
        "不要通过模糊、遮挡或变形文字规避。台词仅保留在音轨中。"
    )


async def _generate_seedance_video(*, prompt: str, route: VideoRoute,
                                    request_kwargs: dict,
                                    out_path: Path,
                                    key_hint: str,
                                    job_id: str | None = None,
                                    shot_idx: int = 0,
                                    database=None,
                                    billing_provider: str = "seedance",
                                    forbid_people: bool = False,
                                    expected_text: str | None = None,
                                    require_lip_sync: bool = True,
                                    require_broll_semantics: bool = False,
                                    continuity_context: dict | None = None,
                                    paid_batch_id: str | None = None,
                                    asr_enabled: bool = True,
                                     fixed_audio_path: Path | None = None,
                                     fixed_transcription: bigasr.Transcription | None = None,
                                     generation_snapshot: dict | None = None,
                                     ) -> SeedanceGenerationResult:
    """Generate, upload and gate a Seedance shot.

    Visuals fail closed. Dialogue uses one pre-generated fixed Volcengine TTS
    track both as Seedance reference audio and as the final muxed soundtrack;
    Seedance is never allowed to choose the role's voice.
    """
    spent_cost_cents = 0
    pre_recorded_cost_cents = 0
    provider_calls: list[dict] = []
    health_database = database or (conn() if job_id else None)
    candidate_path = out_path.with_name(
        f".{out_path.stem}.candidate{out_path.suffix}"
    )

    def update_staged_artifact(task_id: str, artifact_path: Path) -> None:
        if health_database is None or not task_id:
            return
        row = health_database.execute(
            "SELECT usage_json FROM provider_calls WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return
        raw_usage = row["usage_json"] if hasattr(row, "keys") else row[0]
        try:
            usage = json.loads(raw_usage or "{}")
        except (json.JSONDecodeError, TypeError):
            usage = {}
        if str(usage.get("status") or "").casefold() != "staged":
            return
        usage["artifact_path"] = str(artifact_path)
        health_database.execute(
            "UPDATE provider_calls SET usage_json=? WHERE task_id=?",
            (json.dumps(usage, ensure_ascii=False), task_id),
        )
        health_database.commit()

    def latest_task_id() -> str:
        return next((
            str(call.get("task_id") or "")
            for call in reversed(provider_calls)
            if call.get("task_id")
        ), "")

    def commit_candidate(
        video_path: Path, audio_path: Path | None
    ) -> tuple[Path, Path | None]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.replace(out_path)
        update_staged_artifact(latest_task_id(), out_path)
        if audio_path is None:
            return out_path, None
        final_audio = out_path.with_suffix(".wav")
        if audio_path.resolve() == final_audio.resolve():
            return out_path, final_audio
        if audio_path.exists() and fixed_audio_path is not None:
            shutil.copy2(audio_path, final_audio)
        elif audio_path.exists():
            audio_path.replace(final_audio)
        return out_path, final_audio

    def discard_candidate() -> None:
        candidate_path.unlink(missing_ok=True)
        candidate_path.with_suffix(".wav").unlink(missing_ok=True)

    def preserve_candidate() -> Path:
        review_dir = out_path.parent / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"{out_path.stem}-candidate{out_path.suffix}"
        review_path.unlink(missing_ok=True)
        candidate_path.replace(review_path)
        update_staged_artifact(latest_task_id(), review_path)
        candidate_audio = candidate_path.with_suffix(".wav")
        if candidate_audio.exists():
            review_audio = review_path.with_suffix(".wav")
            review_audio.unlink(missing_ok=True)
            candidate_audio.replace(review_audio)
        return review_path

    def preserve_retry_attempt() -> Path:
        task_id = latest_task_id()
        token = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-")
        attempt_dir = out_path.parent / "review" / "attempts"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_path = attempt_dir / (
            f"{out_path.stem}-{token or 'attempt'}{out_path.suffix}"
        )
        attempt_path.unlink(missing_ok=True)
        candidate_path.replace(attempt_path)
        candidate_audio = candidate_path.with_suffix(".wav")
        if candidate_audio.exists():
            attempt_audio = attempt_path.with_suffix(".wav")
            attempt_audio.unlink(missing_ok=True)
            candidate_audio.replace(attempt_audio)
        update_staged_artifact(task_id, attempt_path)
        return attempt_path

    async def generate(
        model: str,
        generate_audio: bool,
        *,
        generation_prompt: str = prompt,
        generation_kwargs: dict | None = None,
        target_path: Path = candidate_path,
        upload_hint: str = key_hint,
    ) -> tuple[bytes, Path, str, Path | None]:
        nonlocal spent_cost_cents, pre_recorded_cost_cents
        metadata: dict = {}
        call_kwargs = dict(generation_kwargs or request_kwargs)
        try:
            configured_capacity_limit = max(1, int(
                (generation_snapshot or {}).get("max_concurrent_masters")
                or os.getenv("SEEDANCE_MAX_CONCURRENT_MASTERS")
                or MAX_CONCURRENT_SHOTS
            ))
        except (TypeError, ValueError):
            configured_capacity_limit = max(1, int(MAX_CONCURRENT_SHOTS))
        _assert_paid_call_within_job_budget(
            job_id,
            spent_cost_cents=spent_cost_cents,
            request_kwargs=call_kwargs,
        )
        if health_database is not None:
            provider_health.assert_available(health_database, "seedance")
            provider_capacity.assert_submit_allowed(
                health_database, "seedance"
            )
        provider_kwargs = dict(call_kwargs)
        provider_kwargs.pop("reference_video_duration_sec", None)
        reference_audit = [
            {
                "kind": str(item.get("kind") or ""),
                "duty": str(item.get("duty") or ""),
                "material_index": int(item.get("material_index") or 0),
            }
            for item in provider_kwargs.pop("reference_manifest", [])
            if isinstance(item, dict)
        ]
        intent = call_kwargs.get("task_intent", "generate")
        deferred_completed: tuple[str, str, dict] | None = None
        attempt_task_ids: set[str] = {
            str(call.get("task_id"))
            for call in provider_calls
            if call.get("task_id")
        }

        def persist_pending_task(task_id: str) -> None:
            nonlocal reservation_id
            pending_call = {
                "task_id": task_id,
                "intent": intent,
                "usage": {
                    "status": "pending",
                    "reference_manifest": reference_audit,
                },
                "cost_cents": 0,
            }
            provider_calls.append(pending_call)
            if health_database is not None and job_id:
                if reservation_id:
                    provider_tasks.persist_remote_task_id(
                        health_database,
                        reservation_id,
                        remote_task_id=task_id,
                        provider=billing_provider,
                        intent=str(intent),
                    )
                    provider_tasks.promote_reservation(
                        health_database,
                        reservation_id,
                        task_id=task_id,
                        job_id=job_id,
                        shot_idx=shot_idx,
                        provider=billing_provider,
                        intent=str(intent),
                    )
                    reservation_id = None
                else:
                    _record_provider_calls(
                        health_database,
                        job_id,
                        shot_idx,
                        billing_provider,
                        (pending_call,),
                    )
                    health_database.commit()
            attempt_task_ids.add(task_id)

        def _persist_task_state(task_id: str, status: str, data: dict) -> None:
            nonlocal spent_cost_cents, pre_recorded_cost_cents
            raw_usage = data.get("usage") if isinstance(data, dict) else {}
            usage = _safe_provider_usage(raw_usage)
            usage["status"] = status
            if reference_audit:
                usage["reference_manifest"] = reference_audit
            if status == "staged" and isinstance(data, dict):
                usage.update({
                    "remote_status": "completed",
                    "artifact_path": str(data.get("artifact_path") or ""),
                    "artifact_url": str(data.get("artifact_url") or ""),
                })
            reported_cost = seedance.usage_cost_cents(usage)
            for source in (data, raw_usage):
                if not isinstance(source, dict):
                    continue
                try:
                    reported_cost = max(
                        reported_cost, int(source.get("cost_cents") or 0)
                    )
                except (TypeError, ValueError):
                    continue
            state_call = {
                "task_id": task_id,
                "intent": intent,
                "usage": usage,
                "cost_cents": max(0, reported_cost),
            }
            if status != "staged":
                existing = next((
                    call for call in reversed(provider_calls)
                    if call.get("task_id") == task_id
                ), None)
                if existing is None:
                    provider_calls.append(state_call)
                else:
                    existing.update(state_call)
            if health_database is not None and job_id:
                database_call = state_call
                if status in {"completed", "staged"}:
                    database_call = {**state_call, "cost_cents": 0}
                delta = _record_provider_calls(
                    health_database,
                    job_id,
                    shot_idx,
                    billing_provider,
                    (database_call,),
                )
                health_database.commit()
                if status != "completed":
                    spent_cost_cents += delta
                pre_recorded_cost_cents += delta

        def persist_task_state(task_id: str, status: str, data: dict) -> None:
            nonlocal deferred_completed
            if status == "completed":
                deferred_completed = (task_id, status, data)
                return
            _persist_task_state(task_id, status, data)

        reservation_id: str | None = None
        if health_database is not None and job_id:
            budget_row = health_database.execute(
                "SELECT storyboard FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            budget_limit = _job_budget_cents(json.loads(
                budget_row[0] or "{}"
            )) if budget_row else 0
            try:
                reservation_id = provider_tasks.reserve_paid_submit(
                    health_database,
                    job_id=job_id,
                    shot_idx=shot_idx,
                    provider=billing_provider,
                    intent=str(intent),
                    authorized_task_ids=tuple(attempt_task_ids),
                    authorized_batch_id=paid_batch_id,
                    estimated_cost_cents=_estimated_request_cost_cents(
                        call_kwargs
                    ),
                    budget_limit_cents=budget_limit,
                )
            except provider_tasks.ProviderTaskBudgetError as exc:
                raise SeedanceBudgetError(str(exc)) from exc
        try:
            video = await seedance.text_to_video(
                prompt=_native_audio_prompt(
                    generation_prompt, generate_audio, expected_text
                ),
                model=model,
                generate_audio=generate_audio,
                metadata_out=metadata,
                on_task_created=persist_pending_task,
                on_task_updated=persist_task_state,
                **provider_kwargs,
            )
            if reservation_id:
                remote_task_id = str(metadata.get("task_id") or "").strip()
                if remote_task_id:
                    provider_tasks.promote_reservation(
                        health_database,
                        reservation_id,
                        task_id=remote_task_id,
                        job_id=job_id,
                        shot_idx=shot_idx,
                        provider=billing_provider,
                        intent=str(intent),
                    )
                else:
                    provider_tasks.release_reservation(
                        health_database, reservation_id
                    )
                reservation_id = None
            call_cost = seedance.usage_cost_cents(metadata.get("usage") or {})
            if not call_cost:
                fallback_duration = float(
                    request_kwargs.get("reference_video_duration_sec")
                    if str(request_kwargs.get("task_intent") or "generate") == "edit"
                    else request_kwargs.get("duration") or 0
                )
                call_cost = seedance.estimate_cost_cents(
                    fallback_duration,
                    request_kwargs.get("resolution", "720p"),
                )
            spent_cost_cents += call_cost
            final_call = {
                "task_id": metadata.get("task_id"),
                "intent": intent,
                "usage": {
                    **_safe_provider_usage(metadata.get("usage") or {}),
                    "status": "completed",
                    "reference_manifest": reference_audit,
                },
                "cost_cents": call_cost,
            }
            pending_call = None
            if final_call["task_id"]:
                pending_call = next(
                    (
                        call for call in reversed(provider_calls)
                        if call.get("task_id") == final_call["task_id"]
                        and (call.get("usage") or {}).get("status") == "pending"
                    ),
                    None,
                )
            if pending_call is None:
                provider_calls.append(final_call)
            else:
                pending_call.update(final_call)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            provider_output = target_path
            if str(call_kwargs.get("output_format") or "mp4").casefold() == "mov":
                provider_output = target_path.with_name(
                    f".{target_path.stem}.provider.mov"
                )
            provider_output.write_bytes(video)
            if provider_output != target_path:
                await ffmpeg.transcode_edit_mov_to_mp4(
                    provider_output, target_path
                )
                video = target_path.read_bytes()
            audio_path = fixed_audio_path
            if fixed_audio_path is not None:
                visual_path = target_path.with_name(
                    f".{target_path.stem}.visual{target_path.suffix}"
                )
                visual_path.unlink(missing_ok=True)
                target_path.replace(visual_path)
                try:
                    await ffmpeg.mux_video_audio(
                        visual_path, fixed_audio_path, target_path
                    )
                finally:
                    visual_path.unlink(missing_ok=True)
                video = target_path.read_bytes()
            elif generate_audio:
                audio_path = target_path.with_suffix(".wav")
                await ffmpeg.extract_audio_wav(target_path, audio_path)
            url = tos.upload(target_path, key_hint=upload_hint)
            if deferred_completed is not None:
                completed_task_id, _status, completed_data = deferred_completed
                _persist_task_state(
                    completed_task_id,
                    "staged",
                    {
                        **completed_data,
                        "artifact_path": str(target_path),
                        "artifact_url": url,
                    },
                )
            elif final_call["task_id"]:
                _persist_task_state(
                    str(final_call["task_id"]),
                    "staged",
                    {
                        "usage": metadata.get("usage") or {},
                        "artifact_path": str(target_path),
                        "artifact_url": url,
                    },
                )
            if provider_output != target_path:
                provider_output.unlink(missing_ok=True)
            return video, target_path, url, audio_path
        except BaseException as exc:
            if reservation_id and health_database is not None:
                submit_is_uncertain = (
                    isinstance(exc, asyncio.CancelledError)
                    or (
                        isinstance(exc, seedance.SeedanceSubmissionUnknownError)
                        and bool(getattr(exc, "submit_result_unknown", True))
                    )
                )
                if submit_is_uncertain:
                    provider_tasks.mark_action_required(
                        health_database, reservation_id, message=str(exc)
                    )
                else:
                    provider_tasks.release_reservation(
                        health_database, reservation_id
                    )
            elif health_database is not None:
                remote_task_id = str(metadata.get("task_id") or "").strip()
                if remote_task_id:
                    remote_row = health_database.execute(
                        "SELECT usage_json FROM provider_calls WHERE task_id=?",
                        (remote_task_id,),
                    ).fetchone()
                    try:
                        raw_remote_usage = (
                            remote_row["usage_json"]
                            if remote_row is not None and hasattr(remote_row, "keys")
                            else remote_row[0] if remote_row else "{}"
                        )
                        remote_status = str(json.loads(
                            raw_remote_usage
                        ).get("status") or "").casefold()
                    except (json.JSONDecodeError, TypeError):
                        remote_status = "action_required"
                    if remote_status in provider_tasks.UNRESOLVED_STATUSES:
                        provider_tasks.mark_action_required(
                            health_database, remote_task_id, message=str(exc)
                        )
            if (
                health_database is not None
                and isinstance(exc, seedance.SeedanceAccountUnavailableError)
            ):
                provider_health.block(
                    health_database,
                    "seedance",
                    "account_unavailable",
                    "Seedance 账号不可用，请处理账号问题后人工解除本地阻断。",
                )
            if (
                health_database is not None
                and isinstance(exc, seedance.SeedanceRateLimitError)
            ):
                provider_capacity.record_rate_limit(
                    health_database,
                    "seedance",
                    configured_limit=configured_capacity_limit,
                )
            _attach_provider_usage(
                exc,
                cost_cents=spent_cost_cents,
                provider_calls=provider_calls,
            )
            if pre_recorded_cost_cents:
                exc.pre_recorded_provider_cost_cents = pre_recorded_cost_cents
            raise

    async def assess(url: str, audio_path: Path | None
                     ) -> tuple[dict, video_qa.QADecision,
                                bigasr.Transcription]:
        transcription = fixed_transcription or bigasr.Transcription("")
        try:
            raw, _ = await asyncio.to_thread(
                video_qa.analyze,
                url,
                expect_speech=bool(expected_text),
                require_lip_sync=require_lip_sync,
                expected_visual=prompt,
                forbid_people=forbid_people,
                forbid_readable_text=True,
                require_broll_semantics=require_broll_semantics,
            )
            transcript = transcription.text
            if (
                fixed_transcription is None
                and audio_path is not None
                and asr_enabled
            ):
                transcription = await asyncio.to_thread(
                    bigasr.transcribe_detailed, audio_path
                )
                transcript = transcription.text
            raw["transcript"] = transcript
            raw["confidence"] = 1.0
            decision = video_qa.evaluate(
                raw,
                expect_speech=bool(expected_text) and asr_enabled,
                require_lip_sync=require_lip_sync,
                expected_text=expected_text if asr_enabled else None,
                expected_visual=prompt,
                forbid_people=forbid_people,
                forbid_readable_text=True,
                require_broll_semantics=require_broll_semantics,
            )
            if continuity_context:
                continuity_raw, continuity_decision = await asyncio.to_thread(
                    video_qa.analyze_continuity,
                    url,
                    contract=continuity_context.get("contract") or {},
                    identity_image_urls=continuity_context.get(
                        "identity_image_urls"
                    ) or [],
                    scene_image_url=continuity_context.get("scene_image_url"),
                    previous_video_url=continuity_context.get(
                        "previous_video_url"
                    ),
                )
                raw["continuity"] = continuity_raw
                if not continuity_decision.passed:
                    decision = video_qa.QADecision(
                        False,
                        tuple(dict.fromkeys([
                            *decision.reasons,
                            *continuity_decision.reasons,
                        ])),
                        decision.transcript_similarity,
                    )
            return raw, decision, transcription
        except Exception as exc:
            return (
                {"notes": f"qa_error: {exc}"},
                video_qa.QADecision(False, ("qa_error",)),
                transcription,
            )

    video, video_path, video_url, audio_path = await generate(
        route.model, route.generate_audio
    )
    if not route.qa_required:
        video_path, audio_path = commit_candidate(video_path, audio_path)
        return SeedanceGenerationResult(
            video, video_path, video_url,
            json.dumps({
                "status": "not_required",
                "provider_calls": provider_calls,
            }, ensure_ascii=False),
            route.generate_audio, route.model, audio_path, spent_cost_cents,
            tuple(provider_calls), fixed_transcription or bigasr.Transcription(""),
        )

    raw, decision, transcription = await assess(video_url, audio_path)
    qa_note = json.dumps(
        {
            "result": raw,
            "reasons": decision.reasons,
            "provider_calls": provider_calls,
        },
        ensure_ascii=False,
    )

    if decision.passed or (
        decision.reasons == ("qa_error",) and not require_broll_semantics
    ):
        if decision.reasons == ("qa_error",):
            qa_note = json.dumps({
                "status": "manual_review_required",
                "result": raw,
                "reasons": decision.reasons,
                "provider_calls": provider_calls,
            }, ensure_ascii=False)
        video_path, audio_path = commit_candidate(video_path, audio_path)
        return SeedanceGenerationResult(
            video, video_path, video_url, qa_note, route.generate_audio, route.model,
            audio_path, spent_cost_cents, tuple(provider_calls),
            transcription,
        )

    if require_broll_semantics:
        review_path = preserve_candidate()
        raise SeedanceQualityError(
            "Seedance 2.5 B-roll semantic quality gate requires manual review; "
            "auto retry skipped; reasons=" + ",".join(decision.reasons),
            cost_cents=spent_cost_cents,
            provider_calls=tuple(provider_calls),
            reasons=decision.reasons,
            candidate_path=review_path,
            candidate_url=video_url,
        )

    projected_retry_cost = (
        int(provider_calls[-1]["cost_cents"])
        if provider_calls else _estimated_request_cost_cents(request_kwargs)
    )
    denial = _auto_retry_denial(
        decision.reasons, projected_retry_cost, generation_snapshot
    )
    if denial is not None:
        # A transcript mismatch is a quality warning, not proof that the
        # already-paid master is unusable. Preserve it for the mandatory
        # final human review instead of dropping the file and blocking all
        # later masters merely because an automatic retry is too expensive.
        if set(decision.reasons) == {"transcript_mismatch"}:
            qa_note = json.dumps({
                "status": "manual_review_required",
                "result": raw,
                "reasons": decision.reasons,
                "auto_retry_skipped": denial,
                "provider_calls": provider_calls,
            }, ensure_ascii=False)
            return SeedanceGenerationResult(
                video, video_path, video_url, qa_note,
                route.generate_audio, route.model, audio_path,
                spent_cost_cents, tuple(provider_calls), transcription,
            )
        review_path = preserve_candidate()
        raise SeedanceQualityError(
            "Seedance 2.5 quality gate requires manual review; "
            f"auto retry skipped: {denial}; reasons="
            f"{','.join(decision.reasons)}",
            cost_cents=spent_cost_cents,
            provider_calls=tuple(provider_calls),
            reasons=decision.reasons,
            candidate_path=review_path,
            candidate_url=video_url,
        )

    preserve_retry_attempt()
    video, video_path, video_url, audio_path = await generate(
        route.model,
        route.generate_audio,
        generation_prompt=_quality_retry_prompt(prompt, decision.reasons),
    )
    retry_raw, retry_decision, retry_transcription = await assess(
        video_url, audio_path
    )
    if not retry_decision.passed:
        if not (_HARD_RETRY_REASONS & set(retry_decision.reasons)):
            review_path = preserve_candidate()
            raise SeedanceQualityError(
                "Seedance 2.5 quality gate requires manual review after retry; "
                f"reasons={','.join(retry_decision.reasons)}",
                cost_cents=spent_cost_cents,
                provider_calls=tuple(provider_calls),
                reasons=retry_decision.reasons,
                candidate_path=review_path,
                candidate_url=video_url,
            )
        discard_candidate()
        raise SeedanceQualityError(
            "Seedance 2.5 quality gate failed after retry: "
            f"{','.join(retry_decision.reasons)}",
            cost_cents=spent_cost_cents,
            provider_calls=tuple(provider_calls),
            reasons=retry_decision.reasons,
        )
    video_path, audio_path = commit_candidate(video_path, audio_path)
    return SeedanceGenerationResult(
        video=video,
        video_path=video_path,
        video_url=video_url,
        qa_note=(
            f"retry model={route.model}; first={qa_note}; "
            f"final={json.dumps(retry_raw, ensure_ascii=False)}; "
            f"provider_calls={json.dumps(provider_calls, ensure_ascii=False)}"
        ),
        native_audio=route.generate_audio,
        model=route.model,
        audio_path=audio_path,
        cost_cents=spent_cost_cents,
        provider_calls=tuple(provider_calls),
        transcription=retry_transcription,
    )


async def _gen_dialogue(job_id: str, shot: dict, char: dict, sb_shot: dict,
                        sem: asyncio.Semaphore,
                        anchor: DialogueAnchor | None = None,
                        *,
                        fixed_voice_track: FixedVoiceTrack | None = None,
                        native_voice_anchor: NativeVoiceAnchor | None = None,
                        cast_characters: list[dict] | None = None,
                        identity_anchors: dict[str, DialogueAnchor] | None = None,
                        scene_reference: str | None = None,
                        scene_video_anchor: DialogueAnchor | None = None,
                        paid_batch_id: str | None = None,
                        asr_enabled: bool = True,
                        ) -> tuple[int, Exception | None,
                                   DialogueAnchor | None]:
    """Generate one dialogue shot using the task-selected audio strategy."""
    async with sem:
        try:
            _mark_shot_generating(job_id, shot["idx"])
            combined = {**shot, **sb_shot}
            combined["voiceover"] = (
                sb_shot.get("voiceover") or shot.get("voiceover") or ""
            )
            repair_mode = str(combined.get("repair_mode") or "").strip()
            lip_sync_edit = repair_mode == "seedance_lip_sync_edit"
            extend_mode = repair_mode == "seedance_extend"
            if fixed_voice_track and not repair_mode:
                combined["duration_sec"] = fixed_voice_track.duration_sec
            speaker_anchor = _incremental_anchor(anchor)
            previous_scene_anchor = _incremental_anchor(scene_video_anchor)
            continuity_context = _continuity_qa_context(
                combined,
                char,
                cast_characters=cast_characters,
                identity_anchors=identity_anchors,
                scene_reference=scene_reference,
                scene_video_anchor=previous_scene_anchor,
                speaker_anchor=speaker_anchor,
            )
            route = select_route(shot, sb_shot)
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            if repair_mode:
                out_path = (
                    ASSETS_DIR / job_id / "shots" / "review" / "repairs"
                    / f"{shot['idx']:02d}-{repair_mode}.mp4"
                )
            if lip_sync_edit:
                prompt = _build_lip_sync_edit_prompt(combined)
                request_kwargs = _lip_sync_edit_request_kwargs(combined)
            elif extend_mode:
                prompt = _build_extend_prompt(combined)
                request_kwargs = _extend_request_kwargs(combined)
            else:
                prompt = _build_native_dialogue_prompt(
                    combined,
                    char,
                    anchored=(
                        speaker_anchor is not None
                        or character_assets.character_identity_key(char)
                        in (identity_anchors or {})
                    ),
                    voice_anchored=bool(
                        fixed_voice_track or native_voice_anchor
                    ),
                    scene_anchored=bool(scene_reference),
                    previous_scene_anchored=previous_scene_anchor is not None,
                )
                request_kwargs = _dialogue_request_kwargs(
                    combined, speaker_anchor,
                    fixed_voice_audio=(
                        fixed_voice_track.audio_path
                        if fixed_voice_track else None
                    ),
                    native_voice_anchor=native_voice_anchor,
                    character=char,
                    cast_characters=cast_characters,
                    identity_anchors=identity_anchors,
                    scene_reference=scene_reference,
                    scene_video_anchor=previous_scene_anchor,
                )
                prompt = _build_native_dialogue_prompt(
                    combined,
                    char,
                    anchored=(
                        speaker_anchor is not None
                        or character_assets.character_identity_key(char)
                        in (identity_anchors or {})
                    ),
                    voice_anchored=bool(
                        fixed_voice_track or native_voice_anchor
                    ),
                    scene_anchored=bool(scene_reference),
                    previous_scene_anchored=previous_scene_anchor is not None,
                    reference_manifest=request_kwargs.get("reference_manifest"),
                )

            async def generate_scene() -> SeedanceGenerationResult:
                return await _generate_seedance_video(
                    prompt=prompt,
                    route=route,
                    request_kwargs=request_kwargs,
                    out_path=out_path,
                    key_hint=f"{job_id}-dialogue-{shot['idx']:02d}",
                    job_id=job_id,
                    shot_idx=shot["idx"],
                    expected_text=(None if extend_mode else combined["voiceover"]),
                    continuity_context=continuity_context,
                    paid_batch_id=paid_batch_id,
                    asr_enabled=asr_enabled,
                    fixed_audio_path=(
                        fixed_voice_track.audio_path
                        if fixed_voice_track and not repair_mode else None
                    ),
                    fixed_transcription=(
                        fixed_voice_track.transcription
                        if fixed_voice_track and not repair_mode else None
                    ),
                    generation_snapshot=production_config.snapshot_from_unit(
                        combined
                    ),
                )

            try:
                result = await generate_scene()
            except seedance.SeedanceInputPrivacyError:
                if repair_mode:
                    raise
                if identity_anchors is None:
                    raise
                await _fallback_static_identity_anchors(
                    job_id,
                    [char, *(cast_characters or [])],
                    identity_anchors,
                )
                _assert_continuity_anchors_approved(conn(), job_id)
                # No static reference was replaced: retrying the same rejected
                # request would not help. Otherwise the review check above pauses.
                raise
            if result.audio_path is None and not extend_mode:
                raise RuntimeError("固定声线音轨未写入 Seedance 成片")

            if repair_mode:
                raise SeedanceQualityError(
                    "Seedance 2.5 保留式编辑已生成候选片，等待人工复核后替换原母片",
                    cost_cents=result.cost_cents,
                    provider_calls=result.provider_calls,
                    reasons=("repair_candidate_ready",),
                    candidate_path=result.video_path,
                    candidate_url=result.video_url,
                )

            _persist_master_transcription(
                job_id, shot["idx"], result.transcription
            )

            _commit_generated_shot(job_id, shot["idx"], result)
            return (
                shot["idx"], None,
                DialogueAnchor(
                    result.video_url,
                    seedance.normalize_generation_duration(
                        combined["duration_sec"]
                    ),
                    chain_depth=max(
                        speaker_anchor.chain_depth if speaker_anchor else 0,
                        previous_scene_anchor.chain_depth
                        if previous_scene_anchor else 0,
                    ) + 1,
                    master_idx=int(shot.get("idx") or 0),
                ),
            )
        except BaseException as exc:
            try:
                _record_failed_generation_independently(
                    job_id, shot["idx"], exc
                )
            except BaseException:
                pass
            if isinstance(exc, Exception):
                return shot["idx"], exc, None
            raise


def _director_time_beats(duration: float, *, dialogue: str,
                         scene_voiceover: bool = False,
                         editorial_duration: float | None = None) -> list[str]:
    """Create a compact, continuous Seedance director timeline."""
    total_seconds = max(1, int(round(float(duration))))
    speech_end = min(
        total_seconds,
        max(2, int(math.ceil(float(editorial_duration or total_seconds)))),
    )
    establish_end = 1
    speech_label = "画外音" if scene_voiceover else "台词"
    subject = "画外音开始，画面继续自然运动" if scene_voiceover else "人物自然开口并完成动作与表情变化"
    beats = [
        f"0-{establish_end}秒：建立场景、人物或物件关系，动作自然起势。",
    ]
    if speech_end - establish_end >= 2:
        settle_start = speech_end - 1
        beats.extend([
            f"{establish_end}-{settle_start}秒：{subject}，完整说出且只能说出 "
            f"{{{speech_label}：{dialogue}}}。",
            f"{settle_start}-{speech_end}秒：话音自然结束，动作收束，画面保持连续稳定。",
        ])
    else:
        beats.append(
            f"{establish_end}-{speech_end}秒：{subject}，完整说出且只能说出 "
            f"{{{speech_label}：{dialogue}}}，句末自然闭嘴并收束动作。"
        )
    if speech_end < total_seconds:
        beats.append(
            f"{speech_end}-{total_seconds}秒：保持无新人声的自然动作和环境连续，供本地剪辑裁切。"
        )
    return beats


def _cinematic_director_time_beats(
        shot: dict, *, duration: float, editorial_duration: float) -> list[str]:
    """Compile one continuous integer-second cinematic dialogue timeline."""
    total_seconds = max(1, int(round(float(duration))))
    raw_beats = list(shot.get("performance_beats") or [])
    if not raw_beats:
        raw_beats = [{
            "speaker": shot.get("speaker"),
            "duration": editorial_duration,
            "dialogue": shot.get("voiceover") or "",
            "visual_intent": shot.get("action") or shot.get("scene") or "",
        }]

    weights = [max(0.1, float(beat.get("duration") or 0.1)) for beat in raw_beats]
    count = len(raw_beats)
    if count <= total_seconds:
        allocated = [1] * count
        remaining = total_seconds - count
        total_weight = sum(weights)
        shares = [remaining * weight / total_weight for weight in weights]
        extras = [int(math.floor(value)) for value in shares]
        for index, value in enumerate(extras):
            allocated[index] += value
        leftover = remaining - sum(extras)
        order = sorted(
            range(count),
            key=lambda index: (shares[index] - extras[index], -index),
            reverse=True,
        )
        for index in order[:leftover]:
            allocated[index] += 1
    else:
        allocated = [1] * count

    camera = non_text_visual_text(shot.get("camera") or "").strip(" ；;，,")
    cursor = 0
    lines: list[str] = []
    for index, beat in enumerate(raw_beats):
        start = cursor
        end = total_seconds if index == count - 1 else min(
            total_seconds, cursor + allocated[index]
        )
        cursor = end
        visual = non_text_visual_text(
            beat.get("visual_intent") or beat.get("broll_hint")
            or shot.get("action") or ""
        ).strip(" ；;，,")
        speaker = str(
            beat.get("speaker") or shot.get("speaker") or "当前人物"
        ).strip()
        dialogue = str(beat.get("dialogue") or "").strip()
        speech_mode = creative_ir.normalize_speech_mode(
            beat.get("speech_mode"), speaker=speaker
        )
        clauses = [f"{start}-{end}秒"]
        if visual:
            clauses.append(f"画面与动作：{visual}")
        if camera:
            clauses.append(f"运镜：{camera}")
        performance_labels = (
            ("emotion", "情绪"), ("tone", "语气"), ("pace", "语速"),
            ("volume", "音量"), ("pause", "停顿"),
            ("facial_expression", "表情"), ("gaze", "视线"),
            ("gesture", "手势"), ("body_action", "身体动作"),
        )
        clauses.extend(
            f"{label}：{str(beat.get(field)).strip()}"
            for field, label in performance_labels
            if str(beat.get(field) or "").strip()
        )
        if any(str(beat.get(field) or "").strip() for field, _ in performance_labels):
            clauses.append(
                "以上表演信息来自原片逐段观察：严格按所列先后顺序、强度和节奏复现，"
                "不得用通用微笑、默认站姿或泛化口播动作替代"
            )
        if dialogue and speech_mode in {"off_screen", "narration"}:
            voice_label = "旁白" if speech_mode == "narration" else "画外音"
            clauses.extend([
                f"{voice_label}{speaker}说出 {{台词：{dialogue}}}",
                "该声音不入镜、不需要人物形象、不做口型；"
                "画面内人物不朗读画外台词，不随画外语音做口型同步；"
                "保留明确的表情、视线与无声动作，包括惊讶张嘴、喘息等反应",
            ])
        elif dialogue:
            clauses.extend([
                f"角色 {speaker} 说出 {{台词：{dialogue}}}",
                "说话者面部与嘴部保持清晰可见，表演和运镜服务剧情，"
                "不得在本句中途切离说话者或遮挡嘴部",
            ])
        else:
            clauses.append("无台词，人物只完成动作与无声反应")
        lines.append("；".join(clauses) + "。")
    return lines


def _sound_design_for(shot: dict) -> str:
    explicit = str(shot.get("sound_design") or "").strip()
    if explicit:
        return explicit
    dynamics = str(shot.get("scene_dynamics") or "").strip()
    if dynamics:
        return f"由“{dynamics}”产生的轻微环境声与物件声"
    return "与场景动作匹配的轻微环境声和物件摩擦声"


def _build_scene_voiceover_video_prompt(
        shot: dict, *, voice_anchored: bool = False) -> str:
    """Build a faceless scene whose exact voice-over is native to Seedance."""
    speaker = shot.get("speaker") or shot.get("speaker_id")
    scene = non_text_visual_text(faceless_visual_text(
        shot.get("scene") or shot.get("prompt"), speaker
    ))
    dynamics = non_text_visual_text(
        faceless_visual_text(shot.get("scene_dynamics"), speaker)
    )
    lens = shot.get("lens") or ""
    lighting = shot.get("lighting") or ""
    camera_text = non_text_visual_text(
        faceless_visual_text(shot.get("camera"), speaker)
    )
    shot_type = faceless_visual_text(shot.get("shot_type"), speaker)
    camera = f"{camera_text} {shot_type}".strip()

    voiceover = str(shot.get("voiceover") or "").strip()
    editorial_duration = float(shot.get("duration_sec") or 8)
    duration = seedance.normalize_generation_duration(editorial_duration)
    sound_design = _sound_design_for(shot)
    parts = [
        "创作意图：用真实、克制的环境与物件动态承载信息，让原生画外音清晰主导叙事。",
        f"镜头: {camera}" + (f", {lens}" if lens else ""),
        f"场景: {scene}",
    ]
    if dynamics:
        parts.append(f"环境与物件动态: {dynamics}")
    if lighting:
        parts.append(f"光线: {lighting}")
    if voice_anchored:
        parts.extend([
            "素材职责：音频1只参考基础音色、口音、语速与节奏。",
            "不得复述音频1中的旧台词，只说本镜新画外音。",
        ])
    parts.extend([
        "时间轴：",
        *_director_time_beats(
            duration,
            dialogue=voiceover,
            scene_voiceover=True,
            editorial_duration=editorial_duration,
        ),
        "这是画外音(V.O.)场景，下面的台词只进入原生音轨，不在画面中呈现说话者",
        "画外音必须完整说出且只能说出时间轴中标注的内容",
        "使用自然、清晰、专业的中国成年人普通话；不得改写、增删、重复或复述其他内容",
        f"<环境音：{sound_design}>；无 BGM，不要字幕。",
        "不得出现任何可识别人脸、正面人物、真人肖像或嘴部特写，不做口型同步",
        "优先拍摄环境、设备、文件、仪表和物件动态；如必须出现人物，只能是脸部完全不可见的手部、背影或远景剪影",
        "所有标签、铭牌、钢印、纸张和屏幕区域必须使用背面、遮挡、裁切或浅景深处理，保持完全失焦",
        "画面内不得出现任何字母或数字，不要生成字幕、标牌文字或其他可读文字",
    ])
    return "\n".join(parts)


def _build_background_voiceover_prompt(
    shot: dict, *, cast_characters: list[dict], reference_manifest: list[dict],
) -> str:
    """Keep visible performance separate from the exact off-screen audio."""
    editorial_duration = float(shot.get("duration_sec") or 8)
    duration = seedance.normalize_generation_duration(editorial_duration)
    cast_names = [
        str(character.get("script_character_name") or character.get("name")
            or character.get("id") or "") for character in cast_characters
    ]
    return "\n".join([
        "创作意图：画外声音推动剧情，明确出镜人物仅作无声表演，不生成口播。",
        "素材职责（编号严格对应本次请求顺序）：",
        *reference_attention.compile_prompt_duties(reference_manifest),
        f"可见人物：{'、'.join(cast_names) if cast_names else '无，不得新增出镜人物'}。",
        f"场景：{non_text_visual_text(shot.get('scene') or '')}",
        f"动作：{non_text_visual_text(shot.get('action') or '')}",
        f"运镜：{non_text_visual_text(shot.get('camera') or '')}",
        "连续整数秒导演时间轴：",
        *_cinematic_director_time_beats(
            shot, duration=duration, editorial_duration=editorial_duration,
        ),
        "台词只进入音轨，严格按各节拍的声音身份、顺序、表演和全文说出，不得增删改写。",
        "音频人物不因发声而入镜；明确出镜人物保持身份、动作与空间连续性，"
        "不朗读画外台词，不随画外语音做口型同步；保留明确表情与无声动作。"
        "身份素材只约束可见人物，不指定画外音声线。",
        f"<环境音：{_sound_design_for(shot)}>；无 BGM。",
        "禁止任何可读文字、字幕、品牌、设备编号、车牌、字母、数字、二维码或水印。",
    ])


def _blocking_anchor_text(blocking_anchor: dict | None) -> str:
    blocking = blocking_anchor or {}
    positions = []
    for item in blocking.get("positions") or []:
        positions.append(
            f"{item.get('display_name') or item.get('identity_key') or '角色'}="
            f"{item.get('screen_position') or 'screen-center'}"
        )
    axis = str(blocking.get("axis") or "preserve-180-degree-axis")
    return f"axis={axis}; positions=" + ", ".join(positions)


def _blocking_mode(shot: dict) -> str:
    blocking = shot.get("blocking_anchor") or {}
    contract = shot.get("continuity_contract") or {}
    spatial = contract.get("spatial") or {}
    mode = str(
        blocking.get("mode") or spatial.get("mode") or "locked"
    ).strip().casefold()
    return mode if mode in {"free", "continuity", "locked"} else "locked"


def _blocking_state_text(value: dict | None) -> str:
    state = value or {}
    source_names = {
        "scene_baseline": "场景基准构图",
        "previous_master_out": "承接上一母片结束状态",
        "performance_end": "由本镜表演结束状态决定",
        "locked_layout": "保持锁定构图",
        "explicit": "剧本明确指定",
    }
    source = str(state.get("source") or "").strip()
    positions = []
    for item in state.get("positions") or []:
        positions.append(
            f"{item.get('display_name') or item.get('identity_key') or '角色'}="
            f"{item.get('screen_position') or 'screen-center'}"
        )
    detail = "、".join(positions) if positions else "按参考画面实际状态"
    return f"{source_names.get(source, source or '未指定')}（{detail}）"


def _spatial_constraint_lines(shot: dict) -> list[str]:
    blocking = shot.get("blocking_anchor") or {}
    mode = _blocking_mode(shot)
    axis = str(blocking.get("axis") or "preserve-180-degree-axis")
    if mode == "free":
        return [
            "人物调度模式：自由调度。场景身份与人物身份保持稳定，但不锁定人物左右槽位、相对距离或固定构图。",
            "人物可根据剧情动作自由进入、离开、靠近、后退、交接和改变视觉中心；不得无故复制、消失或更换身份。",
        ]
    if mode == "continuity":
        movement = str(
            blocking.get("movement") or shot.get("action") or "按剧情自然调度"
        ).strip()
        return [
            "空间连续性契约：站位是镜头间的状态传递，不是全程固定构图。",
            f"入镜状态：{_blocking_state_text(blocking.get('blocking_in'))}",
            f"剧情走位：{movement}",
            f"出镜状态：{_blocking_state_text(blocking.get('blocking_out'))}",
            "允许人物按剧情动作改变位置、相对距离和视觉中心；保持视线、出入方向与动作惯性连续，禁止无动机瞬移、身份交换或跳轴。",
            f"镜头轴线：{axis}；有明确镜头动机时可通过可见运镜建立新的轴线。",
        ]
    return [
        "人物调度模式：精确锁定。场景连续性与人物站位均为硬约束。",
        f"固定场景键：{shot.get('scene_anchor_key') or ''}",
        f"固定站位图：{_blocking_anchor_text(blocking)}",
        "同一人物必须保持同一屏幕侧、相对距离、朝向和身高关系；除非剧本明确指定换位，否则禁止交换左右位置。",
        "锁定建筑、设备位置、地面材质、天气、时段、主光方向与180度镜头轴线。",
    ]


def _continuity_contract_text(contract: dict | None) -> str:
    value = contract or {}
    scene = value.get("scene") or {}
    camera = value.get("camera") or {}
    spatial = value.get("spatial") or {}
    character_lines = []
    for character in value.get("characters") or []:
        character_lines.append(
            f"{character.get('display_name') or character.get('identity_key') or '角色'}:"
            f"服装={character.get('wardrobe') or '保持身份锚点造型'},"
            f"场景基准位置={character.get('screen_position') or 'screen-center'}"
        )
    props = "、".join(str(item) for item in value.get("props") or []) or "无固定道具"
    return "；".join([
        f"场景={scene.get('description') or scene.get('key') or ''}",
        f"时段={scene.get('time_of_day') or '保持场景锚点'}",
        f"光线={scene.get('lighting') or '保持场景锚点'}",
        f"人物=[{'；'.join(character_lines)}]",
        f"道具={props}",
        f"轴线={camera.get('axis') or 'preserve-180-degree-axis'}",
        f"调度模式={spatial.get('mode') or 'locked'}",
        f"入镜={_blocking_state_text(spatial.get('blocking_in'))}",
        f"剧情走位={spatial.get('movement') or '按本镜动作自然调度'}",
        f"出镜={_blocking_state_text(spatial.get('blocking_out'))}",
    ])


def _build_native_dialogue_prompt(
        shot: dict, character: dict, *, anchored: bool = False,
        voice_anchored: bool = False,
        scene_anchored: bool = False,
        previous_scene_anchored: bool = False,
        reference_manifest: list[dict] | None = None) -> str:
    """Build one exact native-speaking Seedance 2.5 presenter prompt."""
    voiceover = str(shot.get("voiceover") or "").strip()
    if not voiceover:
        raise ValueError(f"shot #{shot.get('idx')} has empty voiceover")
    editorial_duration = float(shot.get("duration_sec") or 8)
    duration = seedance.normalize_generation_duration(editorial_duration)
    sound_design = _sound_design_for(shot)
    has_fixed_reference = bool(
        character.get("generation_mode") != "ai_generated"
        and character.get("identity_type") != "custom"
        and character.get("seedance_reference_mode") != "description_only"
        and (
            character.get("reference_image")
            or character.get("image_path")
            or character.get("seedance_asset_uri")
            or _roster_character(character.get("id")).get("reference_image")
            or _roster_character(character.get("id")).get("seedance_asset_uri")
        )
    )
    identity_instruction = (
        "人物必须严格沿用素材中的固定角色身份参考，保持同一张脸、五官比例、"
        "视觉年龄、发际线与基础发型；不得泛化为相似路人或网红脸。"
        if has_fixed_reference
        else "生成一名完全虚构、由 AI 创造且无现实身份对应的讲解角色。"
    )
    cinematic = (
        str(shot.get("render_mode") or "presenter").lower()
        == "cinematic_character"
    )
    master_speakers = [
        str(item).strip() for item in (
            shot.get("audio_speakers") or shot.get("speakers") or []
        )
        if str(item).strip()
    ] or [str(shot.get("speaker") or character.get("id") or "").strip()]
    multi_speaker = len(master_speakers) > 1
    creative_intent = (
        "创作意图：生成一段电影叙事情境表演，人物在真实剧情空间中完成动作、"
        "技能展示与原生对白，镜头主动参与叙事。"
        if cinematic else
        "创作意图：生成一段可信、克制、可直接交付的真人讲解镜头，画面表演与原生对白同步完成。"
    )
    timeline = (
        _cinematic_director_time_beats(
            shot,
            duration=duration,
            editorial_duration=editorial_duration,
        ) if cinematic else _director_time_beats(
            duration,
            dialogue=voiceover,
            editorial_duration=editorial_duration,
        )
    )
    character_name, character_look, character_background = (
        _prompt_character_profile(character)
    )
    scene = non_text_visual_text(
        shot.get("scene") or shot.get("prompt") or ""
    ).strip(" ；;，,")
    action = non_text_visual_text(shot.get("action") or "").strip(
        " ；;，,"
    )
    parts = [
        creative_intent,
        identity_instruction,
        f"人物姓名与设定：{character_name}；{character_look}",
        f"角色背景：{character_background}",
        f"场景：{scene}",
        f"镜头：{shot.get('camera') or ''}",
        f"动作：{action}",
        f"表情变化：{shot.get('expression_arc') or shot.get('dramatic_keywords') or ''}",
        f"声音与语气：{shot.get('voice_directive') or '自然、专业、清晰的中国成年人普通话'}",
        "连续整数秒导演时间轴：" if cinematic else "时间轴：",
        *timeline,
        (
            "多人对白必须严格按照下方剧情节拍中的角色、顺序和台词轮流说出，"
            "不得串词、抢词、改写或让错误角色开口。"
            if multi_speaker else
            (
                "人物只能说出上方连续整数秒导演时间轴中标注的台词，"
                "不得在时间轴外增加或复述任何人声。"
                if cinematic else
                "人物必须完整说出且只能说出时间轴中标注的台词"
            )
        ),
        f"<环境音：{sound_design}>；无 BGM，不要字幕。",
        (
            "不得改写、增删、重复台词，不得加入旁白、未列出的第三人声或背景音乐。"
            if multi_speaker else
            "不得改写、增删、重复台词，不得加入旁白、第二个人声或背景音乐。"
        ),
        "嘴唇、牙齿、舌位和下颌逐字同步，句末自然闭嘴。",
        "只在台词所在的关键音节时间窗保护口型可见性：说话者保持正面或四分之三"
        "中近景，禁止口部遮挡、快速甩镜或在句中切离说话者。",
        "非台词时间窗允许有动机的走位、环绕、推拉、反应镜头和动作特写；"
        "其他同场人物始终闭嘴，只做无声表演。",
        "人物身份、五官、发型、背景和光线全程稳定；服装与职业装备严格服从本镜场景，并在本镜内保持一致。",
        "安全帽、反光背心、工服、工程机械、设备铭牌、工牌、文件、屏幕和背景标牌一律纯色无字，或通过背面、遮挡、裁切、浅景深使其不可辨认。",
        "禁止企业名称、品牌、设备编号、车牌、字母、数字、二维码、字幕、标识或水印。",
    ]
    if reference_manifest:
        parts[1:1] = [
            "素材职责（编号严格对应本次请求顺序）：",
            *reference_attention.compile_prompt_duties(reference_manifest),
        ]
    if scene_anchored:
        if shot.get("_spatial_anchor_scope") == "site":
            parts[1:1] = [
                "PROJECT SITE CONTINUITY：该参考只定义同一项目现场的世界观。",
                f"项目现场键：{shot.get('site_anchor_key') or ''}",
                "保持建筑语言、主要材质、设备体系、安全设施和环境身份一致；",
                "当前母片仍按本镜 scene 生成独立子空间，禁止照搬参考图的具体布局、"
                "人物站位、天气、时段、光线或机位。",
            ]
        else:
            parts[1:1] = [
                "场景身份与可重复识别的空间结构为硬约束，不是风格参考。",
                f"固定场景键：{shot.get('scene_anchor_key') or ''}",
                *_spatial_constraint_lines(shot),
            ]
    if shot.get("continuity_contract"):
        mode = _blocking_mode(shot)
        scope = {
            "free": "场景和人物身份为硬约束；人物空间为自由调度",
            "continuity": "场景和人物身份为硬约束；人物空间按入镜、走位、出镜状态传递",
            "locked": "场景、人物身份和明确站位均为硬约束",
        }[mode]
        parts[1:1] = [
            f"结构化连续性契约（{scope}）：",
            _continuity_contract_text(shot.get("continuity_contract")),
        ]
    if previous_scene_anchored:
        if _blocking_mode(shot) == "free":
            previous_duty = (
                "前序同场景视频只负责延续空间身份、材质和光线，不锁定本镜人物站位或构图；"
            )
        elif _blocking_mode(shot) == "continuity":
            previous_duty = (
                "前序同场景视频负责提供上一母片的出镜状态、视线、动作惯性和镜头轴线；"
                "从该状态自然继续，但允许本镜按剧情走位；"
            )
        else:
            previous_duty = (
                "前序同场景视频只负责延续空间布局、人物站位、视线方向和镜头轴线；"
            )
        parts[1:1] = [
            previous_duty
            + "人物身份仍以定妆照或授权角色资产为准，不得沿用前序视频中的脸部漂移。",
        ]
    if cinematic:
        cast_lines = []
        for member in (shot.get("cast") or []):
            member_name, member_look, member_background = (
                _prompt_character_profile(member)
            )
            cast_lines.append(
                f"{member_name}："
                f"{member_look or member_background or '按既定角色设定'}"
            )
        parts[1:1] = [
            "表演方式：人物必须在场景中走位、观察、交互、闪避或施展动作，"
            "不得持续正对镜头站桩讲解，不得使用直播口播式构图。",
            "摄影方式：执行下述镜头设计，允许有动机的跟拍、横移、环绕、推拉、"
            "低机位与动作特写；运镜服务剧情且保持主体清晰；"
            "对白区间不得用运镜破坏口型可见性。",
            *(["同场人物设定：", *cast_lines] if cast_lines else []),
            (
                "对白约束：仅剧情节拍中当前标注的角色可以在对应时间段开口；"
                "其他同场人物只能做动作和反应，不得抢词、串词或增加台词。"
                if multi_speaker else
                f"对白约束：只有{character_name or '当前主角'}说出台词；"
                "其他同场人物只能做动作和反应，不得开口、旁白或发出可辨识人声。"
            ),
        ]
    if anchored:
        parts[1:1] = [
            "素材职责：",
            "身份参考只负责锁定人物身份中的不可变特征，包括五官、年龄、发型、肤色与体型；"
            "不得照搬定妆照的背景、站姿或服装。",
            *(
                ["音频参考只负责延续原生声线、语速和情绪，不负责人物身份、场景或镜头构图。"]
                if voice_anchored else []
            ),
            "服装、PPE、防护装备和职业状态严格服从本镜场景设定，"
            "并在同一场景后续母片中保持一致。",
            "不得复述参考素材中的旧台词，只说本镜新台词。",
        ]
    return "\n".join(parts)


def _build_broll_video_prompt(
    shot: dict, reference_manifest: list[dict] | None = None,
) -> str:
    """Build a bounded cinematic B-roll request with canonical local mixing."""
    duration = seedance.normalize_generation_duration(
        shot.get("duration_sec") or shot.get("duration") or 4
    )
    visual = non_text_visual_text(
        shot.get("prompt") or shot.get("scene") or ""
    ).strip()
    scene = non_text_visual_text(shot.get("scene") or visual).strip()
    action = non_text_visual_text(shot.get("action") or visual).strip()
    camera = non_text_visual_text(
        shot.get("camera") or "稳定电影感跟拍"
    ).strip()
    parts = [
        "创作意图：生成服务叙事证据的电影化 B-roll，不生成口播画面。",
        f"场景：{scene}",
        f"动作：{action}",
        f"运镜：{camera}",
        "连续整数秒导演时间轴：",
        f"0-{duration}秒；画面与动作：{visual or action}；运镜：{camera}。",
        "无对白、无人声、无旁白；只保留与动作同步的环境音和物件音；无 BGM。",
        "禁止任何可读文字、字幕、品牌、设备编号、车牌、字母、数字、二维码或水印。",
    ]
    if reference_manifest:
        parts[1:1] = [
            "素材职责（编号严格对应本次请求顺序）：",
            *reference_attention.compile_prompt_duties(reference_manifest),
        ]
    if shot.get("_spatial_anchor_scope") == "site":
        parts[1:1] = [
            "PROJECT SITE CONTINUITY：保持项目现场的建筑语言、材质、设备体系和安全设施一致；",
            "本镜是独立子空间，禁止照搬项目现场参考的具体布局、人物站位、天气、时段或机位。",
        ]
    return "\n".join(parts)


def _safe_scene_voiceover_keyframe(shot: dict) -> str | None:
    """Accept only v2.5 faceless keyframes, never legacy portrait frames."""
    path = shot.get("character_in_scene_path")
    if not path:
        return None
    filename = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return str(path) if filename.endswith("_scene_vo.png") else None


async def _gen_broll(job_id: str, shot: dict,
                     anchor_image_path: str | None,
                     sb_shot: dict,
                     sem: asyncio.Semaphore,
                     scene_voiceover: bool = False,
                     voice_anchor: NativeVoiceAnchor | None = None,
                     fixed_voice_track: FixedVoiceTrack | None = None,
                      scene_reference: str | None = None,
                      cast_characters: list[dict] | None = None,
                      identity_anchors: dict[str, DialogueAnchor] | None = None,
                     paid_batch_id: str | None = None,
                     asr_enabled: bool = True,
                     ) -> tuple[int, Exception | None]:
    async with sem:
        try:
            _mark_shot_generating(job_id, shot["idx"])
            background_voiceover = bool(
                scene_voiceover
                and sb_shot.get("render_mode") == "background_voiceover"
            )
            precision_reason = precision_motion_reason(sb_shot)
            if precision_reason:
                raise RuntimeError(
                    "precision-sensitive shot blocked before Seedance request: "
                    f"{precision_reason}; rewrite it as natural motion and put "
                    "the exact value/direction in narration or subtitles"
                )
            prompt_full = (
                _build_scene_voiceover_video_prompt(
                    {**shot, **sb_shot},
                    voice_anchored=voice_anchor is not None,
                )
                if scene_voiceover
                else _build_broll_video_prompt(shot)
            )
            # === Seedance 2.5 reference-role support ===
            # 首帧:s2 生成的 per-shot 关键帧(画面更稳),老 job 退到 anchor 定妆照
            if scene_voiceover:
                # v2.4 及更早版本的 scene-dialogue 首帧含人物正脸。旧任务重试时
                # 必须忽略这类文件并退回 t2v，不能再次送进 Seedance 人脸审核。
                keyframe = _safe_scene_voiceover_keyframe(shot)
            else:
                keyframe = shot.get("character_in_scene_path") or anchor_image_path
            # 尾帧:s2 出了 broll_end 时启用(按命名约定查找,不进 DB schema)
            end_suffix = "scene_vo_end" if scene_voiceover else "broll_end"
            end_keyframe_path = (
                ASSETS_DIR / job_id / "scenes"
                / f"{shot['idx']:02d}_{end_suffix}.png"
            )
            last_frame = str(end_keyframe_path) if end_keyframe_path.exists() else None
            # ⚠️ Seedance 硬约束:first/last_frame 跟 reference_image **互斥**
            # ("first/last frame content cannot be mixed with reference media content")
            # J-VERIFY07 实测:scene-dialogue 模式传了 first_frame + anchor reference 直接 400
            # 选择:i2v 优先(first_frame 已是该镜专属静帧,信息够),不混 reference_image
            extra_refs: list[str] = []  # 保留变量给未来"无 first_frame + 多 ref"模式
            # camera_fixed / motion_strength 优先用 storyboard 显式字段,
            # 老 job(没这俩字段)走 fallback:从 camera 关键词推断 + 默认 0.4
            camera_fixed = sb_shot.get("camera_fixed")
            if camera_fixed is None:
                cam = (shot.get("camera") or "").lower()
                camera_fixed = any(k in cam for k in ("固定", "static", "fixed"))
            motion_strength = sb_shot.get("motion_strength")
            if motion_strength is None:
                motion_strength = 0.4
            route = select_route(shot, sb_shot)
            runtime_shot = {**shot, **sb_shot}
            extend_mode = (
                str(runtime_shot.get("repair_mode") or "").strip()
                == "seedance_extend"
            )
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            if extend_mode:
                out_path = (
                    ASSETS_DIR / job_id / "shots" / "review" / "repairs"
                    / f"{shot['idx']:02d}-seedance_extend.mp4"
                )
                prompt_full = _build_extend_prompt(runtime_shot)
                request_kwargs = _extend_request_kwargs(runtime_shot)
            else:
                request_kwargs = (
                    _scene_voiceover_request_kwargs(runtime_shot, voice_anchor)
                    if scene_voiceover and not background_voiceover else _broll_request_kwargs(
                        runtime_shot,
                        cast_characters=cast_characters,
                        identity_anchors=identity_anchors,
                        scene_reference=scene_reference,
                    )
                )
            if background_voiceover and not extend_mode:
                if voice_anchor is not None:
                    request_kwargs["reference_audios"] = [str(voice_anchor.audio_path)]
                prompt_full = _build_background_voiceover_prompt(
                    runtime_shot, cast_characters=cast_characters or [],
                    reference_manifest=request_kwargs.get("reference_manifest") or [],
                )
            if not scene_voiceover and not extend_mode:
                prompt_full = _build_broll_video_prompt(
                    runtime_shot,
                    reference_manifest=request_kwargs.get(
                        "reference_manifest"
                    ),
                )
            if scene_voiceover and fixed_voice_track is not None and not extend_mode:
                request_kwargs["duration"] = seedance.normalize_generation_duration(
                    fixed_voice_track.duration_sec
                )
                request_kwargs["reference_audios"] = [
                    str(fixed_voice_track.audio_path)
                ]
            if extend_mode or ((not scene_voiceover or background_voiceover) and (
                request_kwargs.get("reference_videos")
                or request_kwargs.get("references")
            )):
                keyframe = None
                last_frame = None
            request_kwargs.update({
                "first_frame": keyframe,
                "last_frame": last_frame,
                "references": (
                    request_kwargs.get("references")
                    if not keyframe else extra_refs
                ) or None,
                "camera_fixed": bool(camera_fixed),
                "motion_strength": float(motion_strength),
            })
            result = await _generate_seedance_video(
                prompt=prompt_full,
                route=route,
                request_kwargs=request_kwargs,
                out_path=out_path,
                key_hint=f"{job_id}-shot-{shot['idx']:02d}",
                job_id=job_id,
                shot_idx=shot["idx"],
                forbid_people=scene_voiceover and not (background_voiceover and cast_characters),
                require_lip_sync=not background_voiceover,
                expected_text=(
                    sb_shot.get("voiceover") or shot.get("voiceover")
                    if scene_voiceover and not extend_mode else None
                ),
                require_broll_semantics=not scene_voiceover,
                continuity_context=(
                    _continuity_qa_context(
                        runtime_shot, {}, cast_characters=cast_characters,
                        identity_anchors=identity_anchors, scene_reference=scene_reference,
                    ) if background_voiceover else
                    {
                        "contract": sb_shot.get("continuity_contract") or {},
                        "identity_image_urls": [],
                        "scene_image_url": scene_reference,
                        "previous_video_url": None,
                    }
                    if (
                        scene_reference
                        and sb_shot.get("_spatial_anchor_scope") != "site"
                    ) else None
                ),
                paid_batch_id=paid_batch_id,
                asr_enabled=asr_enabled,
                fixed_audio_path=(
                    fixed_voice_track.audio_path
                    if fixed_voice_track and not extend_mode else None
                ),
                generation_snapshot=production_config.snapshot_from_unit(
                    sb_shot
                ),
                fixed_transcription=(
                    fixed_voice_track.transcription
                    if fixed_voice_track and not extend_mode else None
                ),
            )

            if extend_mode:
                raise SeedanceQualityError(
                    "Seedance 2.5 延长片段已生成候选片，等待人工复核后替换原母片",
                    cost_cents=result.cost_cents,
                    provider_calls=result.provider_calls,
                    reasons=("repair_candidate_ready",),
                    candidate_path=result.video_path,
                    candidate_url=result.video_url,
                )

            if scene_voiceover:
                _persist_master_transcription(
                    job_id, shot["idx"], result.transcription
                )

            _commit_generated_shot(job_id, shot["idx"], result)
            return shot["idx"], None
        except BaseException as e:
            try:
                _record_failed_generation_independently(
                    job_id, shot["idx"], e
                )
            except BaseException:
                pass
            if isinstance(e, Exception):
                return shot["idx"], e
            raise


def _required_character_ids(units: list[dict]) -> set[str]:
    """Return every character that can appear or speak in the target masters."""
    required: set[str] = set()

    def add(value: object, *, explicit_on_screen: set[str]) -> None:
        if isinstance(value, dict):
            value = value.get("id") or value.get("speaker")
        normalized = str(value or "").strip()
        if (
            normalized
            and (
                normalized.casefold() in explicit_on_screen
                or creative_ir.normalize_speech_mode(
                    None, speaker=normalized
                ) == "on_screen"
            )
        ):
            required.add(normalized)

    for unit in units:
        if unit.get("render_mode") == "background_voiceover":
            for member in unit.get("cast") or []:
                value = member.get("id") if isinstance(member, dict) else member
                if normalized := str(value or "").strip():
                    required.add(normalized)
            continue
        explicit_on_screen = {
            str(beat.get("speaker") or "").strip().casefold()
            for beat in (unit.get("performance_beats") or [])
            if isinstance(beat, dict)
            and str(beat.get("speaker") or "").strip()
            and creative_ir.normalize_speech_mode(
                beat.get("speech_mode"),
                speaker=str(beat.get("speaker") or "").strip(),
            ) == "on_screen"
        }
        for field in ("speaker", "anchor_character"):
            add(unit.get(field), explicit_on_screen=explicit_on_screen)
        for field in ("speakers", "cast"):
            for member in unit.get(field) or []:
                add(member, explicit_on_screen=explicit_on_screen)
        for block in unit.get("dialogue_blocks") or []:
            add(
                block.get("speaker") if isinstance(block, dict) else None,
                explicit_on_screen=explicit_on_screen,
            )
    return required


async def run(
    job_id: str,
    *,
    storyboard_override: dict | None = None,
    characters_override: list[dict] | None = None,
    target_master_indexes: set[int] | None = None,
    prepare_anchors: bool = True,
    required_scene_anchor_keys: set[str] | None = None,
) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError(f"video job not found: {job_id}")
    frozen_input = storyboard_override is not None
    sb = (
        deepcopy(storyboard_override)
        if frozen_input else json.loads(job["storyboard"])
    )
    if characters_override is not None:
        characters = deepcopy(list(characters_override))
    else:
        characters = json.loads(job["characters_json"])
    chars = {c["id"]: c for c in characters}
    characters = list(chars.values())
    generation_units = sb.get("masters") or sb["shots"]
    target_indexes = {
        int(value) for value in (target_master_indexes or set())
        if int(value) > 0
    }
    target_units = [
        unit for unit in generation_units
        if not target_indexes or int(unit.get("idx") or 0) in target_indexes
    ]
    if target_indexes and {
        int(unit.get("idx") or 0) for unit in target_units
    } != target_indexes:
        raise ValueError("frozen master selection is not present in storyboard")
    generation_snapshot = production_config.generation_snapshot(sb)
    audio_mode = normalize_audio_mode(
        generation_snapshot.get("audio_mode")
        or os.getenv("DIALOGUE_AUDIO_MODE")
    )
    bindings_changed = False
    if not frozen_input:
        bindings_changed = character_assets.apply_cached_bindings(db, characters)
        bindings_changed = (
            _resolve_real_person_bindings(db, characters) or bindings_changed
        )
    if audio_mode == AUDIO_MODE_FIXED_TTS and not frozen_input:
        bindings_changed = (
            _resolve_speaking_voice_bindings(db, characters, generation_units)
            or bindings_changed
        )
    continuity_changed = (
        False if frozen_input
        else scene_assets.apply_continuity_metadata(sb, characters)
    )
    dependency_graph = master_dependencies.build_master_dependency_graph(
        generation_units
    )
    if sb.get("master_dependency_graph") != dependency_graph:
        sb["master_dependency_graph"] = dependency_graph
        continuity_changed = True
    if (bindings_changed or continuity_changed) and not frozen_input:
        db.execute(
            "UPDATE jobs SET storyboard=?,characters_json=? WHERE job_id=?",
            (
                json.dumps(sb, ensure_ascii=False),
                json.dumps(characters, ensure_ascii=False),
                job_id,
            ),
        )
        db.commit()
    sb_shots = {
        s["idx"]: production_config.attach_runtime_snapshot(
            s, generation_snapshot
        )
        for s in generation_units
    }

    # Resolve deterministic, reviewed local footage before any stochastic
    # Seedance submission. Matched rows become OK and disappear from the
    # pending-generation query below.
    if sb.get("production_mode") == "native_master_local_director":
        asset_library.reuse_broll_assets(db, job_id, target_units)

    all_pending_rows = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND state!='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if target_indexes:
        all_pending_rows = [
            row for row in all_pending_rows
            if int(row["idx"]) in target_indexes
        ]
    review_rows = [
        row for row in all_pending_rows
        if _is_manual_review_pending(dict(row))
    ]
    rows = [
        row for row in all_pending_rows
        if not _is_manual_review_pending(dict(row))
    ]
    if not rows and not review_rows:
        return

    configured_concurrency = max(
        1, int(generation_snapshot["max_concurrent_masters"])
    )
    sem = asyncio.Semaphore(provider_capacity.effective_limit(
        db, "seedance", configured_limit=configured_concurrency
    ))
    results: list[tuple[int, Exception | None]] = []
    required_character_ids = _required_character_ids(target_units)
    if required_scene_anchor_keys is None:
        derived_scene_keys = {
            str(unit.get(field) or "").strip().casefold()
            for unit in target_units for field in ("scene_anchor_key", "site_anchor_key")
            if str(unit.get(field) or "").strip()
        }
        # Legacy empty derivation retained the job-wide recurrence fallback.
        required_scene_keys = derived_scene_keys or None
        scene_storyboard = sb
    else:
        required_scene_keys = {
            str(value or "").strip().casefold() for value in required_scene_anchor_keys
        }
        scene_storyboard = {**sb, "masters": target_units}
        resolved_keys = {
            item["anchor_key"] for item in scene_assets.required_scene_anchors(
                scene_storyboard, required_scene_keys,
            )
        }
        if required_scene_keys != resolved_keys:
            raise ValueError("required scene anchor keys do not resolve for selected masters")
    anchor_characters = [
        character for character in characters
        if not required_character_ids
        or str(character.get("id") or "") in required_character_ids
    ]
    identity_anchors = (
        await _ensure_character_identity_anchors(
            job_id, anchor_characters, storyboard=sb,
            existing_only=not prepare_anchors,
        )
        if rows else {}
    )
    scene_anchors = (
        await _ensure_scene_visual_anchors(
            job_id, scene_storyboard, anchor_keys=required_scene_keys,
            existing_only=not prepare_anchors,
        ) if rows else {}
    )
    if rows:
        _apply_anchor_review_mode(
            conn(), job_id,
            normalize_anchor_review_mode(
                (sb.get("production_settings") or {}).get("anchor_review_mode")
            ),
        )
        _assert_continuity_anchors_approved(conn(), job_id)
    dialogue_anchors: dict[str, DialogueAnchor] = {}
    voice_anchors: dict[str, NativeVoiceAnchor] = {}
    completed_anchors: list[tuple[int, str, DialogueAnchor]] = []
    completed_scene_anchors: list[tuple[int, str, DialogueAnchor]] = []
    completed_voice_anchors: list[tuple[int, str, NativeVoiceAnchor]] = []
    completed_dialogue = db.execute(
        "SELECT idx, speaker_id, duration_sec, video_path, video_url FROM shots "
        "WHERE job_id=? AND state='OK' AND narrative_type='dialogue' "
        "ORDER BY idx",
        (job_id,),
    ).fetchall()
    for completed in completed_dialogue:
        completed_shot = dict(completed)
        mode = (
            sb_shots.get(completed_shot["idx"], {}).get("render_mode")
            or "presenter"
        ).lower()
        anchor = _dialogue_anchor_from_completed_shot(completed_shot)
        voice_anchor = _voice_anchor_from_completed_shot(completed_shot)
        if mode in (
            "presenter", "digital_human", "cinematic_character"
        ) and anchor is not None:
            completed_character = chars.get(completed_shot["speaker_id"])
            identity_key = (
                character_assets.character_identity_key(completed_character)
                if completed_character else completed_shot["speaker_id"]
            )
            completed_anchors.append((
                completed_shot["idx"], identity_key, anchor
            ))
            completed_scene_key = str(
                sb_shots.get(completed_shot["idx"], {}).get(
                    "scene_anchor_key"
                ) or ""
            ).strip().casefold()
            if completed_scene_key:
                completed_scene_anchors.append((
                    completed_shot["idx"], completed_scene_key, anchor
                ))
            if voice_anchor is not None:
                completed_voice_anchors.append((
                    completed_shot["idx"], identity_key,
                    voice_anchor,
                ))
    scene_video_anchors: dict[str, DialogueAnchor] = {}
    for _, scene_key, completed_anchor in completed_scene_anchors:
        scene_video_anchors[scene_key] = completed_anchor

    async def generate_row(row, paid_batch_id: str):
        r = row
        shot = dict(r)
        sb_shot = sb_shots.get(shot["idx"], {})
        scene_key = str(
            sb_shot.get("scene_anchor_key") or ""
        ).strip().casefold()
        spatial_key = scene_assets.reference_anchor_key(
            sb_shot, scene_anchors.keys()
        )
        site_key = str(
            sb_shot.get("site_anchor_key") or ""
        ).strip().casefold()
        spatial_scope = (
            "site" if spatial_key and spatial_key == site_key else "scene"
        )
        sb_shot = {
            **sb_shot,
            "_spatial_anchor_key": spatial_key,
            "_spatial_anchor_scope": spatial_scope,
        }
        spatial_reference = scene_anchors.get(spatial_key)
        # Dialogue shots are deliberately sequential: each verified native
        # clip becomes the identity + voice reference for that speaker's next
        # clip. This is the consistency route proven in the 2.5 validation.
        render_mode = (sb_shot.get("render_mode") or "presenter").lower()
        cast_characters: list[dict] = []
        if render_mode == "cinematic_character":
            sb_shot = dict(sb_shot)
            cast_characters = _scene_cast_characters(
                sb_shot, chars, shot["speaker_id"]
            )
            sb_shot["cast"] = [
                {
                    "id": character_id,
                    "name": cast_character.get("name") or character_id,
                    "script_character_name": cast_character.get(
                        "script_character_name"
                    ) or character_id,
                    "generation_mode": cast_character.get("generation_mode"),
                    "identity_type": cast_character.get("identity_type"),
                    "seedance_reference_mode": cast_character.get(
                        "seedance_reference_mode"
                    ),
                    "look": cast_character.get("look") or "",
                    "background_description": cast_character.get(
                        "background_description"
                    ) or "",
                }
                for cast_character in cast_characters
                for character_id in [str(cast_character.get("id") or "")]
            ]
        if (shot["narrative_type"] == "dialogue"
                and render_mode in (
                    "presenter", "digital_human", "cinematic_character"
                )):
            char = chars.get(shot["speaker_id"])
            if not char:
                raise RuntimeError(
                    f"shot #{shot['idx']} speaker {shot['speaker_id']} not found"
                )
            identity_key = character_assets.character_identity_key(char)
            if identity_key not in dialogue_anchors:
                earlier = [
                    item for item in completed_anchors
                    if item[1] == identity_key and item[0] < shot["idx"]
                ]
                if earlier:
                    dialogue_anchors[identity_key] = max(
                        earlier, key=lambda item: item[0]
                    )[2]
            if identity_key not in voice_anchors:
                earlier_voice = [
                    item for item in completed_voice_anchors
                    if item[1] == identity_key and item[0] < shot["idx"]
                ]
                if earlier_voice:
                    voice_anchors[identity_key] = max(
                        earlier_voice, key=lambda item: item[0]
                    )[2]
            fixed_voice_track = None
            if audio_mode == AUDIO_MODE_FIXED_TTS:
                voice_database = conn()
                try:
                    fixed_voice_track = await _synthesize_fixed_voice_track(
                        voice_database,
                        job_id=job_id,
                        shot={
                            **shot, **sb_shot,
                            "duration_sec": shot["duration_sec"],
                        },
                        characters=chars,
                        paid_batch_id=paid_batch_id,
                    )
                finally:
                    if voice_database is not db:
                        voice_database.close()
            idx, error, next_anchor = await _gen_dialogue(
                job_id, shot, char, {**sb_shot, "audio_mode": audio_mode}, sem,
                dialogue_anchors.get(identity_key),
                fixed_voice_track=fixed_voice_track,
                native_voice_anchor=(
                    voice_anchors.get(identity_key)
                    if audio_mode != AUDIO_MODE_FIXED_TTS else None
                ),
                cast_characters=cast_characters,
                identity_anchors=identity_anchors,
                scene_reference=spatial_reference,
                scene_video_anchor=scene_video_anchors.get(scene_key),
                paid_batch_id=paid_batch_id,
                asr_enabled=(sb.get("production_settings") or {}).get(
                    "asr_enabled", True
                ) is not False,
            )
            generated_voice = None
            if next_anchor is not None:
                generated_voice = _voice_anchor_from_completed_shot({
                    "video_path": str(
                        ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
                    )
                })
            return (
                idx, error, identity_key, scene_key,
                next_anchor, generated_voice,
            )
        elif shot["narrative_type"] == "dialogue" and render_mode in {
            "scene", "background_voiceover",
        }:
            speaker_id = shot["speaker_id"]
            scene_character = chars.get(speaker_id)
            voice_key = (
                character_assets.character_identity_key(scene_character)
                if scene_character else speaker_id
            )
            if voice_key not in voice_anchors:
                earlier_voice = [
                    item for item in completed_voice_anchors
                    if item[1] == voice_key and item[0] < shot["idx"]
                ]
                if earlier_voice:
                    voice_anchors[voice_key] = max(
                        earlier_voice, key=lambda item: item[0]
                    )[2]
            fixed_voice_track = None
            if audio_mode == AUDIO_MODE_FIXED_TTS:
                voice_database = conn()
                try:
                    fixed_voice_track = await _synthesize_fixed_voice_track(
                        voice_database,
                        job_id=job_id,
                        shot={
                            **shot, **sb_shot,
                            "duration_sec": shot["duration_sec"],
                        },
                        characters=chars,
                        paid_batch_id=paid_batch_id,
                    )
                finally:
                    if voice_database is not db:
                        voice_database.close()
            idx, error = await _gen_broll(
                job_id, shot, None, {**sb_shot, "audio_mode": audio_mode},
                sem, scene_voiceover=True,
                voice_anchor=voice_anchors.get(voice_key),
                fixed_voice_track=fixed_voice_track,
                scene_reference=spatial_reference,
                cast_characters=(
                    _scene_cast_characters(sb_shot, chars, "")
                    if render_mode == "background_voiceover" else None
                ),
                identity_anchors=(
                    identity_anchors if render_mode == "background_voiceover" else None
                ),
                paid_batch_id=paid_batch_id,
                asr_enabled=(sb.get("production_settings") or {}).get(
                    "asr_enabled", True
                ) is not False,
            )
            return idx, error, None, None, None, None
        else:
            anchor_id = shot.get("anchor_character")
            anchor_path = None
            if anchor_id:
                anchor_char = chars.get(anchor_id)
                if not anchor_char:
                    raise RuntimeError(
                        f"shot #{shot['idx']} anchor_character "
                        f"{anchor_id} not in characters"
                    )
                anchor_path = anchor_char.get("image_path")
            broll_cast = _scene_cast_characters(sb_shot, chars, "")
            idx, error = await _gen_broll(
                job_id, shot, anchor_path,
                {**sb_shot, "audio_mode": audio_mode}, sem,
                scene_reference=spatial_reference,
                cast_characters=broll_cast,
                identity_anchors=identity_anchors,
                paid_batch_id=paid_batch_id,
                asr_enabled=(sb.get("production_settings") or {}).get(
                    "asr_enabled", True
                ) is not False,
            )
            return idx, error, None, None, None, None

    pending_rows = {int(row["idx"]): row for row in rows}
    review_indexes = {int(row["idx"]) for row in review_rows}
    while pending_rows:
        ready_indexes, _blocked = master_dependencies.ready_master_wave(
            dependency_graph,
            pending_indexes=set(pending_rows),
            blocked_indexes=review_indexes,
        )
        if not ready_indexes:
            break
        wave = [pending_rows.pop(idx) for idx in ready_indexes]
        sem = asyncio.Semaphore(provider_capacity.effective_limit(
            db, "seedance", configured_limit=configured_concurrency
        ))
        paid_batch_id = provider_tasks.begin_paid_submit_batch(db, job_id)
        wave_results = await asyncio.gather(
            *(generate_row(row, paid_batch_id) for row in wave),
            return_exceptions=True,
        )
        fatal: BaseException | None = None
        wave_failed = False
        for row, outcome in zip(wave, wave_results):
            if isinstance(outcome, BaseException):
                wave_failed = True
                if isinstance(outcome, Exception):
                    results.append((int(row["idx"]), outcome))
                elif fatal is None:
                    fatal = outcome
                continue
            (
                idx, error, identity_key, scene_key,
                next_anchor, generated_voice,
            ) = outcome
            results.append((idx, error))
            wave_failed = wave_failed or error is not None
            if next_anchor is not None and identity_key:
                dialogue_anchors[identity_key] = next_anchor
                if scene_key:
                    scene_video_anchors[scene_key] = next_anchor
                if generated_voice is not None:
                    voice_anchors[identity_key] = generated_voice
        if fatal is not None:
            raise fatal
        if wave_failed:
            break
        provider_capacity.record_wave_success(
            db, "seedance", configured_limit=configured_concurrency
        )

    failed = [(i, e) for i, e in results if e is not None]
    failed.extend((
        int(row["idx"]),
        SeedanceManualReviewPendingError(
            "该母片已有付费候选，等待人工预览并选择接受或重做；"
            "系统不会自动重复生成。"
        ),
    ) for row in review_rows)
    if failed:
        raise _aggregate_shot_failures(failed)
