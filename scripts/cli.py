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

import httpx

# Windows console 默认 GBK 编不出 ¥/部分中文符号(会让子流程 print 直接 crash);
# 入口强制 stdout/stderr UTF-8,管线后续 print 透明支持
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 加载 skill 根目录的 .env
from dotenv import load_dotenv
SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")

# 把 scripts/ 加进 sys.path,确保 factory 包可被导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

import click  # noqa: E402
from factory.db import conn  # noqa: E402
from factory.orchestrator import _process_job, run_batch  # noqa: E402
from factory import (  # noqa: E402
    asset_library,
    character_assets,
    creative_experiments,
    creative_learning,
    production_metrics,
    performance_feedback,
    production_planner,
    media_sources,
    script_drafts,
    script_sources,
    source_analysis,
    topic_briefs,
)
from factory.steps import s1_storyboard  # noqa: E402
from factory.clients import volc_assets  # noqa: E402


@click.group()
def cli():
    """AI Video Factory."""


@cli.command(name="character-resolve")
@click.option("--character-id", required=True)
@click.option("--name", "display_name", required=True)
@click.option(
    "--asset-id",
    default=None,
    help="Provider assetId supplied by the user when API discovery cannot resolve it.",
)
def character_resolve(character_id, display_name, asset_id):
    """Agent-only real-person asset resolution; API first, assetId fallback."""
    database = conn()
    try:
        if asset_id:
            result = character_assets.bind_manual_asset(
                database,
                character_id=character_id,
                display_name=display_name,
                asset_id=asset_id,
            )
        else:
            try:
                api_client = volc_assets.from_environment()
            except volc_assets.AssetApiUnavailable:
                api_client = None
            result = character_assets.resolve_character_asset(
                database,
                character_id=character_id,
                display_name=display_name,
                api_client=api_client,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))


@cli.command()
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--batch-id", default=None, help="Assign and run one cohort only.")
def run(csv_path, batch_id):
    """跑一批主题。"""
    asyncio.run(run_batch(csv_path, batch_id=batch_id))


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
@click.argument(
    "decision",
    type=click.Choice(sorted(production_metrics.REVIEW_DECISIONS)),
)
@click.option("--minutes", type=float, default=0, show_default=True)
@click.option("--notes", default=None)
@click.option("--first-pass/--not-first-pass", default=None)
def review(job_id, decision, minutes, notes, first_pass):
    """Record human review effort and outcome for one job."""
    db = conn()
    try:
        production_metrics.record_review(
            db,
            job_id,
            decision,
            manual_minutes=minutes,
            notes=notes,
            first_pass=first_pass,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"review recorded: {job_id} -> {decision}, manual={minutes:.1f} min"
    )


@cli.command(name="performance-add")
@click.argument("job_id")
@click.option("--platform", required=True)
@click.option("--captured-at", default=None, help="ISO-8601 snapshot time.")
@click.option("--views", type=int, required=True)
@click.option("--completed-views", type=int, default=None)
@click.option("--completion-rate", type=float, default=None)
@click.option("--average-watch-sec", type=float, default=None)
@click.option("--likes", type=int, default=0, show_default=True)
@click.option("--comments", type=int, default=0, show_default=True)
@click.option("--shares", type=int, default=0, show_default=True)
@click.option("--saves", type=int, default=0, show_default=True)
@click.option("--follows", type=int, default=0, show_default=True)
@click.option("--leads", type=int, default=0, show_default=True)
def performance_add(job_id, platform, captured_at, views, completed_views,
                    completion_rate, average_watch_sec, likes, comments,
                    shares, saves, follows, leads):
    """Record one cumulative performance snapshot for a published video."""
    try:
        snapshot_id = performance_feedback.record_snapshot(
            conn(), job_id, platform=platform, captured_at=captured_at,
            views=views, completed_views=completed_views,
            completion_rate=completion_rate,
            average_watch_sec=average_watch_sec, likes=likes,
            comments=comments, shares=shares, saves=saves, follows=follows,
            leads=leads,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"snapshot recorded: {snapshot_id}")


@cli.command(name="performance-import")
@click.argument(
    "csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--platform", default=None, help="Default platform for all rows.")
