"""Reporting helper: HTML report, CSV summary and finance-friendly exports.

Deliverables produced by one call to `write_all()`:
  reports/report.html   - human-readable run report with every finding
  reports/summary.csv   - one row per SKU/meter with old vs new price
  reports/findings.csv  - one row per finding, for the finance ticket queue
  reports/run.log       - written by utils.setup_logging during the run
"""

from __future__ import annotations

import csv
import html
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .utils import get_logger, utc_now_iso

log = get_logger("reporting")

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

SQL_SUMMARY = """
SELECT
    cur.service_name,
    cur.arm_sku_name,
    cur.arm_region_name,
    cur.meter_name,
    cur.unit_of_measure,
    cur.currency_code,
    prev.retail_price AS previous_price,
    cur.retail_price  AS current_price,
    CASE WHEN prev.retail_price IS NULL OR prev.retail_price = 0 THEN NULL
         ELSE ((cur.retail_price - prev.retail_price) / prev.retail_price) * 100
    END AS delta_pct,
    cur.is_synthetic,
    cur.effective_start_date
FROM price_snapshots AS cur
LEFT JOIN price_snapshots AS prev
       ON  prev.run_id          = :previous_run_id
       AND prev.arm_sku_name    = cur.arm_sku_name
       AND prev.arm_region_name = cur.arm_region_name
       AND prev.meter_id        = cur.meter_id
       AND prev.price_type      = cur.price_type
       AND prev.unit_of_measure = cur.unit_of_measure
       AND prev.tier_minimum_units = cur.tier_minimum_units
WHERE cur.run_id = :run_id
GROUP BY cur.arm_sku_name, cur.arm_region_name, cur.meter_id, cur.unit_of_measure,
         cur.tier_minimum_units
ORDER BY cur.service_name, cur.arm_region_name, cur.arm_sku_name, cur.meter_name
"""


@dataclass
class RunSummary:
    run_id: str
    previous_run_id: str | None
    started_at: str
    finished_at: str
    records: int
    targets: list[str]
    findings: Sequence[dict[str, Any]]
    ui_observations: int
    drift_threshold_pct: float
    notes: list[str]

    @property
    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding["rule_id"]] = counts.get(finding["rule_id"], 0) + 1
        return counts

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
        return counts


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def write_summary_csv(db: Any, run_id: str, previous_run_id: str | None, path: Path) -> int:
    rows: list[sqlite3.Row] = db.query(
        SQL_SUMMARY, {"run_id": run_id, "previous_run_id": previous_run_id or ""}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "service_name", "arm_sku_name", "arm_region_name", "meter_name",
        "unit_of_measure", "currency_code", "previous_price", "current_price",
        "delta_pct", "is_synthetic", "effective_start_date",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row[h] if row[h] is not None else "" for h in headers])
    log.info("Wrote %s rows to %s", len(rows), path)
    return len(rows)


