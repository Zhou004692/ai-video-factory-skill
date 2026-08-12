"""s3: 使用 Seedance 2.5 原生生成画面、语音与口型。

- presenter dialogue → 虚构真人原生开口，后续镜头复用视频与声线参考
- scene dialogue → 无人脸场景 + 原生画外音
- broll → 画面 + 原生环境音，不生成人声
"""
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path

from .. import ASSETS_DIR, SKILL_ROOT, asset_library
from ..db import conn
from ..clients import bigasr, ffmpeg, seedance, tos, video_qa
from ..orchestrator import MAX_CONCURRENT_SHOTS
from ..scene_voiceover import faceless_visual_text, non_text_visual_text
from ..video_routing import VideoRoute, precision_motion_reason, select_route


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
    """A verified native clip reused for speaker identity and native voice."""

    video_url: str


@dataclass(frozen=True)
class NativeVoiceAnchor:
    """A verified Seedance-native audio track for faceless voice-over."""

    audio_path: Path


class SeedanceQualityError(RuntimeError):
    """A failed quality gate that still carries billable provider usage."""

    non_retryable = True

    def __init__(self, message: str, *, cost_cents: int,
                 provider_calls: tuple[dict, ...]):
        super().__init__(message)
        self.cost_cents = cost_cents
        self.provider_calls = provider_calls


class SeedanceBudgetError(RuntimeError):
    """Raised before a paid provider call would exceed the job budget."""

    non_retryable = True


_HARD_RETRY_REASONS = frozenset({
    "low_confidence",
    "unexpected_speech",
    "transcript_mismatch",
    "unexpected_person",
    "unexpected_readable_text",
    "qa_error",
})


def _apply_master_transcription(
        storyboard: dict, master_idx: int,
        transcription: bigasr.Transcription) -> bool:
    """Replace estimated edit subtitles with master-relative ASR cues."""
    if storyboard.get("production_mode") != "native_master_local_director":
        return False

    timed = list(transcription.utterances)
    if (not timed and transcription.text
            and transcription.speech_start_ms is not None
            and transcription.speech_end_ms is not None):
        timed = [bigasr.TimedUtterance(
            transcription.text,
            transcription.speech_start_ms,
            transcription.speech_end_ms,
        )]
    if not timed:
        return False

    changed = False
    for edit in storyboard.get("shots") or []:
        audio_idx = int(edit.get("audio_master_idx",
                                 edit.get("video_master_idx", -1)))
        if audio_idx != int(master_idx):
            continue
        edit_start = float(edit.get("audio_start") or 0)
        duration = float(edit.get("duration") or 0)
        edit_end = edit_start + duration
        cues: list[dict] = []
        for utterance in timed:
            cue_start = utterance.start_ms / 1000.0
            cue_end = utterance.end_ms / 1000.0
            overlap_start = max(edit_start, cue_start)
            overlap_end = min(edit_end, cue_end)
            if overlap_end <= overlap_start:
                continue
            cues.append({
                "start": round(overlap_start - edit_start, 3),
                "end": round(overlap_end - edit_start, 3),
                "text": utterance.text,
            })
        if cues:
            edit["subtitle"] = None
            edit["subtitle_cues"] = cues
            changed = True
    return changed


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
    duration = max(1.0, float(request_kwargs.get("duration") or 1))
    resolution = str(request_kwargs.get("resolution") or "720p").lower()
    cents_per_second = {
        "480p": 68,
        "720p": 102,
        "1080p": 136,
    }.get(resolution, 102)
    reference_factor = 2 if request_kwargs.get("reference_videos") else 1
    return round(duration * cents_per_second * reference_factor)


def _job_budget_cents() -> int:
    return _env_nonnegative_int("SEEDANCE_JOB_BUDGET_CENTS", 2500)


