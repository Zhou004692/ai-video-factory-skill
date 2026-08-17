"""从当前 state 续跑一个 job,直到终态 (PUBLISHED / FAILED)。

用法:
    python scripts/resume_job.py <job_id>
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory.db import conn          # noqa: E402
from factory.orchestrator import _process_job  # noqa: E402


async def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    job_id = sys.argv[1]
    db = conn()
    row = db.execute("SELECT state, cost_cents FROM jobs WHERE job_id=?",
                     (job_id,)).fetchone()
    if not row:
        raise SystemExit(f"job not found: {job_id}")
    print(f"[resume] {job_id}  state={row['state']}  cost=CNY{row['cost_cents']/100:.2f}")
    sem = asyncio.Semaphore(1)
    await _process_job(job_id, sem)
    row2 = db.execute("SELECT state, cost_cents FROM jobs WHERE job_id=?",
                      (job_id,)).fetchone()
    print(f"[done]   {job_id}  state={row2['state']}  cost=CNY{row2['cost_cents']/100:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
