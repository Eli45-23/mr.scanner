#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urljoin


HTML_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Options Whale Workbench</title>
  <style>
    :root { --bg:#f5f7f8; --card:#fff; --ink:#102027; --muted:#65737e; --line:#d8e0e4; --teal:#1e7f86; --good:#0b7a44; --warn:#b66d00; --bad:#b3261e; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    header { background:#0f2d35; color:#fff; padding:16px 22px; position:sticky; top:0; z-index:10; }
    h1 { margin:0; font-size:22px; }
    .sub { color:#b9d4d9; margin-top:4px; }
    .tabs { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
    .tab { border:1px solid rgba(255,255,255,.22); border-radius:999px; padding:8px 10px; color:#fff; text-decoration:none; font-weight:800; background:rgba(255,255,255,.08); }
    .tab.active { background:#fff; color:#0f2d35; }
    main { padding:14px; display:grid; gap:14px; }
    .frame-card { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden; box-shadow:0 1px 2px rgba(0,0,0,.04); }
    .frame-head { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px 12px; border-bottom:1px solid var(--line); background:#fbfcfd; }
    .frame-head h2 { margin:0; font-size:16px; }
    .frame-head a { color:var(--teal); font-weight:800; text-decoration:none; }
    iframe { width:100%; height:70vh; border:0; display:block; background:white; }
    .outcome iframe { height:52vh; }
    .links { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
    .link-card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; }
    p { color:var(--muted); line-height:1.45; }
    code { background:#eef3f5; padding:2px 5px; border-radius:5px; }
    .button { display:inline-block; background:var(--teal); color:#fff; text-decoration:none; border:0; border-radius:9px; padding:9px 11px; font-weight:800; cursor:pointer; }
    .button.secondary { background:#23424a; }
    .filters { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; padding:12px; border-bottom:1px solid var(--line); background:#fbfcfd; }
    .field label { display:block; color:var(--muted); font-size:12px; font-weight:800; margin-bottom:4px; }
    input, select { width:100%; border:1px solid var(--line); border-radius:8px; padding:8px; background:#fff; color:var(--ink); }
    .control-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; padding:0 12px 12px; background:#fbfcfd; }
    .flow-list { display:grid; gap:10px; padding:12px; max-height:650px; overflow:auto; }
    .flow-card { border:1px solid var(--line); border-radius:12px; background:#fff; padding:12px; display:grid; gap:10px; }
    .flow-card.near-miss { border-color:#f0d59b; background:#fffaf0; }
    .flow-top { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; }
    .contract { font-size:18px; font-weight:900; }
    .contract .type.put { color:var(--bad); }
    .contract .type.call { color:var(--good); }
    .badges { display:flex; gap:6px; flex-wrap:wrap; }
    .badge { border:1px solid var(--line); border-radius:999px; padding:4px 7px; font-size:12px; font-weight:800; background:#f6f8f9; }
    .badge.warn { background:#fff3df; color:var(--warn); border-color:#f0d59b; }
    .score { color:var(--teal); }
    .flow-grid { display:grid; grid-template-columns:repeat(4,minmax(140px,1fr)); gap:8px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:8px; background:#fbfcfd; }
    .metric span { display:block; color:var(--muted); font-size:12px; font-weight:750; margin-bottom:3px; }
    .metric strong { font-size:14px; overflow-wrap:anywhere; }
    .reason { border-top:1px solid var(--line); padding-top:8px; color:var(--muted); line-height:1.35; }
    .reason strong { color:var(--ink); }
    .muted { color:var(--muted); }
    .notice { padding:10px 12px; color:var(--muted); }
    .notice.warn { color:var(--warn); font-weight:800; }
    .bad { color:var(--bad); }
    @media(max-width:900px){ .flow-grid { grid-template-columns:repeat(2,minmax(140px,1fr)); } iframe { height:62vh; } }
    @media(max-width:560px){ .flow-grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Options Whale Workbench</h1>
    <div class="sub">Scanner filters, real whale alerts first, debug candidates hidden unless you ask for them.</div>
    <nav class="tabs">
      <a class="tab active" href="#clean-flow">Scanner + Filters</a>
      <a class="tab" href="#scanner">Original Scanner</a>
      <a class="tab" href="#outcomes">Outcomes</a>
      <a class="tab" href="#links">Open Separate Tabs</a>
    </nav>
  </header>
  <main>
    <section class="frame-card" id="clean-flow">
      <div class="frame-head">
        <h2>Real Whale Flow</h2>
        <button class="button" id="refreshFlow">Refresh Flow</button>
      </div>
      <div class="filters">
        <div class="field"><label>Max DTE</label><input id="maxDte" type="number"></div>
        <div class="field"><label>Min Score</label><input id="minScore" type="number"></div>
        <div class="field"><label>Min Premium</label><input id="minPremium" type="number"></div>
        <div class="field"><label>Min Volume</label><input id="minVolume" type="number"></div>
        <div class="field"><label>Min Vol/OI</label><input id="minVolOi" type="number" step="0.1"></div>
        <div class="field"><label>Max Spread %</label><input id="maxSpread" type="number" step="0.1"></div>
        <div class="field"><label>Include 0DTE</label><select id="include0dte"><option value="false">No</option><option value="true">Yes</option></select></div>
        <div class="field"><label>Include Weeklies</label><select id="includeWeeklies"><option value="true">Yes</option><option value="false">No</option></select></div>
        <div class="field"><label>Max Results</label><input id="maxResults" type="number"></div>
        <div class="field"><label>Debug Loose Mode</label><select id="debugLoose"><option value="false">Off</option><option value="true">On</option></select></div>
        <div class="field"><label>Show Debug Candidates</label><select id="showDebug"><option value="false">Hide</option><option value="true">Show</option></select></div>
      </div>
      <div class="control-row">
        <button class="button" id="saveFilters">Save Filters</button>
        <button class="button secondary" id="runScan">Run Scan Now</button>
        <span id="filterStatus" class="muted"></span>
      </div>
      <div class="notice">Real alerts are shown first. Debug candidates are hidden by default because they are not alert quality.</div>
      <div class="flow-list" id="flowTable"><div class="notice">Loading whale-flow rows...</div></div>
    </section>
    <section class="frame-card scanner" id="scanner">
      <div class="frame-head">
        <h2>Original Options Whale Scanner</h2>
        <a href="__MAIN_URL__" target="_blank" rel="noreferrer">Open separate tab</a>
      </div>
      <iframe title="Main Scanner Dashboard" src="__MAIN_URL__"></iframe>
    </section>
    <section class="frame-card outcome" id="outcomes">
      <div class="frame-head">
        <h2>Outcome Dashboard</h2>
        <a href="__OUTCOME_URL__" target="_blank" rel="noreferrer">Open separate tab</a>
      </div>
      <iframe title="Outcome Dashboard" src="__OUTCOME_URL__"></iframe>
    </section>
    <section class="links" id="links">
      <div class="link-card"><h2>Main Scanner Dashboard</h2><p>Original page with scanner controls and full table.</p><p><code>__MAIN_URL__</code></p><a class="button" href="__MAIN_URL__" target="_blank" rel="noreferrer">Open Scanner</a></div>
      <div class="link-card"><h2>Outcome Dashboard</h2><p>Proof engine: completed, pending, insufficient close, dirty ignored, and performance by symbol/flow bias.</p><p><code>__OUTCOME_URL__</code></p><a class="button" href="__OUTCOME_URL__" target="_blank" rel="noreferrer">Open Outcomes</a></div>
    </section>
  </main>
  <script>
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
    const pick = (item, field) => (item.candidate && item.candidate[field] !== undefined && item.candidate[field] !== null && item.candidate[field] !== '') ? item.candidate[field] : item[field];
    const money = (v) => v === null || v === undefined || v === '' || Number.isNaN(Number(v)) ? 'Unavailable' : `$${Number(v).toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2})}`;
    const compactMoney = (v) => { if (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) return 'Unavailable'; const n = Number(v); if (Math.abs(n) >= 1000000) return `$${(n / 1000000).toFixed(1)}M`; if (Math.abs(n) >= 1000) return `$${(n / 1000).toFixed(1)}K`; return money(n); };
    const num = (v) => v === null || v === undefined || v === '' || Number.isNaN(Number(v)) ? 'Unavailable' : Number(v).toLocaleString();
    const pct = (v) => v === null || v === undefined || v === '' || Number.isNaN(Number(v)) ? 'Unavailable' : `${Number(v).toFixed(2)}%`;
    const ids = ['maxDte','minScore','minPremium','minVolume','minVolOi','maxSpread','include0dte','includeWeeklies','maxResults','debugLoose','showDebug'];
    const el = Object.fromEntries(ids.concat(['saveFilters','runScan','refreshFlow','filterStatus','flowTable']).map(id => [id, document.getElementById(id)]));
    function contractTitle(item) { const symbol = pick(item,'underlying_symbol') || item.underlying_symbol || item.underlying || 'UNKNOWN'; const type = pick(item,'option_type') || item.option_type || 'OPTION'; const strike = pick(item,'strike') ?? item.strike ?? ''; return `${symbol} <span class="type ${String(type).toLowerCase()}">${esc(type)}</span> ${money(strike)}`; }
    function rowCard(item, kind) {
      const exp = pick(item,'expiration') || item.expiration || 'Unavailable';
      const dte = pick(item,'dte') ?? item.dte ?? item.days_to_expiration ?? 'Unavailable';
      const moneyness = pick(item,'moneyness') || item.moneyness || 'Unavailable';
      const vol = pick(item,'volume') ?? item.volume;
      const oi = pick(item,'open_interest') ?? item.open_interest;
      const volOi = pick(item,'volume_oi_ratio') ?? item.volume_oi_ratio;
      const last = pick(item,'last') ?? pick(item,'midpoint') ?? item.last ?? item.midpoint;
      const spread = pick(item,'spread_percent') ?? item.spread_percent;
      const premium = pick(item,'estimated_premium') ?? item.estimated_premium ?? item.premium;
      const score = item.whale_score ?? item.score ?? 'NA';
      const isNearMiss = kind === 'near_miss';
      return `<article class="flow-card ${isNearMiss ? 'near-miss' : ''}"><div class="flow-top"><div><div class="contract">${contractTitle(item)}</div><div class="muted">${esc(pick(item,'time_detected') || item.timestamp || '')} | Exp ${esc(exp)} | ${esc(dte)} DTE | ${esc(moneyness)}</div></div><div class="badges"><span class="badge ${isNearMiss ? 'warn' : ''}">${isNearMiss ? 'Debug candidate / not alert' : esc(item.alert_tier || pick(item,'tier') || 'No tier')}</span><span class="badge score">Score ${esc(score)}</span><span class="badge">${esc(item.classification || 'Unclassified')}</span></div></div><div class="flow-grid"><div class="metric"><span>Flow Size</span><strong>Vol ${num(vol)} / OI ${num(oi)}</strong></div><div class="metric"><span>Vol/OI</span><strong>${esc(volOi ?? 'Unavailable')}</strong></div><div class="metric"><span>Premium</span><strong>${compactMoney(premium)}</strong></div><div class="metric"><span>Price / Spread</span><strong>${money(last)} / ${pct(spread)}</strong></div></div><div class="reason"><strong>Direction:</strong> ${esc(item.direction_label || 'Unavailable')}<br><strong>Price context:</strong> ${esc(item.price_confirmation_label || 'Unavailable')}<br><strong>Why it matters:</strong> ${esc(item.reason_summary || item.reason || item.reason_rejected || 'No reason available')}</div></article>`;
    }
    async function api(path, options = {}) { const r = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options}); const data = await r.json(); if (!r.ok) throw new Error(data.error || r.statusText); return data; }
    async function loadFilters() { const f = await api('/api/filters'); el.maxDte.value = f.max_dte ?? ''; el.minScore.value = f.min_score ?? ''; el.minPremium.value = f.min_premium ?? ''; el.minVolume.value = f.min_volume ?? ''; el.minVolOi.value = f.min_volume_oi_ratio ?? ''; el.maxSpread.value = f.max_spread_percent ?? ''; el.include0dte.value = String(Boolean(f.include_0dte)); el.includeWeeklies.value = String(Boolean(f.include_weeklies)); el.maxResults.value = f.max_results ?? ''; el.debugLoose.value = String(Boolean(f.debug_loose_mode)); }
    async function loadFlow() { try { const data = await api('/api/whales/latest'); const resultRows = data.results || []; const nearMissRows = data.near_misses || []; const showDebug = el.showDebug.value === 'true'; const rows = resultRows.length ? resultRows : (showDebug ? nearMissRows : []); const kind = resultRows.length ? 'alert' : 'near_miss'; if (!rows.length) { el.flowTable.innerHTML = nearMissRows.length ? '<div class="notice warn">No real whale alerts passed. Debug candidates are hidden. Change “Show Debug Candidates” to Show if you want to inspect near misses.</div>' : '<div class="notice">No real whale alerts yet.</div>'; return; } const banner = kind === 'near_miss' ? '<div class="notice warn">Showing debug candidates because you turned them on. These are not alert-quality flow.</div>' : '<div class="notice">Showing real alert-quality whale-flow candidates.</div>'; el.flowTable.innerHTML = banner + rows.slice(0,40).map(row => rowCard(row, kind)).join(''); } catch (err) { el.flowTable.innerHTML = `<div class="notice bad">${esc(err.message)}. Make sure the main scanner dashboard is running on __MAIN_URL__.</div>`; } }
    async function saveFilters() { const payload = {max_dte:Number(el.maxDte.value), min_score:Number(el.minScore.value), min_premium:Number(el.minPremium.value), min_volume:Number(el.minVolume.value), min_volume_oi_ratio:Number(el.minVolOi.value), max_spread_percent:Number(el.maxSpread.value), include_0dte:el.include0dte.value === 'true', include_weeklies:el.includeWeeklies.value === 'true', max_results:Number(el.maxResults.value), debug_loose_mode:el.debugLoose.value === 'true'}; await api('/api/filters', {method:'POST', body:JSON.stringify(payload)}); el.filterStatus.textContent = 'Filters saved'; loadFlow(); }
    async function runScan() { el.runScan.disabled = true; el.runScan.textContent = 'Scanning...'; try { await api('/api/scan'); } finally { el.runScan.disabled = false; el.runScan.textContent = 'Run Scan Now'; loadFlow(); } }
    el.saveFilters.addEventListener('click', saveFilters); el.runScan.addEventListener('click', runScan); el.refreshFlow.addEventListener('click', loadFlow); el.showDebug.addEventListener('change', loadFlow); loadFilters().catch(()=>{}); loadFlow(); setInterval(loadFlow, 15000);
  </script>
</body>
</html>
"""


def render_html(main_url: str, outcome_url: str) -> str:
    return HTML_TEMPLATE.replace("__MAIN_URL__", main_url).replace("__OUTCOME_URL__", outcome_url)


def fetch_json(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    return data if isinstance(data, dict) else {"data": data}


def post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "OptionsWhaleWorkbench/1.0"
    main_url = "http://127.0.0.1:8765"
    outcome_url = "http://127.0.0.1:8775"

    def log_message(self, fmt: str, *args):  # type: ignore[no-untyped-def]
        return

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}

    def send_json(self, data: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def proxy_get(self, path: str) -> None:
        try:
            self.send_json(fetch_json(urljoin(self.main_url.rstrip('/') + '/', path.lstrip('/'))))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def proxy_post(self, path: str, payload: Dict[str, Any]) -> None:
        try:
            self.send_json(post_json(urljoin(self.main_url.rstrip('/') + '/', path.lstrip('/')), payload))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def do_GET(self) -> None:
        if self.path == "/api/links":
            self.send_json({"main_dashboard": self.main_url, "outcome_dashboard": self.outcome_url})
            return
        if self.path == "/api/whales/latest":
            self.proxy_get("api/options-whales/latest")
            return
        if self.path == "/api/filters":
            self.proxy_get("api/options-whales/filters")
            return
        html = render_html(self.main_url, self.outcome_url)
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        body = self.read_json()
        if self.path == "/api/filters":
            self.proxy_post("api/options-whales/filters", body)
            return
        if self.path == "/api/scan":
            self.proxy_get("api/options-whales/scan")
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a one-page options whale scanner workbench.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--main-url", default="http://127.0.0.1:8765")
    parser.add_argument("--outcome-url", default="http://127.0.0.1:8775")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    WorkbenchHandler.main_url = args.main_url
    WorkbenchHandler.outcome_url = args.outcome_url
    server = ThreadingHTTPServer((args.host, args.port), WorkbenchHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Options Whale Workbench running at {url}")
    print(f"Main dashboard: {args.main_url}")
    print(f"Outcome dashboard: {args.outcome_url}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Options Whale Workbench stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