def _enforce_job_budget(job_id: str | None, *, projected_cost_cents: int,
                        pending_cost_cents: int = 0) -> None:
    limit = _job_budget_cents()
    if not job_id or limit == 0:
        return
    row = conn().execute(
        "SELECT cost_cents FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    current = int(row["cost_cents"] or 0) if row else 0
    projected_total = current + pending_cost_cents + projected_cost_cents
    if projected_total > limit:
        raise SeedanceBudgetError(
            "Seedance job budget exceeded before paid call: "
            f"current={current}, pending={pending_cost_cents}, "
            f"next_estimate={projected_cost_cents}, limit={limit} cents"
        )


def _auto_retry_denial(reasons: tuple[str, ...],
                       projected_retry_cost_cents: int) -> str | None:
    if _env_nonnegative_int("SEEDANCE_MAX_AUTO_RETRIES", 1) == 0:
        return "automatic retries disabled"
    if not (_HARD_RETRY_REASONS & set(reasons)):
        return "manual review required for soft-only visual mismatch"
    retry_budget = _env_nonnegative_int(
        "SEEDANCE_AUTO_RETRY_BUDGET_CENTS", 800
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
    return aggregate


def _record_provider_calls(db, job_id: str, idx: int, provider: str,
                           provider_calls: tuple[dict, ...],
                           *, fallback_cost_cents: int = 0) -> int:
    """Return only newly billable cost, deduplicated by provider task id."""
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
        cursor = db.execute(
            "INSERT OR IGNORE INTO provider_calls("
            "task_id, job_id, shot_idx, provider, intent, cost_cents, "
            "usage_json) VALUES(?,?,?,?,?,?,?)",
            (
                task_id, job_id, idx, provider, call.get("intent"), cost,
                json.dumps(call.get("usage") or {}, ensure_ascii=False),
            ),
        )
        if cursor.rowcount == 1:
            new_cost += cost
    return new_cost


def _record_failed_generation(db, job_id: str, idx: int,
                              exc: BaseException) -> None:
    error = f"{type(exc).__name__}: {str(exc)[:900]}"
    if isinstance(exc, SeedanceQualityError):
        cost = _record_provider_calls(
            db, job_id, idx, "seedance", exc.provider_calls,
            fallback_cost_cents=exc.cost_cents,
        )
        qa_note = json.dumps({
            "status": "failed",
            "reason": str(exc),
            "provider_calls": exc.provider_calls,
        }, ensure_ascii=False)
        db.execute(
            "UPDATE shots SET state='FAILED', error=?, provider='seedance', "
            "video_qa=?, cost_cents=cost_cents+? WHERE job_id=? AND idx=?",
            (error, qa_note, cost, job_id, idx),
        )
        db.execute(
            "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
            (cost, job_id),
        )
    else:
        db.execute(
            "UPDATE shots SET state='FAILED', error=? WHERE job_id=? AND idx=?",
            (error, job_id, idx),
        )
    db.commit()


def _dialogue_request_kwargs(
    shot: dict,
    anchor: DialogueAnchor | None,
    *,
    character: dict | None = None,
) -> dict:
    request = {
        "duration": shot["duration_sec"],
        "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
        "ratio": "9:16",
        "camera_fixed": False,
        "motion_strength": 0.4,
    }
    if anchor is not None:
        request["reference_videos"] = [anchor.video_url]
    elif character:
        roster_character = _roster_character(character.get("id"))
        identity_type = (
            character.get("identity_type")
            or roster_character.get("identity_type")
        )
        asset_uri = (
            character.get("seedance_asset_uri")
            or roster_character.get("seedance_asset_uri")
        )
        if identity_type == "real_person":
            if not str(asset_uri or "").startswith("asset://"):
                raise SeedanceIdentityAuthorizationError(
                    "real-person fixed character requires an authorized "
                    "Seedance portrait-library asset:// URI before any paid "
                    "generation"
                )
            request["references"] = [str(asset_uri)]
            return request

        portrait = character.get("image_path")
        reference_image = (
            character.get("reference_image")
            or roster_character.get("reference_image")
        )
        if not portrait and reference_image:
            candidate = Path(str(reference_image))
            portrait = candidate if candidate.is_absolute() else SKILL_ROOT / candidate
        if portrait and Path(portrait).exists():
            request["references"] = [str(portrait)]
    return request


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


def _dialogue_anchor_from_completed_shot(
        shot: dict) -> DialogueAnchor | None:
    video_url = str(shot.get("video_url") or "").strip()
    video_path = shot.get("video_path")
    if not video_url or not video_path:
        return None
    if not Path(video_path).exists():
        return None
    return DialogueAnchor(video_url)


def _voice_anchor_from_completed_shot(
        shot: dict) -> NativeVoiceAnchor | None:
    video_path = shot.get("video_path")
    if not video_path:
        return None
    audio_path = Path(video_path).with_suffix(".wav")
    return NativeVoiceAnchor(audio_path) if audio_path.exists() else None


def _scene_voiceover_request_kwargs(
        shot: dict, anchor: NativeVoiceAnchor | None) -> dict:
    request = {
        "duration": shot["duration_sec"],
        "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
        "ratio": "9:16",
    }
    if anchor is not None:
        request["reference_audios"] = [str(anchor.audio_path)]
    return request


def _native_audio_prompt(prompt: str, enabled: bool,
                         expected_text: str | None = None) -> str:
    if not enabled:
        return prompt
    if expected_text:
        return prompt
    return (
        f"{prompt}\n\n音频要求：只生成与画面动作匹配的环境音和物件音效；"
        "不得出现任何人声、对白、旁白、哼唱或背景音乐。"
    )


async def _generate_seedance_video(*, prompt: str, route: VideoRoute,
                                    request_kwargs: dict,
                                    out_path: Path,
                                    key_hint: str,
                                    job_id: str | None = None,
                                    forbid_people: bool = False,
                                    expected_text: str | None = None,
                                    ) -> SeedanceGenerationResult:
    """Generate, upload and gate a Seedance shot.

    Native audio and visuals fail closed. A failed 2.5 generation is retried
    once on the same model; the production path never falls back to external
    speech synthesis or another digital-human model.
    """
    spent_cost_cents = 0
    provider_calls: list[dict] = []

    async def generate(
        model: str,
        generate_audio: bool,
        *,
        generation_prompt: str = prompt,
        generation_kwargs: dict | None = None,
        target_path: Path = out_path,
        upload_hint: str = key_hint,
    ) -> tuple[bytes, Path, str, Path | None]:
        nonlocal spent_cost_cents
        metadata: dict = {}
        video = await seedance.text_to_video(
            prompt=_native_audio_prompt(
                generation_prompt, generate_audio, expected_text
            ),
            model=model,
            generate_audio=generate_audio,
            metadata_out=metadata,
            **(generation_kwargs or request_kwargs),
        )
        call_cost = seedance.usage_cost_cents(metadata.get("usage") or {})
        if not call_cost:
            fallback_duration = float(request_kwargs.get("duration") or 0)
            call_cost = seedance.estimate_cost_cents(
                fallback_duration,
                request_kwargs.get("resolution", "720p"),
            )
        spent_cost_cents += call_cost
        call_kwargs = generation_kwargs or request_kwargs
        provider_calls.append({
            "task_id": metadata.get("task_id"),
            "intent": call_kwargs.get("task_intent", "generate"),
            "usage": metadata.get("usage") or {},
            "cost_cents": call_cost,
        })
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(video)
        url = tos.upload(target_path, key_hint=upload_hint)
        audio_path = None
        if generate_audio:
            audio_path = target_path.with_suffix(".wav")
            await ffmpeg.extract_audio_wav(target_path, audio_path)
        return video, target_path, url, audio_path

    async def assess(url: str, audio_path: Path | None
                     ) -> tuple[dict, video_qa.QADecision,
                                bigasr.Transcription]:
        transcription = bigasr.Transcription("")
        try:
            raw, _ = await asyncio.to_thread(
                video_qa.analyze,
                url,
                expect_speech=False,
                expected_visual=prompt,
                forbid_people=forbid_people,
                forbid_readable_text=True,
            )
            transcript = ""
            if audio_path is not None:
                transcription = await asyncio.to_thread(
                    bigasr.transcribe_detailed, audio_path
                )
                transcript = transcription.text
            raw["transcript"] = transcript
            raw["confidence"] = 1.0
            decision = video_qa.evaluate(
                raw,
                expect_speech=bool(expected_text),
                expected_text=expected_text,
                expected_visual=prompt,
                forbid_people=forbid_people,
                forbid_readable_text=True,
            )
            return raw, decision, transcription
        except Exception as exc:
            return (
                {"notes": f"qa_error: {exc}"},
                video_qa.QADecision(False, ("qa_error",)),
                transcription,
            )

    _enforce_job_budget(
        job_id,
        projected_cost_cents=_estimated_request_cost_cents(request_kwargs),
    )
    video, video_path, video_url, audio_path = await generate(
        route.model, route.generate_audio
    )
    if not route.qa_required:
        return SeedanceGenerationResult(
            video, video_path, video_url,
            json.dumps({
                "status": "not_required",
                "provider_calls": provider_calls,
            }, ensure_ascii=False),
            route.generate_audio, route.model, audio_path, spent_cost_cents,
            tuple(provider_calls),
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

    if decision.passed:
        return SeedanceGenerationResult(
            video, video_path, video_url, qa_note, route.generate_audio, route.model,
            audio_path, spent_cost_cents, tuple(provider_calls),
            transcription,
        )

    projected_retry_cost = (
        int(provider_calls[-1]["cost_cents"])
        if provider_calls else _estimated_request_cost_cents(request_kwargs)
    )
    denial = _auto_retry_denial(decision.reasons, projected_retry_cost)
    if denial is None:
        try:
            _enforce_job_budget(
                job_id,
                projected_cost_cents=projected_retry_cost,
                pending_cost_cents=spent_cost_cents,
            )
        except SeedanceBudgetError as exc:
            denial = str(exc)
    if denial is not None:
        raise SeedanceQualityError(
            "Seedance 2.5 quality gate requires manual review; "
            f"auto retry skipped: {denial}; reasons="
            f"{','.join(decision.reasons)}",
            cost_cents=spent_cost_cents,
            provider_calls=tuple(provider_calls),
        )

    video, video_path, video_url, audio_path = await generate(
        route.model, route.generate_audio
    )
    retry_raw, retry_decision, retry_transcription = await assess(
        video_url, audio_path
    )
    if not retry_decision.passed:
        raise SeedanceQualityError(
            "Seedance 2.5 quality gate failed after retry: "
            f"{','.join(retry_decision.reasons)}",
            cost_cents=spent_cost_cents,
            provider_calls=tuple(provider_calls),
        )
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
                        anchor: DialogueAnchor | None = None
                        ) -> tuple[int, Exception | None,
                                   DialogueAnchor | None]:
    """Generate one verified native-speaking Seedance 2.5 shot."""
    async with sem:
        try:
            combined = {**shot, **sb_shot}
            combined["voiceover"] = (
                sb_shot.get("voiceover") or shot.get("voiceover") or ""
            )
            route = select_route(shot, sb_shot)
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            result = await _generate_seedance_video(
                prompt=_build_native_dialogue_prompt(
                    combined, char, anchored=anchor is not None
                ),
                route=route,
                request_kwargs=_dialogue_request_kwargs(
                    shot, anchor, character=char
                ),
                out_path=out_path,
                key_hint=f"{job_id}-dialogue-{shot['idx']:02d}",
                job_id=job_id,
                expected_text=combined["voiceover"],
            )
            if result.audio_path is None:
                raise RuntimeError("Seedance 2.5 returned no native audio")

            _persist_master_transcription(
                job_id, shot["idx"], result.transcription
            )

            db = conn()
            cost = _record_provider_calls(
                db, job_id, shot["idx"], "seedance",
                result.provider_calls,
                fallback_cost_cents=result.cost_cents,
            )
            db.execute(
                "UPDATE shots SET video_path=?, video_url=?, state='OK', "
                "provider='seedance', video_model=?, native_audio=1, "
                "video_qa=?, cost_cents=cost_cents+?, error=NULL "
                "WHERE job_id=? AND idx=?",
                (
                    str(result.video_path), result.video_url, result.model,
                    result.qa_note, cost, job_id, shot["idx"],
                ),
            )
            db.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id),
            )
            db.commit()
            return (
                shot["idx"], None,
                DialogueAnchor(result.video_url),
            )
        except BaseException as exc:
            try:
                db = conn()
                _record_failed_generation(db, job_id, shot["idx"], exc)
            except Exception:
                pass
            if isinstance(exc, Exception):
                return shot["idx"], exc, None
            raise


