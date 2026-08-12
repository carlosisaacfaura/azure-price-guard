"""Orchestration: collect -> persist -> validate -> report.

    python -m price_guard --demo

is the full acceptance scenario: two real collections from the Azure Retail
Prices API, one intentional price change injected into the second, and a
report proving the SQL drift rule caught it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from . import validation_rules
from .api_client import PriceRecord, RetailPricesClient
from .config import Config, load_config
from .db import PriceDatabase
from .reporting import RunSummary, write_all
from .ui.driver import FakeDriver, FakeElement, build_driver
from .ui.pages import PricingCalculatorPage
from .utils import get_logger, new_run_id, setup_logging, utc_now_iso

log = get_logger("runner")


def collect(config: Config, db: PriceDatabase, note: str = "") -> tuple[str, list[PriceRecord]]:
    """One collection run: hit the public API, persist a full snapshot."""
    run_id = new_run_id()
    db.start_run(run_id, source="api", note=note)
    client = RetailPricesClient(config.api)
    records = client.fetch_all(config.targets, config.filters)
    db.insert_records(run_id, records)
    db.finish_run(run_id)
    return run_id, records


def build_fake_dom(price_text: str) -> dict[str, FakeElement]:
    """Deterministic stand-in for the portal DOM, keyed by the real locators."""
    return {
        "input[data-testid='pricing-search-input']": FakeElement(),
        "button[data-testid='pricing-search-submit']": FakeElement(),
        "[data-testid='search-results'] li:first-child a": FakeElement(text="Virtual Machines"),
        "select[data-testid='region-selector']": FakeElement(options=["East US", "West Europe"]),
        "select[data-testid='instance-selector']": FakeElement(options=["D2s v3", "B2ms"]),
        "[data-testid='estimate-total'] .price": FakeElement(text=price_text),
        "onetrust-accept-btn-handler": FakeElement(visible=False),
    }


def capture_ui(
    config: Config,
    db: PriceDatabase,
    run_id: str,
    fake_prices: dict[str, str] | None = None,
) -> int:
    """Drive the Page Object over every `ui.cross_check` entry.

    With `ui.driver = chrome` this opens the real portal. With `fake` it
    replays a deterministic DOM - the Page Object code path is identical.
    """
    captured = 0
    for spec in config.ui.cross_check:
        if config.ui.driver == "fake":
            text = (fake_prices or {}).get(spec["arm_sku_name"], "")
            if not text:
                log.warning(
                    "No fake DOM price for %s; skipping UI capture", spec["arm_sku_name"]
                )
                continue
            driver: Any = FakeDriver(build_fake_dom(text))
        else:
            driver = build_driver(config.ui)
        try:
            page = PricingCalculatorPage(driver, config.ui)
            observation = page.capture_price(spec)
            db.insert_ui_observation(run_id, observation.as_dict())
            captured += 1
        finally:
            driver.quit()
    return captured


def validate_and_report(
    config: Config,
    db: PriceDatabase,
    run_id: str,
    previous_run_id: str | None,
    notes: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    findings = validation_rules.run_all(
        db,
        run_id,
        previous_run_id,
        drift_threshold_pct=config.validation.drift_threshold_pct,
        drift_min_absolute=config.validation.drift_min_absolute_usd,
        ui_tolerance_pct=config.validation.ui_api_tolerance_pct,
    )
    db.insert_findings(findings)

    run_row = db.query("SELECT * FROM runs WHERE run_id = ?", (run_id,))[0]
    ui_count = db.query(
        "SELECT COUNT(*) AS n FROM ui_observations WHERE run_id = ?", (run_id,)
    )[0]["n"]

    summary = RunSummary(
        run_id=run_id,
        previous_run_id=previous_run_id,
        started_at=run_row["started_at"],
        finished_at=run_row["finished_at"] or utc_now_iso(),
        records=db.snapshot_count(run_id),
        targets=[t.name for t in config.targets],
        findings=findings,
        ui_observations=int(ui_count),
        drift_threshold_pct=config.validation.drift_threshold_pct,
        notes=list(notes),
    )
    paths = write_all(
        db,
        summary,
        findings,
        config.html_report_path,
        config.summary_csv_path,
        config.findings_csv_path,
    )
    return findings, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="price_guard",
        description="Scrape, validate and monitor Azure retail prices.",
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Full acceptance scenario: two live collections + an intentional "
             "price change, proving the drift rule fires.",
    )
    parser.add_argument(
        "--inject-drift-sku", default="Standard_D2s_v3",
        help="SKU to apply the intentional price change to (demo only).",
    )
    parser.add_argument(
        "--inject-drift-pct", type=float, default=18.0,
        help="Size of the intentional price change, in percent (demo only).",
    )
    parser.add_argument(
        "--inject-gap", action="store_true",
        help="Also delete one meter from the second run to prove PRICE_GAP.",
    )
    parser.add_argument(
        "--fake-ui", action="store_true",
        help="Run the Page Object against the deterministic FakeDriver so the "
             "UI-vs-API section of the report is populated without a browser.",
    )
    parser.add_argument("--fresh", action="store_true", help="Delete the database first.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    logger = setup_logging(config.log_path)
    logger.info("Azure Price Guard starting; config=%s", args.config or "config.yaml")

    if args.fresh and config.database_path.exists():
        config.database_path.unlink()
        logger.info("Removed existing database %s", config.database_path)

    db = PriceDatabase(config.database_path)
    notes: list[str] = []
    try:
        baseline_id: str | None = None
        if args.demo:
            logger.info("=== PASS 1/2: baseline collection from the live API ===")
            baseline_id, _ = collect(config, db, note="demo baseline")

        logger.info("=== PASS 2/2: current collection from the live API ===")
        run_id, records = collect(config, db, note="demo current")
        if baseline_id is None:
            baseline_id = db.previous_run_id(run_id)

        if args.demo:
            factor = 1.0 + args.inject_drift_pct / 100.0
            target_meters = db.query(
                """SELECT DISTINCT meter_id, meter_name, retail_price
                   FROM price_snapshots WHERE run_id = ? AND arm_sku_name = ?
                   ORDER BY retail_price DESC LIMIT 1""",
                (run_id, args.inject_drift_sku),
            )
            if not target_meters:
                logger.error(
                    "SKU %s not present in this run; cannot inject the drift demo.",
                    args.inject_drift_sku,
                )
            else:
                meter = target_meters[0]
                db.adjust_price(run_id, args.inject_drift_sku, meter["meter_id"], factor)
                notes.append(
                    f"INTENTIONAL PRICE CHANGE (synthetic): meter '{meter['meter_name']}' "
                    f"of {args.inject_drift_sku} was multiplied by {factor:.2f} "
                    f"(+{args.inject_drift_pct:g}%) inside the second snapshot, to prove "
                    "the PRICE_DRIFT SQL rule detects it. It is the only figure in this "
                    "report that did not come from the Azure API."
                )
            if args.inject_gap:
                gap_rows = db.query(
                    """SELECT meter_id, meter_name FROM price_snapshots
                       WHERE run_id = ? AND arm_sku_name = ? AND meter_id != ?
                       ORDER BY meter_id LIMIT 1""",
                    (run_id, args.inject_drift_sku,
                     target_meters[0]["meter_id"] if target_meters else ""),
                )
                if gap_rows:
                    db.delete_meter(run_id, gap_rows[0]["meter_id"])
                    notes.append(
                        f"INTENTIONAL GAP (synthetic): meter "
                        f"'{gap_rows[0]['meter_name']}' was removed from the second "
                        "snapshot to prove the PRICE_GAP SQL rule detects a "
                        "disappearing meter."
                    )

        if args.fake_ui:
            fake_prices = {}
            for spec in config.ui.cross_check:
                rows = db.query(
                    """SELECT retail_price FROM price_snapshots
                       WHERE run_id = ? AND arm_sku_name = ? AND arm_region_name = ?
                         AND price_type = 'Consumption'
                       ORDER BY retail_price DESC LIMIT 1""",
                    (run_id, spec["arm_sku_name"], spec["arm_region_name"]),
                )
                if rows:
                    fake_prices[spec["arm_sku_name"]] = f"${rows[0]['retail_price']:.4f}/hour"
            captured = capture_ui(config, db, run_id, fake_prices)
            notes.append(
                f"UI cross-check ran against the deterministic FakeDriver "
                f"({captured} observation(s)); the Page Object, the locators and the "
                "data-driven search paths are the production ones, but no browser was "
                "launched. Set ui.driver=chrome in config.yaml for the live portal."
            )

        findings, paths = validate_and_report(config, db, run_id, baseline_id, notes)

        logger.info("Run %s complete: %s finding(s)", run_id, len(findings))
        for finding in findings:
            logger.info("  [%s] %s - %s", finding["severity"], finding["rule_id"], finding["details"])
        print(f"\nRun        : {run_id}")
        print(f"Baseline   : {baseline_id}")
        print(f"Records    : {db.snapshot_count(run_id)}")
        print(f"Findings   : {len(findings)}")
        for name, path in paths.items():
            print(f"{name:<11}: {path}")
        print(f"log        : {config.log_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
