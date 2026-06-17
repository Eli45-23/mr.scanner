#!/usr/bin/env python3
"""Patch the main options whale dashboard to hide debug candidates by default.

This is a temporary local patch utility for scanner_dashboard.py. It keeps the
original 8765 scanner page and filters, shows real whale-flow alerts first, and
moves near misses behind a debug button.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scanner_dashboard.py"

OLD = r'''    function renderRows(latest) {
      const official = latest.results || [];
      const near = latest.near_misses || [];
      const useNear = official.length === 0 && near.length > 0;
      latestRows = official;
      if (useNear) {
        els.modeNotice.innerHTML = '<div class="notice warn">Near misses / debug visibility — not alert quality.</div>';
        els.flowRows.innerHTML = near.map((item, idx) => `
          <tr class="clickable" data-kind="near" data-index="${idx}">
            <td></td><td>Near miss</td><td>${esc(item.underlying)}</td><td></td><td></td><td></td><td></td><td></td>
            <td>${intFmt(item.volume)}</td><td>${intFmt(item.open_interest)}</td><td></td><td></td>
            <td>${num(item.spread_percent, '%')}</td><td>${money(item.premium)}</td><td><span class="score">${esc(item.score)}</span></td>
            <td>Not alert quality</td><td>Watch only</td><td>Needs price confirmation</td><td>${esc(item.reason_rejected || (item.thresholds_failed || []).join(', '))}</td>
          </tr>
        `).join('');
        els.rowCount.textContent = `${near.length} near misses`;
        return;
      }
      els.modeNotice.innerHTML = latest.diagnostics?.debug_loose_mode ? '<div class="notice warn">DEBUG LOOSE MODE — not alert quality.</div>' : '';
      els.rowCount.textContent = `${official.length} rows`;
      els.flowRows.innerHTML = official.length ? official.map((item, idx) => {
        const c = item.candidate || {};
        return `<tr class="clickable" data-kind="result" data-index="${idx}">
          <td>${esc(c.time_detected || '')}</td><td>${esc(item.alert_tier || '')}</td><td>${esc(c.underlying_symbol || '')}</td><td>${esc(c.option_type || '')}</td>
          <td>${money(c.strike)}</td><td>${esc(c.expiration || '')}</td><td>${esc(c.dte ?? '')}</td><td>${esc(c.moneyness || '')}</td>
          <td>${intFmt(c.volume)}</td><td>${intFmt(c.open_interest)}</td><td>${esc(c.volume_oi_ratio ?? '')}</td><td>${money(c.last || c.midpoint)}</td>
          <td>${num(c.spread_percent, '%')}</td><td>${money(c.estimated_premium)}</td><td><span class="score">${esc(item.whale_score || 0)}</span></td>
          <td>${esc(item.classification || '')}</td><td>${esc(item.direction_label || '')}</td><td>${esc(item.price_confirmation_label || '')}</td><td>${esc(item.reason_summary || '')}</td>
        </tr>`;
      }).join('') : '<tr><td colspan="19" class="muted">No whale-flow candidates or near-misses yet.</td></tr>';
    }
'''

NEW = r'''    function renderRows(latest) {
      const official = latest.results || [];
      const near = latest.near_misses || [];
      latestRows = official;
      const candidateField = (item, field) => {
        const c = item.candidate || {};
        const value = c[field];
        return value !== undefined && value !== null && value !== '' ? value : item[field];
      };
      const resultRow = (item, idx) => {
        return `<tr class="clickable" data-kind="result" data-index="${idx}">
          <td>${esc(candidateField(item, 'time_detected') || '')}</td><td>${esc(item.alert_tier || '')}</td><td>${esc(candidateField(item, 'underlying_symbol') || '')}</td><td>${esc(candidateField(item, 'option_type') || '')}</td>
          <td>${money(candidateField(item, 'strike'))}</td><td>${esc(candidateField(item, 'expiration') || '')}</td><td>${esc(candidateField(item, 'dte') ?? '')}</td><td>${esc(candidateField(item, 'moneyness') || '')}</td>
          <td>${intFmt(candidateField(item, 'volume'))}</td><td>${intFmt(candidateField(item, 'open_interest'))}</td><td>${esc(candidateField(item, 'volume_oi_ratio') ?? '')}</td><td>${money(candidateField(item, 'last') || candidateField(item, 'midpoint'))}</td>
          <td>${num(candidateField(item, 'spread_percent'), '%')}</td><td>${money(candidateField(item, 'estimated_premium'))}</td><td><span class="score">${esc(item.whale_score || item.score || 0)}</span></td>
          <td>${esc(item.classification || '')}</td><td>${esc(item.direction_label || '')}</td><td>${esc(item.price_confirmation_label || '')}</td><td>${esc(item.reason_summary || '')}</td>
        </tr>`;
      };
      const debugRow = (item, idx) => `
        <tr class="clickable" data-kind="debug" data-index="${idx}">
          <td></td><td>Debug only</td><td>${esc(item.underlying || item.underlying_symbol || '')}</td><td colspan="5" class="muted">Debug candidate — not alert quality</td>
          <td>${intFmt(item.volume)}</td><td>${intFmt(item.open_interest)}</td><td></td><td></td>
          <td>${num(item.spread_percent, '%')}</td><td>${money(item.premium || item.estimated_premium)}</td><td><span class="score">${esc(item.score)}</span></td>
          <td>Not alert quality</td><td>Watch only</td><td>Needs confirmation</td><td>${esc(item.reason_rejected || (item.thresholds_failed || []).join(', '))}</td>
        </tr>`;
      window.__showDebugCandidates = () => {
        els.modeNotice.innerHTML = '<div class="notice warn">Debug Candidates — Not Alerts. Use this only to tune filters, not as whale-flow signals.</div>';
        els.rowCount.textContent = `${near.length} debug candidates`;
        els.flowRows.innerHTML = near.length ? near.map(debugRow).join('') : '<tr><td colspan="19" class="muted">No debug candidates.</td></tr>';
      };
      if (official.length) {
        els.modeNotice.innerHTML = latest.diagnostics?.debug_loose_mode ? '<div class="notice warn">DEBUG LOOSE MODE — not alert quality.</div>' : '<div class="notice good">Real whale-flow alerts. Watch only — not a trade signal.</div>';
        els.rowCount.textContent = `${official.length} real whale alerts`;
        els.flowRows.innerHTML = official.map(resultRow).join('');
        return;
      }
      els.rowCount.textContent = '0 real whale alerts';
      if (near.length) {
        els.modeNotice.innerHTML = `<div class="notice warn">No real whale alerts passed the filters right now. ${near.length} debug candidates are hidden because they are not alert quality. <button type="button" onclick="window.__showDebugCandidates()">Show Debug Candidates</button></div>`;
        els.flowRows.innerHTML = '<tr><td colspan="19" class="muted">No real whale alerts right now. Debug candidates are hidden.</td></tr>';
      } else {
        els.modeNotice.innerHTML = '';
        els.flowRows.innerHTML = '<tr><td colspan="19" class="muted">No real whale alerts yet.</td></tr>';
      }
    }
'''


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    if NEW in text:
        print("Dashboard clarity patch is already applied.")
        return 0
    if OLD not in text:
        raise SystemExit("Could not find the expected renderRows block. Patch not applied.")
    TARGET.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print("Patched scanner_dashboard.py: real alerts first, debug candidates hidden by default.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