@click.option(
    "--captured-at", default=None,
    help="Default ISO-8601 snapshot time for all rows.",
)
@click.option("--dry-run", is_flag=True, help="Validate without writing.")
def performance_import(csv_path, platform, captured_at, dry_run):
    """Preflight and atomically import cumulative performance CSV rows."""
    try:
        result = performance_feedback.import_csv(
            conn(), csv_path, default_platform=platform,
            default_captured_at=captured_at, dry_run=dry_run,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    mode = "dry-run" if result["dry_run"] else "committed"
    click.echo(
        f"performance import {mode}: validated={result['rows']}, "
        f"unique={result['unique_snapshots']}, inserted={result['inserted']}, "
        f"updated={result['updated']}"
    )


@cli.command(name="publication-map")
@click.argument("job_id")
@click.option("--platform", required=True)
@click.option("--item-id", required=True)
@click.option("--item-url", default=None)
@click.option("--title", default=None)
@click.option("--published-at", default=None, help="ISO-8601 publish time.")
def publication_map(job_id, platform, item_id, item_url, title, published_at):
    """Map one canonical platform publication to a published job."""
    try:
        publication_id = performance_feedback.register_publication(
            conn(), job_id, platform=platform, item_id=item_id,
            item_url=item_url, title=title, published_at=published_at,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"publication mapped: {publication_id}")


@cli.command(name="performance-import-platform")
@click.argument(
    "csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--platform", required=True,
    type=click.Choice(["douyin", "weixin_channels"]),
)
@click.option("--captured-at", required=True, help="ISO-8601 snapshot time.")
@click.option("--dry-run", is_flag=True, help="Validate without writing.")
def performance_import_platform(csv_path, platform, captured_at, dry_run):
    """Import a native Douyin or Weixin Channels performance export."""
    try:
        result = performance_feedback.import_platform_csv(
            conn(), csv_path, platform=platform, captured_at=captured_at,
            dry_run=dry_run,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    mode = "dry-run" if result["dry_run"] else "committed"
    click.echo(
        f"platform import {mode}: platform={result['platform']}, "
        f"matched={result['matched']}, unique={result['unique_snapshots']}, "
        f"inserted={result['inserted']}, updated={result['updated']}"
    )


@cli.command(name="learning-report")
@click.option("--min-views", type=int, default=100, show_default=True)
@click.option(
    "--platform", default=None,
    type=click.Choice(["douyin", "weixin_channels"]),
)
@click.option("--window-hours", type=float, default=None)
@click.option(
    "--min-window-coverage", type=click.FloatRange(0, 1, min_open=True),
    default=0.8, show_default=True,
)
def learning_report(min_views, platform, window_hours, min_window_coverage):
    """Print creative/source learning from the latest platform snapshots."""
    try:
        report = performance_feedback.learning_report(
            conn(), min_views=min_views, platform=platform,
            window_hours=window_hours,
            min_window_coverage=min_window_coverage,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@cli.command(name="creative-tag")
@click.argument("brief_id")
@click.option("--angle", type=click.IntRange(1, 3), required=True)
@click.option(
    "--hook-type", required=True,
    type=click.Choice(sorted(creative_learning.VOCABULARY["hook_type"])),
)
@click.option(
    "--narrative-pattern", required=True,
    type=click.Choice(sorted(creative_learning.VOCABULARY["narrative_pattern"])),
)
@click.option(
    "--proof-type", required=True,
    type=click.Choice(sorted(creative_learning.VOCABULARY["proof_type"])),
)
@click.option(
    "--cta-type", required=True,
    type=click.Choice(sorted(creative_learning.VOCABULARY["cta_type"])),
)
@click.option("--notes", default=None)
def creative_tag(brief_id, angle, hook_type, narrative_pattern, proof_type,
                 cta_type, notes):
    """Apply controlled creative tags to one candidate angle."""
    try:
        creative_learning.tag_angle(
            conn(), brief_id, angle_index=angle - 1, hook_type=hook_type,
            narrative_pattern=narrative_pattern, proof_type=proof_type,
            cta_type=cta_type, notes=notes,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"creative tagged: {brief_id} angle={angle}")


@cli.command(name="strategy-report")
@click.option(
    "--platform", required=True,
    type=click.Choice(["douyin", "weixin_channels"]),
)
@click.option("--window-hours", type=float, required=True)
@click.option("--min-views", type=int, default=100, show_default=True)
@click.option("--min-samples", type=int, default=2, show_default=True)
@click.option(
    "--min-window-coverage", type=click.FloatRange(0, 1, min_open=True),
    default=0.8, show_default=True,
)
def strategy_report(platform, window_hours, min_views, min_samples,
                    min_window_coverage):
    """Compare controlled creative variables on a comparable cohort."""
    try:
        report = creative_learning.strategy_report(
            conn(), platform=platform, window_hours=window_hours,
            min_views=min_views, min_samples=min_samples,
            min_window_coverage=min_window_coverage,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@cli.command(name="experiment-create")
@click.option("--name", required=True)
@click.option(
    "--platform", required=True,
    type=click.Choice(["douyin", "weixin_channels"]),
)
@click.option("--window-hours", type=float, required=True)
@click.option(
    "--dimension", "variable_dimension", required=True,
    type=click.Choice(sorted(creative_learning.VOCABULARY)),
)
@click.option("--control", "control_value", required=True)
@click.option("--treatment", "treatment_value", required=True)
@click.option(
    "--primary-metric", required=True,
    type=click.Choice(sorted(creative_experiments.PRIMARY_METRICS)),
)
@click.option("--hypothesis", required=True)
@click.option("--audience", required=True)
@click.option("--production-profile", default="standard", show_default=True)
@click.option("--min-views", type=int, default=100, show_default=True)
@click.option("--min-jobs-per-arm", type=int, default=2, show_default=True)
@click.option("--planned-jobs-per-arm", type=int, default=2, show_default=True)
@click.option("--minimum-effect", type=float, default=0.0, show_default=True)
@click.option(
    "--min-window-coverage", type=click.FloatRange(0, 1, min_open=True),
    default=0.8, show_default=True,
)
def experiment_create(
    name, platform, window_hours, variable_dimension, control_value,
    treatment_value, primary_metric, hypothesis, audience,
    production_profile, min_views, min_jobs_per_arm, minimum_effect,
    min_window_coverage, planned_jobs_per_arm,
):
    """Pre-register a single-variable creative experiment contract."""
    try:
        experiment_id = creative_experiments.create_experiment(
            conn(), name=name, platform=platform, window_hours=window_hours,
            variable_dimension=variable_dimension,
            control_value=control_value, treatment_value=treatment_value,
            primary_metric=primary_metric, hypothesis=hypothesis,
            audience=audience, production_profile=production_profile,
            min_views=min_views, min_jobs_per_arm=min_jobs_per_arm,
            planned_jobs_per_arm=planned_jobs_per_arm,
            minimum_effect=minimum_effect,
            min_window_coverage=min_window_coverage,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"creative experiment created: {experiment_id}")


@cli.command(name="experiment-assign")
@click.argument("experiment_id")
@click.argument("job_id")
@click.option("--arm", required=True, type=click.Choice(["control", "treatment"]))
def experiment_assign(experiment_id, job_id, arm):
    """Assign one job before its platform performance is observed."""
    try:
        creative_experiments.assign_job(
            conn(), experiment_id, job_id, arm=arm,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"creative experiment assigned: {experiment_id} job={job_id} arm={arm}"
    )


@cli.command(name="experiment-allocate")
@click.argument("experiment_id")
@click.argument("subject_key")
def experiment_allocate(experiment_id, subject_key):
    """Issue an idempotent, balanced arm before creative production."""
    try:
        slot = creative_experiments.allocate_slot(
            conn(), experiment_id, subject_key=subject_key,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(slot, ensure_ascii=False, indent=2))


@cli.command(name="experiment-bind")
@click.argument("slot_id")
@click.argument("job_id")
def experiment_bind(slot_id, job_id):
    """Bind a completed creative job to its pre-issued experiment slot."""
    try:
        creative_experiments.bind_slot(conn(), slot_id, job_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"creative experiment slot bound: {slot_id} job={job_id}")


@cli.command(name="experiment-report")
@click.argument("experiment_id")
def experiment_report(experiment_id):
    """Evaluate the pre-registered metric at its fixed observation window."""
    try:
        report = creative_experiments.experiment_report(conn(), experiment_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@cli.command(name="asset-add")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--asset-id", required=True)
@click.option("--tag", "tags", multiple=True, required=True)
@click.option("--duration", type=float, required=True)
@click.option("--orientation", default="9:16", show_default=True)
@click.option(
    "--rights",
    type=click.Choice(sorted(asset_library.REUSABLE_RIGHTS)),
    default="owned",
    show_default=True,
)
def asset_add(path, asset_id, tags, duration, orientation, rights):
    """Register one approved, rights-safe B-roll video."""
    asset_library.register_asset(
        conn(),
        asset_id=asset_id,
        path=path,
        tags=tags,
        duration_sec=duration,
        orientation=orientation,
        rights_status=rights,
    )
    click.echo(f"asset registered: {asset_id} tags={','.join(tags)}")


@cli.command()
@click.option("--tag", "tags", multiple=True)
def assets(tags):
    """Search approved reusable B-roll assets."""
    rows = asset_library.list_assets(conn(), tags=tags)
    if not rows:
        click.echo("(no matching assets)")
        return
    for row in rows:
        click.echo(
            f"{row['asset_id']:24} {row['duration_sec']:5.1f}s "
            f"uses={row['use_count']:3d} tags={','.join(row['tags'])} "
            f"path={row['path']}"
        )


@cli.command(name="source-add")
@click.option(
    "--file", "file_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--text", default=None)
@click.option("--url", default=None)
@click.option("--title", default=None)
@click.option("--topic-id", default=None, help="Optionally link after import.")
def source_add(file_path, text, url, title, topic_id):
    """Archive one traceable script source without generating a video."""
    choices = [file_path is not None, text is not None, url is not None]
    if sum(choices) != 1:
        raise click.ClickException("provide exactly one of --file, --text or --url")
    database = conn()
    try:
        if file_path is not None:
            result = script_sources.ingest_file(database, file_path, title=title)
        elif text is not None:
            result = script_sources.ingest_text(
                database, text, title=title, source_uri="manual://cli"
            )
        else:
            result = script_sources.ingest_url(database, url, title=title)
        if topic_id:
            script_sources.link_source_to_topic(
                database, result.source_id, topic_id
            )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise click.ClickException(str(exc)) from exc
    state = "created" if result.created else "deduplicated"
    suffix = f" linked={topic_id}" if topic_id else ""
    click.echo(f"source {state}: {result.source_id}{suffix}")


@cli.command(name="source-link")
@click.argument("source_id")
@click.argument("topic_id")
def source_link(source_id, topic_id):
    """Link an archived source to one topic used by S1."""
    try:
        script_sources.link_source_to_topic(conn(), source_id, topic_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"source linked: {source_id} -> {topic_id}")


@cli.command(name="source-list")
@click.option("--topic-id", default=None)
def source_list(topic_id):
    """List archived script sources and optional topic links."""
    database = conn()
    if topic_id:
        rows = database.execute(
            "SELECT s.* FROM script_sources s JOIN topic_script_sources t "
            "ON t.source_id=s.source_id WHERE t.topic_id=? "
            "ORDER BY s.created_at DESC", (topic_id,),
        ).fetchall()
    else:
        rows = database.execute(
            "SELECT * FROM script_sources ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        click.echo("(no script sources)")
        return
    for row in rows:
        click.echo(
            f"{row['source_id']} {row['source_type']:8} "
            f"{row['title'] or '-'} {row['source_uri'] or '-'}"
        )


@cli.command(name="media-add")
@click.argument(
    "path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--title", default=None)
@click.option("--topic-id", default=None)
@click.option("--frame-interval", type=float, default=4.0, show_default=True)
def media_add(path, title, topic_id, frame_interval):
    """Archive and transcribe one local audio/video source."""
    try:
        result = asyncio.run(media_sources.ingest_media(
            conn(), path, title=title, topic_id=topic_id,
            frame_interval_sec=frame_interval,
        ))
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    state = "created" if result.created else "deduplicated"
    click.echo(
        f"media {state}: {result.source_id} sha256={result.media_sha256} "
        f"frames={len(result.frame_paths)}"
    )


@cli.command(name="source-analyze")
@click.argument("source_id")
@click.option("--max-frames", type=int, default=8, show_default=True)
@click.option("--force", is_flag=True, help="Recompute even if cached.")
def source_analyze(source_id, max_frames, force):
    """Run OCR/visual observation and story-structure analysis."""
    try:
        report = source_analysis.analyze_source(
            conn(), source_id, max_frames=max_frames, force=force
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@cli.command(name="brief-generate")
@click.argument("source_ids", nargs=-1, required=True)
@click.option("--force", is_flag=True, help="Regenerate even when a cached brief exists.")
def brief_generate(source_ids, force):
    """Score sources and create three reviewable creative angles."""
    try:
        result = topic_briefs.generate_brief(
            conn(), list(source_ids), force=force
        )
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"brief: {result['brief_id']} [{result['status']}]")
    brief = result["brief"]
    for index, angle in enumerate(brief["angles"], start=1):
        marker = "*" if index - 1 == brief["recommended_index"] else " "
        click.echo(f"{marker} {index}. {angle['title']} — {angle['hook']}")
    if result.get("duplicate_warning"):
        warning = result["duplicate_warning"]
        click.echo(
            f"warning: similar {warning['kind']} {warning['id']} "
            f"({warning['similarity']:.0%}) {warning['title']}"
        )


@cli.command(name="brief-approve")
@click.argument("brief_id")
@click.option("--topic-id", required=True)
@click.option("--track", default="")
@click.option("--audience", "target_audience", default="")
@click.option("--priority", type=int, default=3, show_default=True)
@click.option(
    "--angle", type=click.IntRange(1, 3),
    help="Approve angle 1, 2 or 3; defaults to the recommendation.",
)
def brief_approve(brief_id, topic_id, track, target_audience, priority, angle):
    """Approve one candidate and create its pending production job."""
    try:
        job_id = topic_briefs.approve_brief(
            conn(), brief_id, topic_id=topic_id, track=track,
            target_audience=target_audience, priority=priority,
            angle_index=angle - 1 if angle is not None else None,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"brief approved: {brief_id} -> {job_id} [PENDING]")


@cli.command(name="script-generate")
@click.argument("brief_id")
@click.option("--force", is_flag=True, help="Regenerate the three variants.")
def script_generate(brief_id, force):
    """Generate three complete, traceable scripts from one approved Brief."""
    try:
        result = script_drafts.generate_drafts(conn(), brief_id, force=force)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    cache = "cached" if result.get("cached") else "created"
    click.echo(
        f"scripts {cache}: {brief_id} — {len(result['drafts'])} variants"
    )
    for item in result["drafts"]:
        script = item["script"]
        marker = "*" if item["variant_index"] == result["recommended_index"] else " "
        duration = float(
            script.get("total_duration_sec")
            or script.get("target_duration_sec")
            or 0
        )
        click.echo(
            f"{marker} {item['variant_index'] + 1}. {item['draft_id']} "
            f"{script['title']} — {duration:.1f}s"
        )


@cli.command(name="script-list")
@click.argument("brief_id")
def script_list(brief_id):
    """List generated complete scripts for one Brief."""
    drafts = script_drafts.list_drafts(conn(), brief_id)
    if not drafts:
        click.echo("(no script drafts)")
        return
    for item in drafts:
        script = item["script"]
        marker = "*" if item["recommended"] else " "
        duration = float(
            script.get("total_duration_sec")
            or script.get("target_duration_sec")
            or 0
        )
        click.echo(
            f"{marker} {item['variant_index'] + 1}. {item['draft_id']} "
            f"[{item['status']}] {script['title']} — "
            f"{duration:.1f}s"
        )


@cli.command(name="script-approve")
@click.argument("draft_id")
def script_approve(draft_id):
    """Approve one script and build its zero-provider-call Storyboard."""
    try:
        result = script_drafts.approve_draft(conn(), draft_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"script approved: {result['draft_id']} -> {result['job_id']} [storyboard ready]"
    )


@cli.command(name="script-export")
@click.argument("draft_id")
@click.argument(
    "output_path", type=click.Path(dir_okay=False, path_type=Path)
)
def script_export(draft_id, output_path):
    """Export an approved script's standard Storyboard JSON."""
    try:
        storyboard = script_drafts.get_storyboard(conn(), draft_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"storyboard exported: {output_path}")


@cli.command()
@click.option("--batch", "batch_id", default=None, help="Only this batch ID.")
@click.option("--since", default=None, help="Only jobs created on/after this date.")
def metrics(batch_id, since):
    """Show production economics based on approved videos."""
    report = production_metrics.summarize(
        conn(), batch_id=batch_id, since=since
    )
    click.echo("=== Production economics ===")
    if batch_id:
        click.echo(f"batch: {batch_id}")
    if since:
        click.echo(f"since: {since}")
    click.echo(
        f"合格成片率       {report['qualified_yield_rate'] * 100:.1f}% "
        f"({report['approved_jobs']}/{report['created_jobs']})"
    )
    click.echo(
        f"单位合格模型成本 ¥{report['cost_per_approved_cents'] / 100:.2f}"
    )
    click.echo(f"人工处理时长     {report['manual_minutes']:.1f} min")
    click.echo(
        f"付费调用/合格片   {report['paid_calls_per_approved']:.2f}"
    )
    click.echo(
        f"零模型成本修复率 {report['zero_cost_repair_rate'] * 100:.1f}%"
    )
    click.echo(f"首轮通过率       {report['first_pass_rate'] * 100:.1f}%")


@cli.command()
@click.argument(
    "storyboard_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--budget", "budget_cents", type=int, default=None)
@click.option(
    "--resolution",
    type=click.Choice(["480p", "720p", "1080p"]),
    default=None,
)
def plan(storyboard_path, budget_cents, resolution):
    """Preflight a storyboard without calling any paid model."""
    try:
        storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
        s1_storyboard._validate(storyboard)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise click.ClickException(f"invalid storyboard: {exc}") from exc

    report = production_planner.analyze_storyboard(
        conn(),
        storyboard,
        budget_cents=budget_cents,
        resolution=resolution,
    )
    click.echo("=== Production cost preflight (no provider call) ===")
    click.echo(
        f"paid masters: {report['paid_master_count']}/{report['total_masters']}"
    )
    click.echo(f"library hits: {len(report['library_hits'])}")
    click.echo(
        f"estimated cost: ¥{report['estimated_cost_cents'] / 100:.2f} "
        f"({report['resolution']})"
    )
    budget_state = "within budget" if report["budget_ok"] else "OVER BUDGET"
    click.echo(
        f"budget: ¥{report['budget_cents'] / 100:.2f} — {budget_state}"
    )
    for hit in report["library_hits"]:
        click.echo(
            f"  reuse master {hit['master_idx']}: {hit['asset_id']} "
            f"(match {hit['match_score']:.0%})"
        )
    for warning in report["warnings"]:
        click.echo(f"  warning: {warning}")


@cli.command(name="produce-approved")
@click.argument("job_id")
@click.option(
    "--yes",
    is_flag=True,
    help="Confirm paid video generation for the existing approved Storyboard.",
)
def produce_approved(job_id, yes):
    """Resume one approved Storyboard without regenerating it."""
    database = conn()
    row = database.execute(
        "SELECT state,storyboard,cost_cents FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not row:
        raise click.ClickException(f"job not found: {job_id}")
    if row["state"] != "STORYBOARDED" or not row["storyboard"]:
        raise click.ClickException(
            "job must contain an approved Storyboard and be in STORYBOARDED state"
        )
    try:
        storyboard = json.loads(row["storyboard"])
        s1_storyboard._validate(storyboard)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.ClickException(f"invalid approved Storyboard: {exc}") from exc
    report = production_planner.analyze_storyboard(database, storyboard)
    click.echo("=== Approved production preview (no provider call) ===")
    click.echo(f"job: {job_id}")
    click.echo(f"state: {row['state']}")
    click.echo(f"paid masters: {report['paid_master_count']}")
    click.echo(f"estimated cost: ¥{report['estimated_cost_cents'] / 100:.2f}")
    if not yes:
        click.echo("No production submitted. Re-run with --yes after review.")
        return
    if not report["budget_ok"]:
        raise click.ClickException(
            "approved production exceeds the job budget before any paid call: "
            f"estimated={report['estimated_cost_cents']} cents, "
            f"limit={report['budget_cents']} cents"
        )
    click.echo("Confirmed: resuming from s3_shots; s1_storyboard will not run.")
    asyncio.run(_process_job(job_id, asyncio.Semaphore(1)))


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
        "UPDATE jobs SET state='STORYBOARDED' WHERE job_id=? AND state IN "
        "('SHOTS_OK','VOICE_OK','BGM_OK','COMPOSED','FAILED')",
        (job_id,)
    )
    db.commit()
    if n:
        click.echo(
            f"reset {job_id}#{idx} → PENDING, job rolled back to STORYBOARDED"
        )
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
