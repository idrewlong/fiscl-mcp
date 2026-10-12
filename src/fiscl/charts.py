"""Chart data for the show_chart tool, and the MCP Apps view that renders it.

The view is plain HTML/SVG/JS with no external requests, so it works under the host's default
Content-Security-Policy. It speaks the MCP Apps postMessage protocol (spec 2026-01-26):
ui/initialize -> ui/notifications/initialized, then renders ui/notifications/tool-result.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import analysis
from .guard import GuardError, choice
from .recurring import add_months

CHART_URI = "ui://fiscl/chart.html"
KINDS = (
    "spending_by_category",
    "spending_by_month",
    "cash_flow",
    "forecast",
    "budget",
    "net_worth",
    "category_trend",
)


def build_chart(
    conn, cfg, kind: str, from_=None, to=None, month=None, days: int = 30, scope=None, category=None, today=None
) -> dict[str, Any]:
    choice(kind, KINDS, "kind")
    cur = cfg.currency
    if kind == "spending_by_category":
        data = analysis.spending_summary(
            conn,
            cfg,
            from_=from_,
            to=to,
            group_by="category",
            compare_to="none",
            top_n=12,
            scope=scope or "all",
            today=today,
        )
        groups = data["groups"]
        chart = {
            "type": "hbar",
            "title": "Spending by category",
            "subtitle": f"{data['meta']['period']['from']} to {data['meta']['period']['to']} · "
            f"total {data['total_spent']['display']}",
            "labels": [g["name"] for g in groups],
            "series": [{"name": "Spent", "values": [g["spent"]["minor"] for g in groups]}],
        }
        summary = f"Total spending {data['total_spent']['display']}; top: " + ", ".join(
            f"{g['name']} {g['spent']['display']}" for g in groups[:5]
        )
    elif kind == "spending_by_month":
        if not from_:
            end = date.fromisoformat(to) if to else (today or date.today())
            from_ = add_months(end.replace(day=1), -5).isoformat()  # six calendar months
        data = analysis.spending_summary(
            conn, cfg, from_=from_, to=to, group_by="month", compare_to="none", scope=scope or "all", today=today
        )
        groups = data["groups"]
        chart = {
            "type": "bar",
            "title": "Spending by month",
            "subtitle": f"total {data['total_spent']['display']}",
            "labels": [g["name"] for g in groups],
            "series": [{"name": "Spent", "values": [g["spent"]["minor"] for g in groups]}],
        }
        summary = ", ".join(f"{g['name']}: {g['spent']['display']}" for g in groups)
    elif kind == "cash_flow":
        data = analysis.cash_flow(conn, cfg, from_=from_, to=to, scope=scope or "all", today=today)
        periods = data["periods"]
        chart = {
            "type": "bar",
            "title": "Income vs spending",
            "subtitle": f"net {data['totals']['net']['display']} · savings rate "
            f"{data['totals']['savings_rate_pct'] or '—'}%",
            "labels": [p["period"] for p in periods],
            "series": [
                {"name": "Income", "values": [p["income"]["minor"] for p in periods]},
                {"name": "Spending", "values": [p["spending"]["minor"] for p in periods]},
            ],
        }
        summary = "; ".join(
            f"{p['period']}: in {p['income']['display']}, out {p['spending']['display']}" for p in periods
        )
    elif kind == "forecast":
        data = analysis.forecast_cash(conn, cfg, days=days, scope=scope or "personal", today=today)
        pts = data["daily_balance"]
        chart = {
            "type": "line",
            "title": f"Projected balance, next {days} days",
            "subtitle": f"lowest {data['lowest_balance']['amount']['display']} on "
            f"{data['lowest_balance']['date']} · safe to spend {data['safe_to_spend']['display']}",
            "labels": [p["date"][5:] for p in pts],
            "series": [{"name": "Balance", "values": [p["balance"] for p in pts]}],
            "zero_line": True,
        }
        summary = (
            f"Lowest projected balance {data['lowest_balance']['amount']['display']} on "
            f"{data['lowest_balance']['date']}; ends at {data['projected_end_balance']['display']}."
        )
    elif kind == "net_worth":
        data = analysis.net_worth(conn, cfg, today=today)
        net = data["net_worth"]["minor"]
        change = next((c for c in data["change_vs"] if c["days"] == 30), None)
        subtitle = f"as of {data['as_of']} · net worth {data['net_worth']['display']}"
        if change:
            subtitle += f" · accounts {change['change']['display']} vs 30 days ago"
        chart = {
            "type": "bar",
            "title": "Net worth",
            "subtitle": subtitle,
            "labels": ["Assets", "Liabilities", "Net worth"],
            "series": [
                {
                    "name": "Amount",
                    "values": [data["total_assets"]["minor"], data["total_liabilities"]["minor"], net],
                }
            ],
        }
        summary = (
            f"Net worth {data['net_worth']['display']}: assets {data['total_assets']['display']}, "
            f"liabilities {data['total_liabilities']['display']}."
        )
        if data["assets"]:
            summary += " Largest assets: " + ", ".join(
                f"{a['name']} {a['amount']['display']}" for a in data["assets"][:3]
            )
    elif kind == "category_trend":
        data = analysis.category_trend(conn, cfg, category=category, scope=scope or "all", today=today)
        months = data["months"]
        median = data["trend"]["median_last_3_complete_months"]
        chart = {
            "type": "bar",
            "title": f"{data['category']} by month",
            "subtitle": f"trend {data['trend']['direction']} · last month partial"
            + (f" · 3-month median {median['display']}" if median else ""),
            "labels": [m["month"] for m in months],
            "series": [{"name": "Spent", "values": [m["spent"]["minor"] for m in months]}],
        }
        if median:
            chart["reference"] = {"name": "3-month median", "value": median["minor"]}
        summary = f"{data['category']}: trend {data['trend']['direction']}, " + ", ".join(
            f"{m['month']} {m['spent']['display']}" for m in months[-6:]
        )
        if data["budget"]:
            summary += f"; budget {data['budget']['monthly']['display']}/month"
    else:  # budget
        data = analysis.budget_status(conn, cfg, month=month, today=today)
        lines = data["budgets"]
        if not lines:
            raise GuardError("no budgets set yet; add some to budgets.toml or ask for suggest_budgets")
        chart = {
            "type": "hbar",
            "title": f"Budgets for {data['month']}",
            "subtitle": f"day {data['days_elapsed']} of {data['days_in_month']} · "
            f"{data['totals']['spent']['display']} of {data['totals']['budget']['display']}",
            "labels": [b["category"] for b in lines],
            "series": [
                {"name": "Budget", "values": [b["budget"]["minor"] for b in lines]},
                {"name": "Spent", "values": [b["spent"]["minor"] for b in lines]},
            ],
        }
        summary = "; ".join(
            f"{b['category']}: {b['spent']['display']} of {b['budget']['display']} ({b['status']})" for b in lines
        )
    if scope and scope != "all" and kind not in ("budget", "net_worth"):
        chart["title"] += f" ({scope})"
    chart["currency"] = cur
    return {"chart": chart, "summary": summary, "meta": data["meta"]}


CHART_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'">
<title>fiscl chart</title>
<style>
:root{--bg:var(--color-background-primary,#fff);--fg:var(--color-text-primary,#1a1a1a);
--muted:var(--color-text-secondary,#6b6b6b);--grid:rgba(127,127,127,.18);
--s1:#2f6fdd;--s2:#e0803a;--neg:#d23c3c;font-family:var(--font-sans,system-ui,-apple-system,sans-serif)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:var(--color-background-primary,#1b1b1b);
--fg:var(--color-text-primary,#f1f1f1);--muted:var(--color-text-secondary,#a3a3a3);--s1:#6d9cf0;--s2:#f0a066}}
:root[data-theme=dark]{--bg:var(--color-background-primary,#1b1b1b);--fg:var(--color-text-primary,#f1f1f1);
--muted:var(--color-text-secondary,#a3a3a3);--s1:#6d9cf0;--s2:#f0a066}
body{margin:0;background:var(--bg);color:var(--fg)}#wrap{padding:14px 16px}
h1{font-size:15px;margin:0 0 2px;font-weight:600}p.sub{margin:0 0 10px;color:var(--muted);font-size:12px}
.legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-bottom:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
svg{display:block;width:100%;height:auto;overflow:visible}text{fill:var(--muted);font-size:11px}
.tip{position:fixed;pointer-events:none;background:var(--fg);color:var(--bg);font-size:12px;padding:4px 7px;
border-radius:4px;opacity:0;transition:opacity .1s}#empty{color:var(--muted);font-size:13px}
</style></head><body><div id="wrap"><div id="empty">Waiting for chart data…</div></div><div class="tip" id="tip"></div>
<script>
(() => {
  const wrap = document.getElementById('wrap'), tip = document.getElementById('tip');
  let nextId = 1, cur = 'USD';
  const send = m => window.parent.postMessage(Object.assign({jsonrpc: '2.0'}, m), '*');
  const money = (minor, compact) => {
    const v = minor / 100;
    const short = compact && Math.abs(v) >= 1000;
    try { return new Intl.NumberFormat(undefined, {style: 'currency', currency: cur,
      notation: short ? 'compact' : 'standard', maximumFractionDigits: short ? 1 : (compact ? 0 : 2)}).format(v); }
    catch { return (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2); }
  };
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const colors = ['var(--s1)', 'var(--s2)'];
  // Labels come from bank data: set tooltips as text, never as HTML.
  const showTip = (e, text) => { tip.textContent = text; tip.style.opacity = 1;
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 160) + 'px'; tip.style.top = (e.clientY + 12) + 'px'; };
  const hideTip = () => { tip.style.opacity = 0; };
  function niceMax(v) { if (v <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p; return 10 * p; }

  function render(c) {
    cur = c.currency || 'USD';
    const series = (c.series || []).slice(0, 2), labels = c.labels || [];
    let h = `<h1>${esc(c.title || '')}</h1>`;
    if (c.subtitle) h += `<p class="sub">${esc(c.subtitle)}</p>`;
    if (series.length > 1) h += '<div class="legend">' + series.map((s, i) =>
      `<span><i style="background:${colors[i]}"></i>${esc(s.name)}</span>`).join('') + '</div>';
    if (!labels.length) { wrap.innerHTML = h + '<div id="empty">No data for this period.</div>'; return; }
    wrap.innerHTML = h + (c.type === 'hbar' ? hbar(c, series, labels) : vchart(c, series, labels));
    wrap.querySelectorAll('[data-tip]').forEach(el => {
      el.addEventListener('mousemove', e => showTip(e, el.dataset.tip)); el.addEventListener('mouseleave', hideTip); });
    reportSize();
  }

  function hbar(c, series, labels) {
    const W = 640, rowH = series.length > 1 ? 30 : 24, padL = 130, padR = 70, H = labels.length * rowH + 8;
    const max = niceMax(Math.max(1, ...series.flatMap(s => s.values)));
    let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(c.title)}">`;
    labels.forEach((lab, i) => {
      const y = i * rowH + 4, bh = (rowH - 8) / series.length;
      s += `<text x="${padL - 8}" y="${y + rowH / 2}" text-anchor="end" dominant-baseline="middle">${esc(lab.length > 20 ? lab.slice(0, 19) + '…' : lab)}</text>`;
      series.forEach((ser, k) => {
        const v = Math.max(0, ser.values[i] || 0), w = (W - padL - padR) * v / max;
        s += `<rect x="${padL}" y="${y + k * bh}" width="${Math.max(w, 1)}" height="${bh - 2}" rx="2" fill="${colors[k]}"
          data-tip="${esc(lab)} · ${esc(ser.name)}: ${esc(money(ser.values[i] || 0))}"/>`;
        if (k === series.length - 1) s += `<text x="${padL + w + 6}" y="${y + k * bh + bh / 2}" dominant-baseline="middle">${esc(money(v, true))}</text>`;
      });
    });
    return s + '</svg>';
  }

  function vchart(c, series, labels) {
    const W = 640, H = 260, padL = 58, padR = 10, padT = 10, padB = 34;
    const all = series.flatMap(s => s.values);
    const ref = c.reference && Number.isFinite(c.reference.value) ? c.reference : null;
    if (ref && c.type !== 'line') all.push(ref.value);
    let lo, hi;
    if (c.type === 'line') {
      // Fit the data so small movements are visible; include zero only if the data gets close to it.
      const mn = Math.min(...all), mx = Math.max(...all), pad = Math.max((mx - mn) * 0.15, Math.abs(mx) * 0.02, 100);
      const step = niceMax((mx - mn + 2 * pad) / 4) || 1;
      lo = Math.floor((mn - pad) / step) * step; hi = Math.ceil((mx + pad) / step) * step;
      if (lo > 0 && lo < step * 2) lo = 0;
    } else {
      lo = Math.min(0, ...all); hi = niceMax(Math.max(1, ...all));
      if (lo < 0) lo = -niceMax(-lo);
    }
    const y = v => padT + (H - padT - padB) * (hi - v) / (hi - lo);
    const iw = (W - padL - padR) / labels.length;
    let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(c.title)}">`;
    for (let t = 0; t <= 4; t++) {
      const v = lo + (hi - lo) * t / 4, yy = y(v);
      s += `<line x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}" stroke="var(--grid)"/>`;
      s += `<text x="${padL - 6}" y="${yy}" text-anchor="end" dominant-baseline="middle">${esc(money(v, true))}</text>`;
    }
    if (lo < 0 || (c.zero_line && lo === 0)) s += `<line x1="${padL}" x2="${W - padR}" y1="${y(0)}" y2="${y(0)}" stroke="var(--muted)" stroke-dasharray="3 3"/>`;
    const step = Math.ceil(labels.length / 10);
    labels.forEach((lab, i) => { if (i % step === 0) s += `<text x="${padL + iw * i + iw / 2}" y="${H - padB + 16}" text-anchor="middle">${esc(lab)}</text>`; });
    if (c.type === 'line') {
      series.forEach((ser, k) => {
        const pts = ser.values.map((v, i) => `${padL + iw * i + iw / 2},${y(v)}`).join(' ');
        s += `<polyline points="${pts}" fill="none" stroke="${colors[k]}" stroke-width="2"/>`;
        ser.values.forEach((v, i) => {
          s += `<rect x="${padL + iw * i}" y="${padT}" width="${iw}" height="${H - padT - padB}" fill="transparent"
            data-tip="${esc(labels[i])}: ${esc(money(v))}"/>`;
          if (v < 0) s += `<circle cx="${padL + iw * i + iw / 2}" cy="${y(v)}" r="2.5" fill="var(--neg)"/>`;
        });
      });
    } else {
      const bw = Math.max(2, (iw * 0.72) / series.length);
      labels.forEach((lab, i) => series.forEach((ser, k) => {
        const v = ser.values[i] || 0, x = padL + iw * i + (iw - bw * series.length) / 2 + k * bw;
        const top = y(Math.max(v, 0)), bot = y(Math.min(v, 0));
        s += `<rect x="${x}" y="${top}" width="${bw - 1}" height="${Math.max(bot - top, 1)}" rx="1.5" fill="${colors[k]}"
          data-tip="${esc(lab)} · ${esc(ser.name)}: ${esc(money(v))}"/>`;
      }));
      if (ref) {  // dashed reference line (e.g. a 3-month median); the tooltip is text like every other
        s += `<line x1="${padL}" x2="${W - padR}" y1="${y(ref.value)}" y2="${y(ref.value)}" stroke="var(--s2)" stroke-width="2" stroke-dasharray="5 4"
          data-tip="${esc(ref.name || 'Reference')}: ${esc(money(ref.value))}"/>`;
      }
    }
    return s + '</svg>';
  }

  function applyContext(ctx) {
    if (!ctx) return;
    if (ctx.theme) document.documentElement.dataset.theme = ctx.theme;
    const vars = ctx.styles && ctx.styles.variables;
    if (vars) for (const [k, v] of Object.entries(vars)) if (v) document.documentElement.style.setProperty(k, v);
  }
  function reportSize() {
    const r = document.documentElement.getBoundingClientRect();
    send({method: 'ui/notifications/size-changed', params: {width: Math.ceil(r.width), height: Math.ceil(r.height)}});
  }

  window.addEventListener('message', ev => {
    if (ev.source !== window.parent) return;  // only the host may drive this view
    const m = ev.data || {};
    if (m.id === 1 && m.result) {  // ui/initialize response
      applyContext(m.result.hostContext);
      send({method: 'ui/notifications/initialized', params: {}});
    } else if (m.method === 'ui/notifications/tool-result') {
      const sc = m.params && m.params.structuredContent;
      if (sc && sc.chart) render(sc.chart);
      else wrap.innerHTML = '<div id="empty">No chart data in this result.</div>';
    } else if (m.method === 'ui/notifications/host-context-changed') {
      applyContext(m.params);
    }
  });
  send({id: nextId++, method: 'ui/initialize', params: {protocolVersion: '2026-01-26',
    appInfo: {name: 'fiscl-chart', version: '0.1.0'}, appCapabilities: {availableDisplayModes: ['inline']}}});
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);
})();
</script></body></html>
"""
