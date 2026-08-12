"""Validation rules expressed as SQL, not as Python `if` statements.

Each rule is a named SELECT that returns a uniform finding shape. Adding a
rule means adding one SQL string to `RULES` - the runner, the reports and the
findings table need no change. Auditors can copy any statement below straight
into a SQLite console and reproduce the result by hand.
"""

from __future__ import annotations

from typing import Any

from .utils import get_logger, utc_now_iso

log = get_logger("rules")

SEVERITY = {
    "DUPLICATE_METER": "HIGH",
    "PRICE_GAP": "HIGH",
    "PRICE_DRIFT": "HIGH",
    "NON_POSITIVE_PRICE": "MEDIUM",
    "MISSING_UNIT": "LOW",
    "UI_API_MISMATCH": "HIGH",
}

# ---------------------------------------------------------------------------
# R1 - DUPLICATE_METER
# The same natural key - sku + region + meter + price type + unit + pricing
# TIER - must appear at most once per run. More than once means the ingestion
# double-counted a page or Azure published a genuinely ambiguous meter.
# `tier_minimum_units` belongs in the key: Azure bills volume tiers of one
# meter under a single meterId, and without it every tiered meter is a
# false duplicate (and tier 1 vs tier 3 a false +400% drift).
#
# The rule fires only on CONFLICTING prices (COUNT(DISTINCT retail_price) > 1).
# Azure legitimately lists one meter under several products - e.g. the same
# Blob write meter under "General Block Blob" and "Blob Storage" - at the same
# price. Those repeats are a presentation detail; two different prices for one
# billable key is a real defect that would mis-state a forecast.
# ---------------------------------------------------------------------------
SQL_DUPLICATES = """
SELECT
    arm_sku_name,
    arm_region_name,
    meter_id,
    MAX(meter_name)                       AS meter_name,
    COUNT(*)                              AS occurrences,
    COUNT(DISTINCT retail_price)          AS distinct_prices,
    GROUP_CONCAT(DISTINCT retail_price)   AS prices_seen
FROM price_snapshots
WHERE run_id = :run_id
GROUP BY arm_sku_name, arm_region_name, meter_id, price_type, unit_of_measure,
         tier_minimum_units
HAVING COUNT(*) > 1 AND COUNT(DISTINCT retail_price) > 1
ORDER BY distinct_prices DESC, occurrences DESC, arm_sku_name
"""

# ---------------------------------------------------------------------------
# R2 - PRICE_GAP
# A meter that existed in the previous run and vanished in this one. Either
# Azure retired it or the scrape/API call silently returned a short page.
# ---------------------------------------------------------------------------
SQL_GAPS = """
SELECT
    prev.arm_sku_name,
    prev.arm_region_name,
    prev.meter_id,
    prev.meter_name,
    prev.price_type,
    prev.unit_of_measure,
    prev.retail_price AS previous_price
FROM price_snapshots AS prev
WHERE prev.run_id = :previous_run_id
  AND NOT EXISTS (
        SELECT 1 FROM price_snapshots AS cur
        WHERE cur.run_id          = :run_id
          AND cur.arm_sku_name    = prev.arm_sku_name
          AND cur.arm_region_name = prev.arm_region_name
          AND cur.meter_id        = prev.meter_id
          AND cur.price_type      = prev.price_type
          AND cur.unit_of_measure = prev.unit_of_measure
          AND cur.tier_minimum_units = prev.tier_minimum_units
  )
GROUP BY prev.arm_sku_name, prev.arm_region_name, prev.meter_id,
         prev.price_type, prev.unit_of_measure, prev.tier_minimum_units
ORDER BY prev.arm_sku_name
"""

# ---------------------------------------------------------------------------
# R3 - PRICE_DRIFT  (the rule the acceptance demo exercises)
# Joins this run to the previous one on the natural key and reports every
# meter whose relative change exceeds :threshold_pct AND whose absolute change
# exceeds :min_abs (so sub-cent meters do not flood the report).
# ---------------------------------------------------------------------------
SQL_DRIFT = """
SELECT
    cur.arm_sku_name,
    cur.arm_region_name,
    cur.meter_id,
    cur.meter_name,
    cur.unit_of_measure,
    cur.currency_code,
    prev.retail_price AS previous_price,
    cur.retail_price  AS current_price,
    (cur.retail_price - prev.retail_price)                          AS delta_abs,
    ((cur.retail_price - prev.retail_price) / prev.retail_price)*100 AS delta_pct,
    MAX(cur.is_synthetic, prev.is_synthetic)                        AS is_synthetic
FROM price_snapshots AS cur
JOIN price_snapshots AS prev
      ON  prev.run_id          = :previous_run_id
      AND prev.arm_sku_name    = cur.arm_sku_name
      AND prev.arm_region_name = cur.arm_region_name
      AND prev.meter_id        = cur.meter_id
      AND prev.price_type      = cur.price_type
      AND prev.unit_of_measure = cur.unit_of_measure
      AND prev.tier_minimum_units = cur.tier_minimum_units
WHERE cur.run_id = :run_id
  AND prev.retail_price <> 0
  AND ABS(cur.retail_price - prev.retail_price) >= :min_abs
  AND ABS((cur.retail_price - prev.retail_price) / prev.retail_price) * 100 > :threshold_pct
ORDER BY ABS((cur.retail_price - prev.retail_price) / prev.retail_price) DESC
"""