def _director_time_beats(duration: float, *, dialogue: str,
                         scene_voiceover: bool = False) -> list[str]:
    """Create a compact, continuous Seedance director timeline."""
    duration = max(1.0, float(duration))
    establish_end = min(1.0, max(0.4, duration * 0.12))
    settle_start = max(establish_end + 0.5, duration - 0.6)
    speech_label = "画外音" if scene_voiceover else "台词"
    subject = "画外音开始，画面继续自然运动" if scene_voiceover else "人物自然开口并完成动作与表情变化"
    return [
        f"0.0-{establish_end:.1f}秒：建立场景、人物或物件关系，动作自然起势。",
        f"{establish_end:.1f}-{settle_start:.1f}秒：{subject}，完整说出且只能说出 {{{speech_label}：{dialogue}}}。",
        f"{settle_start:.1f}-{duration:.1f}秒：话音自然结束，动作收束，画面保持连续稳定。",
    ]


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
    duration = float(shot.get("duration_sec") or 8)
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
            duration, dialogue=voiceover, scene_voiceover=True
        ),
        "这是画外音(V.O.)场景，下面的台词只进入原生音轨，不在画面中呈现说话者",
        f"画外音完整说出且只能说出 {{画外音：{voiceover}}}",
        "使用自然、清晰、专业的中国成年人普通话；不得改写、增删、重复或复述其他内容",
        f"<环境音：{sound_design}>；无 BGM，不要字幕。",
        "不得出现任何可识别人脸、正面人物、真人肖像或嘴部特写，不做口型同步",
        "优先拍摄环境、设备、文件、仪表和物件动态；如必须出现人物，只能是脸部完全不可见的手部、背影或远景剪影",
        "所有标签、铭牌、钢印、纸张和屏幕区域必须使用背面、遮挡、裁切或浅景深处理，保持完全失焦",
        "画面内不得出现任何字母或数字，不要生成字幕、标牌文字或其他可读文字",
    ])
    return "\n".join(parts)


