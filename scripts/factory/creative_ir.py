"""Lossless creative intent boundary before production planning."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Mapping


CREATIVE_IR_VERSION = 1
SPEECH_MODES = ("on_screen", "off_screen", "narration")
OFFSCREEN_SPEAKER_HINTS = (
    "背景台词", "画外音", "画外台词", "画外对白", "offscreen", "off_screen",
)
NARRATION_SPEAKER_HINTS = ("旁白", "narrator", "narration", "voiceover", "voice_over")

_ROOT_FIELDS = frozenset({
    "fixture_version", "profile_id", "title", "target_duration_sec",
    "characters", "beats", "masters", "script_metadata",
})
_BEAT_FIELDS = frozenset({
    "id", "beat_id", "beat", "kind", "scene", "location", "speaker",
    "speakers", "dialogue", "voiceover", "action",
    "visual_intent", "emotion", "dramatic_keywords", "camera_intent",
    "camera", "must_preserve", "may_adapt",
})


def _text(value: object) -> str:
    return str(value or "").strip()


def normalize_speech_mode(value: object, *, speaker: str = "") -> str:
    """Normalize the relationship between a voice and visible characters."""
    explicit = _text(value).lower()
    if explicit in SPEECH_MODES:
        return explicit
    compact_speaker = re.sub(r"\s+", "", _text(speaker)).lower()
    if any(hint in compact_speaker for hint in NARRATION_SPEAKER_HINTS):
        return "narration"
    if any(hint in compact_speaker for hint in OFFSCREEN_SPEAKER_HINTS):
        return "off_screen"
    return "on_screen"


def _tuple_text(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        text for value in values if (text := _text(
            value.get("id") if isinstance(value, Mapping) else value
        ))
    )


@dataclass(frozen=True)
class CreativeBeat:
    beat_id: str
    kind: str
    scene: str
    speakers: tuple[str, ...]
    dialogue: str
    action: str
    emotion: str
    camera_intent: str
    must_preserve: tuple[str, ...]
    may_adapt: tuple[str, ...]
    extensions: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "beat_id": self.beat_id,
            "kind": self.kind,
            "scene": self.scene,
            "speakers": self.speakers,
            "dialogue": self.dialogue,
            "action": self.action,
            "emotion": self.emotion,
            "camera_intent": self.camera_intent,
            "must_preserve": self.must_preserve,
            "may_adapt": self.may_adapt,
            "extensions": deepcopy(dict(self.extensions)),
        }


@dataclass(frozen=True)
class CreativeIR:
    version: int
    title: str
    target_duration_sec: float | None
    characters: tuple[Mapping[str, object], ...]
    beats: tuple[CreativeBeat, ...]
    extensions: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "title": self.title,
            "target_duration_sec": self.target_duration_sec,
            "characters": tuple(
                deepcopy(dict(character)) for character in self.characters
            ),
            "beats": tuple(beat.as_dict() for beat in self.beats),
            "extensions": deepcopy(dict(self.extensions)),
        }


def explicit_visible_cast(beat: CreativeBeat) -> tuple[str, ...]:
    """Keep explicitly visible identities, independent of their audio mode."""
    raw = beat.extensions.get("characters") or beat.extensions.get("cast") or ()
    return tuple(dict.fromkeys(_tuple_text(raw)))


def on_screen_speakers(beat: CreativeBeat) -> tuple[str, ...]:
    """Infer each voice independently unless a valid beat mode overrides it."""
    return tuple(dict.fromkeys(
        speaker for speaker in beat.speakers
        if normalize_speech_mode(
            beat.extensions.get("speech_mode"), speaker=speaker,
        ) == "on_screen"
    ))


def visible_cast(beat: CreativeBeat) -> tuple[str, ...]:
    """Audio-only identities never imply visibility; explicit cast still does."""
    return tuple(dict.fromkeys((*explicit_visible_cast(beat), *on_screen_speakers(beat))))


def lip_sync_required(beat: CreativeBeat) -> bool:
    """Only a beat with actual on-screen dialogue needs mouth synchronization."""
    return bool(beat.dialogue and on_screen_speakers(beat))


def _speakers(beat: Mapping[str, object]) -> tuple[str, ...]:
    explicit = _tuple_text(beat.get("speakers"))
    if explicit:
        return explicit
    speaker = _text(beat.get("speaker"))
    return (speaker,) if speaker else ()


def _creative_beat(
    beat: Mapping[str, object], index: int, *, source_master_idx: int | None = None,
) -> CreativeBeat:
    extensions = {
        key: deepcopy(value) for key, value in beat.items()
        if key not in _BEAT_FIELDS
    }
    if source_master_idx is not None:
        extensions["source_master_idx"] = source_master_idx
    action = _text(beat.get("action") or beat.get("visual_intent"))
    return CreativeBeat(
        beat_id=_text(beat.get("id") or beat.get("beat_id")) or f"beat-{index}",
        kind=_text(beat.get("kind") or beat.get("beat")) or "scene",
        scene=_text(beat.get("scene") or beat.get("location")),
        speakers=_speakers(beat),
        dialogue=_text(beat.get("dialogue") or beat.get("voiceover")),
        action=action,
        emotion=_text(
            beat.get("emotion") or beat.get("dramatic_keywords")
        ),
        camera_intent=_text(beat.get("camera_intent") or beat.get("camera")),
        must_preserve=_tuple_text(beat.get("must_preserve")),
        may_adapt=_tuple_text(beat.get("may_adapt")),
        extensions=extensions,
    )


def _beats_from_masters(source: Mapping[str, object]) -> list[CreativeBeat]:
    result: list[CreativeBeat] = []
    for master_index, raw_master in enumerate(source.get("masters") or [], 1):
        if not isinstance(raw_master, Mapping):
            continue
        master_idx = int(raw_master.get("idx") or master_index)
        performance_beats = [
            item for item in (raw_master.get("performance_beats") or [])
            if isinstance(item, Mapping)
        ]
        if performance_beats:
            for raw_beat in performance_beats:
                combined = {
                    "kind": raw_master.get("type"),
                    "scene": raw_master.get("scene"),
                    "scene_id": raw_master.get("scene_anchor_key"),
                    "cast": raw_master.get("cast"),
                    "speaker": raw_beat.get("speaker") or raw_master.get("speaker"),
                    "dialogue": raw_beat.get("dialogue"),
                    "action": raw_beat.get("visual_intent") or raw_master.get("action"),
                    "camera": raw_master.get("camera"),
                    "duration_sec": raw_beat.get("duration") or raw_master.get("duration"),
                    **dict(raw_beat),
                }
                if "speech_mode" in raw_master and "speech_mode" not in raw_beat:
                    combined["speech_mode"] = raw_master["speech_mode"]
                result.append(_creative_beat(
                    combined, len(result) + 1, source_master_idx=master_idx,
                ))
            continue
        result.append(_creative_beat(
            raw_master, len(result) + 1, source_master_idx=master_idx,
        ))
    return result


def from_storyboard(source: Mapping[str, object]) -> CreativeIR:
    """Normalize creative content without adapting it for any provider."""
    metadata = source.get("script_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    title = _text(source.get("title") or metadata.get("title"))
    raw_duration = source.get(
        "target_duration_sec", metadata.get("target_duration_sec")
    )
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    raw_beats = [
        item for item in (source.get("beats") or [])
        if isinstance(item, Mapping)
    ]
    beats = (
        [_creative_beat(beat, index) for index, beat in enumerate(raw_beats, 1)]
        if raw_beats else _beats_from_masters(source)
    )
    if duration is None and source.get("masters"):
        duration = sum(
            float(master.get("duration_sec", master.get("duration")) or 0)
            for master in source.get("masters") or []
            if isinstance(master, Mapping)
        ) or None
    characters = tuple(
        deepcopy(dict(character))
        for character in (source.get("characters") or [])
        if isinstance(character, Mapping)
    )
    extensions = {
        key: deepcopy(value) for key, value in source.items()
        if key not in _ROOT_FIELDS
    }
    return CreativeIR(
        version=CREATIVE_IR_VERSION,
        title=title,
        target_duration_sec=duration,
        characters=characters,
        beats=tuple(beats),
        extensions=extensions,
    )


def from_dict(source: Mapping[str, object]) -> CreativeIR:
    """Restore an already-normalized Creative IR without losing extensions."""
    raw_beats = source.get("beats") or []
    beats: list[CreativeBeat] = []
    for index, raw in enumerate(raw_beats, 1):
        if not isinstance(raw, Mapping):
            continue
        extensions = raw.get("extensions")
        beats.append(CreativeBeat(
            beat_id=_text(raw.get("beat_id") or raw.get("id")) or f"beat-{index}",
            kind=_text(raw.get("kind")) or "scene",
            scene=_text(raw.get("scene")),
            speakers=_tuple_text(raw.get("speakers")),
            dialogue=_text(raw.get("dialogue")),
            action=_text(raw.get("action")),
            emotion=_text(raw.get("emotion")),
            camera_intent=_text(raw.get("camera_intent")),
            must_preserve=_tuple_text(raw.get("must_preserve")),
            may_adapt=_tuple_text(raw.get("may_adapt")),
            extensions=deepcopy(dict(extensions))
            if isinstance(extensions, Mapping) else {},
        ))
    raw_duration = source.get("target_duration_sec")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    extensions = source.get("extensions")
    return CreativeIR(
        version=int(source.get("version") or CREATIVE_IR_VERSION),
        title=_text(source.get("title")),
        target_duration_sec=duration,
        characters=tuple(
            deepcopy(dict(character))
            for character in (source.get("characters") or [])
            if isinstance(character, Mapping)
        ),
        beats=tuple(beats),
        extensions=deepcopy(dict(extensions))
        if isinstance(extensions, Mapping) else {},
    )
