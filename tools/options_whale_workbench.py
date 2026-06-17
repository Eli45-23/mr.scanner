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
    :root { --bg:#f5f7f8; --card:#fff; --ink:#102027; --muted:#65737e; --line:#d8e0e4; --teal:#1e7f86; }
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
    .button { display:inline-block; background:var(--teal); color:#fff; text-decoration:none; border-radius:9px; padding:9px 11px; font-weight:800; }
    .table-wrap { overflow:auto; max-height:520px; }
    table { width:100%; min-width:1180px; border-collapse:collapse; font-size:13px; }
    th, td { padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
    th { color:var(--muted); background:#fbfcfd; font-size:12px; }
    .score { color:#087f8c; font-weight:900; }
    .muted { color:var(--muted); }
    .notice { padding:10px 12px; color:var(--muted); }
    .bad { color:#b3261e; }
  </style>
</head>
<body>
  <header>
    <h1>Options Whale Workbench</h1>
    <div class="sub">One page for scanner control, clean whale-flow rows, and outcome proof.</div>
    <nav class="tabs">
      <a class="tab active" href="#clean-flow">Clean Flow Table</a>
      <a class="tab" href="#scanner">Scanner</a>
      <a class="tab" href="#outcomes">Outcomes</a>
      <a class="tab" href="#links">Open Separate Tabs</a>
    </nav>
  </header>
  <main>
    <section class="frame-card" id="clean-flow">
      <div class="frame-head">
        <h2>Clean Whale Flow Table</h2>
        <button class="button" id="refreshFlow">Refresh Flow</button>
      </div>
      <div class="notice">This table reads both nested and flat scanner rows so Type / Strike / Exp / DTE do not disappear.</div>
      <div class="table-wrap" id="flowTable"><div class="notice">Loading whale-flow rows...</div></div>
    </section>
    <section class="frame-card scanner" id="scanner">
      <div class="frame-head">
        <h2>Main Scanner Dashboard</h2>
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
      <div class="link-card">
        <h2>Main Scanner Dashboard</h2>
        <p>Live options whale scanner, flow rows, score components, market context, and scan controls.</p>
        <p><code>__MAIN_URL__</code></p>
        <a class="button" href="__MAIN_URL__" target="_blank" rel="noreferrer">Open Scanner</a>
      </div>
      <div class="link-card">
        <h2>Outcome Dashboard</h2>
        <p>Proof engine: completed, pending, insufficient close, dirty ignored, and performance by symbol/flow bias.</p>
        <p><code>__OUTCOME_URL__</code></p>
        <a class="button" href="__OUTCOME_URL__" target="_blank" rel="noreferrer">Open Outcomes</a>
      </div>
    </section>
  </main>
  <script>
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
    const pick = (item, field) => (item.candidate && item.candidate[field] !== undefined && item.candidate[field] !== null && item.candidate[field] !== '') ? item.candidate[field] : item[field];
    const money = (v) => v === null || v === undefined || v === '' ? '' : `$${Number(v).toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2})}`;
    const num = (v) => v === null || v === undefined || v === '' ? '' : Number(v).toLocaleString();
    const pct = (v) => v === null || v === undefined || v === '' ? '' : `${Number(v).toFixed(2)}%`;
    async function loadFlow() {
      const box = document.getElementById('flowTable');
      try {
        const r = await fetch('/api/whales/latest');
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || r.statusText);
        const rows = data.results || [];
        if (!rows.length) { box.innerHTML = '<div class="notice">No whale-flow rows yet.</div>'; return; }
        box.innerHTML = `<table><thead><tr>
          <th>Time</th><th>Tier</th><th>Symbol</th><th>Type</th><th>Strike</th><th>Exp</th><th>DTE</th><th>Moneyness</th><th>Volume</th><th>OI</th><th>Vol/OI</th><th>Last/Mid</th><th>Spread</th><th>Premium</th><th>Score</th><th>Class</th><th>Direction</th><th>Price Context</th><th>Reason</th>
        </tr></thead><tbody>${rows.slice(0,80).map((item) => `<tr>
          <td>${esc(pick(item,'time_detected') || item.timestamp || '')}</td>
          <td>${esc(item.alert_tier || pick(item,'tier') || '')}</td>
          <td><strong>${esc(pick(item,'underlying_symbol') || item.underlying_symbol || '')}</strong></td>
          <td>${esc(pick(item,'option_type') || item.option_type || '')}</td>
          <td>${money(pick(item,'strike') ?? item.strike)}</td>
          <td>${esc(pick(item,'expiration') || item.expiration || '')}</td>
          <td>${esc(pick(item,'dte') ?? item.dte ?? item.days_to_expiration ?? '')}</td>
          <td>${esc(pick(item,'moneyness') || item.moneyness || '')}</td>
          <td>${num(pick(item,'volume') ?? item.volume)}</td>
          <td>${num(pick(item,'open_interest') ?? item.open_interest)}</td>
          <td>${esc(pick(item,'volume_oi_ratio') ?? item.volume_oi_ratio ?? '')}</td>
          <td>${money(pick(item,'last') ?? pick(item,'midpoint') ?? item.last ?? item.midpoint)}</td>
          <td>${pct(pick(item,'spread_percent') ?? item.spread_percent)}</td>
          <td>${money(pick(item,'estimated_premium') ?? item.estimated_premium)}</td>
          <td><span class="score">${esc(item.whale_score ?? item.score ?? '')}</span></td>
          <td>${esc(item.classification || '')}</td>
          <td>${esc(item.direction_label || '')}</td>
          <td>${esc(item.price_confirmation_label || '')}</td>
          <td>${esc(item.reason_summary || item.reason || '')}</td>
        </tr>`).join('')}</tbody></table>`;
      } catch (err) {
        box.innerHTML = `<div class="notice bad">${esc(err.message)}. Make sure the main scanner dashboard is running on __MAIN_URL__.</div>`;
      }
    }
    document.getElementById('refreshFlow').addEventListener('click', loadFlow);
    loadFlow();
    setInterval(loadFlow, 15000);
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


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "OptionsWhaleWorkbench/1.0"
    main_url = "http://127.0.0.1:8765"
    outcome_url = "http://127.0.0.1:8775"

    def log_message(self, fmt: str, *args):  # type: ignore[no-untyped-def]
        return

    def send_json(self, data: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/api/links":
            self.send_json({"main_dashboard": self.main_url, "outcome_dashboard": self.outcome_url})
            return
        if self.path == "/api/whales/latest":
            try:
                self.send_json(fetch_json(urljoin(self.main_url.rstrip('/') + '/', 'api/options-whales/latest')))
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        html = render_html(self.main_url, self.outcome_url)
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


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