def _build_native_dialogue_prompt(shot: dict, character: dict,
                                  *, anchored: bool = False) -> str:
    """Build one exact native-speaking Seedance 2.5 presenter prompt."""
    voiceover = str(shot.get("voiceover") or "").strip()
    if not voiceover:
        raise ValueError(f"shot #{shot.get('idx')} has empty voiceover")
    duration = float(shot.get("duration_sec") or 8)
    sound_design = _sound_design_for(shot)
    has_fixed_reference = bool(
        character.get("reference_image")
        or character.get("image_path")
        or character.get("seedance_asset_uri")
        or _roster_character(character.get("id")).get("reference_image")
        or _roster_character(character.get("id")).get("seedance_asset_uri")
    )
    identity_instruction = (
        "人物必须严格沿用素材中的固定角色身份参考，保持同一张脸、五官比例、"
        "视觉年龄、发际线与基础发型；不得泛化为相似路人或网红脸。"
        if has_fixed_reference
        else "生成一名完全虚构、由 AI 创造且无现实身份对应的讲解角色。"
    )
    parts = [
        "创作意图：生成一段可信、克制、可直接交付的真人讲解镜头，画面表演与原生对白同步完成。",
        identity_instruction,
        f"人物姓名与设定：{character.get('name', '')}；{character.get('look', '')}",
        f"场景：{shot.get('scene') or shot.get('prompt') or ''}",
        f"镜头：{shot.get('camera') or ''}",
        f"动作：{shot.get('action') or ''}",
        f"表情变化：{shot.get('expression_arc') or shot.get('dramatic_keywords') or ''}",
        f"声音与语气：{shot.get('voice_directive') or '自然、专业、清晰的中国成年人普通话'}",
        "时间轴：",
        *_director_time_beats(duration, dialogue=voiceover),
        f"人物完整说出且只能说出 {{台词：{voiceover}}}",
        f"<环境音：{sound_design}>；无 BGM，不要字幕。",
        "不得改写、增删、重复台词，不得加入旁白、第二个人声或背景音乐。",
        "嘴唇、牙齿、舌位和下颌逐字同步，句末自然闭嘴。",
        "人物身份、五官、发型、服装、背景和光线全程稳定。",
        "不要字幕、可读文字、品牌、标识或水印。",
    ]
    if anchored:
        parts[1:1] = [
            "素材职责：",
            "视频1只参考人物身份与原生声线，以及五官、发型、服装、空间连续性与基础表演。",
            "不得复述参考视频中的台词，只说本镜新台词。",
        ]
    return "\n".join(parts)


