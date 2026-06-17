#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict


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
    header { background:#0f2d35; color:#fff; padding:22px; }
    h1 { margin:0; font-size:24px; }
    .sub { color:#b9d4d9; margin-top:5px; }
    main { padding:20px; display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
    h2 { margin:0 0 8px; font-size:18px; }
    p { color:var(--muted); line-height:1.45; }
    a.button { display:inline-block; background:var(--teal); color:#fff; text-decoration:none; border-radius:9px; padding:10px 12px; font-weight:800; }
    code { background:#eef3f5; padding:2px 5px; border-radius:5px; }
  </style>
</head>
<body>
  <header>
    <h1>Options Whale Workbench</h1>
    <div class="sub">One place to open the scanner dashboard and outcome proof engine.</div>
  </header>
  <main>
    <section class="card">
      <h2>Main Scanner Dashboard</h2>
      <p>Live options whale scanner, flow rows, score components, market context, and scan controls.</p>
      <p><code>{main_url}</code></p>
      <a class="button" href="{main_url}" target="_blank" rel="noreferrer">Open Scanner</a>
    </section>
    <section class="card">
      <h2>Outcome Dashboard</h2>
      <p>Proof engine: completed, pending, insufficient close, dirty ignored, and performance by symbol/flow bias.</p>
      <p><code>{outcome_url}</code></p>
      <a class="button" href="{outcome_url}" target="_blank" rel="noreferrer">Open Outcomes</a>
    </section>
  </main>
</body>
</html>
"""


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "OptionsWhaleWorkbench/1.0"
    main_url = "http://127.0.0.1:8765"
    outcome_url = "http://127.0.0.1:8775"

    def log_message(self, fmt: str, *args):  # type: ignore[no-untyped-def]
        return

    def send_json(self, data: Dict[str, str]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/api/links":
            self.send_json({"main_dashboard": self.main_url, "outcome_dashboard": self.outcome_url})
            return
        html = HTML_TEMPLATE.format(main_url=self.main_url, outcome_url=self.outcome_url)
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a small link page for the options whale scanner workbench.")
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