def write_findings_csv(findings: Sequence[dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "rule_id", "severity", "arm_sku_name", "arm_region_name", "meter_name",
        "previous_value", "current_value", "delta_pct", "details", "detected_at",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for finding in sorted(
            findings, key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["rule_id"])
        ):
            writer.writerow([finding.get(h, "") if finding.get(h) is not None else "" for h in headers])
    log.info("Wrote %s findings to %s", len(findings), path)
    return len(findings)


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.5rem 4rem; font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       background:#f6f7f9; color:#16191d; }
main { max-width: 1180px; margin: 0 auto; }
h1 { font-size:1.7rem; margin:0 0 .25rem; letter-spacing:-.02em; }
h2 { font-size:1.1rem; margin:2.2rem 0 .7rem; letter-spacing:-.01em; }
.sub { color:#5b6470; margin:0 0 1.5rem; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.75rem; margin-bottom:.5rem; }
.card { background:#fff; border:1px solid #e2e5ea; border-radius:10px; padding:.85rem 1rem; }
.card .k { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; color:#6b7480; }
.card .v { font-size:1.5rem; font-weight:600; margin-top:.15rem; }
.card.bad .v { color:#b3261e; }
.card.ok .v { color:#1a7f4b; }
.tablewrap { overflow-x:auto; background:#fff; border:1px solid #e2e5ea; border-radius:10px; }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th, td { padding:.5rem .7rem; text-align:left; border-bottom:1px solid #eceff3; white-space:nowrap; }
th { background:#f0f2f5; font-weight:600; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; color:#4a5361; position:sticky; top:0; }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.sev { display:inline-block; padding:.1rem .45rem; border-radius:5px; font-size:.72rem; font-weight:700; letter-spacing:.03em; }
.sev.HIGH { background:#fce8e6; color:#b3261e; }
.sev.MEDIUM { background:#fff3d6; color:#8a5b00; }
.sev.LOW { background:#e8f0fe; color:#1a55b3; }
.up { color:#b3261e; font-weight:600; }
.down { color:#1a7f4b; font-weight:600; }
.tag { display:inline-block; background:#ede7f6; color:#4527a0; border-radius:5px; padding:.05rem .4rem; font-size:.7rem; font-weight:600; }
.note { background:#fff8e1; border:1px solid #f0dfae; border-radius:8px; padding:.7rem .9rem; margin:.4rem 0; font-size:13.5px; }
.empty { padding:1rem; color:#5b6470; }
code { background:#eef1f5; padding:.1rem .3rem; border-radius:4px; font-size:.85em; }
footer { margin-top:2.5rem; color:#6b7480; font-size:12.5px; }
@media (prefers-color-scheme: dark) {
  body { background:#14171a; color:#e6e8eb; }
  .card,.tablewrap { background:#1c2024; border-color:#2c3238; }
  th { background:#242a30; color:#a8b1bd; }
  th,td { border-color:#262c32; }
  .sub,.empty,footer,.card .k { color:#98a2ad; }
  .note { background:#2a2413; border-color:#4a3f1c; }
  code { background:#242a30; }
}
"""


def render_html(
    summary: RunSummary,
    findings: Sequence[dict[str, Any]],
    price_rows: Sequence[sqlite3.Row],
    ui_rows: Sequence[sqlite3.Row],
) -> str:
    e = html.escape
    high = summary.by_severity.get("HIGH", 0)
    status_class = "bad" if high else "ok"

    def finding_rows() -> str:
        if not findings:
            return '<tr><td colspan="7" class="empty">No findings. All rules passed.</td></tr>'
        out = []
        for f in sorted(
            findings, key=lambda x: (SEVERITY_ORDER.get(x["severity"], 9), x["rule_id"])
        ):
            delta = f.get("delta_pct")
            delta_html = "-"
            if delta is not None:
                cls = "up" if delta > 0 else "down"
                delta_html = f'<span class="{cls}">{delta:+.2f}%</span>'
            out.append(
                "<tr>"
                f'<td><span class="sev {e(f["severity"])}">{e(f["severity"])}</span></td>'
                f'<td><code>{e(f["rule_id"])}</code></td>'
                f'<td>{e(str(f.get("arm_sku_name") or "-"))}</td>'
                f'<td>{e(str(f.get("arm_region_name") or "-"))}</td>'
                f'<td class="num">{e(str(f.get("previous_value") or "-"))}</td>'
                f'<td class="num">{e(str(f.get("current_value") or "-"))}</td>'
                f'<td>{delta_html} {e(str(f.get("details") or ""))}</td>'
                "</tr>"
            )
        return "".join(out)

    def price_table() -> str:
        if not price_rows:
            return '<tr><td colspan="8" class="empty">No prices captured.</td></tr>'
        out = []
        for r in price_rows:
            delta = r["delta_pct"]
            if delta is None:
                delta_html = '<span style="color:#6b7480">new</span>'
            else:
                cls = "up" if delta > 0 else ("down" if delta < 0 else "")
                delta_html = f'<span class="{cls}">{delta:+.2f}%</span>' if delta else "0.00%"
            synthetic = ' <span class="tag">SYNTHETIC</span>' if r["is_synthetic"] else ""
            out.append(
                "<tr>"
                f'<td>{e(r["service_name"])}</td>'
                f'<td>{e(r["arm_sku_name"])}</td>'
                f'<td>{e(r["arm_region_name"])}</td>'
                f'<td>{e(r["meter_name"] or "")}{synthetic}</td>'
                f'<td>{e(r["unit_of_measure"] or "")}</td>'
                f'<td class="num">{_fmt(r["previous_price"])}</td>'
                f'<td class="num">{_fmt(r["current_price"])}</td>'
                f'<td class="num">{delta_html}</td>'
                "</tr>"
            )
        return "".join(out)

    def ui_table() -> str:
        if not ui_rows:
            return (
                '<tr><td colspan="5" class="empty">No UI observations in this run '
                "(Selenium disabled - see README).</td></tr>"
            )
        return "".join(
            "<tr>"
            f'<td>{e(r["arm_sku_name"])}</td>'
            f'<td>{e(r["arm_region_name"])}</td>'
            f'<td>{e(r["raw_price_text"] or "")}</td>'
            f'<td class="num">{_fmt(r["observed_price"])}</td>'
            f'<td>{e(r["search_path"] or "")}</td>'
            "</tr>"
            for r in ui_rows
        )

    rule_chips = "".join(
        f'<div class="card"><div class="k">{e(rule)}</div>'
        f'<div class="v">{count}</div></div>'
        for rule, count in sorted(summary.by_rule.items())
    ) or '<div class="card ok"><div class="k">rules triggered</div><div class="v">0</div></div>'

    notes_html = "".join(f'<div class="note">{e(note)}</div>' for note in summary.notes)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Azure Price Guard - Run {e(summary.run_id)}</title>
<style>{_CSS}</style></head><body><main>
<h1>Azure Price Guard - validation report</h1>
<p class="sub">Run <code>{e(summary.run_id)}</code> &middot; baseline
<code>{e(summary.previous_run_id or "none")}</code> &middot; started
{e(summary.started_at)} &middot; finished {e(summary.finished_at)}</p>

<div class="cards">
  <div class="card"><div class="k">Price records</div><div class="v">{summary.records}</div></div>
  <div class="card {status_class}"><div class="k">Findings</div><div class="v">{len(findings)}</div></div>
  <div class="card {status_class}"><div class="k">High severity</div><div class="v">{high}</div></div>
  <div class="card"><div class="k">Drift threshold</div><div class="v">{summary.drift_threshold_pct:g}%</div></div>
  <div class="card"><div class="k">UI observations</div><div class="v">{summary.ui_observations}</div></div>
</div>

<h2>Rules triggered</h2>
<div class="cards">{rule_chips}</div>

{notes_html}

<h2>Findings</h2>
<div class="tablewrap"><table>
<thead><tr><th>Severity</th><th>Rule</th><th>SKU</th><th>Region</th>
<th>Previous</th><th>Current</th><th>Detail</th></tr></thead>
<tbody>{finding_rows()}</tbody></table></div>

<h2>UI vs API cross-check</h2>
<div class="tablewrap"><table>
<thead><tr><th>SKU</th><th>Region</th><th>Portal text</th><th>Parsed</th><th>Search path</th></tr></thead>
<tbody>{ui_table()}</tbody></table></div>

<h2>Price detail ({len(price_rows)} meters)</h2>
<div class="tablewrap"><table>
<thead><tr><th>Service</th><th>SKU</th><th>Region</th><th>Meter</th><th>Unit</th>
<th>Previous</th><th>Current</th><th>Change</th></tr></thead>
<tbody>{price_table()}</tbody></table></div>

<footer>
Targets: {e(", ".join(summary.targets))}<br>
Source: Azure Retail Prices API (<code>https://prices.azure.com/api/retail/prices</code>),
public and anonymous. Rows tagged SYNTHETIC are the intentional price change
injected to prove drift detection - every other figure came from the API.<br>
Generated {e(utc_now_iso())} by Azure Price Guard.
</footer>
</main></body></html>"""


def write_all(
    db: Any,
    summary: RunSummary,
    findings: Sequence[dict[str, Any]],
    html_path: Path,
    summary_csv_path: Path,
    findings_csv_path: Path,
) -> dict[str, Path]:
    price_rows = db.query(
        SQL_SUMMARY,
        {"run_id": summary.run_id, "previous_run_id": summary.previous_run_id or ""},
    )
    ui_rows = db.query(
        "SELECT * FROM ui_observations WHERE run_id = ?", (summary.run_id,)
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        render_html(summary, findings, price_rows, ui_rows), encoding="utf-8"
    )
    write_summary_csv(db, summary.run_id, summary.previous_run_id, summary_csv_path)
    write_findings_csv(findings, findings_csv_path)
    log.info("Report written to %s", html_path)
    return {
        "html": html_path,
        "summary_csv": summary_csv_path,
        "findings_csv": findings_csv_path,
    }
