"""Controlled downstream identity A/B: Seedance video master vs Seedream 5.0 still.

This experiment deliberately evaluates the final Seedance clips, not merely
the attractiveness of the source portrait.  It is isolated from production
jobs and stops the B branch immediately when Volcengine rejects a static face
reference under its privacy policy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import httpx


SKILL_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory.clients import ffmpeg, seedance  # noqa: E402


MODEL_IMAGE = "doubao-seedream-5-0-260128"
MODEL_VIDEO = "doubao-seedance-2-5-260628"
DEFAULT_JOB_ID = "J-TOPIC-5beb74c718ca32dd"
DEFAULT_A_MASTER = (
    "https://ai-video-factory.tos-cn-beijing.volces.com/ai-video-factory/"
    "J-TOPIC-5beb74c718ca32dd-identity-79eed3b99a85f61eee2d-"
    "1786946256-51616f03.mp4"
)
RESOLUTION = "480p"
DURATION = 4.0
RATIO = "9:16"
MAX_VIDEO_CALLS = 4
MAX_ESTIMATED_CENTS = 1_200
MEASURED_VIDEO_COST_CENTS = {"A": 541, "B": 272}

CHARACTER_SPEC = (
    "A fictional African man in his early thirties, dark brown skin, short "
    "tightly curled black hair, neatly trimmed short beard, calm attentive "
    "brown eyes, average athletic build, wearing one plain medium-grey V-neck "
    "knit sweater over a white undershirt. Preserve exactly the same face, "
    "apparent age, hairstyle, beard, skin tone, body shape, and clothing."
)

PORTRAIT_PROMPT = f"""
Use case: photorealistic-natural
Asset type: reusable fictional character identity reference for vertical video
Primary request: Create a neutral identity portrait of {CHARACTER_SPEC}
Scene/backdrop: warm light-grey seamless photography studio, no props
Composition/framing: vertical 9:16, head-to-knees, straight-on camera, relaxed
neutral stance, hands naturally visible, face unobstructed
Lighting/mood: soft natural studio light, realistic skin and fabric texture
Constraints: one fictional adult only; no text, logo, watermark, jewelry, hat,
glasses, dramatic makeup, stylization, or celebrity resemblance
""".strip()

SCENES = [
    (
        "yard_walk",
        "Outdoor construction-equipment rental yard in soft overcast daylight, "
        "yellow boom lift and stacked safety barriers in the background. "
        f"{CHARACTER_SPEC} Medium full shot. He walks two natural steps beside "
        "the lift, briefly turns his head toward it, then looks to camera. "
        "Smooth slow lateral dolly, realistic body motion, continuous action, "
        "no speaking, no readable text, no logos, no other people.",
    ),
    (
        "tablet_point",
        "Outdoor construction-equipment rental yard in soft overcast daylight, "
        "yellow boom lift in the background. "
        f"{CHARACTER_SPEC} Three-quarter medium shot. He checks a plain tablet, "
        "points once toward the equipment with his free hand, then looks up. "
        "Gentle arc camera move, realistic hand motion, continuous action, no "
        "speaking, no readable text, no logos, no other people.",
    ),
]


def _is_privacy_rejection(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "privacyinformation" in message or "may contain a real person" in message


def _video_cost_guard_estimate(branch: str) -> int:
    """Use measured branch costs; generic duration estimates underpriced A by 2x."""
    return MEASURED_VIDEO_COST_CENTS[branch]


async def _generate_video(
    *,
    prompt: str,
    branch: str,
    portrait: Path,
    a_master: str,
    output_path: Path,
) -> dict:
    metadata: dict = {}
    kwargs: dict = {
        "duration": DURATION,
        "resolution": RESOLUTION,
        "ratio": RATIO,
        "model": MODEL_VIDEO,
        "generate_audio": False,
        "motion_strength": 0.45,
        "metadata_out": metadata,
        "timeout": 900,
    }
    if branch == "A":
        kwargs["reference_videos"] = [a_master]
    else:
        kwargs["references"] = [str(portrait)]
    video = await seedance.text_to_video(prompt, **kwargs)
    output_path.write_bytes(video)
    usage = metadata.get("usage") or {}
    metadata["actual_cost_cents"] = seedance.usage_cost_cents(usage)
    metadata["output_path"] = str(output_path)
    return metadata


async def _recover_video_task(task_id: str, output_path: Path) -> dict:
    """Resume polling an already-paid task without submitting a duplicate."""
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set in .env")
    headers = {"Authorization": f"Bearer {key}"}
    endpoint = f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}"
    async with httpx.AsyncClient(timeout=120, headers=headers) as client:
        for _ in range(180):
            response = await client.get(endpoint)
            response.raise_for_status()
            data = response.json()
            status = data.get("status")
            if status == "succeeded":
                video_url = data["content"]["video_url"]
                video = await client.get(video_url)
                video.raise_for_status()
                output_path.write_bytes(video.content)
                usage = data.get("usage") or {}
                return {
                    "task_id": task_id,
                    "usage": usage,
                    "actual_cost_cents": seedance.usage_cost_cents(usage),
                    "output_path": str(output_path),
                    "recovered": True,
                }
            if status == "failed":
                raise RuntimeError(f"recovered task {task_id} failed: {json.dumps(data, ensure_ascii=False)}")
            await asyncio.sleep(5)
    raise TimeoutError(f"recovered task {task_id} timed out")


async def _run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "results.json"

    result: dict = {
        "experiment": "seedream50_identity_ab",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "job_id": args.job_id,
        "models": {"image": MODEL_IMAGE, "video": MODEL_VIDEO},
        "settings": {
            "resolution": RESOLUTION,
            "duration_sec": DURATION,
            "ratio": RATIO,
            "max_video_calls": MAX_VIDEO_CALLS,
            "estimated_cost_cap_cents": MAX_ESTIMATED_CENTS,
        },
        "a_reference_video": args.a_master,
        "b_portrait": None,
        "scenes": [],
        "status": "RUNNING",
    }

    portrait_path = output_dir / "B-seedream50-portrait.png"
    if args.reuse_portrait and portrait_path.is_file():
        print(f"[B] reusing existing portrait {portrait_path}", flush=True)
    else:
        # Explicitly route t2i through the configured Seedream 5.0 full endpoint.
        # seedance.text_to_image reads ARK_IMAGE_MODEL at call time.
        previous_image_model = os.environ.get("ARK_IMAGE_MODEL")
        os.environ["ARK_IMAGE_MODEL"] = MODEL_IMAGE
        try:
            print("[B] generating Seedream 5.0 identity portrait", flush=True)
            portrait_bytes = await seedance.text_to_image(
                PORTRAIT_PROMPT,
                resolution="1440x2560",
                seed=20260817,
            )
        finally:
            if previous_image_model is None:
                os.environ.pop("ARK_IMAGE_MODEL", None)
            else:
                os.environ["ARK_IMAGE_MODEL"] = previous_image_model
        portrait_path.write_bytes(portrait_bytes)
    result["b_portrait"] = str(portrait_path)
    result["estimated_image_cost_cents"] = 29
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    estimated_spend = 29
    paid_calls = 0
    recovered_branches: dict[str, dict] = {}
    for branch, task_id in (("A", args.recover_a_task), ("B", args.recover_b_task)):
        if not task_id:
            continue
        estimate = _video_cost_guard_estimate(branch)
        estimated_spend += estimate
        paid_calls += 1
        output_path = output_dir / f"{SCENES[0][0]}-{branch}.mp4"
        print(f"[{branch}] recovering already-paid task {task_id}", flush=True)
        metadata = await _recover_video_task(task_id, output_path)
        frames_dir = output_dir / "frames" / f"{SCENES[0][0]}-{branch}"
        frames = await ffmpeg.extract_frames(output_path, frames_dir, [1.0, 3.0])
        recovered_branches[branch] = {
            "status": "SUCCEEDED",
            "metadata": metadata,
            "frames": [str(path) for path in frames],
        }
    for scene_name, prompt in SCENES[: args.max_scenes]:
        scene_result = {"name": scene_name, "prompt": prompt, "branches": {}}
        result["scenes"].append(scene_result)
        if scene_name == SCENES[0][0]:
            scene_result["branches"].update(recovered_branches)
        for branch in args.branches:
            if branch in scene_result["branches"]:
                continue
            estimate = _video_cost_guard_estimate(branch)
            if paid_calls >= MAX_VIDEO_CALLS or estimated_spend + estimate > MAX_ESTIMATED_CENTS:
                raise RuntimeError(
                    f"experiment cost guard stopped before {branch}/{scene_name}: "
                    f"estimated={estimated_spend + estimate}, cap={MAX_ESTIMATED_CENTS} cents"
                )
            output_path = output_dir / f"{scene_name}-{branch}.mp4"
            print(f"[{branch}] generating {scene_name} (estimate {estimate} cents)", flush=True)
            estimated_spend += estimate
            paid_calls += 1
            try:
                metadata = await _generate_video(
                    prompt=prompt,
                    branch=branch,
                    portrait=portrait_path,
                    a_master=args.a_master,
                    output_path=output_path,
                )
                frames_dir = output_dir / "frames" / f"{scene_name}-{branch}"
                frames = await ffmpeg.extract_frames(output_path, frames_dir, [1.0, 3.0])
                scene_result["branches"][branch] = {
                    "status": "SUCCEEDED",
                    "metadata": metadata,
                    "frames": [str(path) for path in frames],
                }
            except Exception as exc:  # persist provider evidence before stopping
                scene_result["branches"][branch] = {
                    "status": "FAILED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "privacy_rejection": _is_privacy_rejection(exc),
                }
                result["status"] = (
                    "B_STATIC_REFERENCE_REJECTED"
                    if branch == "B" and _is_privacy_rejection(exc)
                    else "FAILED"
                )
                result["estimated_spend_cents"] = estimated_spend
                result["paid_video_calls_attempted"] = paid_calls
                result["finished_at"] = datetime.now(timezone.utc).isoformat()
                result_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if result["status"] == "B_STATIC_REFERENCE_REJECTED":
                    print("[STOP] B static face reference rejected by provider privacy policy", flush=True)
                    return 2
                raise
            result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    result["status"] = "SUCCEEDED"
    result["estimated_spend_cents"] = estimated_spend
    result["actual_video_cost_cents"] = sum(
        branch["metadata"].get("actual_cost_cents", 0)
        for scene in result["scenes"]
        for branch in scene["branches"].values()
        if branch.get("status") == "SUCCEEDED"
    )
    result["actual_total_cost_cents_including_estimated_image"] = (
        result["actual_video_cost_cents"] + result["estimated_image_cost_cents"]
    )
    result["paid_video_calls_attempted"] = paid_calls
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] {result_path}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default=DEFAULT_JOB_ID)
    parser.add_argument("--a-master", default=DEFAULT_A_MASTER)
    parser.add_argument("--reuse-portrait", action="store_true")
    parser.add_argument(
        "--max-scenes",
        type=int,
        choices=(1, 2),
        default=1,
        help="Default to one controlled pair after measured 480p cost exceeded estimates",
    )
    parser.add_argument(
        "--branches",
        nargs="+",
        choices=("A", "B"),
        default=["A", "B"],
        help="Branches to generate for any scene not supplied through recovery",
    )
    parser.add_argument(
        "--recover-a-task",
        help="Existing paid A task id to poll instead of submitting it again",
    )
    parser.add_argument(
        "--recover-b-task",
        help="Existing paid B task id to poll instead of submitting it again",
    )
    parser.add_argument(
        "--output-dir",
        default=str(
            SKILL_ROOT
            / "data"
            / "seedream50_identity_ab"
            / DEFAULT_JOB_ID
        ),
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
