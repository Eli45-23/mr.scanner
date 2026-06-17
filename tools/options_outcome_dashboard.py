#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from tools.summarize_options_outcomes import OUTCOMES_PATH, summarize_outcome_file


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Options Whale Outcomes</title>
  <style>
    :root { --bg:#f5f7f8; --card:#fff; --ink:#102027; --muted:#65737e; --line:#d8e0e4; --teal:#1e7f86; --green:#247a44; --amber:#9a6500; --red:#b2362b; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    header { background:#0f2d35; color:#fff; padding:18px 22px; }
    h1 { margin:0; font-size:22px; }
    .sub { color:#b9d4d9; margin-top:4px; }
    main { padding:18px; display:grid; gap:16px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
    .stats { display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:10px; }
    .stat { border:1px solid var(--line); border-radius:10px; padding:12px; background:#fbfcfd; }
    .label { font-size:12px; color:var(--muted); font-weight:700; text-transform:uppercase; letter-spacing:.03em; }
    .value { margin-top:5px; font-size:22px; font-weight:800; }
    .good { color:var(--green); } .warn { color:var(--amber); } .bad { color:var(--red); } .teal { color:var(--teal); }
    .controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    select, button { border:1px solid var(--line); border-radius:8px; background:white; padding:8px 10px; font-size:14px; }
    button { background:var(--teal); color:white; font-weight:700; cursor:pointer; }
    table { width:100%; border-collapse:collapse; min-width:900px; }
    th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { font-size:12px; color:var(--muted); background:#fbfcfd; }
    .table-wrap { overflow:auto; }
    .muted { color:var(--muted); }
    .pill { display:inline-block; padding:3px 7px; border-radius:999px; background:#eef3f5; font-size:12px; font-weight:700; }
    .empty { padding:22px; text-align:center; color:var(--muted); }
    @media(max-width:900px){ .stats{grid-template-columns:repeat(2,minmax(120px,1fr));} }
  </style>
</head>
<body>
  <header>
    <h1>Options Whale Outcome Dashboard</h1>
    <div class="sub">Proof engine: completed, pending, insufficient-close, and performance by category.</div>
  </header>
  <main>
    <section class="card">
      <div class="controls">
        <label>Group <select id="group">
          <option value="symbol_flow_bias">Symbol + Flow Bias</option>
          <option value="symbol">Symbol</option>
          <option value="option_type">Option Type</option>
          <option value="flow_bias">Flow Bias</option>
          <option value="flow_bias_source">Flow Bias Source</option>
          <option value="alert_tier">Alert Tier</option>
          <option value="score_bucket">Whale Score Bucket</option>
          <option value="unusualness_bucket">Unusualness Bucket</option>
        </select></label>
        <button id="refresh">Refresh</button>
        <span class="muted" id="updated">Waiting</span>
      </div>
    </section>
    <section class="card">
      <div class="stats" id="stats"></div>
    </section>
    <section class="card">
      <h2 id="tableTitle">Performance</h2>
      <div class="table-wrap" id="table"></div>
    </section>
  </main>
  <script>
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
    const pct = (v) => v === null || v === undefined ? 'Not enough data' : `${(Number(v) * 100).toFixed(1)}%`;
    const num = (v) => v === null || v === undefined ? '' : Number(v).toLocaleString();
    const move = (v) => v === null || v === undefined ? '' : `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(4)}%`;
    async function load() {
      const group = document.getElementById('group').value;
      const r = await fetch(`/api/outcomes?group=${encodeURIComponent(group)}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.statusText);
      render(data, group);
    }
    function stat(label, value, cls='') { return `<div class="stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div></div>`; }
    function render(data, group) {
      const o = data.overall || {};
      document.getElementById('updated').textContent = `Updated ${new Date().toLocaleTimeString()} | ${esc(data.source_path || '')}`;
      document.getElementById('stats').innerHTML = [
        stat('Unique Alerts', num(data.unique_alert_count), 'teal'),
        stat('Completed', num(o.completed || 0), 'good'),
        stat('Pending', num(o.pending || 0), 'warn'),
        stat('Insufficient Close', num(o.insufficient_future_session || 0), 'warn'),
        stat('Dirty Ignored', num(o.dirty_completed_ignored || 0), 'bad'),
        stat('Favorable Rate', pct(o.favorable_rate), 'teal'),
      ].join('');
      const rows = ((data.groups || {})[group] || []);
      document.getElementById('tableTitle').textContent = `${group.replaceAll('_', ' ')} performance`;
      if (!rows.length) { document.getElementById('table').innerHTML = '<div class="empty">No outcome data yet.</div>'; return; }
      document.getElementById('table').innerHTML = `<table><thead><tr>
        <th>Key</th><th>Count</th><th>Completed</th><th>Pending</th><th>Insufficient</th><th>Dirty</th><th>Favorable Rate</th><th>Avg Fav Move</th><th>Avg Score</th>
      </tr></thead><tbody>${rows.map(row => `<tr>
        <td><span class="pill">${esc(row.key)}</span></td><td>${num(row.count)}</td><td>${num(row.completed)}</td><td>${num(row.pending)}</td><td>${num(row.insufficient_future_session)}</td><td>${num(row.dirty_completed_ignored)}</td><td>${pct(row.favorable_rate)}</td><td>${move(row.average_max_favorable_move_pct)}</td><td>${esc(row.average_whale_score ?? '')}</td>
      </tr>`).join('')}</tbody></table>`;
    }
    document.getElementById('refresh').addEventListener('click', load);
    document.getElementById('group').addEventListener('change', load);
    load().catch(err => { document.getElementById('table').innerHTML = `<div class="empty">${esc(err.message)}</div>`; });
    setInterval(() => load().catch(() => {}), 15000);
  </script>
</body>
</html>
"""


def outcome_payload(group: str = "symbol_flow_bias", path: Path = OUTCOMES_PATH) -> Dict[str, Any]:
    report = summarize_outcome_file(path, min_completed=0)
    report["selected_group"] = group
    return report


class OutcomeDashboardHandler(BaseHTTPRequestHandler):
    server_version = "OptionsOutcomeDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, data: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self) -> None:
        payload = HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_html()
            elif parsed.path == "/api/outcomes":
                group = parse_qs(parsed.query).get("group", ["symbol_flow_bias"])[0]
                self.send_json(outcome_payload(group))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> int:
    parser = argparse.ArgumentParser(description="Browser dashboard for options whale outcome stats.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8775)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), OutcomeDashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Outcome dashboard running at {url}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Outcome dashboard stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
