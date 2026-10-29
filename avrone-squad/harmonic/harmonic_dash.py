import os

"""Proposal D: D3 live gauge for the Harmonic Thermo Ledger.

Renders thermo debt as a radial gauge in static/index.html.
Pure client-side D3, zero new backend routes. Polls /api/codex/events.
"""

GAUGE_SPEC = {
    "id": "harmonic-thermo-gauge",
    "type": "radial-gauge",
    "domain": [0.0, 1.0],
    "zones": [
        {"from": 0.0, "to": 0.3, "color": "#3dd68c", "label": "low-debt / grounded"},
        {"from": 0.3, "to": 0.7, "color": "#f5a623", "label": "mid-debt / abstract"},
        {"from": 0.7, "to": 1.0, "color": "#e24b4b", "label": "high-debt / literal"},
    ],
    "needle": "energy_ratio",
    "poll_ms": 2000,
    "source": "/api/codex/events?follow=1",
    "derive": "thermo.restart_cost blocks per lane -> ThermoLedger.debt(lane)",
}

D3_SNIPPET = """
const zones = [
  {from: 0.0, to: 0.3, color: "#3dd68c"},
  {from: 0.3, to: 0.7, color: "#f5a623"},
  {from: 0.7, to: 1.0, color: "#e24b4b"},
];
const el = d3.select("#harmonic-thermo-gauge");
const svg = el.append("svg").attr("viewBox", "0 0 200 120");
const arc = d3.arc().innerRadius(38).outerRadius(52).startAngle(-Math.PI/2);
const g = svg.append("g").attr("transform", "translate(100,78)");
g.selectAll("path").data(zones).join("path")
  .attr("d", z => arc.endAngle(-Math.PI/2 + Math.PI * z.to).startAngle(-Math.PI/2 + Math.PI * z.from)())
  .attr("fill", z => z.color).attr("opacity", 0.85);
const needle = g.append("line").attr("x1", 0).attr("y1", 0)
  .attr("x2", 0).attr("y2", -46).attr("stroke", "#e8e8e8").attr("stroke-width", 3)
  .attr("stroke-linecap", "round");
const label = svg.append("text").attr("x", 100).attr("y", 112)
  .attr("text-anchor", "middle").attr("fill", "#9aa0a6").attr("font-size", 11);
function setDebt(d) {
  const clamped = Math.max(0, Math.min(1, d));
  needle.transition().duration(600)
    .attr("transform", `rotate(${-90 + 180 * clamped})`);
  const zone = zones.find(z => clamped >= z.from && clamped < z.to) || zones[2];
  label.text(`entropy ${clamped.toFixed(2)}  ·  ${zone.color === "#e24b4b" ? "literal" : zone.color === "#f5a623" ? "abstract" : "grounded"}`);
}
async function poll() {
  try {
    const r = await fetch("/api/codex/events?follow=1");
    const lines = (await r.text()).trim().split("\n").filter(Boolean);
    let worst = 0;
    for (const line of lines) {
      let ev; try { ev = JSON.parse(line); } catch { continue; }
      const rc = ev.restart_cost;
      if (rc && typeof rc.debt === "number") worst = Math.max(worst, rc.debt);
    }
    setDebt(worst);
  } catch (_) {}
}
setInterval(poll, 2000); poll();
"""

def inject_into_index(html_path_or_src):
    """Insert gauge container + script before </body>. Guard on quoted id to avoid
    false positives from the d3.select("#harmonic-thermo-gauge") call inside the snippet."""
    if os.path.isfile(str(html_path_or_src)):
        with open(html_path_or_src) as f:
            html = f.read()
    else:
        html = str(html_path_or_src)
    if 'id="harmonic-thermo-gauge"' not in html:
        container = '<div id="harmonic-thermo-gauge" style="width:220px;height:130px"></div>\n'
        html = html.replace(
            "</body>",
            container
            + "<script src='https://d3js.org/d3.v7.min.js'></script>\n"
            + "<script>\n" + D3_SNIPPET + "</script>\n</body>",
        )
    return html
