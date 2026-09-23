"""Zero-cost transport diagnostics for the AI Video Factory provider path.

The ARK_VIDEO check is a read-only transport probe and never submits a task.
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import dotenv_values

from factory import transport_diagnostics


ROOT = Path(__file__).resolve().parent.parent


def _environment() -> dict[str, str]:
    import os

    values = dict(os.environ)
    values.update({
        str(key): str(value)
        for key, value in dotenv_values(ROOT / ".env").items()
        if value is not None
    })
    return values


def main() -> int:
    report_path = ROOT / "data" / "runtime" / "transport-preflight.json"
    report = transport_diagnostics.run_preflight(
        environment=_environment(),
        report_path=report_path,
    )
    print("\nAI Video Factory · 零费用连接诊断\n")
    for target in report["targets"]:
        status = "OK" if target["ready"] else "FAIL"
        print(f"[{status}] {target['label']} · {target['host']}")
        for phase in target.get("phases") or []:
            detail = (
                f"HTTP {phase['http_status']}"
                if phase.get("http_status") is not None
                else phase.get("reason_code") or f"{phase.get('latency_ms', 0)} ms"
            )
            print(f"  - {phase['phase']}: {phase['status']} · {detail}")
    print("\n" + json.dumps({
        "diagnostic_id": report["diagnostic_id"],
        "ready": report["ready"],
        "paid_provider_calls": report["paid_provider_calls"],
        "provider_task_created": report["provider_task_created"],
        "report_path": "data/runtime/transport-preflight.json",
    }, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
