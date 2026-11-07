#!/usr/bin/env python3
"""Render a parity_scan JSON report into a self-contained HTML page.

The page is deliberately static: no API calls, no keys, no JavaScript data
fetching. GitHub Actions runs ``parity_scan.py``, then this script, and
publishes the result to ``/parity.html`` on the same Pages site as the paper
dashboard.

Usage:
    python parity_page.py --input-dir state/parity --output state/parity/parity.html
    python parity_page.py --input parity_2026-11-07.json --output parity.html
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ORANGE = "#f59e0b"
GREEN = "#34d399"
RED = "#fb7185"
BLUE = "#7dd3fc"

REJECT_LABELS = {
    "not_negrisk": "事件不是互斥 negRisk，不能当互补篮子",
    "event_closed": "事件已关闭",
    "group_unverified": "官方腿数未知，无法确认篮子完整",
    "group_incomplete": "快照腿数 ≠ 官方腿数（缺腿）",
    "missing_quote": "某条腿缺少有效卖一",
    "horizon": "距结束不足最小小时数",
    "below_threshold": "扣费后 edge 不足",
    "borderline_edge": "edge 异常高，疑似脏数据",
    "thin_liquidity": "卖一深度不足",
    "book_unavailable": "CLOB 盘口拉取失败",
    "leg_closed_or_inactive": "某条腿已关闭或未激活",
    "non_binary": "不是二元市场",
}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def price(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "—"


def money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def short_time(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return text


def latest_report(input_dir: Path) -> Path | None:
    if not input_dir.exists():
        return None
    reports = sorted(input_dir.glob("parity_*.json"))
    return reports[-1] if reports else None


def load_report(input_path: Path | None, input_dir: Path) -> tuple[dict, str]:
    if input_path is None:
        input_path = latest_report(input_dir)
    if input_path is None or not input_path.exists():
        return {}, ""
    try:
        report = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, input_path.name
    return report if isinstance(report, dict) else {}, input_path.name


def rows_basket(rows: Iterable[dict]) -> str:
    out = []
    for row in rows:
        edge = float(row.get("edge") or 0.0)
        cls = "pos" if edge > 0 else "neg"
        out.append(
            "<tr>"
            f"<td class='wide'>{esc(row.get('title'))}</td>"
            f"<td>{num(row.get('legs'))}</td>"
            f"<td>{num(row.get('official_legs')) if row.get('official_legs') is not None else '—'}</td>"
            f"<td>{price(row.get('cost'))}</td>"
            f"<td>{price(row.get('fees'))}</td>"
            f"<td class='{cls}'>{pct(edge)}</td>"
            "<td class='muted'>仍需核对深度与原子执行；不是收益</td>"
            "</tr>"
        )
    return "".join(out)


def rows_suspicious(rows: Iterable[dict]) -> str:
    out = []
    for row in rows:
        reason = str(row.get("reason") or "")
        official = row.get("official_legs")
        out.append(
            "<tr>"
            f"<td class='wide'>{esc(row.get('title'))}</td>"
            f"<td>{num(row.get('legs'))}</td>"
            f"<td>{num(official) if official is not None else '—'}</td>"
            f"<td>{price(row.get('cost'))}</td>"
            f"<td class='warn'>{pct(row.get('edge'))}</td>"
            f"<td>{esc(REJECT_LABELS.get(reason, reason))}</td>"
            "</tr>"
        )
    return "".join(out)


def rows_parity(rows: Iterable[dict]) -> str:
    out = []
    for row in rows:
        out.append(
            "<tr>"
            f"<td class='wide'>{esc(row.get('question'))}</td>"
            f"<td>{price(row.get('yes_ask'))}</td>"
            f"<td>{price(row.get('no_ask'))}</td>"
            f"<td class='pos'>{pct(row.get('edge'))}</td>"
            f"<td>{money(row.get('yes_depth_usd'))}</td>"
            f"<td>{money(row.get('no_depth_usd'))}</td>"
            "</tr>"
        )
    return "".join(out)


def reject_table(rejects: dict) -> str:
    if not rejects:
        return "<p class='empty'>没有拒绝记录。</p>"
    rows = []
    for reason, count in sorted(rejects.items(), key=lambda item: -int(item[1] or 0)):
        rows.append(
            "<tr>"
            f"<td><code>{esc(reason)}</code></td>"
            f"<td>{esc(REJECT_LABELS.get(reason, '—'))}</td>"
            f"<td class='right'>{num(count)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>原因</th><th>含义</th><th class='right'>数量</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def kpi(label: str, value: str, note: str, color: str = "#e5edf7") -> str:
    return (
        "<div class='kpi'>"
        f"<div class='kpi-label'>{esc(label)}</div>"
        f"<div class='kpi-value' style='color:{color}'>{esc(value)}</div>"
        f"<div class='kpi-note'>{esc(note)}</div>"
        "</div>"
    )


def section(title: str, body: str, note: str = "") -> str:
    note_html = f"<p class='section-note'>{note}</p>" if note else ""
    return f"<section class='card'><h2>{esc(title)}</h2>{note_html}{body}</section>"


def render(report: dict, source_name: str = "") -> str:
    coverage = report.get("coverage") or {}
    basket = report.get("basket") or {}
    parity = report.get("parity") or {}
    settings = report.get("settings") or {}

    basket_candidates = list(basket.get("candidates") or [])
    suspicious = list(basket.get("suspicious") or [])
    parity_candidates = list(parity.get("candidates") or []) if isinstance(parity, dict) else []
    parity_skipped = parity.get("skipped") if isinstance(parity, dict) else None

    verified = int(coverage.get("verified_events") or basket.get("verified_events") or 0)
    group_incomplete = int((basket.get("rejects") or {}).get("group_incomplete") or 0)
    group_unverified = int((basket.get("rejects") or {}).get("group_unverified") or 0)
    checked_parity = int(coverage.get("parity_checked") or (parity.get("checked") if isinstance(parity, dict) else 0) or 0)

    if basket_candidates or parity_candidates:
        verdict_tone = "warn"
        verdict_title = "发现候选，但还不是收益"
        verdict_body = (
            f"篮子候选 {len(basket_candidates)} 个，同市场平价候选 {len(parity_candidates)} 个。"
            "下一步是人工核对盘口深度、两条腿的原子执行和手续费；不要把它当成已实现盈亏。"
        )
    elif not settings.get("event_details"):
        verdict_tone = "neutral"
        verdict_title = "离线模式：0 候选"
        verdict_body = (
            "本次没有拉取 Gamma 官方腿数，所以任何 raw 事件组都不算候选。"
            "先看“可疑原始组”：高 edge 多半是缺腿，不是套利。"
        )
    elif verified == 0:
        verdict_tone = "neutral"
        verdict_title = "0 个已验证完整篮子"
        verdict_body = (
            "所有多腿事件都因官方腿数未知或与快照不一致被拒绝。"
            "这不是扫描失败，而是“没有可确认完整的篮子”。"
        )
    else:
        verdict_tone = "ok"
        verdict_title = "0 候选：当前快照没有免费钱"
        verdict_body = (
            f"已验证 {verified} 个完整事件篮子，扣费后没有达到阈值的缺口；"
            f"另有 {group_incomplete} 个组因缺腿被拒绝。0 是正常读数，不要下调阈值来找信号。"
        )

    if not report:
        content = (
            "<div class='empty-state'>"
            "<h1>还没有平价扫描数据</h1>"
            "<p>页面已就位，但 GitHub Actions 尚未成功运行第一次扫描。</p>"
            "<p>可以在 Actions 里手动运行 <code>Parity Scan</code>，或等待每日定时任务。</p>"
            "<p><a href='./'>← 返回纸盘 v2 面板</a></p>"
            "</div>"
        )
        return page_shell("平价扫描 · 暂无数据", content)

    kpis = "".join(
        [
            kpi("扫描市场", num(coverage.get("markets")), f"事件 {num(coverage.get('events'))} 个"),
            kpi("多腿事件", num(coverage.get("multi_leg_events")), f"可评估 {num(coverage.get('eligible_events'))} 个"),
            kpi("已验证事件", num(verified), "官方腿数与快照一致", GREEN if verified else "#e5edf7"),
            kpi("篮子候选", num(len(basket_candidates)), "扣费后完整篮子", GREEN if basket_candidates else "#e5edf7"),
            kpi("同市场候选", num(len(parity_candidates)), f"检查 {num(checked_parity)} 个市场"),
            kpi("可疑原始组", num(len(suspicious)), f"缺腿 {group_incomplete} · 未验证 {group_unverified}", ORANGE if suspicious else "#e5edf7"),
        ]
    )

    verdict = (
        f"<div class='verdict {verdict_tone}'>"
        f"<div class='verdict-title'>{esc(verdict_title)}</div>"
        f"<div class='verdict-body'>{esc(verdict_body)}</div>"
        "</div>"
    )

    basket_body = (
        "<table><thead><tr><th>事件</th><th>腿数</th><th>官方腿数</th><th>成本</th><th>费用</th><th>edge</th><th>状态</th></tr></thead>"
        f"<tbody>{rows_basket(basket_candidates)}</tbody></table>"
        if basket_candidates
        else "<p class='empty'>没有通过“官方腿数 == 快照腿数”且扣费后仍有缺口的完整篮子。</p>"
    )

    suspicious_body = (
        "<table><thead><tr><th>事件</th><th>快照腿数</th><th>官方腿数</th><th>成本</th><th>表面 edge</th><th>拒绝原因</th></tr></thead>"
        f"<tbody>{rows_suspicious(suspicious[:20])}</tbody></table>"
        if suspicious
        else "<p class='empty'>没有需要提示的未验证/不完整高 edge 组。</p>"
    )

    if parity_skipped:
        parity_body = f"<p class='empty'>本次跳过同市场平价：{esc(parity_skipped)}</p>"
    elif parity_candidates:
        parity_body = (
            "<table><thead><tr><th>市场</th><th>YES 卖一</th><th>NO 卖一</th><th>edge</th><th>YES 深度</th><th>NO 深度</th></tr></thead>"
            f"<tbody>{rows_parity(parity_candidates)}</tbody></table>"
        )
    else:
        parity_body = "<p class='empty'>没有扣费后仍低于 1 的 YES+NO 卖一组合。</p>"

    examples = list(basket.get("examples") or [])
    examples_body = (
        "<table><thead><tr><th>事件</th><th>腿数</th><th>成本</th><th>edge</th></tr></thead><tbody>"
        + "".join(
            "<tr>"
            f"<td class='wide'>{esc(row.get('title'))}</td>"
            f"<td>{num(row.get('legs'))}</td>"
            f"<td>{price(row.get('cost'))}</td>"
            f"<td class='warn'>{pct(row.get('edge'))}</td>"
            "</tr>"
            for row in examples[:8]
        )
        + "</tbody></table>"
        if examples
        else "<p class='empty'>没有触发“edge 过高、疑似脏数据”的例子。</p>"
    )

    meta = (
        "<div class='meta-grid'>"
        f"<div><span>生成时间</span><strong>{esc(short_time(report.get('generated_at')))}</strong></div>"
        f"<div><span>数据快照</span><strong>{esc(short_time(report.get('snapshot_at')))}</strong></div>"
        f"<div><span>市场来源</span><strong>{esc(report.get('snapshot'))}</strong></div>"
        f"<div><span>阈值</span><strong>edge ≥ {pct(settings.get('min_edge'))} · ≤ {pct(settings.get('max_edge'))} · 剩余 ≥ {esc(settings.get('min_hours'))}h</strong></div>"
        f"<div><span>官方腿数</span><strong>{'已拉取' if settings.get('event_details') else '未拉取（离线）'}</strong></div>"
        f"<div><span>数据文件</span><strong>{esc(source_name or '—')}</strong></div>"
        "</div>"
    )

    method = (
        "<div class='method'>"
        "<p><strong>事件篮子</strong>：同一个互斥事件的所有 YES 腿都买入。若 <code>Σ卖一 + 费用 &lt; 1</code>，"
        "结算时恰好有一腿付 $1，差额才是毛利。前提是腿必须完整。</p>"
        "<p><strong>同市场平价</strong>：同一市场同时买 YES 和 NO。若 <code>YES卖一 + NO卖一 + 费用 &lt; 1</code>，"
        "到期必有一份付 $1。需要两个盘口都有足够卖一深度。</p>"
        "<p><strong>四道闸门</strong>：① 仅 negRisk 互斥事件；② 官方腿数与快照腿数必须完全一致；"
        "③ 扣费后 edge 至少达到阈值；④ 同市场平价还要求两条腿的卖一深度足够。"
        "任一不满足就不算候选。</p>"
        "<p><strong>0 候选</strong>是正常读数。真正要警惕的是“看起来 80%~100% 的 edge”："
        "本次离线样例里，2 腿 vs 官方 3/24/68 腿的组全部是缺腿假机会。</p>"
        "</div>"
    )

    caveats = (
        "<ul class='caveats'>"
        "<li>本页只读：不读私钥、不下单、不修改纸盘账本或引擎参数。</li>"
        "<li>即使出现候选，也可能无法按报价同时成交；腿风险、滑点、手续费和原子执行都需要单独验证。</li>"
        "<li>快照有延迟；同市场平价的深度只检查卖一档，不代表大额可成交。</li>"
        "<li>扫描器不会自动交易，也不会把候选写进任何账本。</li>"
        "</ul>"
    )

    content = (
        "<header class='hero'>"
        "<div class='hero-top'>"
        "<div>"
        "<div class='eyebrow'>POLYMARKET · RESEARCH · READ ONLY</div>"
        "<h1>平价 / 事件篮子扫描</h1>"
        "<p class='sub'>Parity &amp; complete-event basket scan · 只读研究页，不接交易</p>"
        "</div>"
        "<div class='hero-actions'>"
        "<a class='button' href='./'>← 返回纸盘 v2</a>"
        "<a class='button ghost' href='parity.json'>原始 JSON</a>"
        "</div>"
        "</div>"
        "<div class='badges'><span>只读</span><span>不读私钥</span><span>不下单</span><span>不改引擎</span></div>"
        f"{meta}"
        "</header>"
        f"<div class='kpis'>{kpis}</div>"
        f"{verdict}"
        + section("篮子候选（已验证腿数）", basket_body, "只有官方腿数与快照完全一致，扣费后 edge 仍达阈值的完整篮子才出现在这里。")
        + section("可疑原始组（不可当机会）", suspicious_body, "这些组看起来有巨大 edge，但腿数不完整或无法验证；它们正是“4 腿 vs 28 腿”陷阱。")
        + section("同市场平价", parity_body, "YES 卖一 + NO 卖一 + 费用 < 1，且两边卖一深度足够，才算候选。")
        + section("篮子拒绝原因", reject_table(basket.get("rejects") or {}), "先看拒绝原因，再看候选；`group_incomplete` 就是缺腿。")
        + section("边缘样例（edge 过高，疑似脏数据）", examples_body, "edge 超过上限通常是快照过期、腿数错误或报价异常，不应视为机会。")
        + section("怎么读这个页面", method)
        + section("边界与风险", caveats)
        + "<footer class='footer'>"
        "<span>Polymarket Quant · Paper v2 · Research only</span>"
        f"<span>页面生成于 {esc(short_time(report.get('generated_at')))}</span>"
        "</footer>"
    )
    return page_shell("平价扫描 · Polymarket Paper v2", content)


def page_shell(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{
    --bg: #090d14;
    --panel: #111827;
    --panel-2: #0f1723;
    --border: #223044;
    --text: #e5edf7;
    --muted: #93a4bb;
    --faint: #64748b;
    --green: {GREEN};
    --red: {RED};
    --orange: {ORANGE};
    --blue: {BLUE};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    color: var(--text);
    background:
      radial-gradient(900px 420px at 12% -10%, rgba(125,211,252,.10), transparent 60%),
      radial-gradient(800px 380px at 88% 0%, rgba(52,211,153,.08), transparent 55%),
      var(--bg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    line-height: 1.55;
  }}
  a {{ color: var(--blue); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .9em; }}
  .shell {{ max-width: 1280px; margin: 0 auto; padding: 26px 22px 48px; }}
  .hero {{ border: 1px solid var(--border); background: linear-gradient(180deg, rgba(17,24,39,.96), rgba(15,23,35,.92)); border-radius: 18px; padding: 24px; box-shadow: 0 18px 50px rgba(0,0,0,.25); }}
  .hero-top {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; flex-wrap: wrap; }}
  .eyebrow {{ font-size: 10px; letter-spacing: .16em; color: var(--faint); font-weight: 700; }}
  h1 {{ margin: 6px 0 4px; font-size: clamp(24px, 3vw, 36px); letter-spacing: -.02em; }}
  .sub {{ margin: 0; color: var(--muted); font-size: 13px; }}
  .hero-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .button {{ display: inline-flex; align-items: center; gap: 6px; border: 1px solid #2b3d56; background: #162338; color: var(--text); padding: 9px 12px; border-radius: 10px; font-size: 12px; font-weight: 600; }}
  .button:hover {{ text-decoration: none; border-color: #3b5575; background: #1b2b44; }}
  .button.ghost {{ background: transparent; color: var(--muted); }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 16px; }}
  .badges span {{ font-size: 10px; color: #bfe8d8; background: rgba(52,211,153,.08); border: 1px solid rgba(52,211,153,.22); border-radius: 999px; padding: 3px 8px; }}
  .meta-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }}
  .meta-grid div {{ min-width: 0; }}
  .meta-grid span {{ display: block; font-size: 9px; text-transform: uppercase; letter-spacing: .08em; color: var(--faint); }}
  .meta-grid strong {{ display: block; margin-top: 2px; font-size: 11px; font-weight: 500; color: var(--muted); overflow-wrap: anywhere; }}
  .kpis {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }}
  .kpi {{ border: 1px solid var(--border); background: var(--panel); border-radius: 14px; padding: 13px 14px; min-width: 0; }}
  .kpi-label {{ font-size: 10px; color: var(--muted); }}
  .kpi-value {{ font-size: clamp(20px, 2.2vw, 28px); font-weight: 750; letter-spacing: -.02em; margin-top: 2px; }}
  .kpi-note {{ font-size: 10px; color: var(--faint); margin-top: 2px; }}
  .verdict {{ border: 1px solid var(--border); border-left: 4px solid var(--muted); background: var(--panel); border-radius: 12px; padding: 14px 16px; margin: 12px 0 16px; }}
  .verdict.ok {{ border-left-color: var(--green); background: linear-gradient(90deg, rgba(52,211,153,.07), var(--panel) 35%); }}
  .verdict.warn {{ border-left-color: var(--orange); background: linear-gradient(90deg, rgba(245,158,11,.08), var(--panel) 35%); }}
  .verdict.neutral {{ border-left-color: var(--blue); background: linear-gradient(90deg, rgba(125,211,252,.06), var(--panel) 35%); }}
  .verdict-title {{ font-weight: 700; font-size: 14px; }}
  .verdict-body {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
  .card {{ border: 1px solid var(--border); background: var(--panel); border-radius: 14px; padding: 17px 18px; margin-top: 12px; overflow: hidden; }}
  .card h2 {{ margin: 0 0 4px; font-size: 15px; letter-spacing: -.01em; }}
  .section-note {{ margin: 0 0 12px; color: var(--faint); font-size: 11px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; font-size: 9px; letter-spacing: .06em; text-transform: uppercase; color: var(--faint); padding: 7px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td {{ padding: 8px; border-bottom: 1px solid rgba(34,48,68,.65); vertical-align: top; }}
  tbody tr:hover {{ background: rgba(125,211,252,.035); }}
  td.wide {{ min-width: 220px; max-width: 420px; overflow-wrap: anywhere; }}
  td.right, th.right {{ text-align: right; }}
  .muted {{ color: var(--faint); font-size: 11px; }}
  .pos {{ color: var(--green); font-weight: 700; }}
  .neg {{ color: var(--red); font-weight: 700; }}
  .warn {{ color: var(--orange); font-weight: 700; }}
  .empty {{ color: var(--faint); font-size: 12px; padding: 8px 0; }}
  .method p {{ margin: 0 0 8px; color: var(--muted); font-size: 12px; }}
  .method strong {{ color: var(--text); }}
  .caveats {{ margin: 0; padding-left: 18px; color: var(--muted); font-size: 12px; }}
  .caveats li {{ margin: 4px 0; }}
  .footer {{ display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; color: var(--faint); font-size: 10px; padding: 18px 4px 0; }}
  .empty-state {{ max-width: 640px; margin: 12vh auto; border: 1px solid var(--border); background: var(--panel); border-radius: 18px; padding: 28px; text-align: center; }}
  .empty-state h1 {{ font-size: 24px; }}
  .empty-state p {{ color: var(--muted); font-size: 13px; }}
  @media (max-width: 980px) {{
    .kpis {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .meta-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  }}
  @media (max-width: 640px) {{
    .shell {{ padding: 14px 12px 28px; }}
    .hero {{ padding: 18px; }}
    .kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .meta-grid {{ grid-template-columns: 1fr; }}
    .card {{ overflow-x: auto; }}
    td.wide {{ min-width: 180px; }}
  }}
</style>
</head>
<body>
<main class="shell">
{body}
</main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a parity_scan report as static HTML")
    parser.add_argument("--input", default="", help="specific parity_*.json")
    parser.add_argument("--input-dir", default="state/parity", help="directory containing parity_*.json")
    parser.add_argument("--output", default="state/parity/parity.html")
    args = parser.parse_args()

    input_path = Path(args.input) if args.input else None
    report, source_name = load_report(input_path, Path(args.input_dir))
    html_text = render(report, source_name)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")
    print(f"[parity_page] wrote {output} ({len(html_text)} bytes, source={source_name or 'none'})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
