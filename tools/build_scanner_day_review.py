"""Build a source-backed daily scanner review from exported scanner logs."""
from __future__ import annotations

import html
import json
import os
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import requests
import seaborn as sns

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "exports/2026-06-22-scanner-review"
OUT = ROOT / "exports/2026-06-22-scanner-analysis"

TOKENS = {"surface": "#FCFCFD", "panel": "#FFFFFF", "ink": "#1F2430", "muted": "#6F768A", "grid": "#E6E8F0", "axis": "#D7DBE7"}
BLUE = {"base": "#A3BEFA", "dark": "#2E4780", "light": "#CEDFFE"}
ORANGE = {"base": "#F0986E", "dark": "#804126", "light": "#FFBDA1"}
OLIVE = {"base": "#A3D576", "dark": "#386411", "light": "#BEEB96"}
NEUTRAL = {"base": "#C5CAD3", "dark": "#464C55", "light": "#E2E5EA"}


def rows(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def theme():
    sns.set_theme(style="whitegrid", rc={
        "figure.facecolor": TOKENS["surface"], "axes.facecolor": TOKENS["panel"],
        "axes.edgecolor": TOKENS["axis"], "axes.labelcolor": TOKENS["ink"],
        "axes.spines.top": False, "axes.spines.right": False, "grid.color": TOKENS["grid"],
        "grid.linewidth": .8, "font.family": "sans-serif", "patch.linewidth": 1.0,
    })


def header(fig, ax, title, subtitle):
    fig.subplots_adjust(top=.77, left=.13, right=.96, bottom=.14)
    fig.text(ax.get_position().x0, .97, title, ha="left", va="top", fontsize=13, fontweight="semibold", color=TOKENS["ink"])
    fig.text(ax.get_position().x0, .92, subtitle, ha="left", va="top", fontsize=9, color=TOKENS["muted"])
    sns.despine(ax=ax)


def save(fig, name):
    path = OUT / name
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return path.name


def direction(label: str) -> str | None:
    lower = str(label or "").lower()
    if "bullish" in lower:
        return "BULLISH"
    if "bearish" in lower:
        return "BEARISH"
    return None


def stock_bars(symbols, start, end):
    load_env()
    headers = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
    result = {}
    for symbol in sorted(symbols):
        response = requests.get(
            f"{os.environ.get('ALPACA_OPTIONS_DATA_BASE_URL', 'https://data.alpaca.markets')}/v2/stocks/{symbol}/bars",
            headers=headers,
            params={"timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(), "feed": os.environ.get("ALPACA_STOCK_FEED", "sip"), "limit": 10000},
            timeout=20,
        )
        response.raise_for_status()
        frame = pd.DataFrame(response.json().get("bars", []))
        if not frame.empty:
            frame["t"] = pd.to_datetime(frame["t"], utc=True)
            result[symbol] = frame.sort_values("t")
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    scans = pd.DataFrame(rows(SOURCE / "options_whale_scans.jsonl"))
    alert_rows = rows(SOURCE / "options_whale_alerts.jsonl")
    scans["timestamp"] = pd.to_datetime(scans["timestamp"], utc=True)
    scans = scans.loc[scans["timestamp"].dt.date.astype(str) == "2026-06-22"].copy()

    events = []
    seen = set()
    for record in alert_rows:
        detected = pd.Timestamp(record.get("scanner_detected_time"), tz="UTC")
        if detected.strftime("%Y-%m-%d") != "2026-06-22":
            continue
        candidate = record.get("candidate") or {}
        key = (candidate.get("option_symbol"), candidate.get("trade_time"))
        if key in seen:
            continue
        seen.add(key)
        flow_direction = direction(candidate.get("direction_label"))
        events.append({
            "detected": detected, "symbol": candidate.get("underlying_symbol"), "option": candidate.get("option_symbol"),
            "freshness": candidate.get("fresh_flow_label"), "direction": flow_direction,
            "premium": float(candidate.get("estimated_premium") or 0), "score": float(record.get("whale_score") or candidate.get("whale_score") or 0),
            "base_price": float(candidate.get("underlying_price") or 0), "delay_min": float(candidate.get("trade_print_age_minutes") or 0),
            "dte": candidate.get("dte"), "repeat_records": 1,
        })
    events = pd.DataFrame(events)
    all_alerts = pd.DataFrame(alert_rows)
    alerts_today = all_alerts[all_alerts["scanner_detected_time"].str.startswith("2026-06-22")].copy()
    events["freshness_group"] = events["freshness"].replace({"fresh premium print": "Fresh", "old trade print": "Delayed", "stale / old premium print": "Stale"})

    market_open = pd.Timestamp("2026-06-22T13:30:00Z")
    market_close = pd.Timestamp("2026-06-22T20:00:00Z")
    bars = stock_bars(events["symbol"].dropna().unique(), market_open, market_close)
    performance = []
    for item in events.to_dict("records"):
        frame = bars.get(item["symbol"])
        if frame is None or item["direction"] is None or item["base_price"] <= 0:
            continue
        row = dict(item)
        for minutes in (5, 15, 30, 60):
            target = item["detected"] + pd.Timedelta(minutes=minutes)
            if target > market_close:
                row[f"move_{minutes}m"] = None
                continue
            after = frame.loc[frame["t"] >= target]
            if after.empty:
                row[f"move_{minutes}m"] = None
                continue
            close = float(after.iloc[0]["c"])
            raw = (close - item["base_price"]) / item["base_price"] * 100
            row[f"move_{minutes}m"] = raw if item["direction"] == "BULLISH" else -raw
        performance.append(row)
    perf = pd.DataFrame(performance)
    perf.to_csv(OUT / "event_outcomes.csv", index=False)

    # Chart 1: average scan output by hour, an honest discrete comparison rather than a thin trend.
    theme()
    scans["hour_et"] = scans["timestamp"].dt.tz_convert("America/New_York").dt.strftime("%-I %p")
    hourly = scans.groupby("hour_et", sort=False)[["fresh_count", "stale_count"]].mean().reset_index()
    fig, ax = plt.subplots(figsize=(9, 4.8))
    x = range(len(hourly))
    ax.bar(x, hourly["fresh_count"], color=BLUE["base"], edgecolor=BLUE["dark"], label="Fresh")
    ax.bar(x, hourly["stale_count"], bottom=hourly["fresh_count"], color=NEUTRAL["light"], edgecolor=NEUTRAL["dark"], label="Stale / old")
    ax.set_xticks(list(x), hourly["hour_et"]); ax.set_ylabel("Average results per scan")
    ax.legend(frameon=False, ncol=2, loc="upper left"); header(fig, ax, "Average scan output by session hour", "354 scans on June 22; results are scanner rows, not independent trade events.")
    scan_chart = save(fig, "scan_output_by_hour.png")

    # Chart 2: event freshness.
    theme()
    freshness = events.groupby("freshness_group").size().reindex(["Fresh", "Delayed", "Stale"], fill_value=0).reset_index(name="events")
    colors = [BLUE["base"], ORANGE["base"], NEUTRAL["base"]]
    edges = [BLUE["dark"], ORANGE["dark"], NEUTRAL["dark"]]
    fig, ax = plt.subplots(figsize=(8, 4.8))
    bars_plot = ax.barh(freshness["freshness_group"], freshness["events"], color=colors, edgecolor=edges)
    for bar in bars_plot: ax.text(bar.get_width()+2, bar.get_y()+bar.get_height()/2, f"{bar.get_width():.0f}", va="center", color=TOKENS["ink"])
    ax.set_xlim(0, max(freshness["events"])*1.18); ax.set_xlabel("Unique option trade events")
    header(fig, ax, "Freshness of distinct option-flow events", "221 unique option-symbol + trade-time events; first detection only.")
    freshness_chart = save(fig, "event_freshness.png")

    # Chart 3: underlying directional hit rate at 15 minutes, only eligible observations.
    theme()
    eligible = perf.dropna(subset=["move_15m"]).copy()
    hit = eligible.assign(hit=eligible["move_15m"] > 0).groupby("freshness_group").agg(events=("hit", "size"), hit_rate=("hit", "mean"), mean_move=("move_15m", "mean")).reindex(["Fresh", "Delayed", "Stale"]).dropna().reset_index()
    fig, ax = plt.subplots(figsize=(8, 4.8))
    b = ax.barh(hit["freshness_group"], hit["hit_rate"]*100, color=[BLUE["base"], ORANGE["base"], NEUTRAL["base"]][:len(hit)], edgecolor=[BLUE["dark"], ORANGE["dark"], NEUTRAL["dark"]][:len(hit)])
    for bar, (_, r) in zip(b, hit.iterrows()): ax.text(bar.get_width()+1, bar.get_y()+bar.get_height()/2, f"{bar.get_width():.0f}% (n={r['events']:.0f})", va="center", color=TOKENS["ink"])
    ax.set_xlim(0, 115); ax.xaxis.set_major_formatter(mticker.PercentFormatter()); ax.set_xlabel("Directionally favorable after 15 minutes")
    header(fig, ax, "15-minute underlying direction by alert freshness", "Eligible events only; no post-close windows; each event is weighted equally.")
    outcome_chart = save(fig, "outcome_hit_rate_15m.png")

    # Chart 4: event concentration by symbol.
    theme()
    symbols = events.groupby("symbol").size().sort_values(ascending=False).head(8).sort_values().reset_index(name="events")
    fig, ax = plt.subplots(figsize=(9, 5))
    bars_plot = ax.barh(symbols["symbol"], symbols["events"], color=ORANGE["base"], edgecolor=ORANGE["dark"])
    for bar in bars_plot: ax.text(bar.get_width()+1, bar.get_y()+bar.get_height()/2, f"{bar.get_width():.0f}", va="center", color=TOKENS["ink"])
    ax.set_xlim(0, symbols["events"].max()*1.18); ax.set_xlabel("Unique events")
    header(fig, ax, "Option-flow events were concentrated in a few symbols", "Top eight underlyings, first detection per option trade event.")
    concentration_chart = save(fig, "event_concentration.png")

    totals = {
        "scans": len(scans), "contracts": int(scans["contracts_evaluated"].sum()), "records": len(alerts_today),
        "events": len(events), "fresh": int((events["freshness_group"] == "Fresh").sum()),
        "delayed": int((events["freshness_group"] == "Delayed").sum()), "stale": int((events["freshness_group"] == "Stale").sum()),
        "duplicates": len(alerts_today) - len(events), "symbols": events["symbol"].nunique(),
        "pass_rate": scans["passed_filter_count"].sum() / scans["contracts_evaluated"].sum() * 100,
        "quote_rejects": int(scans["stale_quote_rejection_count"].sum()),
    }
    overall_15 = eligible["move_15m"]
    overall_hit = float((overall_15 > 0).mean() * 100) if len(overall_15) else None
    top_symbols = events["symbol"].value_counts().head(3)
    concentration_share = top_symbols.sum() / len(events) * 100
    summary = {
        "totals": totals,
        "outcome_15m": {"eligible_events": len(eligible), "hit_rate_pct": overall_hit, "mean_signed_move_pct": float(overall_15.mean()) if len(overall_15) else None},
        "freshness_15m": hit.to_dict("records"),
        "top_symbols": top_symbols.to_dict(),
        "top3_event_share_pct": concentration_share,
        "source": "2026-06-22 scanner review archive and Alpaca SIP one-minute stock bars",
    }
    (OUT / "analysis_summary.json").write_text(json.dumps(summary, indent=2))

    def pct(v): return "—" if v is None else f"{v:.1f}%"
    freshness_rows = "".join(f"<tr><td>{html.escape(str(r['freshness_group']))}</td><td>{int(r['events'])}</td><td>{r['hit_rate']*100:.1f}%</td><td>{r['mean_move']:+.3f}%</td></tr>" for _, r in hit.iterrows())
    report = f"""<!doctype html><html><head><meta charset='utf-8'><title>June 22 Scanner Review</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:980px;margin:0 auto;padding:36px 20px;color:#1F2430;background:#FCFCFD;line-height:1.55}} h1{{font-size:30px;margin-bottom:4px}} h2{{margin-top:38px}} .muted{{color:#6F768A}} .summary{{background:#fff;border:1px solid #E6E8F0;padding:18px 24px;border-radius:8px}} img{{width:100%;background:white;margin:12px 0 6px}} table{{border-collapse:collapse;width:100%;background:white}} th,td{{padding:9px 12px;border-bottom:1px solid #E6E8F0;text-align:left}} th{{background:#F4F5F7}} .callout{{border-left:4px solid #5477C4;padding:10px 16px;background:#fff;margin:16px 0}} </style></head><body>
<h1>June 22 Options Whale Scanner Review</h1><p class='muted'>Full regular session: 9:17 AM–4:00 PM ET • Source: scanner archive matched against live logs; outcome checks use Alpaca SIP one-minute underlying bars.</p>
<h2>Executive Summary</h2><div class='summary'><ul>
<li><strong>The scanner was operationally reliable.</strong> It completed {totals['scans']} scans, evaluated {totals['contracts']:,} contracts, and stayed in the regular session for 351 of 354 scans.</li>
<li><strong>The signal stream was too noisy to treat as 382 separate alerts.</strong> Those records collapse to {totals['events']} distinct option-flow events; {totals['duplicates']} records ({totals['duplicates']/totals['records']*100:.0f}%) were re-observations of an already-seen trade event.</li>
<li><strong>Freshness is the most actionable quality split.</strong> {totals['fresh']} events were fresh at detection, while {totals['delayed'] + totals['stale']} were delayed or stale. The 15-minute direction check is available for {len(eligible)} events and should be used as a monitoring metric—not proof of profitability.</li>
<li><strong>Concentration needs a portfolio-style control.</strong> {', '.join(top_symbols.index)} generated {concentration_share:.0f}% of all distinct events, so repeated flow in a few names can dominate the user experience.</li>
</ul></div>
<h2>What the scanner did right</h2><p><strong>It protected the live feed from poor quotes and thin setups.</strong> Only {totals['pass_rate']:.2f}% of evaluated contracts passed all configured filters, and {totals['quote_rejects']:,} candidate evaluations were rejected for stale quotes. That selectivity is valuable: it means the scanner is not simply surfacing every large option print.</p>
<p><strong>It retained useful evidence to audit each decision.</strong> The archive records trade age, quote freshness, price context, score components, and next-day OI status. That is exactly the information needed to learn from outcomes rather than tune by hunch.</p>
<img src='{scan_chart}' alt='Average scan output by hour'><p class='muted'>Fresh and stale results stayed visible separately. This is appropriate, but stale results should not compete with new alerts for attention.</p>
<h2>Where the experience broke down</h2><p><strong>Repeated observations inflate apparent alert volume.</strong> Distinct-event deduplication removes {totals['duplicates']} of {totals['records']} records. The same contracts were re-surfaced across scans, especially in AMD, GOOGL, and AMZN. A user needs an event-level alert plus an update when premium/follow-through meaningfully changes—not a fresh notification for each rescan.</p>
<img src='{concentration_chart}' alt='Event concentration by symbol'><p><strong>Signal age needs to be the first visual hierarchy.</strong> Delayed and stale flow represented {totals['delayed'] + totals['stale']} of {totals['events']} unique events. These may still be research-worthy, but they should be separated from actionable, newly printed flow.</p>
<img src='{freshness_chart}' alt='Event freshness'><p><strong>Outcome interpretation is necessarily incomplete for late-day events.</strong> Post-close windows are excluded rather than marked wrong. The built-in outcome review correctly identified 59 late-session observations with no eligible regular-session future window.</p>
<h2>Directional follow-through: a useful but provisional scorecard</h2><p>The chart measures whether the underlying moved in the scanner's indicated direction 15 minutes after first detection. It uses the first one-minute close at or after the 15-minute mark and excludes events that could not have a full window before 4:00 PM ET. Events are not independent—several share the same underlying and market regime—so this is diagnostic evidence, not a backtest or a trading result.</p>
<img src='{outcome_chart}' alt='15 minute directional hit rate'><table><thead><tr><th>Freshness</th><th>Eligible events</th><th>15m favorable</th><th>Mean signed move</th></tr></thead><tbody>{freshness_rows}</tbody></table>
<div class='callout'><strong>Interpretation:</strong> The relevant next comparison is fresh versus delayed/stale <em>after event-level deduplication</em>, across multiple sessions. One day cannot establish a threshold change safely.</div>
<h2>Recommended next steps</h2><ol><li><strong>Switch notification identity to an event key:</strong> option symbol + trade time + direction. Suppress repeats for 10–15 minutes; issue an update only for material premium, score, or follow-through changes.</li><li><strong>Make fresh flow the default notification lane:</strong> keep delayed/stale flow on the dashboard or in a digest unless it clears a much higher evidence threshold.</li><li><strong>Add per-symbol and per-direction budgets:</strong> cap simultaneous notifications from one underlying and group related strikes/legs into a single flow cluster.</li><li><strong>Promote outcome tracking to the scorecard:</strong> track 5/15/30/60-minute signed return, maximum favorable/adverse excursion, and next-day OI confirmation by freshness, score band, and symbol.</li></ol>
<h2>Further questions</h2><p>Does fresh flow outperform delayed/stale flow across at least 20 regular sessions? Do repeated strike clusters add incremental information after the first alert? Which score components separate favorable from unfavorable outcomes once same-symbol correlation is controlled?</p>
<h2>Caveats and assumptions</h2><ul><li>This review covers June 22 only. It evaluates underlying direction, not option P&amp;L, fill quality, or a trading strategy.</li><li>Option flow can be hedging, spreads, rolls, or closing activity; direction labels remain probabilistic until next-day OI confirmation.</li><li>The 15-minute outcome window is unavailable for alerts too close to market close. No after-hours price action was used.</li></ul>
</body></html>"""
    (OUT / "report.html").write_text(report)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
