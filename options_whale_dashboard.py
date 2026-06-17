#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import elite_momentum_scanner as scanner_app
import scanner_dashboard as dashboard_core

logger = logging.getLogger("options_whale_dashboard")

HTML = """<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Options Whale Scanner</title>
<style>
body{margin:0;background:#071015;color:#edf6f9;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}header{background:#0b151b;border-bottom:1px solid #22343d;position:sticky;top:0;z-index:2}.wrap{width:min(1500px,calc(100vw - 28px));margin:auto}.bar{min-height:70px;display:flex;align-items:center;justify-content:space-between;gap:14px}h1{margin:0;font-size:24px}.sub,.muted{color:#91a4ad}.panel{background:#101b22;border:1px solid #22343d;border-radius:10px}.pad{padding:14px}main{padding:18px 0 32px}.grid{display:grid;grid-template-columns:330px 1fr;gap:14px}.cards{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:10px}.card{background:#13232b;border:1px solid #22343d;border-radius:9px;padding:12px}.label{color:#91a4ad;font-size:12px}.value{font-size:18px;font-weight:800;margin-top:6px;overflow-wrap:anywhere}button{border:1px solid #22343d;background:#13232b;color:#edf6f9;border-radius:8px;min-height:38px;padding:0 12px;font-weight:700;cursor:pointer}button.primary{background:#0f766e;border-color:#0f766e}button:disabled{opacity:.55}.row{display:flex;gap:8px;flex-wrap:wrap}.notice{border:1px solid #7c5a00;background:#201806;color:#ffd271;border-radius:8px;padding:10px;margin-top:10px;font-weight:700}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:1250px}th,td{padding:10px;border-bottom:1px solid #22343d;vertical-align:top;text-align:left}th{color:#91a4ad;background:#0c171d;position:sticky;top:0}.score{font-weight:900}.good{color:#36d399}.mid{color:#fbbf24}.bad{color:#fb7185}pre{white-space:pre-wrap;overflow:auto;background:#081116;border:1px solid #22343d;border-radius:8px;padding:10px;max-height:360px}.hidden{display:none}@media(max-width:1000px){.grid{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
</head>
<body>
<header><div class='wrap bar'><div><h1>Options Whale Scanner</h1><div class='sub'>Full-market options-flow dashboard. Read-only. Not a trade signal.</div></div><div class='row'><button id='runBtn' class='primary'>Run Scan Now</button><button id='pauseBtn'>Pause Auto Scan</button><button id='resumeBtn' class='hidden'>Resume Auto Scan</button></div></div></header>
<main class='wrap'><div class='grid'><aside class='panel pad'><h2>Controls</h2><div class='row'><button id='universeBtn'>Rebuild Universe</button><button id='csvBtn'>Export CSV</button><button id='jsonBtn'>Export JSON</button></div><div class='notice'>Possible whale flow — not a trade signal.</div><h2>Status</h2><div id='sideStatus' class='muted'>Loading...</div><h2>Filters</h2><div id='filters' class='muted'>Loading...</div></aside><div style='display:grid;gap:14px'><section class='panel pad'><div id='cards' class='cards'></div><div id='warning' class='muted' style='margin-top:10px'></div></section><section class='panel'><div class='pad row' style='justify-content:space-between'><h2 style='margin:0'>Whale Flow Table</h2><div id='tableMeta' class='muted'>No scan loaded</div></div><div class='table-wrap'><table><thead><tr><th>Time</th><th>Tier</th><th>Symbol</th><th>Type</th><th>Strike</th><th>Exp</th><th>DTE</th><th>Vol</th><th>OI</th><th>Vol/OI</th><th>Last/Mid</th><th>Spread</th><th>Premium</th><th>Score</th><th>Class</th><th>Direction</th><th>Reason</th></tr></thead><tbody id='tbody'><tr><td colspan='17' class='muted'>Waiting for scan...</td></tr></tbody></table></div></section><section class='panel pad'><h2>Detail</h2><div id='detail' class='muted'>Click a row for details.</div></section></div></div></main>
<script>
const $=id=>document.getElementById(id);let autoScan=true,scanRunning=false,lastStarted=0,rows=[];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,d=2)=>Number.isFinite(Number(v))?Number(v).toLocaleString(undefined,{maximumFractionDigits:d}):'—';
const money=v=>Number.isFinite(Number(v))?'$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:0}):'—';
const tm=v=>v?new Date(v).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});if(!r.ok)throw new Error(path+': '+r.status);return path.endsWith('.csv')?r.text():r.json();}
const cand=r=>r?.candidate||r||{};const scoreClass=s=>Number(s)>=80?'good':Number(s)>=60?'mid':'muted';
function cards(status,data){const d=data?.diagnostics||data||{};const a=[['Alpaca',status?.alpaca_connected?'Connected':'Check'],['Options',status?.options_contracts_available?'Available':'Unavailable'],['Feed',status?.official_options_feed_available?'Official':'Unknown'],['Last Scan',tm(data?.timestamp||d.timestamp)],['Contracts',num(d.contracts_scanned??data?.contracts_scanned,0)],['Candidates',num((data?.results||[]).length,0)],['Near Misses',num((data?.near_misses||[]).length,0)],['Universe',num(status?.universe_size??d.universe_size,0)]];$('cards').innerHTML=a.map(([k,v])=>`<div class='card'><div class='label'>${esc(k)}</div><div class='value'>${esc(v)}</div></div>`).join('');$('warning').textContent=data?.stale_warning||d.partial_scan_warning||status?.data_plan_warning||'';}
function table(data){rows=(data?.results||[]).length?data.results:(data?.near_misses||[]);const label=(data?.results||[]).length?'official candidates':((data?.near_misses||[]).length?'near misses / debug visibility':'no candidates');$('tableMeta').textContent=rows.length+' '+label;if(!rows.length){$('tbody').innerHTML=`<tr><td colspan='17' class='muted'>No whale candidates found yet.</td></tr>`;return;}$('tbody').innerHTML=rows.slice(0,150).map((r,i)=>{const c=cand(r);const sc=r.whale_score??c.whale_score??r.score??c.score;return `<tr data-i='${i}'><td>${esc(tm(r.time_detected||c.time_detected||data?.timestamp))}</td><td>${esc(r.alert_tier||c.alert_tier||'WATCH')}</td><td><b>${esc(c.underlying_symbol)}</b></td><td>${esc(c.option_type)}</td><td>${esc(c.strike)}</td><td>${esc(c.expiration)}</td><td>${esc(c.dte??c.days_to_expiration)}</td><td>${num(c.volume,0)}</td><td>${num(c.open_interest,0)}</td><td>${num(c.volume_oi_ratio,2)}x</td><td>${num(c.last??c.midpoint,2)}</td><td>${num(c.spread_percent,2)}%</td><td>${money(c.estimated_premium)}</td><td class='score ${scoreClass(sc)}'>${num(sc,0)}</td><td>${esc(r.classification||c.classification)}</td><td>${esc(r.direction_label||c.direction_label)}</td><td>${esc(r.reason_summary||c.reason_summary||'Possible whale flow')}</td></tr>`}).join('');[...$('tbody').querySelectorAll('tr')].forEach(tr=>tr.onclick=()=>detail(rows[Number(tr.dataset.i)]));}
function detail(r){$('detail').innerHTML=`<pre>${esc(JSON.stringify(r,null,2))}</pre>`;}
async function refresh(){try{const [s,d,f]=await Promise.all([api('/api/options-whales/status'),api('/api/options-whales/latest'),api('/api/options-whales/filters')]);cards(s,d);table(d);$('sideStatus').innerHTML=`Contracts: <b>${esc(s?.options_contracts_available?'available':'unavailable')}</b><br>Snapshots: <b>${esc(s?.options_snapshots_available?'available':'unavailable')}</b><br>Last error: ${esc(s?.last_error||'None')}`;$('filters').innerHTML=Object.entries(f.filters||{}).slice(0,22).map(([k,v])=>`<div>${esc(k)}: <b>${esc(v)}</b></div>`).join('');}catch(e){$('warning').textContent=e.message;}}
async function scan(){if(scanRunning)return;scanRunning=true;lastStarted=Date.now();$('runBtn').disabled=true;$('runBtn').textContent='Scanning...';try{const d=await api('/api/options-whales/scan');table(d);await refresh();}catch(e){$('warning').textContent=e.message;}finally{scanRunning=false;$('runBtn').disabled=false;$('runBtn').textContent='Run Scan Now';}}
$('runBtn').onclick=scan;$('pauseBtn').onclick=()=>{autoScan=false;$('pauseBtn').classList.add('hidden');$('resumeBtn').classList.remove('hidden')};$('resumeBtn').onclick=()=>{autoScan=true;$('resumeBtn').classList.add('hidden');$('pauseBtn').classList.remove('hidden')};$('universeBtn').onclick=async()=>{await api('/api/options-whales/universe/rebuild',{method:'POST',body:'{}'});refresh()};$('csvBtn').onclick=()=>location.href='/api/options-whales/export.csv';$('jsonBtn').onclick=()=>location.href='/api/options-whales/export.json';
setInterval(refresh,5000);setInterval(()=>{if(autoScan&&!scanRunning&&Date.now()-lastStarted>30000)scan()},5000);refresh();scan();
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    server_version = "OptionsWhaleDashboard/1.0"
    def log_message(self, fmt: str, *args: Any) -> None: logger.info("%s - %s", self.address_string(), fmt % args)
    def read_json(self) -> Dict[str, Any]:
        n=int(self.headers.get("Content-Length","0"));return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n>0 else {}
    def send_json(self, data: Dict[str, Any], status: HTTPStatus=HTTPStatus.OK) -> None:
        p=json.dumps(data,default=str).encode("utf-8");self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(p)));self.end_headers();self.wfile.write(p)
    def send_html(self) -> None:
        p=HTML.encode("utf-8");self.send_response(HTTPStatus.OK);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(p)));self.end_headers();self.wfile.write(p)
    def send_csv(self, text: str) -> None:
        p=text.encode("utf-8");self.send_response(HTTPStatus.OK);self.send_header("Content-Type","text/csv; charset=utf-8");self.send_header("Content-Disposition","attachment; filename=options_whale_history.csv");self.send_header("Content-Length",str(len(p)));self.end_headers();self.wfile.write(p)
    def do_GET(self) -> None:
        try:
            path=urlparse(self.path).path; qs=parse_qs(urlparse(self.path).query)
            if path=="/": self.send_html()
            elif path=="/api/options-whales/status": self.send_json(dashboard_core.options_whales_status())
            elif path=="/api/options-whales/scan": self.send_json(dashboard_core.options_whales_scan())
            elif path=="/api/options-whales/latest": self.send_json(dashboard_core.options_whales_latest())
            elif path=="/api/options-whales/history": self.send_json(dashboard_core.options_whales_history(limit=int(qs.get("limit",["100"])[0])))
            elif path=="/api/options-whales/filters": self.send_json(dashboard_core.options_whales_filters())
            elif path=="/api/options-whales/universe/status": self.send_json(dashboard_core.options_whale_scanner().universe_status())
            elif path=="/api/options-whales/export.json": self.send_json(dashboard_core.options_whales_export_json())
            elif path=="/api/options-whales/export.csv": self.send_csv(dashboard_core.options_whales_export_csv())
            else: self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            logger.exception("Request failed: %s", exc); self.send_json({"error":str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
    def do_POST(self) -> None:
        try:
            path=urlparse(self.path).path; body=self.read_json()
            if path=="/api/options-whales/filters": self.send_json(dashboard_core.update_options_whales_filters(body))
            elif path=="/api/options-whales/universe/rebuild": self.send_json(dashboard_core.options_whale_scanner().rebuild_universe())
            else: self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            logger.exception("Request failed: %s", exc); self.send_json({"error":str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

def main() -> int:
    scanner_app.load_dotenv(); parser=argparse.ArgumentParser(description="Options Whale Scanner dashboard"); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8765); parser.add_argument("--config"); parser.add_argument("--open",action="store_true"); args=parser.parse_args()
    if args.config: dashboard_core.STATE.config_path=dashboard_core.Path(args.config).resolve()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    server=ThreadingHTTPServer((args.host,args.port),Handler); url=f"http://{args.host}:{args.port}"; logger.info("Options Whale Dashboard running at %s", url)
    if args.open: threading.Timer(0.4, lambda:webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: logger.info("Options Whale Dashboard stopped")
    finally: server.server_close()
    return 0

if __name__ == "__main__": raise SystemExit(main())
