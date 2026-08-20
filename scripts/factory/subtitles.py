"""Deterministic phrase-level subtitle timing helpers."""
from __future__ import annotations

import re
from typing import Any


_BOUNDARY = re.compile(r"(?<=[，,；;。！？!?])")
_TRAILING_PLAIN_PUNCTUATION = "，,；;。．."


def _display_text(text: str) -> str:
    return str(text or "").strip().rstrip(_TRAILING_PLAIN_PUNCTUATION)


def split_phrases(text: str, *, max_chars: int = 14) -> list[str]:
    """Split narration at semantic pauses, then cap visual line length."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    clauses = [part.strip() for part in _BOUNDARY.split(normalized) if part.strip()]
    result: list[str] = []
    for clause in clauses:
        display = _display_text(clause)
        while len(display) > max_chars:
            result.append(display[:max_chars])
            display = display[max_chars:]
        if display:
            result.append(display)
    return result


def timed_cues(
    text: str,
    start: float,
    end: float,
    *,
    max_chars: int = 14,
) -> list[dict]:
    """Allocate one speech interval across phrase cues by visible length."""
    phrases = split_phrases(text, max_chars=max_chars)
    start = max(0.0, float(start))
    end = max(start, float(end))
    if not phrases or end <= start:
        return []
    weights = [max(1, len(phrase)) for phrase in phrases]
    total = sum(weights)
    span = end - start
    cues: list[dict] = []
    elapsed = 0
    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        cue_start = start + span * elapsed / total
        elapsed += weight
        cue_end = (
            end if index == len(phrases) - 1
            else start + span * elapsed / total
        )
        cues.append({
            "start": round(cue_start, 3),
            "end": round(cue_end, 3),
            "text": phrase,
        })
    return cues


def apply_master_transcription(
    storyboard: dict,
    master_idx: int,
    transcription: Any,
) -> bool:
    """Replace estimated director subtitles with master-relative ASR cues."""
    if storyboard.get("production_mode") != "native_master_local_director":
        return False

    timed = list(getattr(transcription, "utterances", ()) or ())
    speech_start_ms = getattr(transcription, "speech_start_ms", None)
    speech_end_ms = getattr(transcription, "speech_end_ms", None)
    text = str(getattr(transcription, "text", "") or "").strip()
    if not timed and text and speech_start_ms is not None \
            and speech_end_ms is not None:
        timed = [type("TimedUtterance", (), {
            "text": text,
            "start_ms": speech_start_ms,
            "end_ms": speech_end_ms,
        })()]
    if not timed:
        return False

    changed = False
    for edit in storyboard.get("shots") or []:
        audio_idx = int(edit.get(
            "audio_master_idx", edit.get("video_master_idx", -1)
        ))
        if audio_idx != int(master_idx):
            continue
        edit_start = float(edit.get("audio_start") or 0)
        edit_end = edit_start + float(edit.get("duration") or 0)
        cues: list[dict] = []
        for utterance in timed:
            for cue in timed_cues(
                utterance.text,
                utterance.start_ms / 1000.0,
                utterance.end_ms / 1000.0,
            ):
                overlap_start = max(edit_start, float(cue["start"]))
                overlap_end = min(edit_end, float(cue["end"]))
                if overlap_end <= overlap_start:
                    continue
                cues.append({
                    "start": round(overlap_start - edit_start, 3),
                    "end": round(overlap_end - edit_start, 3),
                    "text": cue["text"],
                })
        if cues:
            edit["subtitle"] = None
            edit["subtitle_cues"] = cues
            changed = True
    return changed
