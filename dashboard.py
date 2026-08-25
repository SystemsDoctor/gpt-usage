"""
dashboard.py - Local web dashboard for Codex / GPT usage, served on localhost:8090.

Single-file http.server app: GET /api/data returns the full snapshot (client filters by
date range + model), POST /api/rescan runs an incremental scan. The UI is one embedded
HTML/JS page using Chart.js from CDN. See docs/DEVELOPMENT_PLAN.md §7.

The JS PRICING const below MUST stay in sync with cli.py PRICING (a parity test guards it).
"""

import json
import os
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime

from scanner import VERSION, init_db, SESSIONS_DIR

DB_PATH = Path(os.environ.get("GPT_USAGE_DB", Path.home() / ".codex" / "gpt-usage.db"))


def get_dashboard_data(db_path=DB_PATH):
    if not Path(db_path).exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    init_db(conn)  # idempotent; dashboard may serve before the background scan migrates

    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model,''),'unknown') as model
        FROM turns GROUP BY COALESCE(NULLIF(model,''),'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    daily_rows = conn.execute("""
        SELECT substr(timestamp,1,10) as day,
               COALESCE(NULLIF(model,''),'unknown') as model,
               SUM(input_tokens) as input, SUM(cached_input_tokens) as cached,
               SUM(output_tokens) as output, SUM(reasoning_output_tokens) as reasoning,
               COUNT(*) as turns
        FROM turns GROUP BY day, COALESCE(NULLIF(model,''),'unknown')
        ORDER BY day, model
    """).fetchall()
    daily_by_model = [{
        "day": r["day"], "model": r["model"],
        "input": r["input"] or 0, "cached": r["cached"] or 0,
        "output": r["output"] or 0, "reasoning": r["reasoning"] or 0,
        "turns": r["turns"] or 0,
    } for r in daily_rows]

    hourly_rows = conn.execute("""
        SELECT substr(timestamp,1,10) as day,
               CAST(substr(timestamp,12,2) AS INTEGER) as hour,
               SUM(output_tokens) as output, COUNT(*) as turns
        FROM turns WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour ORDER BY day, hour
    """).fetchall()
    hourly = [{"day": r["day"], "hour": r["hour"] or 0,
               "output": r["output"] or 0, "turns": r["turns"] or 0} for r in hourly_rows]

    session_rows = conn.execute("""
        SELECT session_id, project_name, thread_source, model, topic,
               first_timestamp, last_timestamp, turn_count,
               total_input_tokens, total_cached_input, total_output_tokens,
               total_reasoning_tokens
        FROM sessions ORDER BY last_timestamp DESC
    """).fetchall()
    sessions_all = [{
        "session_id": r["session_id"],
        "project": r["project_name"] or "unknown",
        "source": r["thread_source"] or "user",
        "model": r["model"] or "unknown",
        "topic": r["topic"] or "",
        "day": (r["last_timestamp"] or "")[:10],
        "last": (r["last_timestamp"] or "")[:16].replace("T", " "),
        "turns": r["turn_count"] or 0,
        "input": r["total_input_tokens"] or 0,
        "cached": r["total_cached_input"] or 0,
        "output": r["total_output_tokens"] or 0,
        "reasoning": r["total_reasoning_tokens"] or 0,
    } for r in session_rows]

    rl_rows = conn.execute("""
        SELECT timestamp, plan_type, primary_used_percent, primary_window_minutes,
               primary_resets_at, secondary_used_percent, secondary_window_minutes,
               secondary_resets_at
        FROM rate_limit_snapshots ORDER BY timestamp
    """).fetchall()
    rate_limits = [{
        "day": (r["timestamp"] or "")[:10],
        "timestamp": r["timestamp"],
        "plan_type": r["plan_type"],
        "primary_pct": r["primary_used_percent"],
        "primary_window": r["primary_window_minutes"],
        "primary_resets_at": r["primary_resets_at"],
        "secondary_pct": r["secondary_used_percent"],
        "secondary_window": r["secondary_window_minutes"],
        "secondary_resets_at": r["secondary_resets_at"],
    } for r in rl_rows]

    conn.close()
    return {
        "all_models": all_models,
        "daily_by_model": daily_by_model,
        "hourly": hourly,
        "sessions_all": sessions_all,
        "rate_limits": rate_limits,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── Embedded UI ─────────────────────────────────────────────────────────────
# PRICING is duplicated from cli.py (Python) here as a JS object. A parity test
# (tests/test_dashboard.py) fails if the two drift. Keep the shapes identical:
# {model: {input, cached, output}} or null for unpriced models.
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Codex / GPT Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>window.APP_CONFIG = __APP_CONFIG_JSON__;</script>
<style>
  :root {
    --bg:#0f1115; --card:#171a21; --border:#262b36; --text:#c7ccd6; --muted:#6b7280;
    --accent:#10a37f; --blue:#4c8bf5; --green:#3fb950; --amber:#d9a84e; --red:#e5534b;
    --raised:#1e222b;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:14px; }
  header { background:var(--card); border-bottom:1px solid var(--border); padding:16px 24px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
  header h1 { font-size:18px; font-weight:600; }
  header h1 .dot { color:var(--accent); }
  .meta { color:var(--muted); font-size:12px; text-align:right; line-height:1.5; }
  #rescan-btn { background:var(--card); border:1px solid var(--border); color:var(--muted); padding:5px 12px; border-radius:6px; cursor:pointer; font-size:12px; }
  #rescan-btn:hover { color:var(--text); border-color:var(--accent); }
  #rescan-btn:disabled { opacity:0.5; cursor:not-allowed; }
  #filter-bar { background:var(--card); border-bottom:1px solid var(--border); padding:10px 24px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .filter-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
  select { appearance:none; background:var(--card); border:1px solid var(--border); border-radius:6px; color:var(--text); font-size:12px; padding:5px 10px; cursor:pointer; }
  select:hover, select:focus { border-color:var(--accent); outline:none; }
  .container { max-width:1400px; margin:0 auto; padding:24px; }
  .stats-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:24px; }
  .stat-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; }
  .stat-card .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px; }
  .stat-card .value { font-size:22px; font-weight:700; }
  .stat-card .sub { color:var(--muted); font-size:11px; margin-top:4px; }
  .charts-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:24px; }
  .chart-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:20px; min-width:0; }
  .chart-card.wide { grid-column:1 / -1; }
  .chart-card h2 { font-size:13px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:16px; cursor:pointer; user-select:none; }
  .chart-card h2:hover { color:var(--text); }
  .card-caret { display:inline-block; width:.9em; margin-right:6px; transform:rotate(90deg); transition:transform .15s; }
  .collapsed .card-caret { transform:rotate(0deg); }
  .collapsed .chart-wrap, .collapsed table, .collapsed .table-foot, .collapsed .caption { display:none; }
  .caption { font-size:11px; color:var(--muted); margin-top:10px; line-height:1.5; }
  .caption b { color:var(--text); font-weight:600; }
  .chart-wrap { position:relative; height:240px; }
  .chart-wrap.tall { height:300px; }
  table { width:100%; border-collapse:collapse; }
  th { text-align:left; padding:8px 12px; font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); border-bottom:1px solid var(--border); white-space:nowrap; cursor:pointer; }
  th:hover { color:var(--text); }
  td { padding:9px 12px; border-bottom:1px solid var(--border); font-size:13px; }
  tr:last-child td { border-bottom:none; }
  tr:hover td { background:var(--raised); }
  .num { font-family:monospace; }
  .cost { color:var(--green); font-family:monospace; }
  .cost-na { color:var(--muted); font-family:monospace; font-size:11px; }
  .tag { display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; }
  .tag.model { background:rgba(76,139,245,.15); color:var(--blue); }
  .tag.model.est { background:rgba(217,168,78,.15); color:var(--amber); cursor:help; }
  .src-user { color:var(--green); }
  .src-subagent { color:var(--amber); }
  .src-automation { color:var(--blue); }
  .table-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:20px; margin-bottom:24px; overflow-x:auto; }
  .table-card h2 { font-size:13px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:12px; cursor:pointer; user-select:none; }
  .table-card h2:hover { color:var(--text); }
  .section-header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
  .section-header h2 { margin-bottom:0; }
  .table-foot { display:flex; justify-content:flex-end; margin-top:12px; }
  .show-more { background:transparent; border:1px solid var(--border); color:var(--muted); padding:4px 12px; border-radius:6px; cursor:pointer; font-size:12px; }
  .show-more:hover { color:var(--text); border-color:var(--accent); }
  footer { border-top:1px solid var(--border); padding:20px 24px; margin-top:8px; }
  .footer-content { max-width:1400px; margin:0 auto; color:var(--muted); font-size:12px; line-height:1.7; }
  .footer-content a { color:var(--blue); text-decoration:none; }
  .footer-content a:hover { text-decoration:underline; }
  #banner { display:none; background:var(--red); color:#fff; padding:8px 24px; font-size:13px; }
  @media (max-width:768px){ .charts-grid { grid-template-columns:1fr; } .chart-card.wide { grid-column:1; } }
</style>
</head>
<body>
<header>
  <h1><span class="dot">&#9679;</span> Codex / GPT Usage</h1>
  <div style="display:flex; align-items:center; gap:16px;">
    <div class="meta" id="meta">Loading...</div>
    <button id="rescan-btn" onclick="triggerRescan()" title="Incremental scan for new usage">&#x21bb; Rescan</button>
  </div>
</header>
<div id="banner"></div>
<div id="filter-bar">
  <span class="filter-label">Model</span>
  <select id="model-select" onchange="setModel(this.value)"><option value="all">All models</option></select>
  <span class="filter-label">Range</span>
  <select id="range-select" onchange="setRange(this.value)">
    <option value="today">Today</option>
    <option value="week">This Week</option>
    <option value="month">This Month</option>
    <option value="prev-month">Previous Month</option>
    <option value="7d">Last 7 Days</option>
    <option value="30d" selected>Last 30 Days</option>
    <option value="90d">Last 90 Days</option>
    <option value="all">All Time</option>
  </select>
  <span class="filter-label">Cost in</span>
  <select id="currency-select" onchange="setCurrency(this.value)" title="Codex/ChatGPT plans meter in credits (1 credit = $0.04); API bills in US dollars.">
    <option value="usd">US$ (API)</option>
    <option value="credits">Credits (plan)</option>
  </select>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide" data-card="ratelimit"><h2><span class="card-caret">&#9656;</span>Rate-Limit Usage Over Time</h2><div class="chart-wrap tall"><canvas id="chart-ratelimit"></canvas></div><div class="caption" id="rl-caption"></div></div>
    <div class="chart-card wide" data-card="daily"><h2><span class="card-caret">&#9656;</span>Daily Tokens</h2><div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div></div>
    <div class="chart-card" data-card="cost"><h2><span class="card-caret">&#9656;</span>Daily Cost by Model</h2><div class="chart-wrap"><canvas id="chart-cost"></canvas></div></div>
    <div class="chart-card" data-card="modelmix"><h2><span class="card-caret">&#9656;</span>Model Mix (tokens)</h2><div class="chart-wrap"><canvas id="chart-modelmix"></canvas></div></div>
    <div class="chart-card" data-card="projectchart"><h2><span class="card-caret">&#9656;</span>Top Projects by Tokens</h2><div class="chart-wrap"><canvas id="chart-project"></canvas></div></div>
    <div class="chart-card" data-card="sourcechart"><h2><span class="card-caret">&#9656;</span>Tokens by Thread Source</h2><div class="chart-wrap"><canvas id="chart-source"></canvas></div></div>
    <div class="chart-card wide" data-card="hourly"><h2><span class="card-caret">&#9656;</span>Usage by Hour (local)</h2><div class="chart-wrap"><canvas id="chart-hourly"></canvas></div><div class="caption" id="hourly-caption"></div></div>
  </div>
  <div class="table-card" data-card="modeltable"><div class="section-header"><h2><span class="card-caret">&#9656;</span>By Model</h2><button class="show-more" onclick="exportCSV('model')" title="Export to CSV">&#x2913; CSV</button></div>
    <table><thead><tr><th onclick="sortModel('model')">Model</th><th onclick="sortModel('sessions')">Sessions</th><th onclick="sortModel('turns')">Turns</th><th onclick="sortModel('input')">Input</th><th onclick="sortModel('cached')">Cached</th><th onclick="sortModel('output')">Output</th><th onclick="sortModel('cost')">Est. Cost</th></tr></thead>
    <tbody id="model-body"></tbody></table></div>
  <div class="table-card" data-card="projects"><div class="section-header"><h2><span class="card-caret">&#9656;</span>Projects</h2><button class="show-more" onclick="exportCSV('projects')" title="Export to CSV">&#x2913; CSV</button></div>
    <table><thead><tr><th onclick="sortProjects('project')">Project</th><th onclick="sortProjects('sessions')">Sessions</th><th onclick="sortProjects('turns')">Turns</th><th onclick="sortProjects('input')">Input</th><th onclick="sortProjects('output')">Output</th><th onclick="sortProjects('cost')">Est. Cost</th></tr></thead>
    <tbody id="projects-body"></tbody></table></div>
  <div class="table-card" data-card="sessions"><div class="section-header"><h2><span class="card-caret">&#9656;</span>Sessions</h2><button class="show-more" onclick="exportCSV('sessions')" title="Export to CSV">&#x2913; CSV</button></div>
    <table><thead><tr><th>Title</th><th>Project</th><th>Source</th><th>Model</th><th onclick="sortSessions('last')">Last</th><th onclick="sortSessions('turns')">Turns</th><th onclick="sortSessions('input')">Input</th><th onclick="sortSessions('output')">Output</th><th onclick="sortSessions('cost')">Est. Cost</th></tr></thead>
    <tbody id="sessions-body"></tbody></table>
    <div class="table-foot"><button class="show-more" id="sessions-more" onclick="moreSessions()">Show more</button></div></div>
</div>

<footer><div class="footer-content">
  <p>Cost basis: OpenAI pricing as of 2026-08-25. Use the <b>Cost in</b> toggle to switch between <b>US$</b> (API rates) and <b>Credits</b> (Codex/ChatGPT-plan metering; 1 credit = $0.04, i.e. API US$ &times; 25). Unlisted models show <em>n/a</em>.</p>
  <p><b>*</b> <em>Estimated pricing.</em> <em>codex-auto-review</em> is Codex&rsquo;s automatic code-review pass and has <b>no published SKU</b> (OpenAI counts it toward general Codex usage); it is billed here at the gpt-5.4 / gpt-5.6-terra tier as an estimate. Subscription (Plus/Pro/Business) real costs differ from API pricing &mdash; the rate-limit chart is the better signal for subscribers.</p>
  <p>Reads local Codex rollout logs only &mdash; token counts and metadata, never conversation content. Modeled on <a href="https://github.com/phuryn/claude-usage" target="_blank">phuryn/claude-usage</a>. <span id="footer-meta"></span></p>
</div></footer>

<script>
const CFG = window.APP_CONFIG || {version:'?'};
function esc(s){ const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }

// PRICING — keep in sync with cli.py PRICING (parity-tested). $ per MTok.
const PRICING = {"gpt-5.6-sol":{"input":4.0,"cached":0.4,"output":20.0},"gpt-5.6-terra":{"input":2.0,"cached":0.2,"output":12.0},"gpt-5.6-luna":{"input":0.2,"cached":0.02,"output":1.2},"gpt-5.5-pro":{"input":30.0,"cached":3.0,"output":180.0},"gpt-5.5":{"input":5.0,"cached":0.5,"output":30.0},"gpt-5.4-mini":{"input":0.75,"cached":0.075,"output":4.5},"gpt-5.4-nano":{"input":0.2,"cached":0.02,"output":1.25},"gpt-5.4-pro":{"input":30.0,"cached":3.0,"output":180.0},"gpt-5.4":{"input":2.5,"cached":0.25,"output":15.0},"gpt-5.3-codex":{"input":1.75,"cached":0.175,"output":14.0},"gpt-5":{"input":1.25,"cached":0.125,"output":10.0},"codex-auto-review":{"input":2.5,"cached":0.25,"output":15.0}};
// codex-auto-review price is an ESTIMATE (terra/5.4 tier) — OpenAI publishes no SKU (§ Phase 3.5).
const ESTIMATED = new Set(["codex-auto-review"]);
const PRICING_KEYS = Object.keys(PRICING).sort((a,b)=>b.length-a.length);
function getPricing(m){ if(!m) return null; if(m in PRICING) return PRICING[m]; for(const k of PRICING_KEYS){ if(m.startsWith(k)) return PRICING[k]; } return null; }
function calcCost(m, inp, cached, out){ const p=getPricing(m); if(!p) return 0; const fresh=Math.max(inp-cached,0); return fresh*p.input/1e6 + cached*p.cached/1e6 + out*p.output/1e6; }
function priced(m){ return getPricing(m)!==null; }

function fmt(n){ n=n||0; if(n>=1e9) return (n/1e9).toFixed(2)+'B'; if(n>=1e6) return (n/1e6).toFixed(2)+'M'; if(n>=1e3) return (n/1e3).toFixed(1)+'K'; return Math.round(n).toLocaleString(); }
const MODEL_COLORS=['#10a37f','#4c8bf5','#d9a84e','#e5534b','#9b7ec7','#3fb950','#c2705a','#5bb8a3','#c77e9b'];

// Cost display unit. Codex/ChatGPT plans meter in CREDITS; OpenAI's credit rate card equals the
// API US$ rate x25 across every model (e.g. GPT-5.6 Sol $5.00 = 125 credits), i.e. 1 credit =
// $0.04. So the toggle is a pure display conversion — no second pricing dict to keep in sync.
const CREDITS_PER_USD = 25;
let currency = 'usd';  // 'usd' | 'credits'
function fmtMoney(usd){
  if(currency==='credits') return Math.round(usd*CREDITS_PER_USD).toLocaleString()+' cr';
  return '$'+usd.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}
function costUnitLabel(){ return currency==='credits'?'est. cost (credits)':'est. cost (US$)'; }

// Estimated-pricing marker: codex-auto-review has no published SKU (§ Phase 3.5), so every place
// its cost appears carries a "*" + explanatory tooltip.
const EST_NOTE = 'Estimated pricing: codex-auto-review is Codex’s automatic code-review pass with no published SKU; billed here at the gpt-5.4 / gpt-5.6-terra tier.';
function isEst(m){ return ESTIMATED.has(m); }
function estStar(m){ return isEst(m) ? ' *' : ''; }

let raw=null, model='all', range='30d', charts={}, sessLimit=15;
let projSort='cost', sessSort='last', modelSort='cost';

function ymd(d){ return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
function todayISO(){ return ymd(new Date()); }
// Calendar-aligned ranges (week/month/prev-month) use local calendar components, never
// toISOString()/UTC — a UTC-based bound is wrong for any UTC+ reader near midnight.
function rangeBounds(r){
  const now=new Date();
  if(r==='all') return {start:null, end:null};
  if(r==='today') return {start:todayISO(), end:todayISO()};
  if(r==='week'){ const d=new Date(now); const dow=(d.getDay()+6)%7; d.setDate(d.getDate()-dow); return {start:ymd(d), end:todayISO()}; }
  if(r==='month'){ return {start:ymd(new Date(now.getFullYear(),now.getMonth(),1)), end:todayISO()}; }
  if(r==='prev-month'){ return {start:ymd(new Date(now.getFullYear(),now.getMonth()-1,1)), end:ymd(new Date(now.getFullYear(),now.getMonth(),0))}; }
  const days=r==='7d'?7:r==='30d'?30:90; const d=new Date(now); d.setDate(d.getDate()-days); return {start:ymd(d), end:null};
}
function inRange(day){ const {start,end}=rangeBounds(range); if(start&&day<start) return false; if(end&&day>end) return false; return true; }
function matchModel(m){ return model==='all' || m===model; }

function setModel(v){ model=v; syncURL(); render(); }
function setRange(v){ range=v; syncURL(); render(); }
function setCurrency(v){ currency=v; localStorage.setItem('gptusage.currency', v); const s=document.getElementById('currency-select'); if(s) s.value=v; render(); }
function syncURL(){ const p=new URLSearchParams(); if(model!=='all') p.set('model',model); p.set('range',range); history.replaceState(null,'',location.pathname+'?'+p.toString()); }
function readURL(){ const p=new URLSearchParams(location.search); if(p.get('range')) range=p.get('range'); if(p.get('model')) model=p.get('model'); const c=localStorage.getItem('gptusage.currency'); if(c==='usd'||c==='credits') currency=c; }

async function loadData(){
  try{
    const res=await fetch('/api/data'); raw=await res.json();
    if(raw.error){ showBanner(raw.error); return; }
    initModelFilter(); render();
    document.getElementById('meta').textContent='Updated '+raw.generated_at;
    document.getElementById('footer-meta').textContent='v'+CFG.version;
  }catch(e){ showBanner('Failed to load data: '+e); }
}
function showBanner(msg){ const b=document.getElementById('banner'); b.textContent=msg; b.style.display='block'; }

function initModelFilter(){
  const sel=document.getElementById('model-select');
  sel.innerHTML='<option value="all">All models</option>';
  (raw.all_models||[]).forEach(m=>{ const o=document.createElement('option'); o.value=m; o.textContent=m+(isEst(m)?' * (est.)':''); sel.appendChild(o); });
  sel.value=model;
  document.getElementById('range-select').value=range;
  document.getElementById('currency-select').value=currency;
}

async function triggerRescan(){
  const btn=document.getElementById('rescan-btn'); btn.disabled=true; btn.textContent='Scanning...';
  try{ await fetch('/api/rescan',{method:'POST'}); await loadData(); }
  catch(e){ showBanner('Rescan failed: '+e); }
  finally{ btn.disabled=false; btn.innerHTML='&#x21bb; Rescan'; }
}

// ── Derived data ────────────────────────────────────────────────────────────
function filteredDaily(){ return (raw.daily_by_model||[]).filter(d=>inRange(d.day)&&matchModel(d.model)); }
function filteredSessions(){ return (raw.sessions_all||[]).filter(s=>inRange(s.day)&&matchModel(s.model)); }
function filteredRL(){ return (raw.rate_limits||[]).filter(r=>inRange(r.day)); }
function filteredHourly(){ return (raw.hourly||[]).filter(h=>inRange(h.day)); }

function render(){
  renderStats(); renderRateLimit(); renderDaily(); renderCost(); renderModelMix(); renderHourly();
  renderProjectsChart(); renderSourceChart(); renderModelTable();
  renderProjects(); renderSessions();
}

const RANGE_LABELS={today:'today', week:'this week', month:'this month', 'prev-month':'previous month', '7d':'last 7 days', '30d':'last 30 days', '90d':'last 90 days', all:'all time'};

function renderStats(){
  const sessions=filteredSessions();
  let turns=0,input=0,cached=0,output=0,reasoning=0,cost=0,subTokens=0;
  sessions.forEach(s=>{
    turns+=s.turns; input+=s.input; cached+=s.cached; output+=s.output; reasoning+=s.reasoning;
    cost+=calcCost(s.model,s.input,s.cached,s.output);
    if(s.source==='subagent') subTokens+=s.input+s.output;
  });
  const rl=(raw.rate_limits||[]);
  const latest=rl.length?rl[rl.length-1]:null;
  const rangeLabel=RANGE_LABELS[range]||range;
  const cards=[
    ['Sessions', sessions.length.toLocaleString(), rangeLabel],
    ['Turns', fmt(turns), rangeLabel],
    ['Input Tokens', fmt(input), rangeLabel],
    ['Cached Input', fmt(cached), (input?Math.round(100*cached/input):0)+'% of input'],
    ['Output Tokens', fmt(output), rangeLabel],
    ['Reasoning Tokens', fmt(reasoning), 'subset of output'],
    ['Subagent Tokens', fmt(subTokens), 'included in totals'],
    ['Est. Cost', fmtMoney(cost), rangeLabel],
    ['Plan', latest?latest.plan_type||'unknown':'—', latest&&latest.primary_pct!=null?latest.primary_pct.toFixed(1)+'% of '+winLabel(latest.primary_window)+' used':'no rate data'],
  ];
  document.getElementById('stats-row').innerHTML=cards.map(c=>`<div class="stat-card"><div class="label">${esc(c[0])}</div><div class="value">${esc(c[1])}</div><div class="sub">${esc(c[2])}</div></div>`).join('');
}
function winLabel(m){ if(!m) return 'rolling'; if(m%10080===0){ const w=m/10080; return w===1?'weekly':w+'-week'; } if(m%1440===0){ const d=m/1440; return d===1?'daily':d+'-day'; } if(m%60===0) return (m/60)+'-hour'; return m+'-min'; }
function fmtReset(epoch){ if(!epoch) return '?'; try{ return new Date(epoch*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }catch(e){ return '?'; } }

function mkChart(id, cfg){ if(charts[id]) charts[id].destroy(); const el=document.getElementById(id); if(el) charts[id]=new Chart(el, cfg); }
const AXIS={ ticks:{color:'#6b7280'}, grid:{color:'#262b36'} };
// Axis with a title label and optional value formatter (task 5: units on every axis).
function axis(title, opts){ opts=opts||{}; return { ticks:{color:'#6b7280', callback:opts.fmt}, grid:opts.noGrid?{drawOnChartArea:false}:{color:'#262b36'}, stacked:!!opts.stacked, position:opts.position, min:opts.min, suggestedMax:opts.max, title:{display:!!title, text:title, color:'#6b7280'} }; }
const tokFmt=(v)=>fmt(v);
const usdFmt=(v)=>'$'+v;

function renderRateLimit(){
  const rows=filteredRL();
  const labels=rows.map(r=>r.timestamp?r.timestamp.slice(5,16).replace('T',' '):'');
  // Window semantics changed across Codex versions (older builds used primary=5h/secondary=weekly;
  // recent builds use primary=weekly/secondary=none). Describe the CURRENT window from the latest
  // snapshot so the caption matches the Plan tile, not a stale early record.
  const latest=rows.length?rows[rows.length-1]:null;
  const pWin=latest?latest.primary_window:null;
  const sWin=latest?latest.secondary_window:null;
  const latestHasSec=!!(latest && latest.secondary_pct!=null);
  const anySec=rows.some(r=>r.secondary_pct!=null);  // plot the series if ANY point has it
  const ds=[{label:'Primary — '+winLabel(pWin)+' window', data:rows.map(r=>r.primary_pct), borderColor:'#10a37f', backgroundColor:'#10a37f22', tension:.25, pointRadius:0, spanGaps:true}];
  if(anySec) ds.push({label:'Secondary'+(sWin?' — '+winLabel(sWin)+' window':''), data:rows.map(r=>r.secondary_pct), borderColor:'#4c8bf5', backgroundColor:'#4c8bf522', tension:.25, pointRadius:0, spanGaps:true});
  mkChart('chart-ratelimit',{ type:'line', data:{ labels, datasets:ds },
    options:{ responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
      scales:{ x:axis('time'), y:axis('% of window used',{min:0,max:100}) },
      plugins:{ legend:{labels:{color:'#c7ccd6'}},
        tooltip:{callbacks:{label:c=>c.dataset.label+': '+(c.parsed.y==null?'—':c.parsed.y.toFixed(1)+'%')}} } } });
  const cap=document.getElementById('rl-caption');
  if(cap){
    cap.innerHTML='Codex enforces a rolling usage limit. <b>Primary</b> is your '
      +winLabel(pWin)+' allowance'+(latestHasSec?'; <b>secondary</b> is the '+winLabel(sWin)+' allowance':'')
      +'. The line is the share of that window&rsquo;s allowance consumed so far (0&ndash;100%), resetting at the window boundary'
      +(latest&&latest.primary_resets_at?' &mdash; primary resets '+fmtReset(latest.primary_resets_at):'')
      +'. (Older Codex builds logged different windows, so early points may reflect a shorter window.)';
  }
}

function dayList(){ const days=new Set(); filteredDaily().forEach(d=>days.add(d.day)); return [...days].sort(); }

function renderDaily(){
  const days=dayList();
  const idx=Object.fromEntries(days.map((d,i)=>[d,i]));
  const mk=()=>days.map(()=>0);
  const fresh=mk(), cached=mk(), out=mk();
  filteredDaily().forEach(d=>{ const i=idx[d.day]; fresh[i]+=Math.max(d.input-d.cached,0); cached[i]+=d.cached; out[i]+=d.output; });
  // Codex is hugely input-heavy: output is ~0.3% of input, so it's sub-pixel if stacked with
  // input. Plot output as a line on its own right axis so it's actually visible (§3.5 task 3).
  mkChart('chart-daily',{
    data:{ labels:days, datasets:[
      {type:'bar', label:'Fresh input', data:fresh, backgroundColor:'#4c8bf5cc', yAxisID:'y', stack:'in'},
      {type:'bar', label:'Cached input', data:cached, backgroundColor:'#5bb8a3cc', yAxisID:'y', stack:'in'},
      {type:'line', label:'Output (right axis)', data:out, borderColor:'#d97757', backgroundColor:'#d97757', yAxisID:'y1', tension:.25, pointRadius:2, borderWidth:2},
    ]},
    options:{ responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
      scales:{
        x:axis('date',{stacked:true}),
        y:axis('input tokens',{stacked:true, fmt:tokFmt}),
        y1:axis('output tokens',{position:'right', noGrid:true, fmt:tokFmt}),
      },
      plugins:{ legend:{labels:{color:'#c7ccd6'}},
        tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmt(c.parsed.y)+' tok'}} } } });
}

function renderCost(){
  const days=dayList();
  const idx=Object.fromEntries(days.map((d,i)=>[d,i]));
  const models=[...new Set(filteredDaily().map(d=>d.model))];
  // Data stays in USD; the credits view only re-labels the axis/tooltip (×25). Estimated
  // models get a " *" in the legend label.
  const datasets=models.map((m,k)=>{ const arr=days.map(()=>0); filteredDaily().filter(d=>d.model===m).forEach(d=>{ arr[idx[d.day]]+=calcCost(d.model,d.input,d.cached,d.output); }); return {label:m+estStar(m), data:arr, backgroundColor:MODEL_COLORS[k%MODEL_COLORS.length]}; });
  const moneyTick=(v)=> currency==='credits' ? fmt(v*CREDITS_PER_USD) : usdFmt(v);
  mkChart('chart-cost',{ type:'bar', data:{labels:days, datasets},
    options:{ responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
      scales:{ x:axis('date',{stacked:true}), y:axis(costUnitLabel(),{stacked:true, fmt:moneyTick}) },
      plugins:{ legend:{labels:{color:'#c7ccd6',boxWidth:12,font:{size:10}}},
        tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmtMoney(c.parsed.y)}} } } });
}

function renderModelMix(){
  const by={}; filteredDaily().forEach(d=>{ by[d.model]=(by[d.model]||0)+d.input+d.output; });
  const models=Object.keys(by); const data=models.map(l=>by[l]);
  const labels=models.map(m=>m+estStar(m));
  const total=data.reduce((a,b)=>a+b,0)||1;
  mkChart('chart-modelmix',{ type:'doughnut', data:{labels, datasets:[{data, backgroundColor:models.map((_,i)=>MODEL_COLORS[i%MODEL_COLORS.length])}]},
    options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{position:'right',labels:{color:'#c7ccd6',boxWidth:12,font:{size:11}}},
      tooltip:{callbacks:{label:c=>c.label+': '+fmt(c.parsed)+' tok ('+(100*c.parsed/total).toFixed(1)+'%)'}} } } });
}

function renderProjectsChart(){
  const top=groupByProject(filteredSessions()).sort((a,b)=>(b.input+b.output)-(a.input+a.output)).slice(0,10);
  const labels=top.map(p=>p.project.length>22?'…'+p.project.slice(-20):p.project);
  mkChart('chart-project',{ type:'bar', data:{ labels, datasets:[
      {label:'Input', data:top.map(p=>p.input), backgroundColor:'#4c8bf5cc'},
      {label:'Output', data:top.map(p=>p.output), backgroundColor:'#d97757cc'},
    ]},
    options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales:{ x:axis('tokens',{fmt:tokFmt}), y:{ticks:{color:'#6b7280',font:{size:11}}, grid:{color:'#262b36'}} },
      plugins:{ legend:{labels:{color:'#c7ccd6'}}, tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmt(c.parsed.x)+' tok'}} } } });
}

function renderSourceChart(){
  // Bucket by whatever thread_source values actually appear (schema drift observed locally:
  // real data carries at least user/subagent/automation/chatgpt_handoff) — never silently fold
  // an unrecognized source into "user".
  const by={};
  filteredSessions().forEach(s=>{ const key=s.source||'unknown'; const b=by[key]||(by[key]={fresh:0,cached:0,output:0}); b.fresh+=Math.max(s.input-s.cached,0); b.cached+=s.cached; b.output+=s.output; });
  const order=['user','subagent','automation'];
  const labels=order.filter(l=>by[l]).concat(Object.keys(by).filter(l=>!order.includes(l)).sort());
  mkChart('chart-source',{ type:'bar', data:{ labels, datasets:[
      {label:'Fresh input', data:labels.map(l=>by[l].fresh), backgroundColor:'#4c8bf5cc', stack:'tok'},
      {label:'Cached input', data:labels.map(l=>by[l].cached), backgroundColor:'#5bb8a3cc', stack:'tok'},
      {label:'Output', data:labels.map(l=>by[l].output), backgroundColor:'#d97757cc', stack:'tok'},
    ]},
    options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales:{ x:axis('tokens',{stacked:true, fmt:tokFmt}), y:{stacked:true, ticks:{color:'#6b7280'}, grid:{color:'#262b36'}} },
      plugins:{ legend:{labels:{color:'#c7ccd6'}}, tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmt(c.parsed.x)+' tok'}} } } });
}

function renderHourly(){
  // Raw sums get misleading as the range grows (30d "hourly" totals dwarf 7d ones for reasons
  // having nothing to do with usage pattern) — average per day-in-range instead (§3.6 task 7).
  const off=-new Date().getTimezoneOffset()/60|0;
  const turnBuckets=new Array(24).fill(0), outBuckets=new Array(24).fill(0);
  const days=new Set();
  filteredHourly().forEach(h=>{ const lh=((h.hour+off)%24+24)%24; turnBuckets[lh]+=h.turns; outBuckets[lh]+=h.output; days.add(h.day); });
  const dayCount=days.size||1;
  const avgTurns=turnBuckets.map(v=>v/dayCount);
  const avgOut=outBuckets.map(v=>v/dayCount);
  mkChart('chart-hourly',{
    data:{ labels:[...Array(24).keys()].map(h=>String(h).padStart(2,'0')+':00'),
      datasets:[
        {type:'bar', label:'Avg turns/hour', data:avgTurns, backgroundColor:'#10a37fcc', yAxisID:'y'},
        {type:'line', label:'Avg output tokens/hour', data:avgOut, borderColor:'#d97757', backgroundColor:'#d97757', yAxisID:'y1', tension:.25, pointRadius:2, borderWidth:2},
      ]},
    options:{ responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
      scales:{ x:axis('hour of day (local)'), y:axis('avg turns',{fmt:tokFmt}), y1:axis('avg output tokens',{position:'right', noGrid:true, fmt:tokFmt}) },
      plugins:{legend:{labels:{color:'#c7ccd6'}}, tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmt(c.parsed.y)}}} } });
  const cap=document.getElementById('hourly-caption');
  if(cap) cap.textContent=dayCount+' day'+(dayCount===1?'':'s')+' averaged (local time).';
}

function renderProjects(){
  let rows=groupByProject(filteredSessions());
  rows.sort((a,b)=> projSort==='project' ? a.project.localeCompare(b.project) : b[projSort]-a[projSort]);
  document.getElementById('projects-body').innerHTML=rows.map(p=>`<tr><td>${esc(p.project)}</td><td class="num">${p.sessions}</td><td class="num">${fmt(p.turns)}</td><td class="num">${fmt(p.input)}</td><td class="num">${fmt(p.output)}</td><td class="${p.cost?'cost':'cost-na'}">${p.cost?fmtMoney(p.cost):'n/a'}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No data in range.</td></tr>';
}

function groupByModel(sessions){
  const by={};
  sessions.forEach(s=>{ const m=by[s.model]||(by[s.model]={model:s.model, sessions:0, turns:0, input:0, cached:0, output:0, cost:0}); m.sessions++; m.turns+=s.turns; m.input+=s.input; m.cached+=s.cached; m.output+=s.output; m.cost+=calcCost(s.model,s.input,s.cached,s.output); });
  return Object.values(by);
}
function groupByProject(sessions){
  const by={};
  sessions.forEach(s=>{ const p=by[s.project]||(by[s.project]={project:s.project, sessions:0, turns:0, input:0, cached:0, output:0, cost:0}); p.sessions++; p.turns+=s.turns; p.input+=s.input; p.cached+=s.cached; p.output+=s.output; p.cost+=calcCost(s.model,s.input,s.cached,s.output); });
  return Object.values(by);
}

function renderModelTable(){
  let rows=groupByModel(filteredSessions());
  rows.sort((a,b)=> modelSort==='model' ? a.model.localeCompare(b.model) : b[modelSort]-a[modelSort]);
  document.getElementById('model-body').innerHTML=rows.map(m=>`<tr>
    <td><span class="tag model${isEst(m.model)?' est':''}"${isEst(m.model)?` title="${esc(EST_NOTE)}"`:''}>${esc(m.model)}${estStar(m.model)}</span></td>
    <td class="num">${m.sessions}</td><td class="num">${fmt(m.turns)}</td><td class="num">${fmt(m.input)}</td>
    <td class="num">${fmt(m.cached)}</td><td class="num">${fmt(m.output)}</td>
    <td class="${m.cost?'cost':'cost-na'}">${priced(m.model)?fmtMoney(m.cost)+estStar(m.model):'n/a'}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">No data in range.</td></tr>';
}
function sortModel(c){ modelSort=c; renderModelTable(); }

function sortProjects(c){ projSort=c; renderProjects(); }
function sortSessions(c){ sessSort=c; renderSessions(); }
function moreSessions(){ sessLimit+=25; renderSessions(); }

function renderSessions(){
  let rows=filteredSessions().slice();
  rows.forEach(s=>s._cost=calcCost(s.model,s.input,s.cached,s.output));
  rows.sort((a,b)=> sessSort==='last' ? (a.last<b.last?1:-1) : (sessSort==='cost'? b._cost-a._cost : b[sessSort]-a[sessSort]));
  const shown=rows.slice(0,sessLimit);
  const srcClass={user:'src-user',subagent:'src-subagent',automation:'src-automation'};
  document.getElementById('sessions-body').innerHTML=shown.map(s=>`<tr>
    <td>${s.topic?esc(s.topic):'<span class="muted">untitled</span>'}</td>
    <td>${esc(s.project)}</td>
    <td class="${srcClass[s.source]||''}">${esc(s.source)}</td>
    <td><span class="tag model${isEst(s.model)?' est':''}"${isEst(s.model)?` title="${esc(EST_NOTE)}"`:''}>${esc(s.model)}${estStar(s.model)}</span></td>
    <td class="num">${esc(s.last)}</td>
    <td class="num">${fmt(s.turns)}</td>
    <td class="num">${fmt(s.input)}</td>
    <td class="num">${fmt(s.output)}</td>
    <td class="${s._cost?'cost':'cost-na'}"${isEst(s.model)?` title="${esc(EST_NOTE)}"`:''}>${priced(s.model)?fmtMoney(s._cost)+estStar(s.model):'n/a'}</td></tr>`).join('') || '<tr><td colspan="9" class="muted">No sessions in range.</td></tr>';
  document.getElementById('sessions-more').style.display = rows.length>sessLimit ? 'inline-block' : 'none';
}

// ── CSV export (client-side, no server round-trip) ─────────────────────────
function csvCell(v){ const s=String(v==null?'':v); return /[",\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s; }
function downloadCSV(name, header, rows){
  const lines=[header.map(csvCell).join(',')].concat(rows.map(r=>r.map(csvCell).join(',')));
  const blob=new Blob([lines.join('\n')], {type:'text/csv;charset=utf-8;'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a'); a.href=url; a.download='gptusage_'+name+'_'+todayISO()+'.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
}
function exportCSV(kind){
  const sessions=filteredSessions();
  if(kind==='model'){
    const rows=groupByModel(sessions).map(m=>[m.model, m.sessions, m.turns, m.input, m.cached, m.output, m.cost.toFixed(4)]);
    downloadCSV('by_model', ['Model','Sessions','Turns','Input','Cached','Output','Est Cost USD'], rows);
  } else if(kind==='projects'){
    const rows=groupByProject(sessions).map(p=>[p.project, p.sessions, p.turns, p.input, p.cached, p.output, p.cost.toFixed(4)]);
    downloadCSV('by_project', ['Project','Sessions','Turns','Input','Cached','Output','Est Cost USD'], rows);
  } else if(kind==='sessions'){
    const rows=sessions.map(s=>[s.session_id, s.topic, s.project, s.source, s.model, s.last, s.turns, s.input, s.cached, s.output, calcCost(s.model,s.input,s.cached,s.output).toFixed(4)]);
    downloadCSV('sessions', ['Session ID','Title','Project','Source','Model','Last Active','Turns','Input','Cached','Output','Est Cost USD'], rows);
  }
}

// ── Collapsible cards (localStorage) ────────────────────────────────────────
function initCollapse(){
  document.querySelectorAll('[data-card]').forEach(card=>{
    const key='gptusage.collapse.'+card.dataset.card;
    if(localStorage.getItem(key)==='1') card.classList.add('collapsed');
    const h=card.querySelector('h2');
    if(h) h.addEventListener('click',()=>{ card.classList.toggle('collapsed'); localStorage.setItem(key, card.classList.contains('collapsed')?'1':'0'); });
  });
}

let autoTimer=null;
function scheduleAutoRefresh(){ if(autoTimer) clearInterval(autoTimer); autoTimer=setInterval(loadData, 30000); }

readURL(); initCollapse(); loadData(); scheduleAutoRefresh();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            config = json.dumps({"version": VERSION})
            html = HTML_TEMPLATE.replace("__APP_CONFIG_JSON__", config)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            # Pass DB_PATH explicitly so a monkey-patched dashboard.DB_PATH is honored
            # (default args freeze at def time — same contract tests rely on).
            data = get_dashboard_data(DB_PATH)
            self._send(200, json.dumps(data).encode("utf-8"), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            # Incremental scan (never deletes the DB — it's the durable history store).
            # Pass db_path explicitly so patched globals in tests are honored.
            import scanner
            result = scanner.scan(db_path=DB_PATH, verbose=False)
            self._send(200, json.dumps(result).encode("utf-8"), "application/json")
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8090"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
