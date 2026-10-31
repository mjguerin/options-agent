# ruff: noqa: E501  (the embedded page is HTML, CSS and JavaScript, not Python)
"""The one-page HTML: a single self-contained file, data embedded, filtered in the browser.

Same look as the old trading report (dark, tables, a range selector), built around the five
accounts: a card for each with its positions, trades and signals, executed or not."""

from __future__ import annotations

import json

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Options Tournament: one page</title>
<style>
  * { box-sizing: border-box; }
  :root { --bg:#0a0e14; --panel:#131824; --line:#1e2635; --line2:#161b26; --text:#e2e8f0;
          --mute:#64748b; --soft:#94a3b8; --pos:#4ade80; --neg:#f87171; --blue:#60a5fa;
          --amber:#fbbf24; --violet:#c084fc; }
  body { background:var(--bg); color:var(--text); margin:0; padding:20px 24px 40px;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:13px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .subtitle { color:var(--mute); font-size:12px; margin-bottom:12px; }
  .top { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin-bottom:12px; }
  .chips { display:flex; gap:6px; flex-wrap:wrap; }
  .chip { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
          border:1px solid var(--line); color:var(--soft); white-space:nowrap; }
  .chip.ok { color:var(--pos); border-color:#14532d; } .chip.bad { color:var(--neg); border-color:#7f1d1d; }
  .chip.warn { color:var(--amber); border-color:#78350f; } .chip.info { color:var(--blue); border-color:#1e3a8a; }
  .selector { display:flex; gap:8px; align-items:center; margin:0 0 14px; flex-wrap:wrap; }
  .btn { background:var(--panel); border:1px solid var(--line); color:var(--soft); padding:6px 12px;
         border-radius:6px; font-size:12px; cursor:pointer; }
  .btn:hover { border-color:#3b82f6; } .btn.active { background:#1d4ed8; border-color:#1d4ed8; color:#fff; }
  .custom { display:none; gap:6px; align-items:center; } .custom.show { display:flex; }
  .custom input { background:var(--panel); border:1px solid var(--line); color:var(--text);
                  padding:5px 8px; border-radius:6px; font-size:12px; }
  .stats { display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin-bottom:18px; }
  .stat { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
  .stat-label { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:.05em; }
  .stat-value { font-size:20px; font-weight:600; margin-top:3px; }
  .stat-sub { color:var(--mute); font-size:11px; margin-top:2px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(560px,1fr)); gap:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px;
          min-width:0; }
  .card-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }
  .code { font-size:16px; font-weight:700; letter-spacing:.04em; }
  .name { color:var(--mute); font-size:12px; margin-left:6px; }
  .kpis { display:grid; grid-template-columns:repeat(6,1fr); gap:8px; margin:10px 0 8px; }
  .kpi-label { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:.04em; }
  .kpi-value { font-size:15px; font-weight:600; margin-top:2px; }
  .funnel { color:var(--soft); font-size:11px; margin-bottom:6px; }
  details { margin-top:8px; } summary { cursor:pointer; color:var(--soft); font-size:11px;
    text-transform:uppercase; letter-spacing:.05em; padding:4px 0; border-bottom:1px solid var(--line); }
  summary .n { color:var(--mute); }
  .scroll { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:4px; }
  th { text-align:left; color:var(--mute); font-weight:500; font-size:10px; text-transform:uppercase;
       letter-spacing:.03em; padding:5px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:5px 8px; border-bottom:1px solid var(--line2); white-space:nowrap; }
  td.wrap { white-space:normal; max-width:240px; }
  tr:hover td { background:#171d2b; }
  .pos { color:var(--pos); } .neg { color:var(--neg); } .mute { color:var(--mute); }
  .up { color:var(--pos); } .down { color:var(--violet); }
  .empty { color:#475569; padding:8px; font-size:12px; }
  .tag { padding:1px 6px; border-radius:4px; font-size:10px; font-weight:600; letter-spacing:.03em; }
  .tag.exec { background:#052e16; color:var(--pos); } .tag.rej { background:#450a0a; color:var(--neg); }
  .tag.unf { background:#451a03; color:var(--amber); }
  select.pick { background:var(--panel); border:1px solid #1d4ed8; color:var(--text); padding:6px 10px;
                border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; }
  .sum td { padding:9px 10px; } .sum tbody tr { cursor:pointer; }
  .sum .code { font-size:14px; } .sum tfoot td { border-top:1px solid var(--line); font-weight:600;
    border-bottom:none; color:var(--soft); }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:6px 10px 10px; }
  .one { max-width:1200px; }
  .board { display:flex; flex-wrap:wrap; gap:14px; margin:8px 0 10px; }
  .board-box { flex:1 1 360px; min-width:0; background:#0f141f; border:1px solid var(--line2); border-radius:8px; padding:8px 10px; }
  .foot { color:#475569; font-size:11px; margin-top:18px; }
  svg.spark { display:block; }
  @media (max-width:900px) { .stats { grid-template-columns:repeat(3,1fr); }
    .kpis { grid-template-columns:repeat(3,1fr); } .grid { grid-template-columns:1fr; } }
</style></head>
<body>
<h1>Options Tournament <span class="mute" style="font-weight:400">· one page</span></h1>
<div class="top"><div class="subtitle" id="sub"></div><div class="chips" id="chips"></div></div>
<div class="selector" id="sel"></div>
<div class="stats" id="stats"></div>
<div class="grid" id="grid"></div>
<div class="foot" id="foot"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
let DATA = JSON.parse(document.getElementById('data').textContent);
const POLL = __POLL__;
const state = { range: 'latest', start: '', end: '', view: 'all' };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tz = () => DATA.timezone;
const money = (v, plus) => v == null ? '–' : (v < 0 ? '-' : (plus && v > 0 ? '+' : '')) + '$' + Math.abs(v).toLocaleString('en-US', {maximumFractionDigits: 0});
const cls = (v) => v > 0 ? 'pos' : (v < 0 ? 'neg' : 'mute');
const px = (v) => v == null ? '–' : Number(v).toFixed(2);
const timeET = (iso) => iso ? new Date(iso).toLocaleTimeString('en-GB', {timeZone: tz(), hour12: false}) : '–';
const dayET = (iso) => new Date(iso).toLocaleDateString('en-CA', {timeZone: tz()});
const dur = (s) => s == null ? '–' : Math.floor(s / 60) + ':' + String(Math.round(s % 60)).padStart(2, '0');
const rfmt = (v) => v == null ? '–' : (v > 0 ? '+' : '') + v.toFixed(2) + 'R';
const arrow = (d) => d === 'bullish' ? '<span class="up">▲ bull</span>' : '<span class="down">▼ bear</span>';
const nice = (s) => String(s || '').replace(/^REF_/, '').replaceAll('_', ' ').toLowerCase();

function days() {
  const all = DATA.days;
  if (state.range === 'latest') return all.slice(-1);
  if (state.range === '5') return all.slice(-5);
  if (state.range === 'custom') return all.filter((d) => (!state.start || d >= state.start) && (!state.end || d <= state.end));
  return all;
}
const inDays = (set) => (x) => set.has(x.day);

function spark(daily) {
  if (!daily.length) return '';
  let cum = 0; const pts = daily.map((d) => (cum += d.net_usd));
  const w = 120, h = 34, lo = Math.min(0, ...pts), hi = Math.max(0, ...pts), span = hi - lo || 1;
  const x = (i) => pts.length === 1 ? w / 2 : (i / (pts.length - 1)) * (w - 4) + 2;
  const y = (v) => h - 2 - ((v - lo) / span) * (h - 4);
  const line = pts.map((v, i) => x(i).toFixed(1) + ',' + y(v).toFixed(1)).join(' ');
  const zero = y(0).toFixed(1);
  const col = pts[pts.length - 1] >= 0 ? '#4ade80' : '#f87171';
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><title>cumulative net by session</title>
    <line x1="0" x2="${w}" y1="${zero}" y2="${zero}" stroke="#1e2635" stroke-dasharray="2 3"/>
    <polyline fill="none" stroke="${col}" stroke-width="1.6" points="${line}"/></svg>`;
}

function statusChip(a) {
  const e = DATA.engine, l = a.live;
  if (!e.reporting || !l) return '<span class="chip">no live data</span>';
  if (l.halted) return `<span class="chip bad">halted · ${esc(nice(l.halted))}</span>`;
  if (l.operator_halt) return `<span class="chip warn">paused · ${esc(nice(l.operator_halt))}</span>`;
  if (l.entries_blocked) return `<span class="chip warn">entries blocked</span>`;
  if ((l.positions || []).length) return '<span class="chip info">in position</span>';
  if (l.pending_orders) return '<span class="chip info">order working</span>';
  return '<span class="chip ok">ready</span>';
}

function openTable(a) {
  const l = a.live, e = DATA.engine;
  if (!e.reporting || !l) return `<div class="empty">Live positions are shown while the engine is running.</div>`;
  if (!(l.positions || []).length) return '<div class="empty">Flat.</div>';
  const rows = l.positions.map((p) => {
    const pct = p.bid != null && p.avg_entry ? (p.bid / p.avg_entry - 1) * 100 : null;
    return `<tr><td>${esc(p.symbol)}</td><td>${p.qty}</td><td>${px(p.avg_entry)}</td><td>${px(p.bid)}</td>
      <td class="${cls(p.unrealized_usd)}">${money(p.unrealized_usd, true)}</td><td class="${cls(pct)}">${pct == null ? 'no fresh bid' : pct.toFixed(1) + '%'}</td></tr>`;
  }).join('');
  return `<div class="scroll"><table><thead><tr><th>Contract</th><th>Qty</th><th>Entry</th><th>Bid</th><th>Unrealized</th><th>%</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function tradeTable(trades, multi) {
  if (!trades.length) return '<div class="empty">No trades in this range.</div>';
  const max = DATA.limits.trades, shown = trades.slice().sort((x, y) => (y.entry_at || '').localeCompare(x.entry_at || '')).slice(0, max);
  const rows = shown.map((t) => `<tr>
    <td>${esc(t.day)}</td><td>${timeET(t.entry_at)}</td><td>${timeET(t.exit_at)}</td>
    <td>${esc(t.symbol)}</td><td>${t.qty}</td><td>${px(t.entry)}</td><td>${px(t.exit)}</td>
    <td class="${cls(t.net_usd)}" title="${tipOf(t.rules, (v) => money(v, true))}"><b>${money(t.net_usd, true)}</b></td>
    <td class="wrap">${esc((t.exit_reasons || []).map(nice).join(', ') || (t.open_qty ? 'still open' : '–'))}</td>
    <td>${dur(t.held_s)}</td><td>${px(t.peak_bid)}</td><td>${px(t.low_bid)}</td><td>${px(t.after_exit_peak)}</td>
    <td class="${cls(t.ref && t.ref.result_r)}">${t.ref ? rfmt(t.ref.result_r) : '–'}</td></tr>`).join('');
  const more = trades.length > max ? `<div class="empty">${trades.length - max} older trades not shown.</div>` : '';
  return `<div class="scroll"><table><thead><tr><th>Date</th><th>Entry (ET)</th><th>Exit (ET)</th><th>Contract</th><th>Qty</th><th>Entry price</th><th>Exit price</th><th>P&amp;L</th><th>Why it exited</th><th>Held</th><th title="best bid while held">Peak bid</th><th title="worst bid while held">Low bid</th><th title="best bid after the exit">Best after</th><th title="the strategy's own result on SPY, in units of its risk">Signal R</th></tr></thead><tbody>${rows}</tbody></table></div>${more}`;
}

function signalTable(signals, multi) {
  if (!signals.length) return '<div class="empty">No signals in this range.</div>';
  const max = DATA.limits.signals, shown = signals.slice().sort((x, y) => (y.at || '').localeCompare(x.at || '')).slice(0, max);
  const rows = shown.map((s) => {
    const tag = s.status === 'EXECUTED' ? '<span class="tag exec">EXECUTED</span>'
      : s.status === 'ACCEPTED_UNFILLED' ? '<span class="tag unf">NOT FILLED</span>' : '<span class="tag rej">NOT TRADED</span>';
    const why = s.reasons && s.reasons.length ? `<span class="mute"> ${esc(s.reasons.map(nice).join(', '))}</span>` : '';
    const q = s.qty != null ? s.qty + (s.requested_qty && s.requested_qty !== s.qty ? ` <span class="mute">of ${s.requested_qty}${s.capped_by ? ' (' + nice(s.capped_by) + ')' : ''}</span>` : '') : '–';
    const r = s.ref;
    return `<tr><td>${esc(s.day)}</td><td>${timeET(s.at)}</td><td>${arrow(s.direction)}</td>
      <td class="wrap">${tag}${why}</td><td>${esc(s.contract || '–')}</td><td>${q}</td><td>${px(s.entry_reference)}</td>
      <td title="${tipOf(r && r.rules_r, rfmt)}">${r ? esc(nice(r.exit_reason)) : '–'}</td><td class="${cls(r && r.result_r)}">${r ? rfmt(r.result_r) : '–'}</td>
      <td class="mute">${r ? rfmt(r.best_excursion_r) : '–'}</td></tr>`;
  }).join('');
  const more = signals.length > max ? `<div class="empty">${signals.length - max} older signals not shown.</div>` : '';
  return `<div class="scroll"><table><thead><tr><th>Date</th><th>Signal (ET)</th><th>Side</th><th>Result</th><th>Contract</th><th>Qty</th><th>SPY at signal</th><th title="how the strategy's own position on SPY ended, traded or not">Strategy exit</th><th title="its result in units of its own risk">Result R</th><th title="furthest it ran in favour, in R">Best R</th></tr></thead><tbody>${rows}</tbody></table></div>${more}`;
}

const tipOf = (obj, f) => obj ? esc(Object.entries(obj).map(([k, v]) => k + ': ' + f(v)).join('\n')) : '';

function ruleBoard(trades, signals) {
  const names = DATA.rule_names;
  const withRules = trades.filter((t) => t.rules);
  const base = names.option[0];
  const opt = names.option.map((n) => {
    const tot = withRules.reduce((s, t) => s + (t.rules[n] ?? 0), 0);
    const baseTot = withRules.reduce((s, t) => s + (t.rules[base] ?? 0), 0);
    const changed = withRules.filter((t) => Math.abs((t.rules[n] ?? 0) - t.rules[base]) > 0.005).length;
    const losers = withRules.filter((t) => t.rules[base] < 0).reduce((s, t) => s + (t.rules[n] ?? 0), 0);
    return `<tr><td>${esc(n)}</td><td class="${cls(tot)}"><b>${money(tot, true)}</b></td><td class="${cls(tot - baseTot)}">${n === base ? '–' : money(tot - baseTot, true)}</td><td>${n === base ? '–' : changed}</td><td class="${cls(losers)}">${money(losers, true)}</td></tr>`;
  }).join('');
  const rs = signals.filter((s) => s.ref && s.ref.rules_r);
  const rbase = names.r[0];
  const baseMean = rs.length ? rs.reduce((s, x) => s + x.ref.rules_r[rbase], 0) / rs.length : 0;
  const rr = names.r.map((n) => {
    const m = rs.length ? rs.reduce((s, x) => s + x.ref.rules_r[n], 0) / rs.length : null;
    const w = rs.length ? rs.filter((x) => x.ref.rules_r[n] > 0).length / rs.length : null;
    return `<tr><td>${esc(n)}</td><td class="${cls(m)}"><b>${m == null ? '–' : rfmt(m)}</b></td><td class="${cls(m == null ? 0 : m - baseMean)}">${n === rbase || m == null ? '–' : rfmt(m - baseMean)}</td><td>${w == null ? '–' : Math.round(w * 100) + '%'}</td></tr>`;
  }).join('');
  if (!withRules.length && !rs.length) return '';
  return `<div class="board">
    <div class="board-box"><div class="kpi-label">Exit rules on the option (traded signals: ${withRules.length})</div>
      <table><thead><tr><th>Rule</th><th>P&amp;L</th><th>vs executed</th><th>Trades changed</th><th>Losers only</th></tr></thead><tbody>${opt || ''}</tbody></table></div>
    <div class="board-box"><div class="kpi-label">Exit rules on SPY, in R (every signal, traded or not: ${rs.length})</div>
      <table><thead><tr><th>Rule</th><th>Mean R</th><th>vs specified</th><th>Win share</th></tr></thead><tbody>${rr}</tbody></table></div>
    <div class="subtitle" style="flex-basis:100%;margin:2px 0 0">What-ifs on the recorded paths, ${esc(DATA.rule_note)}. They change no trade. A rule that fires after the real exit never acts. The win share rises when small gains are locked even when the money does not; read the money. A few trades are an anecdote, not a finding.</div></div>`;
}

function card(a, set) {
  const trades = a.trades.filter(inDays(set)), signals = a.signals.filter(inDays(set));
  const multi = set.size > 1;
  const pnl = a.daily.filter(inDays(set)).reduce((s, d) => s + d.net_usd, 0);
  const cum = a.daily.reduce((s, d) => s + d.net_usd, 0);
  const closed = trades.filter((t) => t.exit_at), wins = closed.filter((t) => t.net_usd > 0).length;
  const ex = signals.filter((s) => s.status === 'EXECUTED').length;
  const fun = Object.entries(a.funnel).filter(([d]) => set.has(d)).map(([, f]) => f);
  const started = fun.reduce((s, f) => s + f.started, 0), ended = {};
  fun.forEach((f) => Object.entries(f.ended).forEach(([k, v]) => ended[k] = (ended[k] || 0) + v));
  const endedTxt = Object.entries(ended).sort((x, y) => y[1] - x[1]).map(([k, v]) => `${nice(k)} ×${v}`).join(', ');
  const nOpen = a.live && DATA.engine.reporting ? (a.live.positions || []).length : 0;
  return `<div class="card">
    <div class="card-head"><div><span class="code">${esc(a.key.toUpperCase())}</span><span class="name">${esc(a.name)}</span></div>
      <div style="text-align:right">${statusChip(a)}<div style="margin-top:6px">${spark(a.daily)}</div></div></div>
    <div class="kpis">
      <div><div class="kpi-label">P&amp;L (range)</div><div class="kpi-value ${cls(pnl)}">${money(pnl, true)}</div></div>
      <div><div class="kpi-label">All sessions</div><div class="kpi-value ${cls(cum)}">${money(cum, true)}</div></div>
      <div><div class="kpi-label">Trades</div><div class="kpi-value">${trades.length}</div></div>
      <div><div class="kpi-label">Win rate</div><div class="kpi-value">${closed.length ? Math.round(wins / closed.length * 100) + '%' : '–'}<span class="mute" style="font-size:11px"> ${wins}/${closed.length}</span></div></div>
      <div><div class="kpi-label">Signals</div><div class="kpi-value">${ex}<span class="mute" style="font-size:11px"> traded · ${signals.length - ex} not</span></div></div>
      <div><div class="kpi-label">Open now</div><div class="kpi-value">${nOpen}</div></div>
    </div>
    <div class="funnel">Setups started ${started}${endedTxt ? ' · dropped: ' + esc(endedTxt) : ''} · signals ${signals.length}</div>
    <details open><summary>Positions <span class="n">${nOpen}</span></summary>${openTable(a)}</details>
    <details open><summary>Trades <span class="n">${trades.length}</span></summary>${tradeTable(trades, multi)}</details>
    <details open><summary>Signals <span class="n">${signals.length}</span> <span class="n">· traded or not</span></summary>${ruleBoard(trades, signals)}${signalTable(signals, multi)}</details>
  </div>`;
}

function scope() { return state.view === 'all' ? DATA.accounts : DATA.accounts.filter((a) => a.key === state.view); }

function summaryTable(set) {
  const e = DATA.engine;
  let T = { pnl: 0, cum: 0, trades: 0, wins: 0, closed: 0, sig: 0, ex: 0, open: 0 };
  const rows = DATA.accounts.map((a) => {
    const t = a.trades.filter(inDays(set)), s = a.signals.filter(inDays(set));
    const pnl = a.daily.filter(inDays(set)).reduce((x, d) => x + d.net_usd, 0), cum = a.daily.reduce((x, d) => x + d.net_usd, 0);
    const closed = t.filter((x) => x.exit_at), wins = closed.filter((x) => x.net_usd > 0).length;
    const ex = s.filter((x) => x.status === 'EXECUTED').length;
    const open = a.live && e.reporting ? (a.live.positions || []).length : 0;
    T.pnl += pnl; T.cum += cum; T.trades += t.length; T.wins += wins; T.closed += closed.length; T.sig += s.length; T.ex += ex; T.open += open;
    return `<tr onclick="setView('${a.key}')" title="open ${esc(a.key.toUpperCase())}">
      <td><span class="code">${esc(a.key.toUpperCase())}</span><span class="name">${esc(a.name)}</span></td><td>${statusChip(a)}</td>
      <td class="${cls(pnl)}"><b>${money(pnl, true)}</b></td><td class="${cls(cum)}">${money(cum, true)}</td><td>${t.length}</td>
      <td>${closed.length ? Math.round(wins / closed.length * 100) + '%' : '–'} <span class="mute">${wins}/${closed.length}</span></td>
      <td>${ex} <span class="mute">traded</span> · ${s.length - ex} <span class="mute">not</span></td><td>${open}</td><td>${spark(a.daily)}</td></tr>`;
  }).join('');
  const foot = `<tr><td>All accounts</td><td></td><td class="${cls(T.pnl)}">${money(T.pnl, true)}</td><td class="${cls(T.cum)}">${money(T.cum, true)}</td><td>${T.trades}</td>
    <td>${T.closed ? Math.round(T.wins / T.closed * 100) + '%' : '–'} <span class="mute">${T.wins}/${T.closed}</span></td><td>${T.ex} traded · ${T.sig - T.ex} not</td><td>${T.open}</td><td></td></tr>`;
  return `<div class="panel scroll"><table class="sum"><thead><tr><th>Account</th><th>Status</th><th>P&amp;L (range)</th><th>All sessions</th><th>Trades</th><th>Win rate</th><th>Signals</th><th>Open now</th><th>Trend</th></tr></thead>
    <tbody>${rows}</tbody><tfoot>${foot}</tfoot></table></div>
    <div class="subtitle" style="margin-top:8px">Click an account, or use the menu above, to see its positions, trades and signals.</div>`;
}

function render() {
  const set = new Set(days());
  const e = DATA.engine, inScope = scope();
  $('sub').textContent = `${(e.mode || 'shadow')} mode · ${DATA.label} · updated ${timeET(DATA.generated_at)} ET`;
  const feeds = e.feeds || {};
  $('chips').innerHTML = [
    e.reporting ? `<span class="chip ok">engine running · slot ${e.slot ?? '–'}</span>` : '<span class="chip">engine not running</span>',
    e.reporting && feeds.stock ? `<span class="chip ${feeds.stock.connected ? 'ok' : 'bad'}">stock feed</span>` : '',
    e.reporting && feeds.option ? `<span class="chip ${feeds.option.connected ? 'ok' : 'bad'}">option feed</span>` : '',
  ].join('');
  const opts = ['<option value="all">All accounts: summary</option>'].concat(DATA.accounts.map((a) =>
    `<option value="${a.key}" ${state.view === a.key ? 'selected' : ''}>${esc(a.key.toUpperCase())}: ${esc(a.name)}</option>`));
  opts[0] = opts[0].replace('<option', state.view === 'all' ? '<option selected' : '<option');
  const rangeBtn = (k, t) => `<button class="btn ${state.range === k ? 'active' : ''}" onclick="setRange('${k}')">${t}</button>`;
  $('sel').innerHTML = `<select class="pick" onchange="setView(this.value)" aria-label="account">${opts.join('')}</select>` +
    rangeBtn('latest', 'Latest session') + rangeBtn('5', 'Last 5 sessions') + rangeBtn('all', 'All sessions') + rangeBtn('custom', 'Custom') +
    `<span class="custom ${state.range === 'custom' ? 'show' : ''}"><input type="date" id="d1" value="${state.start}" onchange="setDates()"> to <input type="date" id="d2" value="${state.end}" onchange="setDates()"></span>` +
    `<span class="subtitle" style="margin:0">${set.size ? [...set][0] + (set.size > 1 ? ' to ' + [...set].pop() : '') : 'no sessions'}</span>`;
  let pnl = 0, trades = 0, wins = 0, closed = 0, sig = 0, ex = 0, open = 0, halted = 0;
  inScope.forEach((a) => {
    pnl += a.daily.filter(inDays(set)).reduce((s, d) => s + d.net_usd, 0);
    const t = a.trades.filter(inDays(set)); trades += t.length;
    t.filter((x) => x.exit_at).forEach((x) => { closed++; if (x.net_usd > 0) wins++; });
    const s = a.signals.filter(inDays(set)); sig += s.length; ex += s.filter((x) => x.status === 'EXECUTED').length;
    if (a.live && e.reporting) { open += (a.live.positions || []).length; if (a.live.halted) halted++; }
  });
  const who = state.view === 'all' ? 'all accounts' : state.view.toUpperCase();
  $('stats').innerHTML = [
    ['Net P&L · ' + who, `<span class="${cls(pnl)}">${money(pnl, true)}</span>`, DATA.label],
    ['Trades', trades, ''], ['Win rate', closed ? Math.round(wins / closed * 100) + '%' : '–', `${wins} of ${closed} closed`],
    ['Signals', `${ex} <span class="mute" style="font-size:12px">of ${sig}</span>`, 'traded / emitted'],
    ['Open positions', e.reporting ? open : '–', e.reporting ? '' : 'engine not running'],
    [state.view === 'all' ? 'Accounts halted' : 'Halted', e.reporting ? halted : '–', 'daily loss limit'],
  ].map(([l, v, s]) => `<div class="stat"><div class="stat-label">${l}</div><div class="stat-value">${v}</div><div class="stat-sub">${s}</div></div>`).join('');
  $('grid').className = state.view === 'all' ? '' : 'one';
  $('grid').innerHTML = state.view === 'all' ? summaryTable(set) : card(inScope[0], set);
  $('foot').textContent = `All times ${tz()}. P&L is the evaluation ledger plus open positions at a fresh bid, ${DATA.label}. "Strategy outcome" is what the strategy's own position did on SPY, whether or not we traded it, in units of its own risk (R). Shadow results are modeled fills and are development data.`;
}
function setRange(r) { state.range = r; render(); }
function setView(v) { state.view = v; history.replaceState(null, '', v === 'all' ? location.pathname : '#' + v); render(); window.scrollTo(0, 0); }
function setDates() { state.start = $('d1').value; state.end = $('d2').value; render(); }
const fromHash = location.hash.replace('#', '');
if (DATA.accounts.some((a) => a.key === fromHash)) state.view = fromHash;
render();
if (POLL) setInterval(async () => {
  try { const r = await fetch('data.json', {cache: 'no-store'}); if (r.ok) { DATA = await r.json(); render(); } } catch (e) {}
}, DATA.poll_s * 1000);
</script></body></html>
"""


def page(data: dict, poll: bool) -> str:
    blob = json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", blob).replace("__POLL__", "true" if poll else "false")
