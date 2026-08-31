"""Historical production-spec mutations used to calibrate semantic detectors."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable

from .. import creative_ir, production_compiler
from .invariants import evaluate_production_spec


PROJECT_ROOT = Path(__file__).resolve().parents[3]
Mutation = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class HistoricalMutation:
    mutation_id: str
    expected_code: str
    apply: Mutation


def _baseline_spec() -> dict[str, object]:
    source = json.loads((
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "production_profiles"
        / "experimental_multi_character_action.json"
    ).read_text(encoding="utf-8"))
    compilation = production_compiler.compile_ir(
        creative_ir.from_storyboard(source),
        job_id="J-FLOW-GUARD-MUTATION",
        capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
        budget_cents=100_000,
    )
    if not compilation.compilable or compilation.spec is None:
        raise RuntimeError("historical mutation baseline did not compile")
    return compilation.spec.as_dict()


def _extra_master(spec: dict[str, object]) -> None:
    master = deepcopy(spec["masters"][0])
    master["idx"] = max(item["idx"] for item in spec["masters"]) + 1
    spec["masters"] = [*spec["masters"], master]


def _missing_shot_master(spec: dict[str, object]) -> None:
    spec["runtime_storyboard"]["shots"][0]["video_master_idx"] = 999


def _empty_dialogue_voiceover(spec: dict[str, object]) -> None:
    spec["runtime_storyboard"]["masters"][0]["voiceover"] = ""


def _unknown_character(spec: dict[str, object]) -> None:
    master = spec["runtime_storyboard"]["masters"][0]
    master["cast"] = [*master["cast"], "historical-ghost"]


def _unknown_speaker(spec: dict[str, object]) -> None:
    master = spec["runtime_storyboard"]["masters"][0]
    master["speaker"] = "historical-ghost"
    master["speakers"] = ["historical-ghost"]


def _shot_exceeds_master(spec: dict[str, object]) -> None:
    master = spec["runtime_storyboard"]["masters"][0]
    shot = spec["runtime_storyboard"]["shots"][0]
    shot["video_start"] = float(master["duration"]) - 0.1
    shot["duration"] = 1.0


def _missing_dependency(spec: dict[str, object]) -> None:
    graph = spec["runtime_storyboard"]["master_dependency_graph"]
    graph["nodes"][0]["depends_on"] = [999]


def _missing_edge_node(spec: dict[str, object]) -> None:
    spec["edges"][0]["from_node_id"] = "master:999"


def _duplicate_master(spec: dict[str, object]) -> None:
    runtime = spec["runtime_storyboard"]
    runtime["masters"] = [
        *runtime["masters"], deepcopy(runtime["masters"][0])
    ]


HISTORICAL_MUTATIONS = (
    HistoricalMutation(
        "extra_meaningless_master", "master_projection_mismatch", _extra_master
    ),
    HistoricalMutation(
        "director_shot_missing_master", "shot_master_missing", _missing_shot_master
    ),
    HistoricalMutation(
        "dialogue_master_empty_voiceover",
        "dialogue_voiceover_empty",
        _empty_dialogue_voiceover,
    ),
    HistoricalMutation(
        "character_unknown", "character_unknown", _unknown_character
    ),
    HistoricalMutation("speaker_unknown", "speaker_unknown", _unknown_speaker),
    HistoricalMutation(
        "edit_shot_exceeds_master", "shot_exceeds_master", _shot_exceeds_master
    ),
    HistoricalMutation(
        "master_dependency_missing", "master_dependency_missing", _missing_dependency
    ),
    HistoricalMutation(
        "graph_edge_missing_node", "edge_node_missing", _missing_edge_node
    ),
    HistoricalMutation(
        "duplicate_master_index", "master_index_duplicate", _duplicate_master
    ),
)


def run_historical_mutation_calibration() -> dict[str, object]:
    """Require independent semantic oracles to kill known production blockers."""
    baseline = _baseline_spec()
    baseline_codes = [
        finding.code for finding in evaluate_production_spec(baseline)
    ]
    results: list[dict[str, object]] = []
    for mutation in HISTORICAL_MUTATIONS:
        candidate = deepcopy(baseline)
        mutation.apply(candidate)
        observed = {
            finding.code for finding in evaluate_production_spec(candidate)
        }
        killed = mutation.expected_code in observed
        results.append({
            "mutation_id": mutation.mutation_id,
            "expected_code": mutation.expected_code,
            "killed": killed,
            "observed_codes": sorted(observed),
        })
    killed_count = sum(bool(item["killed"]) for item in results)
    mutation_count = len(results)
    survived = mutation_count - killed_count
    score = round((killed_count / mutation_count * 100), 2)
    return {
        "status": (
            "passed" if not baseline_codes and score >= 95.0 else "failed"
        ),
        "mutation_count": mutation_count,
        "killed": killed_count,
        "survived": survived,
        "score_pct": score,
        "baseline_findings": baseline_codes,
        "results": results,
    }
