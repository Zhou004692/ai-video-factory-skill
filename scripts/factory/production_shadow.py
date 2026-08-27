"""Side-effect-free v1/v2 compilation comparison for staged migration."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .production_contract import compile_production_spec


@dataclass(frozen=True)
class ShadowCompilationResult:
    job_id: str
    status: str
    v1_spec_hash: str | None
    v2_spec_hash: str | None
    diff_codes: tuple[str, ...]
    blockers: tuple[str, ...]
    mode: str | None
    error: str | None = None


def _issue_codes(compilation) -> tuple[str, ...]:
    return tuple(
        issue.code for issue in compilation.issues
        if issue.severity == "blocking"
    )


def compile_and_compare(
    database: sqlite3.Connection,
    job_id: str,
    root: str | Path,
) -> ShadowCompilationResult:
    """Compile both contracts and only append an audit record."""
    try:
        v1 = compile_production_spec(
            database, job_id, root, persist=False, target_version=1,
        )
        v2 = compile_production_spec(
            database, job_id, root, persist=False, target_version=2,
        )
        v1_blockers = _issue_codes(v1)
        v2_blockers = _issue_codes(v2)
        blockers = tuple(dict.fromkeys(
            [f"v1:{code}" for code in v1_blockers]
            + [f"v2:{code}" for code in v2_blockers]
        ))
        diffs: list[str] = []
        if bool(v1.spec) != bool(v2.spec):
            diffs.append("compilability_changed")
        if v1.spec is not None and v2.spec is not None:
            if len(v1.spec.masters) != len(v2.spec.masters):
                diffs.append("master_count_changed")
            if int(v1.spec.budget_cents) != int(v2.spec.budget_cents):
                diffs.append("budget_changed")
        if v2_blockers:
            status = "v2_blocked"
        elif v1_blockers:
            status = "legacy_blocked"
        elif diffs:
            status = "compatible_difference"
        else:
            status = "match"
        result = ShadowCompilationResult(
            job_id=job_id,
            status=status,
            v1_spec_hash=v1.spec.spec_hash if v1.spec else None,
            v2_spec_hash=v2.spec.spec_hash if v2.spec else None,
            diff_codes=tuple(diffs),
            blockers=blockers,
            mode=v2.spec.mode if v2.spec else None,
        )
    except Exception as exc:  # Shadow failures must never change legacy flow.
        result = ShadowCompilationResult(
            job_id=job_id,
            status="compiler_internal_error",
            v1_spec_hash=None,
            v2_spec_hash=None,
            diff_codes=("compiler_internal_error",),
            blockers=(),
            mode=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    database.execute(
        "INSERT INTO production_compilation_runs("
        "job_id,status,v1_spec_hash,v2_spec_hash,diff_codes_json,"
        "blockers_json,mode,error) VALUES(?,?,?,?,?,?,?,?)",
        (
            result.job_id, result.status, result.v1_spec_hash,
            result.v2_spec_hash,
            json.dumps(result.diff_codes, ensure_ascii=False),
            json.dumps(result.blockers, ensure_ascii=False),
            result.mode, result.error,
        ),
    )
    database.commit()
    return result
