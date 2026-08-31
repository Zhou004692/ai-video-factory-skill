"""Pure flow-level invariants evaluated after every exploration run."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import subprocess

from .model import ExplorationReport, Finding


TERMINAL_STATES = frozenset({"READY_TO_PUBLISH", "PUBLISHED"})
SEMANTIC_CHECK_IDS = (
    "master_projection_consistent",
    "shot_master_exists",
    "dialogue_voiceover_present",
    "character_references_known",
    "speaker_references_known",
    "shot_within_master",
    "master_dependencies_exist",
    "graph_edges_resolve",
    "final_media_decodable",
)


def evaluate_production_spec(spec: object) -> tuple[Finding, ...]:
    """Evaluate persisted production semantics independently of graph progress."""
    if not isinstance(spec, Mapping):
        return (Finding(
            code="production_spec_missing",
            severity="error",
            detail="production spec is not a mapping",
        ),)

    findings: list[Finding] = []
    seen: set[str] = set()

    def add(code: str, detail: str) -> None:
        if code in seen:
            return
        findings.append(Finding(code=code, severity="error", detail=detail))
        seen.add(code)

    def records(value: object) -> list[Mapping[str, object]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    def indexes(items: list[Mapping[str, object]]) -> list[int]:
        result: list[int] = []
        for item in items:
            try:
                result.append(int(item.get("idx") or 0))
            except (TypeError, ValueError):
                result.append(0)
        return result

    characters = {
        str(item.get("id") or "").strip()
        for item in records(spec.get("characters"))
        if str(item.get("id") or "").strip()
    }
    masters = records(spec.get("masters"))
    runtime = spec.get("runtime_storyboard")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    runtime_masters = records(runtime.get("masters"))
    shots = records(runtime.get("shots"))
    nodes = records(spec.get("nodes"))
    edges = records(spec.get("edges"))

    declared_indexes = indexes(masters)
    runtime_indexes = indexes(runtime_masters)
    node_ids = {
        str(node.get("node_id") or "").strip()
        for node in nodes
        if str(node.get("node_id") or "").strip()
    }
    node_master_indexes: set[int] = set()
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if str(node.get("node_type") or "") != "master":
            continue
        try:
            node_master_indexes.add(int(node_id.split(":", 1)[1]))
        except (IndexError, TypeError, ValueError):
            add("master_node_invalid", f"invalid master node id: {node_id}")

    declared_set = set(declared_indexes)
    runtime_set = set(runtime_indexes)
    if (
        len(declared_indexes) != len(declared_set)
        or len(runtime_indexes) != len(runtime_set)
    ):
        add("master_index_duplicate", "master indexes must be unique")
    if declared_set != runtime_set or declared_set != node_master_indexes:
        add(
            "master_projection_mismatch",
            "compiled masters, runtime masters and graph master nodes differ",
        )

    duration_by_master: dict[int, float] = {}
    for master in runtime_masters:
        try:
            idx = int(master.get("idx") or 0)
            duration_by_master[idx] = float(
                master.get("duration")
                or master.get("duration_sec")
                or master.get("editorial_duration_sec")
                or 0
            )
        except (TypeError, ValueError):
            continue
        cast = {
            str(item).strip() for item in (master.get("cast") or [])
            if str(item).strip()
        }
        speakers = {
            str(item).strip() for item in (master.get("speakers") or [])
            if str(item).strip()
        }
        speaker = str(master.get("speaker") or "").strip()
        narrative = str(master.get("narrative_type") or "").casefold()
        if cast - characters:
            add(
                "character_unknown",
                f"runtime master {idx} references unknown cast members",
            )
        if (
            narrative == "dialogue"
            and (speakers | ({speaker} if speaker else set())) - characters
        ):
            add(
                "speaker_unknown",
                f"runtime master {idx} references unknown speakers",
            )
        if (
            narrative == "dialogue"
            and (speaker or speakers)
            and not str(master.get("voiceover") or "").strip()
        ):
            add(
                "dialogue_voiceover_empty",
                f"dialogue master {idx} has empty voiceover",
            )

    for shot in shots:
        try:
            shot_idx = int(shot.get("idx") or 0)
            duration = float(shot.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        for prefix in ("video", "audio"):
            try:
                master_idx = int(shot.get(f"{prefix}_master_idx") or 0)
                start = float(shot.get(f"{prefix}_start") or 0)
            except (TypeError, ValueError):
                master_idx, start = 0, 0
            if master_idx not in runtime_set:
                add(
                    "shot_master_missing",
                    f"shot {shot_idx} references missing {prefix} master {master_idx}",
                )
                continue
            if start < 0 or duration <= 0 or (
                start + duration > duration_by_master.get(master_idx, 0) + 0.05
            ):
                add(
                    "shot_exceeds_master",
                    f"shot {shot_idx} exceeds {prefix} master {master_idx}",
                )

    dependency = runtime.get("master_dependency_graph")
    dependency = dependency if isinstance(dependency, Mapping) else {}
    for item in records(dependency.get("nodes")):
        try:
            idx = int(item.get("idx") or 0)
        except (TypeError, ValueError):
            idx = 0
        references = set()
        for raw in item.get("depends_on") or []:
            try:
                references.add(int(raw))
            except (TypeError, ValueError):
                references.add(0)
        if idx not in runtime_set or not references <= runtime_set:
            add(
                "master_dependency_missing",
                f"master dependency entry {idx} references a missing master",
            )

    for edge in edges:
        source = str(edge.get("from_node_id") or "").strip()
        target = str(edge.get("to_node_id") or "").strip()
        if source not in node_ids or target not in node_ids:
            add(
                "edge_node_missing",
                f"graph edge references missing node: {source} -> {target}",
            )

    return tuple(findings)


def evaluate_media_artifact(path: object) -> dict[str, object]:
    """Return independent file and decode evidence for one final video."""
    media_path = Path(str(path or ""))
    if not media_path.is_file():
        return {
            "findings": (Finding(
                code="final_media_missing",
                severity="error",
                detail=f"final media does not exist: {media_path}",
            ),),
            "metrics": {},
        }
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=codec_type,avg_frame_rate",
                "-of", "json", str(media_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(completed.stdout or "{}")
    except (
        OSError,
        subprocess.CalledProcessError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        return {
            "findings": (Finding(
                code="final_media_decode_failed",
                severity="error",
                detail=f"ffprobe could not decode final media: {exc}",
            ),),
            "metrics": {},
        }

    streams = payload.get("streams") or []
    has_video = any(item.get("codec_type") == "video" for item in streams)
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    fps = 0.0
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        raw = str(stream.get("avg_frame_rate") or "0/1")
        try:
            numerator, denominator = raw.split("/", 1)
            fps = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        break
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    findings: list[Finding] = []
    if not has_video or duration <= 0 or fps <= 0:
        findings.append(Finding(
            code="final_media_invalid",
            severity="error",
            detail="final media must contain a positive-duration video stream",
        ))
    return {
        "findings": tuple(findings),
        "metrics": {
            "duration_sec": round(duration, 3),
            "fps": round(fps, 3),
            "has_video": has_video,
            "has_audio": has_audio,
            "size_bytes": media_path.stat().st_size,
        },
    }


def evaluate_report(
    report: ExplorationReport,
    *,
    max_steps: int = 32,
) -> tuple[Finding, ...]:
    """Return deterministic blockers without mutating production evidence."""
    findings = list(report.findings)
    existing = {item.code for item in findings}

    def add(code: str, detail: str) -> None:
        if code not in existing:
            findings.append(Finding(
                code=code,
                severity="error",
                detail=detail,
                state=report.final_state,
                next_action=(
                    report.observed_actions[-1]
                    if report.observed_actions else ""
                ),
            ))
            existing.add(code)

    if report.final_state not in TERMINAL_STATES:
        add(
            "nonterminal_dead_end",
            "scenario stopped before reaching a publishable terminal state",
        )
    if report.paid_provider_calls:
        add(
            "paid_provider_call",
            f"offline explorer observed {report.paid_provider_calls} paid calls",
        )
    pairs = tuple(zip(report.observed_states, report.observed_actions))
    if len(pairs) != len(set(pairs)):
        add(
            "state_action_loop",
            "the same state and next action recurred without progress",
        )
    if report.steps >= max_steps and report.final_state not in TERMINAL_STATES:
        add(
            "step_limit_exceeded",
            f"scenario exhausted the {max_steps}-step safety boundary",
        )
    return tuple(findings)