def _build_broll_video_prompt(shot: dict) -> str:
    """Use only the shot-level visual direction."""
    return shot["prompt"]


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
                     ) -> tuple[int, Exception | None]:
    async with sem:
        try:
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
            out_path = ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
            request_kwargs = (
                _scene_voiceover_request_kwargs(shot, voice_anchor)
                if scene_voiceover else {
                    "duration": shot["duration_sec"],
                    "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
                    "ratio": "9:16",
                }
            )
            request_kwargs.update({
                "first_frame": keyframe,
                "last_frame": last_frame,
                "references": extra_refs or None,
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
                forbid_people=scene_voiceover,
                expected_text=(
                    sb_shot.get("voiceover") or shot.get("voiceover")
                    if scene_voiceover else None
                ),
            )

            if scene_voiceover:
                _persist_master_transcription(
                    job_id, shot["idx"], result.transcription
                )

            db = conn()
            cost = _record_provider_calls(
                db, job_id, shot["idx"], "seedance",
                result.provider_calls,
                fallback_cost_cents=result.cost_cents,
            )
            db.execute(
                "UPDATE shots SET video_path=?, video_url=?, state='OK', "
                "provider='seedance', video_model=?, native_audio=?, "
                "video_qa=?, cost_cents=cost_cents+?, error=NULL "
                "WHERE job_id=? AND idx=?",
                (
                    str(result.video_path), result.video_url, result.model,
                    int(result.native_audio), result.qa_note, cost,
                    job_id, shot["idx"],
                )
            )
            db.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id)
            )
            db.commit()
            return shot["idx"], None
        except Exception as e:
            db = conn()
            _record_failed_generation(db, job_id, shot["idx"], e)
            return shot["idx"], e


