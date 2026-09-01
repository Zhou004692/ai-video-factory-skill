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
    {_summary_card("浏览器动态动作", f"{_number(coverage.get('executed_console_action_count'))} / {_number(coverage.get('console_action_count'))}", _percent(coverage.get('console_action_coverage_pct')))}
    {_summary_card("完整耗时", _duration(payload.get('duration_sec')))}
  </div>

  <section>
    <h2>覆盖与缺口</h2>
    <div class="coverage-grid">
      <div class="coverage-item"><span class="muted">语义与媒体校验</span><strong>{_number(coverage.get('semantic_check_count'))} 项</strong>{_chips(_items(coverage.get('semantic_checks')))}</div>
      <div class="coverage-item"><span class="muted">执行节点类型</span><strong>{_number(coverage.get('executed_node_type_count'))} 类</strong>{_chips(_items(coverage.get('executed_node_types')))}</div>
      <div class="coverage-item"><span class="muted">管理台动作分发契约</span><strong>{_percent(coverage.get('dispatch_contract_coverage_pct'))}</strong><span class="muted">{_number(coverage.get('dispatch_contract_action_count'))} 个动作均存在入口</span></div>
      <div class="coverage-item"><span class="muted">浏览器动态覆盖</span><strong>{_percent(coverage.get('console_action_coverage_pct'))}</strong><span class="muted">本轮实际执行 {_number(coverage.get('executed_console_action_count'))} 个动作</span></div>
      <div class="coverage-item wide"><span class="muted">尚未由浏览器动态执行的动作</span><div>{_chips(missing_actions, empty="全部覆盖")}</div></div>
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
