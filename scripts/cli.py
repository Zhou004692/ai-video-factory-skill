"""AI Video Factory CLI.

Usage:
    python cli.py run <csv_path>          # 跑一批
    python cli.py status                   # 看进度
    python cli.py cost                     # 总花费
    python cli.py inspect <job_id>         # 看单 job 详情
    python cli.py retry <job_id>           # 重试 failed job
    python cli.py retry-shot <job_id> <i>  # 重试单个分镜
"""
import asyncio
import json
import sys
from pathlib import Path

# 加载 skill 根目录的 .env
from dotenv import load_dotenv
SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")

# 把 scripts/ 加进 sys.path,确保 factory 包可被导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

import click  # noqa: E402
from factory.db import conn  # noqa: E402
from factory.orchestrator import run_batch  # noqa: E402


@click.group()
def cli():
    """AI Video Factory."""


@cli.command()
@click.argument("csv_path", type=click.Path(exists=True))
def run(csv_path):
    """跑一批主题。"""
    asyncio.run(run_batch(csv_path))


@cli.command()
def status():
    """看进度。"""
    db = conn()
    rows = db.execute(
        "SELECT state, COUNT(*) c FROM jobs GROUP BY state ORDER BY state"
    ).fetchall()
    if not rows:
        click.echo("(no jobs)")
        return
    for r in rows:
        click.echo(f"{r['state']:15} {r['c']}")


@cli.command()
def cost():
    """总花费(分)。"""
    db = conn()
    total = db.execute("SELECT COALESCE(SUM(cost_cents), 0) FROM jobs").fetchone()[0]
    click.echo(f"总花费: ¥{total/100:.2f}")
    by_state = db.execute(
        "SELECT state, COALESCE(SUM(cost_cents),0) c FROM jobs GROUP BY state"
    ).fetchall()
    for r in by_state:
        click.echo(f"  {r['state']:15} ¥{r['c']/100:.2f}")


@cli.command()
@click.argument("job_id")
def inspect(job_id):
    """看单 job 详情。"""
    db = conn()
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        click.echo(f"job {job_id} not found")
        sys.exit(1)
    click.echo(json.dumps(dict(job), ensure_ascii=False, indent=2, default=str))
    click.echo("\n--- shots ---")
    shots = db.execute(
        "SELECT idx, state, provider, video_path, error FROM shots "
        "WHERE job_id=? ORDER BY idx",
        (job_id,)
    ).fetchall()
    for s in shots:
        click.echo(f"  {s['idx']:2d} [{s['state']:8}] {s['provider'] or '-':10} "
                   f"{s['video_path'] or '-'}")
        if s["error"]:
            click.echo(f"      ! {s['error'][:200]}")


@cli.command()
@click.argument("job_id")
def retry(job_id):
    """重试 failed job(从 PENDING 重新开始)。"""
    db = conn()
    n = db.execute(
        "UPDATE jobs SET state='PENDING', retries=0, error=NULL WHERE job_id=?",
        (job_id,)
    ).rowcount
    db.commit()
    if n:
        click.echo(f"reset {job_id} → PENDING")
    else:
        click.echo(f"job {job_id} not found")


@cli.command(name="retry-shot")
@click.argument("job_id")
@click.argument("idx", type=int)
def retry_shot(job_id, idx):
    """重试单个分镜。"""
    db = conn()
    n = db.execute(
        "UPDATE shots SET state='PENDING', video_path=NULL, error=NULL "
        "WHERE job_id=? AND idx=?",
        (job_id, idx)
    ).rowcount
    db.execute(
        "UPDATE jobs SET state='CHARACTER_OK' WHERE job_id=? AND state IN "
        "('SHOTS_OK','VOICE_OK','BGM_OK','COMPOSED','FAILED')",
        (job_id,)
    )
    db.commit()
    if n:
        click.echo(f"reset {job_id}#{idx} → PENDING, job rolled back to CHARACTER_OK")
    else:
        click.echo(f"shot {job_id}#{idx} not found")


@cli.command()
def events():
    """看最近 50 条事件日志。"""
    db = conn()
    rows = db.execute(
        "SELECT ts, job_id, step, level, message FROM events "
        "ORDER BY id DESC LIMIT 50"
    ).fetchall()
    for r in rows:
        click.echo(f"{r['ts']} [{r['level']:5}] {r['job_id']:10} {r['step']:15} "
                   f"{r['message'][:120]}")


if __name__ == "__main__":
    cli()
