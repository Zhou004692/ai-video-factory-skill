"""跑单个 step,并把 jobs.state 推进到对应 next-state(不接着自动跑后续 step)。

用法:
    python scripts/run_step.py <job_id> <step_name>

step_name 必须是 TRANSITIONS 里登记的:
    s1_storyboard / s3_shots / s5_bgm / s6_compose / s7_publish
"""
import asyncio
import importlib
import sys
from pathlib import Path

# Windows consoles commonly default to GBK, which cannot print ¥ and may
# crash a completed paid step before its result is persisted.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory.db import conn, log  # noqa: E402
from factory.models import TRANSITIONS  # noqa: E402


async def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    job_id = sys.argv[1]
    step_name = sys.argv[2]

    next_state = None
    for (cur, st, nxt) in TRANSITIONS:
        if st == step_name:
            next_state = nxt.value
            break
    if next_state is None:
        raise SystemExit(
            f"unknown step: {step_name} (registered: "
            f"{[t[1] for t in TRANSITIONS]})"
        )

    db = conn()
    row = db.execute("SELECT state, cost_cents FROM jobs WHERE job_id=?",
                     (job_id,)).fetchone()
    if not row:
        raise SystemExit(f"job not found: {job_id}")

    print(f"[before] {job_id}  state={row['state']}  "
          f"cost=¥{row['cost_cents']/100:.2f}")
    print(f"[run]    {step_name} ...")

    mod = importlib.import_module(f"factory.steps.{step_name}")
    log(db, job_id, step_name, "INFO", "manual run_step")

    try:
        await mod.run(job_id)
    except Exception as e:
        import traceback
        tb = traceback.format_exc(limit=6)
        log(db, job_id, step_name, "ERROR", f"{e}\n{tb}")
        db.execute("UPDATE jobs SET state='FAILED', error=? WHERE job_id=?",
                   (f"{step_name}: {e}", job_id))
        db.commit()
        print(f"[FAIL]   {step_name}: {e}")
        print(tb)
        sys.exit(2)

    db.execute(
        "UPDATE jobs SET state=?, updated_at=CURRENT_TIMESTAMP, retries=0 "
        "WHERE job_id=?",
        (next_state, job_id)
    )
    db.commit()
    row2 = db.execute("SELECT cost_cents FROM jobs WHERE job_id=?",
                      (job_id,)).fetchone()
    print(f"[after]  {job_id}  state={next_state}  "
          f"cost=¥{row2['cost_cents']/100:.2f}  "
          f"(this step: ¥{(row2['cost_cents']-row['cost_cents'])/100:.2f})")


if __name__ == "__main__":
    asyncio.run(main())