# ---------------------------------------------------------------------------
# R4 - NON_POSITIVE_PRICE - a consumption meter priced at 0 or below.
# A NEGATIVE price is always a defect (HIGH). A ZERO price is usually a
# genuinely free operation (Azure prices delete operations at 0), so it is
# reported at LOW severity for review rather than treated as a failure.
# ---------------------------------------------------------------------------
SQL_NON_POSITIVE = """
SELECT arm_sku_name, arm_region_name, meter_id, meter_name, retail_price
FROM price_snapshots
WHERE run_id = :run_id
  AND price_type = 'Consumption'
  AND retail_price <= 0
ORDER BY retail_price ASC, arm_sku_name
"""

# ---------------------------------------------------------------------------
# R5 - MISSING_UNIT - a price with no unit of measure cannot be reconciled.
# ---------------------------------------------------------------------------
SQL_MISSING_UNIT = """
SELECT arm_sku_name, arm_region_name, meter_id, meter_name, retail_price
FROM price_snapshots
WHERE run_id = :run_id
  AND (unit_of_measure IS NULL OR TRIM(unit_of_measure) = '')
ORDER BY arm_sku_name
"""

# ---------------------------------------------------------------------------
# R6 - UI_API_MISMATCH - side-by-side reconciliation of what the portal showed
# against what the REST API returned, for the same sku + region, in SQL.
#
# One SKU has SEVERAL consumption meters (Linux vs Windows, per-hour vs
# per-month). The portal shows one number, so the rule fires only when the
# observed price reconciles with NO meter of that SKU - a naive row-by-row
# join would flag the Linux meter every time the page displayed the Windows
# price. The nearest API price is reported so a human can see the gap.
# ---------------------------------------------------------------------------
SQL_UI_MISMATCH = """
SELECT
    ui.arm_sku_name,
    ui.arm_region_name,
    ui.observed_price,
    ui.raw_price_text,
    api.meter_id,
    api.meter_name,
    api.retail_price AS api_price,
    MIN(ABS(api.retail_price - ui.observed_price)) AS nearest_gap
FROM ui_observations AS ui
JOIN price_snapshots AS api
      ON  api.run_id          = ui.run_id
      AND api.arm_sku_name    = ui.arm_sku_name
      AND api.arm_region_name = ui.arm_region_name
      AND api.price_type      = 'Consumption'
      AND api.retail_price   <> 0
WHERE ui.run_id = :run_id
  AND ui.observed_price IS NOT NULL
  AND NOT EXISTS (
        SELECT 1 FROM price_snapshots AS ok
        WHERE ok.run_id          = ui.run_id
          AND ok.arm_sku_name    = ui.arm_sku_name
          AND ok.arm_region_name = ui.arm_region_name
          AND ok.price_type      = 'Consumption'
          AND ok.retail_price   <> 0
          AND ABS((ui.observed_price - ok.retail_price) / ok.retail_price) * 100
              <= :tolerance_pct
  )
-- Bare columns alongside MIN() resolve to the row holding the minimum in
-- SQLite, so `api.*` describes the closest API meter to what the portal showed.
GROUP BY ui.id
ORDER BY ui.arm_sku_name
"""


def _finding(run_id: str, rule_id: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "detected_at": utc_now_iso(),
        "rule_id": rule_id,
        "severity": kwargs.get("severity") or SEVERITY.get(rule_id, "MEDIUM"),
        "arm_sku_name": kwargs.get("arm_sku_name"),
        "arm_region_name": kwargs.get("arm_region_name"),
        "meter_id": kwargs.get("meter_id"),
        "meter_name": kwargs.get("meter_name"),
        "previous_value": kwargs.get("previous_value"),
        "current_value": kwargs.get("current_value"),
        "delta_pct": kwargs.get("delta_pct"),
        "details": kwargs["details"],
    }


def run_duplicates(db: Any, run_id: str) -> list[dict[str, Any]]:
    rows = db.query(SQL_DUPLICATES, {"run_id": run_id})
    return [
        _finding(
            run_id, "DUPLICATE_METER",
            arm_sku_name=r["arm_sku_name"], arm_region_name=r["arm_region_name"],
            meter_id=r["meter_id"], meter_name=r["meter_name"],
            current_value=str(r["occurrences"]),
            details=(
                f"Meter appears {r['occurrences']} times in this run with "
                f"{r['distinct_prices']} conflicting prices: {r['prices_seen']}."
            ),
        )
        for r in rows
    ]


