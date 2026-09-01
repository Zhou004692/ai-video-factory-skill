"""Render an offline, human-readable Flow Guard report."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from html import escape
from typing import Any


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: object, default: str = "—") -> str:
    if value is None or value == "":
        return default
    return escape(str(value))


def _number(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _percent(value: object) -> str:
    try:
        return f"{float(value or 0):.2f}".rstrip("0").rstrip(".") + "%"
    except (TypeError, ValueError):
        return "0%"


def _duration(value: object) -> str:
    try:
        total = max(0, round(float(value or 0)))
    except (TypeError, ValueError):
        total = 0
    minutes, seconds = divmod(total, 60)
    return f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"


def _status(value: object) -> tuple[str, str]:
    normalized = str(value or "unknown").lower()
    if normalized == "passed":
        return "通过", "success"
    if normalized == "failed":
        return "失败", "danger"
    return _text(value, "未知"), "neutral"


def _chips(values: Iterable[object], *, empty: str = "无") -> str:
    rendered = "".join(f'<span class="chip">{_text(value)}</span>' for value in values)
    return rendered or f'<span class="muted">{escape(empty)}</span>'


def _finding_text(finding: object) -> str:
    item = _mapping(finding)
    if item:
        code = _text(item.get("code"), "unknown")
        message = _text(item.get("message"), "未提供详情")
        return f"<code>{code}</code><span>{message}</span>"
    return f"<span>{_text(finding)}</span>"


def _summary_card(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="metric-note">{escape(note)}</div>' if note else ""
    return (
        '<article class="metric">'
        f'<div class="metric-label">{escape(label)}</div>'
        f'<div class="metric-value">{value}</div>{note_html}'
        '</article>'
    )


def render_html_report(
    payload: Mapping[str, object], *, json_filename: str = "latest.json"
) -> str:
    """Return a self-contained HTML report safe to open from the filesystem."""
    coverage = _mapping(payload.get("coverage"))
    mutation = _mapping(payload.get("mutation_calibration"))
    incidents = _mapping(payload.get("historical_incident_replays"))
    video_golden = _mapping(payload.get("video_source_golden"))
    shadow_audit = _mapping(payload.get("production_shadow_audit"))
    perceptual_media = _mapping(payload.get("perceptual_media_golden"))
    journey_suite = _mapping(payload.get("journey_suite"))
    browser_suite = _mapping(journey_suite.get("browser_suite"))
    status_label, status_class = _status(payload.get("status"))

    scenario_count = _number(payload.get("scenario_count"))
    scenario_passed = _number(payload.get("passed"))
    journey_count = _number(payload.get("journey_count"))
    journey_passed = _number(payload.get("journeys_passed"))
    mutation_count = _number(payload.get("historical_mutation_count"))
    mutation_killed = _number(payload.get("historical_mutations_killed"))
    paid_calls = _number(payload.get("paid_provider_calls"))

    service_rows = []
    for raw in _items(journey_suite.get("journeys")):
        item = _mapping(raw)
        label, css_class = _status(item.get("status"))
        findings = sum(
            len(_items(item.get(key)))
            for key in ("semantic_findings", "structural_findings", "media_findings")
        )
        service_rows.append(
            "<tr>"
            f'<td><code>{_text(item.get("scenario_id"))}</code></td>'
            f'<td><span class="status {css_class}">{label}</span></td>'
            f'<td>{_text(item.get("final_state"))}</td>'
            f'<td>{_number(item.get("character_count"))}</td>'
            f'<td>{_number(item.get("scene_count"))}</td>'
            f'<td>{_number(item.get("background_voiceover_master_count"))}</td>'
            f'<td>{findings}</td>'
            "</tr>"
        )

    browser_rows = []
    for raw in _items(browser_suite.get("journeys")):
        item = _mapping(raw)
        label, css_class = _status(item.get("status"))
        browser_rows.append(
            "<tr>"
            f'<td><code>{_text(item.get("scenario_id"))}</code></td>'
            f'<td><span class="status {css_class}">{label}</span></td>'
            f'<td>{_number(item.get("scenario_count"))}</td>'
            f'<td>{_text(item.get("exit_code"))}</td>'
            "</tr>"
        )

    mutation_rows = []
    for raw in _items(mutation.get("results")):
        item = _mapping(raw)
        killed = bool(item.get("killed"))
        mutation_rows.append(
            "<tr>"
            f'<td><code>{_text(item.get("mutation_id"))}</code></td>'
            f'<td><code>{_text(item.get("expected_code"))}</code></td>'
            f'<td><span class="status {"success" if killed else "danger"}">'
            f'{"已检出" if killed else "未检出"}</span></td>'
            f'<td>{_chips(_items(item.get("observed_codes")))}</td>'
            "</tr>"
        )

    incident_rows = []
    for raw in _items(incidents.get("results")):
        item = _mapping(raw)
        passed = str(item.get("status") or "").lower() == "passed"
        incident_rows.append(
            "<tr>"
            f'<td><code>{_text(item.get("incident_id"))}</code></td>'
            f'<td>{_text(item.get("title"))}</td>'
            f'<td><span class="status {"success" if passed else "danger"}">'
            f'{"通过" if passed else "失败"}</span></td>'
            f'<td>{_chips(_items(item.get("expected_guards")))}</td>'
            f'<td>{_number(item.get("test_count"))}</td>'
            "</tr>"
        )

    perceptual_rows = []
    for raw in _items(perceptual_media.get("results")):
        item = _mapping(raw)
        killed = bool(item.get("killed"))
        perceptual_rows.append(
            "<tr>"
            f'<td><code>{_text(item.get("mutation_id"))}</code></td>'
            f'<td><code>{_text(item.get("expected_code"))}</code></td>'
            f'<td><span class="status {"success" if killed else "danger"}">'
            f'{"已检出" if killed else "未检出"}</span></td>'
            f'<td>{_chips(_items(item.get("observed_codes")))}</td>'
            "</tr>"
        )

    shadow_rows = []
    for raw in _items(shadow_audit.get("jobs")):
        item = _mapping(raw)
        if not item.get("paused_node_count") and not _items(
            item.get("regression_codes")
        ):
            continue
        shadow_rows.append(
            "<tr>"
            f'<td><code>{_text(item.get("job_id"))}</code></td>'
            f'<td>{_text(item.get("state"))}</td>'
            f'<td>{_number(item.get("paused_node_count"))}</td>'
            f'<td>{_chips(_items(item.get("regression_codes")), empty="无")}</td>'
            "</tr>"
        )

    failed_reports = [
        _mapping(report)
        for report in _items(payload.get("reports"))
        if str(_mapping(report).get("status") or "").lower() != "passed"
    ]
    failure_cards = []
    for report in failed_reports:
        findings = _items(report.get("findings"))
        finding_list = "".join(
            f"<li>{_finding_text(finding)}</li>" for finding in findings
        ) or '<li class="muted">没有结构化问题详情</li>'
        failure_cards.append(
            '<article class="failure-card">'
            f'<div><code>{_text(report.get("scenario_id"))}</code>'
            f'<span class="status danger">{_text(report.get("final_state"), "失败")}</span></div>'
            f'<ul class="findings">{finding_list}</ul>'
            '</article>'
        )

    missing_actions = _items(coverage.get("missing_console_actions"))
    graph_actions = _items(coverage.get("graph_actions"))
    ui_actions = _items(coverage.get("ui_dispatch_actions"))
    backend_actions = _items(coverage.get("integrated_backend_actions"))
    worker_nodes = _items(coverage.get("worker_node_types"))
    source_types = _items(coverage.get("source_types"))
    provider_responses = _items(coverage.get("provider_response_types"))
    generated_at = _text(payload.get("generated_at"))
    provider_mode = _text(payload.get("provider_mode"))
    json_href = escape(json_filename, quote=True)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flow Guard 零费用回归报告</title>
  <style>
    :root {{ color-scheme: light; --blue:#356bff; --ink:#222; --muted:#6b7280; --line:#e5e7eb; --bg:#f5f7fa; --good:#237a4b; --good-bg:#edf7f1; --bad:#b42318; --bad-bg:#fff1f0; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--bg); font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif; }}
    main {{ width:min(1440px,calc(100% - 48px)); margin:28px auto 64px; }}
    header,section {{ background:#fff; border:1px solid var(--line); border-radius:12px; }}
    header {{ padding:26px 28px; display:flex; justify-content:space-between; gap:24px; align-items:flex-start; }}
    h1 {{ margin:4px 0 8px; font-size:28px; line-height:1.25; }}
    h2 {{ margin:0 0 18px; font-size:18px; }}
    .eyebrow {{ color:var(--blue); font-size:12px; font-weight:700; letter-spacing:.12em; }}
    .meta,.muted {{ color:var(--muted); }}
    .meta span {{ margin-right:18px; }}
    .raw-link {{ color:var(--blue); text-decoration:none; font-weight:600; }}
    .hero-status {{ min-width:118px; padding:12px 18px; border-radius:10px; text-align:center; font-size:18px; font-weight:700; }}
    .hero-status.success {{ color:var(--good); background:var(--good-bg); }}
    .hero-status.danger {{ color:var(--bad); background:var(--bad-bg); }}
    .hero-status.neutral {{ color:var(--muted); background:#f3f4f6; }}
    .metrics {{ display:grid; grid-template-columns:repeat(6,minmax(140px,1fr)); gap:12px; margin:16px 0; }}
    .metric {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:16px 18px; }}
    .metric-label {{ color:var(--muted); font-size:12px; }}
    .metric-value {{ margin-top:4px; font-size:23px; font-weight:700; font-variant-numeric:tabular-nums; }}
    .metric-note {{ color:var(--muted); font-size:12px; }}
    section {{ margin-top:16px; padding:22px 24px; overflow:hidden; }}
    .coverage-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }}
    .coverage-item {{ border:1px solid var(--line); border-radius:10px; padding:14px; }}
    .coverage-item strong {{ display:block; margin-bottom:6px; font-size:18px; }}
    .coverage-item.wide {{ grid-column:span 4; }}
    .chip {{ display:inline-block; margin:2px 5px 2px 0; padding:2px 8px; color:#3f4c5f; background:#f1f4f8; border-radius:999px; font:12px/1.7 ui-monospace,Consolas,monospace; }}
    .table-wrap {{ overflow-x:auto; margin-top:12px; }}
    table {{ width:100%; border-collapse:collapse; min-width:760px; }}
    th,td {{ padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ background:#f6f8fb; color:#4b5563; font-size:12px; }}
    code {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; }}
    .status {{ display:inline-block; margin-left:6px; padding:1px 8px; border-radius:999px; font-size:12px; font-weight:700; white-space:nowrap; }}
    .status.success {{ color:var(--good); background:var(--good-bg); }}
    .status.danger {{ color:var(--bad); background:var(--bad-bg); }}
    .status.neutral {{ color:var(--muted); background:#f3f4f6; }}
    .failure-card {{ border-left:4px solid var(--bad); background:var(--bad-bg); padding:14px 16px; margin-top:10px; border-radius:6px; }}
    .findings {{ margin:10px 0 0; padding-left:18px; }}
    .findings li {{ margin:5px 0; }}
    .findings code {{ margin-right:10px; font-weight:700; }}
    .empty {{ color:var(--good); background:var(--good-bg); border-radius:8px; padding:12px 14px; }}
    footer {{ color:var(--muted); text-align:center; margin-top:18px; font-size:12px; }}
    @media (max-width:1000px) {{ .metrics {{ grid-template-columns:repeat(3,1fr); }} .coverage-grid {{ grid-template-columns:repeat(2,1fr); }} .coverage-item.wide {{ grid-column:span 2; }} }}
    @media (max-width:640px) {{ main {{ width:calc(100% - 24px); margin-top:12px; }} header {{ display:block; padding:20px; }} .hero-status {{ margin-top:16px; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} section {{ padding:18px 16px; }} }}
    @media print {{ body {{ background:#fff; }} main {{ width:100%; margin:0; }} header,section,.metric {{ break-inside:avoid; box-shadow:none; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <div class="eyebrow">CONTINUOUS FLOW REGRESSION</div>
      <h1>Flow Guard 零费用回归报告</h1>
      <div class="meta"><span>生成时间：{generated_at}</span><span>Provider：{provider_mode}</span><a class="raw-link" href="{json_href}">查看原始 JSON</a></div>
    </div>
    <div class="hero-status {status_class}">{status_label}</div>
  </header>

  <div class="metrics">
    {_summary_card("有效路径", f"{scenario_passed} / {scenario_count}", f"唯一轨迹 {_number(payload.get('unique_trace_count'))} 条")}
    {_summary_card("业务与管理台旅程", f"{journey_passed} / {journey_count}")}
    {_summary_card("历史故障检出", f"{mutation_killed} / {mutation_count}", f"杀伤率 {_percent(payload.get('historical_mutation_score_pct'))}")}
    {_summary_card("付费调用", str(paid_calls), "必须保持为 0")}
    {_summary_card("真实证据动作", f"{len(ui_actions)} UI / {len(backend_actions)} 后端", f"Worker {len(worker_nodes)} 类")}
    {_summary_card("完整耗时", _duration(payload.get('duration_sec')))}
  </div>

  <section>
    <h2>覆盖与缺口</h2>
    <div class="coverage-grid">
      <div class="coverage-item"><span class="muted">Graph 模拟动作</span><strong>{len(graph_actions)} 项</strong>{_chips(graph_actions)}</div>
      <div class="coverage-item"><span class="muted">UI 实际派发</span><strong>{len(ui_actions)} 项</strong>{_chips(ui_actions)}</div>
      <div class="coverage-item"><span class="muted">后端真实处理</span><strong>{len(backend_actions)} 项</strong>{_chips(backend_actions)}</div>
      <div class="coverage-item"><span class="muted">Worker 实际节点</span><strong>{len(worker_nodes)} 类</strong>{_chips(worker_nodes)}</div>
      <div class="coverage-item"><span class="muted">来源类型</span><strong>{len(source_types)} 类</strong>{_chips(source_types)}</div>
      <div class="coverage-item"><span class="muted">Provider 响应类型</span><strong>{len(provider_responses)} 类</strong>{_chips(provider_responses)}</div>
      <div class="coverage-item"><span class="muted">语义与媒体校验</span><strong>{_number(coverage.get('semantic_check_count'))} 项</strong>{_chips(_items(coverage.get('semantic_checks')))}</div>
      <div class="coverage-item"><span class="muted">执行节点类型</span><strong>{_number(coverage.get('executed_node_type_count'))} 类</strong>{_chips(_items(coverage.get('executed_node_types')))}</div>
      <div class="coverage-item"><span class="muted">管理台动作分发契约</span><strong>{_percent(coverage.get('dispatch_contract_coverage_pct'))}</strong><span class="muted">{_number(coverage.get('dispatch_contract_action_count'))} 个动作均存在入口</span></div>
      <div class="coverage-item wide"><span class="muted">尚未获得任何运行证据的动作</span><div>{_chips(missing_actions, empty="全部覆盖")}</div></div>
    </div>
  </section>

  <section>
    <h2>正式服务旅程</h2>
    <div class="table-wrap"><table><thead><tr><th>旅程</th><th>结果</th><th>最终状态</th><th>人物</th><th>场景</th><th>纯旁白母片</th><th>问题</th></tr></thead><tbody>{''.join(service_rows) or '<tr><td colspan="7" class="muted">未执行正式服务旅程</td></tr>'}</tbody></table></div>
  </section>

  <section>
    <h2>管理台浏览器旅程</h2>
    <div class="table-wrap"><table><thead><tr><th>套件</th><th>结果</th><th>场景数</th><th>退出码</th></tr></thead><tbody>{''.join(browser_rows) or '<tr><td colspan="4" class="muted">未执行浏览器旅程</td></tr>'}</tbody></table></div>
  </section>

  <section>
    <h2>正式库只读影子审计</h2>
    <div class="meta">只读检查最近 {_number(shadow_audit.get('audited_job_count'))} 个真实任务；业务暂停单独展示，结构回归才阻断测试。数据库：{_text(shadow_audit.get('database_path'))}</div>
    <div class="coverage-grid">
      <div class="coverage-item"><span class="muted">需外部处理</span><strong>{_number(shadow_audit.get('action_required_job_count'))} 个任务</strong></div>
      <div class="coverage-item"><span class="muted">需人工复核</span><strong>{_number(shadow_audit.get('review_required_job_count'))} 个任务</strong></div>
      <div class="coverage-item wide"><span class="muted">结构回归代码</span><div>{_chips(_items(shadow_audit.get('regression_codes')), empty="无")}</div></div>
    </div>
    <div class="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>暂停节点</th><th>结构回归</th></tr></thead><tbody>{''.join(shadow_rows) or '<tr><td colspan="4" class="muted">未发现暂停任务或结构回归</td></tr>'}</tbody></table></div>
  </section>

  <section>
    <h2>本地视频表演金标</h2>
    <div class="coverage-grid">
      <div class="coverage-item"><span class="muted">结果</span><strong>{_status(video_golden.get('status'))[0]}</strong><span class="muted">{_text(video_golden.get('source_type'))}</span></div>
      <div class="coverage-item"><span class="muted">解析证据帧</span><strong>{_number(video_golden.get('analysis_frame_count'))} 帧</strong></div>
      <div class="coverage-item wide"><span class="muted">已通过语义护栏</span><div>{_chips(_items(video_golden.get('passed_guards')))}</div></div>
      <div class="coverage-item wide"><span class="muted">失败护栏</span><div>{_chips(_items(video_golden.get('failed_guards')), empty="无")}</div></div>
    </div>
  </section>

  <section>
    <h2>成片感知质量变异校准</h2>
    <div class="meta">正常运动视频必须无告警；冻结、黑帧、帧时间戳断层和音画时长错位必须分别被检出。全程仅使用本地 FFmpeg，不产生模型费用。</div>
    <div class="coverage-grid">
      <div class="coverage-item"><span class="muted">正常样本告警</span><strong>{len(_items(perceptual_media.get('clean_findings')))} 项</strong>{_chips(_items(perceptual_media.get('clean_findings')), empty="无")}</div>
      <div class="coverage-item"><span class="muted">变异检出</span><strong>{_number(perceptual_media.get('killed'))} / {_number(perceptual_media.get('mutation_count'))}</strong><span class="muted">检出率 {_percent(perceptual_media.get('score_pct'))}</span></div>
      <div class="coverage-item wide"><span class="muted">漏检变异</span><div>{"<span class='muted'>无</span>" if not _number(perceptual_media.get('survived')) else f"<span class='status danger'>{_number(perceptual_media.get('survived'))} 项</span>"}</div></div>
    </div>
    <div class="table-wrap"><table><thead><tr><th>变异样本</th><th>预期问题码</th><th>结果</th><th>实际检出</th></tr></thead><tbody>{''.join(perceptual_rows) or '<tr><td colspan="4" class="muted">未运行成片感知质量校准</td></tr>'}</tbody></table></div>
  </section>

  <section>
    <h2>历史事故逐条回放</h2>
    <div class="meta">测试 21–25 的用户可见故障分别运行，任一失败都会阻断整轮 Flow Guard。</div>
    <div class="table-wrap"><table><thead><tr><th>事故</th><th>问题</th><th>结果</th><th>预期护栏</th><th>测试数</th></tr></thead><tbody>{''.join(incident_rows) or '<tr><td colspan="5" class="muted">未运行历史事故回放</td></tr>'}</tbody></table></div>
  </section>

  <section>
    <h2>历史故障变异校准</h2>
    <div class="meta">通过主动注入已知坏状态，验证检测器确实能够发现问题，防止“流程跑完但什么也没检查”。</div>
    <div class="table-wrap"><table><thead><tr><th>故障变异</th><th>预期问题码</th><th>结果</th><th>实际检出</th></tr></thead><tbody>{''.join(mutation_rows) or '<tr><td colspan="4" class="muted">没有变异校准记录</td></tr>'}</tbody></table></div>
  </section>

  <section>
    <h2>失败路径与阻塞</h2>
    {''.join(failure_cards) or '<div class="empty">本轮没有失败路径或语义阻塞。</div>'}
  </section>

  <footer>此报告由 Flow Guard 自动生成；原始事实以同目录 JSON 为准。</footer>
</main>
</body>
</html>
"""
