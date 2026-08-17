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
import re

from .. import (
    ASSETS_DIR, SKILL_ROOT, asset_library, character_assets, scene_assets,
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
)
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


class SeedanceBudgetError(RuntimeError):
    """The next paid provider call would exceed the confirmed job budget."""

    non_retryable = True


class SeedanceManualReviewPendingError(RuntimeError):
    """An already-paid candidate is waiting for a human decision."""

    non_retryable = True


class ContinuityAnchorReviewPendingError(RuntimeError):
    """Pause production after paid anchors are ready for human approval."""

    non_retryable = True
    pause_state = "ANCHOR_REVIEW_REQUIRED"


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
    duration = seedance.normalize_generation_duration(
        request_kwargs.get("duration")
    )
    resolution = str(request_kwargs.get("resolution") or "720p").lower()
    cents_per_second = {
        "480p": 68,
        "720p": 102,
        "1080p": 136,
    }.get(resolution, 102)
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
            f"next_estimate={estimate}, limit={limit} cents"
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
            "status": "manual_review_required",
            "reason": str(exc),
            "reasons": exc.reasons,
            "candidate_path": (
                str(exc.candidate_path) if exc.candidate_path else None
            ),
            "candidate_url": exc.candidate_url,
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
            f"limit={limit} cents"
        )