def run_gaps(db: Any, run_id: str, previous_run_id: str | None) -> list[dict[str, Any]]:
    if not previous_run_id:
        return []
    rows = db.query(SQL_GAPS, {"run_id": run_id, "previous_run_id": previous_run_id})
    return [
        _finding(
            run_id, "PRICE_GAP",
            arm_sku_name=r["arm_sku_name"], arm_region_name=r["arm_region_name"],
            meter_id=r["meter_id"], meter_name=r["meter_name"],
            previous_value=f"{r['previous_price']}",
            current_value="MISSING",
            details=(
                f"Meter '{r['meter_name']}' was priced at {r['previous_price']} in run "
                f"{previous_run_id} and is absent from this run."
            ),
        )
        for r in rows
    ]


def run_drift(
    db: Any,
    run_id: str,
    previous_run_id: str | None,
    threshold_pct: float,
    min_abs: float = 0.0,
) -> list[dict[str, Any]]:
    if not previous_run_id:
        return []
    rows = db.query(
        SQL_DRIFT,
        {
            "run_id": run_id,
            "previous_run_id": previous_run_id,
            "threshold_pct": threshold_pct,
            "min_abs": min_abs,
        },
    )
    findings = []
    for r in rows:
        flag = " [SYNTHETIC - intentionally injected]" if r["is_synthetic"] else ""
        findings.append(
            _finding(
                run_id, "PRICE_DRIFT",
                arm_sku_name=r["arm_sku_name"], arm_region_name=r["arm_region_name"],
                meter_id=r["meter_id"], meter_name=r["meter_name"],
                previous_value=f"{r['previous_price']:.6f}",
                current_value=f"{r['current_price']:.6f}",
                delta_pct=round(r["delta_pct"], 4),
                details=(
                    f"{r['meter_name']} ({r['unit_of_measure']}) moved "
                    f"{r['previous_price']:.6f} -> {r['current_price']:.6f} "
                    f"{r['currency_code']} = {r['delta_pct']:+.2f}%, above the "
                    f"{threshold_pct}% threshold.{flag}"
                ),
            )
        )
    return findings


def run_non_positive(db: Any, run_id: str) -> list[dict[str, Any]]:
    rows = db.query(SQL_NON_POSITIVE, {"run_id": run_id})
    return [
        _finding(
            run_id, "NON_POSITIVE_PRICE",
            arm_sku_name=r["arm_sku_name"], arm_region_name=r["arm_region_name"],
            meter_id=r["meter_id"], meter_name=r["meter_name"],
            current_value=str(r["retail_price"]),
            severity="HIGH" if r["retail_price"] < 0 else "LOW",
            details=(
                f"Consumption meter '{r['meter_name']}' priced at {r['retail_price']}"
                + (
                    " - negative price, always a defect."
                    if r["retail_price"] < 0
                    else " - zero price; confirm the operation is genuinely free."
                )
            ),
        )
        for r in rows
    ]


def run_missing_unit(db: Any, run_id: str) -> list[dict[str, Any]]:
    rows = db.query(SQL_MISSING_UNIT, {"run_id": run_id})
    return [
        _finding(
            run_id, "MISSING_UNIT",
            arm_sku_name=r["arm_sku_name"], arm_region_name=r["arm_region_name"],
            meter_id=r["meter_id"], meter_name=r["meter_name"],
            current_value=str(r["retail_price"]),
            details="Price published without a unit of measure; cannot be reconciled.",
        )
        for r in rows
    ]


def run_ui_mismatch(db: Any, run_id: str, tolerance_pct: float) -> list[dict[str, Any]]:
    rows = db.query(SQL_UI_MISMATCH, {"run_id": run_id, "tolerance_pct": tolerance_pct})
    findings = []
    for r in rows:
        delta_pct = (r["observed_price"] - r["api_price"]) / r["api_price"] * 100
        findings.append(
            _finding(
                run_id, "UI_API_MISMATCH",
                arm_sku_name=r["arm_sku_name"], arm_region_name=r["arm_region_name"],
                meter_id=r["meter_id"], meter_name=r["meter_name"],
                previous_value=f"{r['api_price']:.6f}",
                current_value=f"{r['observed_price']:.6f}",
                delta_pct=round(delta_pct, 4),
                details=(
                    f"Portal showed '{r['raw_price_text']}' ({r['observed_price']:.6f}) but "
                    f"no API meter for this SKU is within {tolerance_pct}%; nearest is "
                    f"'{r['meter_name']}' at {r['api_price']:.6f} = {delta_pct:+.2f}%."
                ),
            )
        )
    return findings


def run_all(
    db: Any,
    run_id: str,
    previous_run_id: str | None,
    drift_threshold_pct: float,
    drift_min_absolute: float = 0.0,
    ui_tolerance_pct: float = 1.0,
) -> list[dict[str, Any]]:
    """Execute every rule and return one flat list of findings."""
    findings: list[dict[str, Any]] = []
    findings += run_duplicates(db, run_id)
    findings += run_gaps(db, run_id, previous_run_id)
    findings += run_drift(db, run_id, previous_run_id, drift_threshold_pct, drift_min_absolute)
    findings += run_non_positive(db, run_id)
    findings += run_missing_unit(db, run_id)
    findings += run_ui_mismatch(db, run_id, ui_tolerance_pct)
    log.info(
        "Validation produced %s finding(s) for run %s (baseline=%s)",
        len(findings), run_id, previous_run_id or "none",
    )
    return findings