async def run(job_id: str) -> None:
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    sb = json.loads(job["storyboard"])
    chars = {c["id"]: c for c in json.loads(job["characters_json"])}
    generation_units = sb.get("masters") or sb["shots"]
    sb_shots = {s["idx"]: s for s in generation_units}

    # Resolve deterministic, reviewed local footage before any stochastic
    # Seedance submission. Matched rows become OK and disappear from the
    # pending-generation query below.
    if sb.get("production_mode") == "native_master_local_director":
        asset_library.reuse_broll_assets(db, job_id, generation_units)

    rows = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND state!='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
    if not rows:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_SHOTS)
    results: list[tuple[int, Exception | None]] = []
    dialogue_anchors: dict[str, DialogueAnchor] = {}
    voice_anchors: dict[str, NativeVoiceAnchor] = {}
    completed_anchors: list[tuple[int, str, DialogueAnchor]] = []
    completed_voice_anchors: list[tuple[int, str, NativeVoiceAnchor]] = []
    completed_dialogue = db.execute(
        "SELECT idx, speaker_id, video_path, video_url FROM shots "
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
        if mode in ("presenter", "digital_human") and anchor is not None:
            completed_anchors.append((
                completed_shot["idx"], completed_shot["speaker_id"], anchor
            ))
            if voice_anchor is not None:
                completed_voice_anchors.append((
                    completed_shot["idx"], completed_shot["speaker_id"],
                    voice_anchor,
                ))
    for r in rows:
        shot = dict(r)
        sb_shot = sb_shots.get(shot["idx"], {})
        # Dialogue shots are deliberately sequential: each verified native
        # clip becomes the identity + voice reference for that speaker's next
        # clip. This is the consistency route proven in the 2.5 validation.
        render_mode = (sb_shot.get("render_mode") or "presenter").lower()
        if (shot["narrative_type"] == "dialogue"
                and render_mode in ("presenter", "digital_human")):
            char = chars.get(shot["speaker_id"])
            if not char:
                raise RuntimeError(
                    f"shot #{shot['idx']} speaker {shot['speaker_id']} not found"
                )
            if shot["speaker_id"] not in dialogue_anchors:
                earlier = [
                    item for item in completed_anchors
                    if item[1] == shot["speaker_id"] and item[0] < shot["idx"]
                ]
                if earlier:
                    dialogue_anchors[shot["speaker_id"]] = max(
                        earlier, key=lambda item: item[0]
                    )[2]
            idx, error, next_anchor = await _gen_dialogue(
                job_id, shot, char, sb_shot, sem,
                dialogue_anchors.get(shot["speaker_id"]),
            )
            results.append((idx, error))
            if next_anchor is not None:
                dialogue_anchors[shot["speaker_id"]] = next_anchor
                generated_voice = _voice_anchor_from_completed_shot({
                    "video_path": str(
                        ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
                    )
                })
                if generated_voice is not None:
                    voice_anchors[shot["speaker_id"]] = generated_voice
        elif shot["narrative_type"] == "dialogue" and render_mode == "scene":
            speaker_id = shot["speaker_id"]
            if speaker_id not in voice_anchors:
                earlier_voice = [
                    item for item in completed_voice_anchors
                    if item[1] == speaker_id and item[0] < shot["idx"]
                ]
                if earlier_voice:
                    voice_anchors[speaker_id] = max(
                        earlier_voice, key=lambda item: item[0]
                    )[2]
            results.append(await _gen_broll(
                job_id, shot, None, sb_shot, sem, scene_voiceover=True,
                voice_anchor=voice_anchors.get(speaker_id),
            ))
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
            results.append(await _gen_broll(
                job_id, shot, anchor_path, sb_shot, sem
            ))

    failed = [(i, e) for i, e in results if e is not None]
    if failed:
        raise _aggregate_shot_failures(failed)