async def _ensure_character_identity_anchors(
    job_id: str,
    characters: list[dict],
) -> dict[str, DialogueAnchor]:
    """Prepare every non-Volc character before any paid scene generation.

    Authorized real/virtual portrait-library assets are already identity
    anchors. AI-created roles default to one Seedream 5.0 static casting
    portrait. Local custom roles retain the privacy-safe dynamic Seedance
    identity-master route. The table makes this stage resumable and prevents a
    second purchase after a worker restart.
    """
    database = conn()
    anchors: dict[str, DialogueAnchor] = {}
    makeup_plan = character_assets.character_makeup_plan(characters)
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
        if existing and existing["state"] == "READY":
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
                    image = await seedance.text_to_image(
                        _identity_portrait_prompt(character),
                        resolution=os.getenv(
                            "ARK_IDENTITY_IMAGE_SIZE", "1440x2560"
                        ),
                        model=identity_image_model,
                    )
                    out_path.write_bytes(image)
                    cost = _record_provider_calls(
                        database,
                        job_id,
                        -1000 - ordinal,
                        "seedream_identity",
                        ({
                            "task_id": (
                                f"seedream-identity:{job_id}:"
                                f"{entry['identity_token']}"
                            ),
                            "intent": "character_identity_anchor",
                            "usage": {
                                "model": identity_image_model,
                                "anchor_mode": "static_image",
                            },
                            "cost_cents": image_cost,
                        },),
                        fallback_cost_cents=image_cost,
                    )
                    database.execute(
                        "UPDATE character_identity_anchors SET "
                        "anchor_mode='static_image',image_path=?,"
                        "preview_path=?,duration_sec=0,"
                        "cost_cents=cost_cents+?,error=NULL,"
                        "updated_at=CURRENT_TIMESTAMP "
                        "WHERE job_id=? AND identity_key=?",
                        (
                            str(out_path), str(out_path), cost,
                            job_id, identity_key,
                        ),
                    )
                    database.execute(
                        "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                        (cost, job_id),
                    )
                    database.commit()
                image_url = tos.upload(
                    out_path,
                    key_hint=f"{job_id}-identity-{entry['identity_token']}",
                )
                database.execute(
                    "UPDATE character_identity_anchors SET state='READY',"
                    "anchor_mode='static_image',image_path=?,image_url=?,"
                    "preview_path=?,video_path=NULL,video_url=NULL,"
                    "duration_sec=0,error=NULL,"
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND identity_key=?",
                    (
                        str(out_path), image_url, str(out_path),
                        job_id, identity_key,
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
                    model=os.getenv(
                        "ARK_VIDEO_PRIMARY_MODEL",
                        "doubao-seedance-2-5-260628",
                    ),
                    tier="seedance25",
                    generate_audio=False,
                    qa_required=False,
                    fallback_model=None,
                ),
                request_kwargs={
                    "duration": 4,
                    "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
                    "ratio": "9:16",
                    "camera_fixed": True,
                    "motion_strength": 0.15,
                },
                out_path=out_path,
                key_hint=f"{job_id}-identity-{entry['identity_token']}",
                job_id=job_id,
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
                "UPDATE character_identity_anchors SET state='READY',"
                "video_path=?,video_url=?,preview_path=NULL,duration_sec=4,"
                "cost_cents=cost_cents+?,error=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND identity_key=?",
                (
                    str(result.video_path), result.video_url, cost,
                    job_id, identity_key,
                ),
            )
            database.execute(
                "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                (cost, job_id),
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
        try:
            preview_paths = await ffmpeg.extract_frames(
                result.video_path, anchor_dir / "preview", [1.0]
            )
            preview_path = str(preview_paths[0]) if preview_paths else ""
            if preview_path:
                database.execute(
                    "UPDATE character_identity_anchors SET preview_path=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
                    (preview_path, job_id, identity_key),
                )
                database.commit()
        except Exception as preview_exc:
            database.execute(
                "INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)",
                (
                    job_id, "character_identity_preview", "warning",
                    f"{entry['display_name']} 定妆帧提取失败，身份母片仍可复用："
                    f"{preview_exc}",
                ),
            )
            database.commit()
    return anchors


async def _fallback_static_identity_anchors(
    job_id: str,
    characters: list[dict],
    identity_anchors: dict[str, DialogueAnchor],
) -> None:
    """Replace static identities with native clips after a privacy rejection.

    Seedance rejects an ordinary reference image before creating the paid
    scene task. The fallback therefore buys one four-second, non-speaking
    native identity master and retries the scene once without the image.
    """
    database = conn()
    seen: set[str] = set()
    for ordinal, character in enumerate(characters, start=1):
        identity_key = character_assets.character_identity_key(character)
        if identity_key in seen:
            continue
        seen.add(identity_key)
        current = identity_anchors.get(identity_key)
        if current is None or current.anchor_mode != "static_image":
            continue
        token = character_assets.identity_token(identity_key)
        anchor_dir = ASSETS_DIR / job_id / "character_identity" / token
        anchor_dir.mkdir(parents=True, exist_ok=True)
        out_path = anchor_dir / "identity-fallback.mp4"
        database.execute(
            "UPDATE character_identity_anchors SET state='GENERATING',"
            "source_type='seedance_identity_fallback',"
            "anchor_mode='dynamic_video',error=NULL,"
            "updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND identity_key=?",
            (job_id, identity_key),
        )
        database.commit()
        try:
            result = await _generate_seedance_video(
                prompt=_identity_anchor_prompt(character),
                route=VideoRoute(
                    model=os.getenv(
                        "ARK_VIDEO_PRIMARY_MODEL",
                        "doubao-seedance-2-5-260628",
                    ),
                    tier="seedance25",
                    generate_audio=False,
                    qa_required=False,
                    fallback_model=None,
                ),
                request_kwargs={
                    "duration": 4,
                    "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
                    "ratio": "9:16",
                    "camera_fixed": True,
                    "motion_strength": 0.15,
                },
                out_path=out_path,
                key_hint=f"{job_id}-identity-fallback-{token}",
                job_id=job_id,
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
                "UPDATE character_identity_anchors SET state='READY',"
                "source_type='seedance_identity_fallback',"
                "anchor_mode='dynamic_video',video_path=?,video_url=?,"
                "duration_sec=4,cost_cents=cost_cents+?,error=NULL,"
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND identity_key=?",
                (
                    str(result.video_path), result.video_url, cost,
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
            identity_anchors[identity_key] = DialogueAnchor(
                result.video_url,
                4,
                anchor_mode="dynamic_video",
            )
            try:
                previews = await ffmpeg.extract_frames(
                    result.video_path, anchor_dir / "fallback-preview", [1.0]
                )
                if previews:
                    database.execute(
                        "UPDATE character_identity_anchors SET preview_path=? "
                        "WHERE job_id=? AND identity_key=?",
                        (str(previews[0]), job_id, identity_key),
                    )
                    database.commit()
            except Exception:
                pass
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
) -> dict[str, str]:
    """Generate one empty-location image for each explicitly repeated scene."""
    database = conn()
    resolved: dict[str, str] = {}
    image_model = os.getenv(
        "ARK_SCENE_IMAGE_MODEL", "doubao-seedream-5-0-260128"
    )
    for ordinal, spec in enumerate(
        scene_assets.recurring_scene_anchors(storyboard), start=1
    ):
        key = spec["anchor_key"]
        existing = database.execute(
            "SELECT * FROM scene_visual_anchors WHERE job_id=? AND anchor_key=?",
            (job_id, key),
        ).fetchone()
        if (
            existing and existing["state"] == "READY"
            and existing["image_url"] and existing["image_path"]
            and Path(existing["image_path"]).is_file()
        ):
            resolved[key] = str(existing["image_url"])
            continue
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
        anchor_dir = ASSETS_DIR / job_id / "scene_identity"
        anchor_dir.mkdir(parents=True, exist_ok=True)
        out_path = anchor_dir / f"{scene_assets.anchor_token(key)}.png"
        image_cost = seedream_45.estimate_cost_cents()
        try:
            reusable = bool(
                existing and existing["image_path"]
                and Path(existing["image_path"]).is_file()
                and int(existing["cost_cents"] or 0) > 0
            )
            if reusable:
                out_path = Path(existing["image_path"])
            else:
                _assert_fixed_cost_within_job_budget(job_id, image_cost)
                image = await seedance.text_to_image(
                    scene_assets.scene_anchor_prompt(spec),
                    resolution=os.getenv("ARK_SCENE_IMAGE_SIZE", "1440x2560"),
                    model=image_model,
                )
                out_path.write_bytes(image)
                cost = _record_provider_calls(
                    database, job_id, -2000 - ordinal, "seedream_scene",
                    ({
                        "task_id": f"seedream-scene:{job_id}:{key}",
                        "intent": "scene_visual_anchor",
                        "usage": {"model": image_model, "anchor_key": key},
                        "cost_cents": image_cost,
                    },), fallback_cost_cents=image_cost,
                )
                database.execute(
                    "UPDATE scene_visual_anchors SET image_path=?,preview_path=?,"
                    "cost_cents=cost_cents+? WHERE job_id=? AND anchor_key=?",
                    (str(out_path), str(out_path), cost, job_id, key),
                )
                database.execute(
                    "UPDATE jobs SET cost_cents=cost_cents+? WHERE job_id=?",
                    (cost, job_id),
                )
                database.commit()
            image_url = tos.upload(
                out_path,
                key_hint=f"{job_id}-scene-{scene_assets.anchor_token(key)}",
            )
            database.execute(
                "UPDATE scene_visual_anchors SET state='READY',image_path=?,"
                "image_url=?,preview_path=?,error=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=? AND anchor_key=?",
                (str(out_path), image_url, str(out_path), job_id, key),
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


def _dialogue_request_kwargs(
    shot: dict,
    anchor: DialogueAnchor | None,
    *,
    character: dict | None = None,
    cast_characters: list[dict] | None = None,
    identity_anchors: dict[str, DialogueAnchor] | None = None,
    scene_reference: str | None = None,
    scene_video_anchor: DialogueAnchor | None = None,
) -> dict:
    request = {
        "duration": seedance.normalize_generation_duration(
            shot["duration_sec"]
        ),
        "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
        "ratio": "9:16",
        "camera_fixed": False,
        "motion_strength": 0.4,
    }
    video_references: list[DialogueAnchor] = []
    asset_references: list[str] = []
    identity_anchors = identity_anchors or {}
    if scene_reference:
        asset_references.append(scene_reference)

    if scene_video_anchor is not None:
        video_references.append(scene_video_anchor)

    if anchor is not None:
        video_references.append(anchor)

    if character:
        identity_anchor = identity_anchors.get(
            character_assets.character_identity_key(character)
        )
        if identity_anchor is not None:
            if identity_anchor.image_reference:
                asset_references.append(identity_anchor.image_reference)
            elif anchor is None and identity_anchor.video_url:
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
        if len(deduped_videos) > 3:
            raise SeedanceIdentityAuthorizationError(
                "one scene contains more than three generated identities; "
                "Seedance 2.5 accepts at most three reference videos, so the "
                "scene must be split before any paid generation"
            )
        request["reference_videos"] = [
            reference.video_url for reference in deduped_videos
        ]
        request["reference_video_duration_sec"] = sum(
            reference.duration_sec for reference in deduped_videos
        )
    if asset_references:
        request["references"] = list(dict.fromkeys(asset_references))

    if anchor is None and character and not (
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
    explicit = shot.get("cast") or []
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
    for character_id, character in characters.items():
        if character_id == speaker_id:
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
        included = bool(names & explicit_names) if explicit_names else any(
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
    request = {
        "duration": seedance.normalize_generation_duration(
            shot["duration_sec"]
        ),
        "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
        "ratio": "9:16",
    }
    if anchor is not None:
        request["reference_audios"] = [str(anchor.audio_path)]
    return request


def _broll_request_kwargs(shot: dict) -> dict:
    return {
        "duration": seedance.normalize_generation_duration(
            shot["duration_sec"]
        ),
        "resolution": os.getenv("ARK_VIDEO_RESOLUTION", "720p"),
        "ratio": "9:16",
    }


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
                                    require_broll_semantics: bool = False,
                                    continuity_context: dict | None = None,
                                    ) -> SeedanceGenerationResult:
    """Generate, upload and gate a Seedance shot.

    Native audio and visuals fail closed. A failed 2.5 generation is retried
    once on the same model; the production path never falls back to external
    speech synthesis or another digital-human model.
    """
    spent_cost_cents = 0
    provider_calls: list[dict] = []
    candidate_path = out_path.with_name(
        f".{out_path.stem}.candidate{out_path.suffix}"
    )

    def commit_candidate(
        video_path: Path, audio_path: Path | None
    ) -> tuple[Path, Path | None]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.replace(out_path)
        if audio_path is None:
            return out_path, None
        final_audio = out_path.with_suffix(".wav")
        if audio_path.exists():
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
        candidate_audio = candidate_path.with_suffix(".wav")
        if candidate_audio.exists():
            review_audio = review_path.with_suffix(".wav")
            review_audio.unlink(missing_ok=True)
            candidate_audio.replace(review_audio)
        return review_path

    async def generate(
        model: str,
        generate_audio: bool,
        *,
        generation_prompt: str = prompt,
        generation_kwargs: dict | None = None,
        target_path: Path = candidate_path,
        upload_hint: str = key_hint,
    ) -> tuple[bytes, Path, str, Path | None]:
        nonlocal spent_cost_cents
        metadata: dict = {}
        call_kwargs = dict(generation_kwargs or request_kwargs)
        _assert_paid_call_within_job_budget(
            job_id,
            spent_cost_cents=spent_cost_cents,
            request_kwargs=call_kwargs,
        )
        provider_kwargs = dict(call_kwargs)
        provider_kwargs.pop("reference_video_duration_sec", None)
        video = await seedance.text_to_video(
            prompt=_native_audio_prompt(
                generation_prompt, generate_audio, expected_text
            ),
            model=model,
            generate_audio=generate_audio,
            metadata_out=metadata,
            **provider_kwargs,
        )
        call_cost = seedance.usage_cost_cents(metadata.get("usage") or {})
        if not call_cost:
            fallback_duration = float(request_kwargs.get("duration") or 0)
            call_cost = seedance.estimate_cost_cents(
                fallback_duration,
                request_kwargs.get("resolution", "720p"),
            )
        spent_cost_cents += call_cost
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
                require_broll_semantics=require_broll_semantics,
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
    denial = _auto_retry_denial(decision.reasons, projected_retry_cost)
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

    video, video_path, video_url, audio_path = await generate(
        route.model, route.generate_audio
    )
    retry_raw, retry_decision, retry_transcription = await assess(
        video_url, audio_path
    )
    if not retry_decision.passed:
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
                        cast_characters: list[dict] | None = None,
                        identity_anchors: dict[str, DialogueAnchor] | None = None,
                        scene_reference: str | None = None,
                        scene_video_anchor: DialogueAnchor | None = None,
                        ) -> tuple[int, Exception | None,
                                   DialogueAnchor | None]:
    """Generate one verified native-speaking Seedance 2.5 shot."""
    async with sem:
        try:
            combined = {**shot, **sb_shot}
            combined["voiceover"] = (
                sb_shot.get("voiceover") or shot.get("voiceover") or ""
            )
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
            prompt = _build_native_dialogue_prompt(
                combined,
                char,
                anchored=(
                    speaker_anchor is not None
                    or character_assets.character_identity_key(char)
                    in (identity_anchors or {})
                ),
                scene_anchored=bool(scene_reference),
                previous_scene_anchored=previous_scene_anchor is not None,
            )

            async def generate_scene() -> SeedanceGenerationResult:
                return await _generate_seedance_video(
                    prompt=prompt,
                    route=route,
                    request_kwargs=_dialogue_request_kwargs(
                        shot, speaker_anchor, character=char,
                        cast_characters=cast_characters,
                        identity_anchors=identity_anchors,
                        scene_reference=scene_reference,
                        scene_video_anchor=previous_scene_anchor,
                    ),
                    out_path=out_path,
                    key_hint=f"{job_id}-dialogue-{shot['idx']:02d}",
                    job_id=job_id,
                    expected_text=combined["voiceover"],
                    continuity_context=continuity_context,
                )

            try:
                result = await generate_scene()
            except seedance.SeedanceInputPrivacyError:
                if identity_anchors is None:
                    raise
                await _fallback_static_identity_anchors(
                    job_id,
                    [char, *(cast_characters or [])],
                    identity_anchors,
                )
                result = await generate_scene()
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
                DialogueAnchor(
                    result.video_url,
                    seedance.normalize_generation_duration(
                        shot["duration_sec"]
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
                db = conn()
                _record_failed_generation(db, job_id, shot["idx"], exc)
            except Exception:
                pass
            if isinstance(exc, Exception):
                return shot["idx"], exc, None
            raise


def _director_time_beats(duration: float, *, dialogue: str,
                         scene_voiceover: bool = False,
                         editorial_duration: float | None = None) -> list[str]:
    """Create a compact, continuous Seedance director timeline."""
    duration = max(1.0, float(duration))
    speech_end = min(
        duration,
        max(0.1, float(editorial_duration or duration)),
    )
    establish_end = min(1.0, max(0.05, speech_end * 0.12))
    settle_start = max(establish_end, speech_end - min(0.6, speech_end * 0.2))
    speech_label = "画外音" if scene_voiceover else "台词"
    subject = "画外音开始，画面继续自然运动" if scene_voiceover else "人物自然开口并完成动作与表情变化"
    beats = [
        f"0.0-{establish_end:.1f}秒：建立场景、人物或物件关系，动作自然起势。",
        f"{establish_end:.1f}-{settle_start:.1f}秒：{subject}，完整说出且只能说出 {{{speech_label}：{dialogue}}}。",
        f"{settle_start:.1f}-{speech_end:.1f}秒：话音自然结束，动作收束，画面保持连续稳定。",
    ]
    if speech_end < duration:
        beats.append(
            f"{speech_end:.1f}-{duration:.1f}秒：保持无新人声的自然动作和环境连续，供本地剪辑裁切。"
        )
    return beats


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
        f"画外音完整说出且只能说出 {{画外音：{voiceover}}}",
        "使用自然、清晰、专业的中国成年人普通话；不得改写、增删、重复或复述其他内容",
        f"<环境音：{sound_design}>；无 BGM，不要字幕。",
        "不得出现任何可识别人脸、正面人物、真人肖像或嘴部特写，不做口型同步",
        "优先拍摄环境、设备、文件、仪表和物件动态；如必须出现人物，只能是脸部完全不可见的手部、背影或远景剪影",
        "所有标签、铭牌、钢印、纸张和屏幕区域必须使用背面、遮挡、裁切或浅景深处理，保持完全失焦",
        "画面内不得出现任何字母或数字，不要生成字幕、标牌文字或其他可读文字",
    ])
    return "\n".join(parts)


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


def _continuity_contract_text(contract: dict | None) -> str:
    value = contract or {}
    scene = value.get("scene") or {}
    camera = value.get("camera") or {}
    character_lines = []
    for character in value.get("characters") or []:
        character_lines.append(
            f"{character.get('display_name') or character.get('identity_key') or '角色'}:"
            f"服装={character.get('wardrobe') or '保持身份锚点造型'},"
            f"站位={character.get('screen_position') or 'screen-center'}"
        )
    props = "、".join(str(item) for item in value.get("props") or []) or "无固定道具"
    return "；".join([
        f"场景={scene.get('description') or scene.get('key') or ''}",
        f"时段={scene.get('time_of_day') or '保持场景锚点'}",
        f"光线={scene.get('lighting') or '保持场景锚点'}",
        f"人物=[{'；'.join(character_lines)}]",
        f"道具={props}",
        f"轴线={camera.get('axis') or 'preserve-180-degree-axis'}",
    ])


def _build_native_dialogue_prompt(
        shot: dict, character: dict, *, anchored: bool = False,
        scene_anchored: bool = False,
        previous_scene_anchored: bool = False) -> str:
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
    creative_intent = (
        "创作意图：生成一段电影叙事情境表演，人物在真实剧情空间中完成动作、"
        "技能展示与原生对白，镜头主动参与叙事。"
        if cinematic else
        "创作意图：生成一段可信、克制、可直接交付的真人讲解镜头，画面表演与原生对白同步完成。"
    )
    timeline = (
        [] if cinematic else
        _director_time_beats(
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
        "时间轴：",
        *timeline,
        f"人物完整说出且只能说出 {{台词：{voiceover}}}",
        f"<环境音：{sound_design}>；无 BGM，不要字幕。",
        "不得改写、增删、重复台词，不得加入旁白、第二个人声或背景音乐。",
        "嘴唇、牙齿、舌位和下颌逐字同步，句末自然闭嘴。",
        "人物身份、五官、发型、服装、背景和光线全程稳定。",
        "不要字幕、可读文字、品牌、标识或水印。",
    ]
    if scene_anchored:
        parts[1:1] = [
            "场景连续性为硬约束，不是风格参考。",
            f"固定场景键：{shot.get('scene_anchor_key') or ''}",
            f"固定站位图：{_blocking_anchor_text(shot.get('blocking_anchor'))}",
            "同一人物必须保持同一屏幕侧、相对距离、朝向和身高关系；"
            "除非动作明确要求换位，否则禁止交换左右位置。",
            "锁定建筑、设备位置、地面材质、天气、时段、主光方向与180度镜头轴线。",
        ]
    if shot.get("continuity_contract"):
        parts[1:1] = [
            "结构化连续性契约（以下字段均为硬约束）：",
            _continuity_contract_text(shot.get("continuity_contract")),
        ]
    if previous_scene_anchored:
        parts[1:1] = [
            "前序同场景视频用于继承空间布局、人物站位、视线方向和镜头轴线；"
            "人物身份仍以定妆照或授权角色资产为准，不得沿用前序视频中的脸部漂移。",
        ]
    if cinematic:
        performance_lines = []
        for index, beat in enumerate(shot.get("performance_beats") or [], 1):
            start = float(beat.get("start") or 0)
            duration = float(beat.get("duration") or 0)
            end = start + duration
            visual_intent = non_text_visual_text(
                beat.get("visual_intent") or beat.get("broll_hint") or ""
            ).strip(" ；;，,")
            performance_lines.append(
                f"剧情节拍{index}（{start:.1f}-{end:.1f}秒）："
                f"{visual_intent}；"
                f"在该动作中自然说出 {{台词：{beat.get('dialogue') or ''}}}"
            )
        if editorial_duration < duration:
            performance_lines.append(
                f"补足段（{editorial_duration:.1f}-{duration:.1f}秒）："
                "台词已经结束，保持无新人声的自然动作与场景连续，供本地剪辑裁切。"
            )
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
            "低机位与动作特写；运镜服务剧情且保持主体清晰。",
            *(performance_lines or [
                "剧情节拍：严格根据场景与动作完成连续电影化表演。"
            ]),
            *(["同场人物设定：", *cast_lines] if cast_lines else []),
            f"对白约束：只有{character_name or '当前主角'}说出台词；"
            "其他同场人物只能做动作和反应，不得开口、旁白或发出可辨识人声。",
        ]
    if anchored:
        parts[1:1] = [
            "素材职责：",
            "身份参考图只锁定人物外貌，包括五官、年龄、发型、体型与服装；"
            "不得照搬定妆照的背景或站姿。",
            "前序视频只延续原生声线、空间连续性与基础表演；"
            "不得照搬其镜头构图或动作。",
            "不得复述参考素材中的旧台词，只说本镜新台词。",
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
                     scene_reference: str | None = None,
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
                if scene_voiceover else _broll_request_kwargs(shot)
            )
            request_kwargs.update({
                "first_frame": keyframe,
                "last_frame": last_frame,
                "references": (
                    [scene_reference]
                    if scene_reference and not keyframe else extra_refs
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
                forbid_people=scene_voiceover,
                expected_text=(
                    sb_shot.get("voiceover") or shot.get("voiceover")
                    if scene_voiceover else None
                ),
                require_broll_semantics=not scene_voiceover,
                continuity_context=(
                    {
                        "contract": sb_shot.get("continuity_contract") or {},
                        "identity_image_urls": [],
                        "scene_image_url": scene_reference,
                        "previous_video_url": None,
                    }
                    if scene_reference else None
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
    characters = list(chars.values())
    bindings_changed = character_assets.apply_cached_bindings(db, characters)
    bindings_changed = (
        _resolve_real_person_bindings(db, characters) or bindings_changed
    )
    continuity_changed = scene_assets.apply_continuity_metadata(
        sb, characters
    )
    if bindings_changed or continuity_changed:
        db.execute(
            "UPDATE jobs SET storyboard=?,characters_json=? WHERE job_id=?",
            (
                json.dumps(sb, ensure_ascii=False),
                json.dumps(characters, ensure_ascii=False),
                job_id,
            ),
        )
        db.commit()
    generation_units = sb.get("masters") or sb["shots"]
    sb_shots = {s["idx"]: s for s in generation_units}

    # Resolve deterministic, reviewed local footage before any stochastic
    # Seedance submission. Matched rows become OK and disappear from the
    # pending-generation query below.
    if sb.get("production_mode") == "native_master_local_director":
        asset_library.reuse_broll_assets(db, job_id, generation_units)

    all_pending_rows = db.execute(
        "SELECT * FROM shots WHERE job_id=? AND state!='OK' ORDER BY idx",
        (job_id,)
    ).fetchall()
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

    sem = asyncio.Semaphore(MAX_CONCURRENT_SHOTS)
    results: list[tuple[int, Exception | None]] = []
    identity_anchors = (
        await _ensure_character_identity_anchors(job_id, characters)
        if rows else {}
    )
    scene_anchors = (
        await _ensure_scene_visual_anchors(job_id, sb) if rows else {}
    )
    if rows:
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
    for r in rows:
        shot = dict(r)
        sb_shot = sb_shots.get(shot["idx"], {})
        scene_key = str(
            sb_shot.get("scene_anchor_key") or ""
        ).strip().casefold()
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
            idx, error, next_anchor = await _gen_dialogue(
                job_id, shot, char, sb_shot, sem,
                dialogue_anchors.get(identity_key),
                cast_characters=cast_characters,
                identity_anchors=identity_anchors,
                scene_reference=scene_anchors.get(
                    scene_key
                ),
                scene_video_anchor=scene_video_anchors.get(scene_key),
            )
            results.append((idx, error))
            if next_anchor is not None:
                dialogue_anchors[identity_key] = next_anchor
                if scene_key:
                    scene_video_anchors[scene_key] = next_anchor
                generated_voice = _voice_anchor_from_completed_shot({
                    "video_path": str(
                        ASSETS_DIR / job_id / "shots" / f"{shot['idx']:02d}.mp4"
                    )
                })
                if generated_voice is not None:
                    voice_anchors[identity_key] = generated_voice
        elif shot["narrative_type"] == "dialogue" and render_mode == "scene":
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
            results.append(await _gen_broll(
                job_id, shot, None, sb_shot, sem, scene_voiceover=True,
                voice_anchor=voice_anchors.get(voice_key),
                scene_reference=scene_anchors.get(
                    str(sb_shot.get("scene_anchor_key") or "").casefold()
                ),
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
                job_id, shot, anchor_path, sb_shot, sem,
                scene_reference=scene_anchors.get(
                    str(sb_shot.get("scene_anchor_key") or "").casefold()
                ),
            ))

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
