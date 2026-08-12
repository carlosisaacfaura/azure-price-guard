"""Coverage for the non-drift SQL rules: duplicates, gaps, sanity, UI mismatch."""

from __future__ import annotations

import pytest

from price_guard import validation_rules
from price_guard.utils import new_run_id


def test_duplicate_meter_with_conflicting_prices_is_detected(db, record_factory):
    """Two different prices for one billable key is a real defect."""
    run = db.start_run(new_run_id())
    db.insert_records(
        run,
        [
            record_factory(meter_id="m-1", retail_price=0.096),
            record_factory(meter_id="m-1", retail_price=0.104),   # contradiction
            record_factory(meter_id="m-2", retail_price=0.192, arm_sku_name="Standard_D4s_v3"),
        ],
    )
    db.finish_run(run)

    findings = validation_rules.run_duplicates(db, run)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "DUPLICATE_METER"
    assert findings[0]["meter_id"] == "m-1"
    assert findings[0]["current_value"] == "2"


def test_identical_repeats_of_one_meter_are_not_flagged(db, record_factory):
    """Azure lists one meter under several products at the same price.

    Observed live on Storage in eastus: 80 such repeats, all in agreement.
    Flagging them would bury the real findings.
    """
    run = db.start_run(new_run_id())
    db.insert_records(
        run,
        [record_factory(meter_id="m-1", retail_price=0.096) for _ in range(3)],
    )
    db.finish_run(run)
    assert validation_rules.run_duplicates(db, run) == []


def test_same_meter_different_unit_is_not_a_duplicate(db, record_factory):
    """A meter billed per hour and per month is legitimate, not a duplicate."""
    run = db.start_run(new_run_id())
    db.insert_records(
        run,
        [
            record_factory(meter_id="m-1", unit_of_measure="1 Hour"),
            record_factory(meter_id="m-1", unit_of_measure="1 Month"),
        ],
    )
    db.finish_run(run)
    assert validation_rules.run_duplicates(db, run) == []


def test_gap_detected_when_meter_disappears(db, record_factory):
    baseline = db.start_run(new_run_id())
    db.insert_records(
        baseline,
        [
            record_factory(meter_id="m-1"),
            record_factory(meter_id="m-2", arm_sku_name="Standard_D4s_v3", retail_price=0.192),
        ],
    )
    db.finish_run(baseline)

    current = db.start_run(new_run_id())
    db.insert_records(current, [record_factory(meter_id="m-1")])
    db.finish_run(current)

    findings = validation_rules.run_gaps(db, current, baseline)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "PRICE_GAP"
    assert findings[0]["meter_id"] == "m-2"
    assert findings[0]["current_value"] == "MISSING"


def test_no_gaps_reported_without_a_baseline(db, record_factory):
    run = db.start_run(new_run_id())
    db.insert_records(run, [record_factory()])
    db.finish_run(run)
    assert validation_rules.run_gaps(db, run, None) == []
    assert validation_rules.run_drift(db, run, None, 5.0) == []


def test_non_positive_and_missing_unit(db, record_factory):
    run = db.start_run(new_run_id())
    db.insert_records(
        run,
        [
            record_factory(meter_id="m-free", retail_price=0.0),
            record_factory(meter_id="m-nounit", unit_of_measure="   "),
            record_factory(meter_id="m-ok"),
        ],
    )
    db.finish_run(run)

    zero_findings = validation_rules.run_non_positive(db, run)
    assert [f["meter_id"] for f in zero_findings] == ["m-free"]
    assert zero_findings[0]["severity"] == "LOW"   # free operations are normal
    assert [f["meter_id"] for f in validation_rules.run_missing_unit(db, run)] == ["m-nounit"]


def test_ui_api_mismatch_detected(db, record_factory):
    run = db.start_run(new_run_id())
    db.insert_records(run, [record_factory(meter_id="m-1", retail_price=0.096)])
    db.insert_ui_observation(
        run,
        {
            "source_page": "fake://calculator",
            "search_path": "calculator_service_search",
            "service_name": "Virtual Machines",
            "arm_region_name": "eastus",
            "arm_sku_name": "Standard_D2s_v3",
            "raw_price_text": "$0.1100/hour",
            "observed_price": 0.11,
            "currency_code": "USD",
        },
    )
    db.finish_run(run)

    findings = validation_rules.run_ui_mismatch(db, run, tolerance_pct=1.0)
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "UI_API_MISMATCH"
    assert findings[0]["delta_pct"] == pytest.approx(14.5833, abs=1e-3)


def test_ui_matching_api_within_tolerance_is_silent(db, record_factory):
    run = db.start_run(new_run_id())
    db.insert_records(run, [record_factory(meter_id="m-1", retail_price=0.096)])
    db.insert_ui_observation(
        run,
        {
            "source_page": "fake://calculator",
            "search_path": "calculator_service_search",
            "service_name": "Virtual Machines",
            "arm_region_name": "eastus",
            "arm_sku_name": "Standard_D2s_v3",
            "raw_price_text": "$0.0960/hour",
            "observed_price": 0.096,
        },
    )
    db.finish_run(run)
    assert validation_rules.run_ui_mismatch(db, run, tolerance_pct=1.0) == []


def test_run_all_aggregates_every_rule(db, record_factory):
    baseline = db.start_run(new_run_id())
    db.insert_records(
        baseline,
        [record_factory(meter_id="m-1"), record_factory(meter_id="m-gone", retail_price=0.5)],
    )
    db.finish_run(baseline)

    current = db.start_run(new_run_id())
    db.insert_records(
        current,
        [
            record_factory(meter_id="m-1", retail_price=0.15),   # drift
            record_factory(meter_id="m-dup", retail_price=0.10),
            record_factory(meter_id="m-dup", retail_price=0.11), # conflicting price
            record_factory(meter_id="m-zero", retail_price=0.0), # non-positive
        ],
    )
    db.finish_run(current)

    findings = validation_rules.run_all(db, current, baseline, drift_threshold_pct=5.0)
    rules = {f["rule_id"] for f in findings}
    assert {"PRICE_DRIFT", "PRICE_GAP", "DUPLICATE_METER", "NON_POSITIVE_PRICE"} <= rules
    assert db.insert_findings(findings) == len(findings)


def test_pricing_tiers_are_not_duplicates_and_do_not_drift(db, record_factory):
    """Regression: tiered meters share a meterId.

    Before `tier_minimum_units` joined the natural key, this fixture produced
    a false DUPLICATE_METER and a false +400% PRICE_DRIFT against live Azure
    Storage data.
    """
    import dataclasses

    def tiered(price, tier):
        return dataclasses.replace(
            record_factory(meter_id="m-storage", retail_price=price),
            tier_minimum_units=tier,
        )

    baseline = db.start_run(new_run_id())
    db.insert_records(baseline, [tiered(0.2, 0.0), tiered(0.6, 51200.0), tiered(1.0, 512000.0)])
    db.finish_run(baseline)

    current = db.start_run(new_run_id())
    db.insert_records(current, [tiered(0.2, 0.0), tiered(0.6, 51200.0), tiered(1.0, 512000.0)])
    db.finish_run(current)

    assert validation_rules.run_duplicates(db, current) == []
    assert validation_rules.run_drift(db, current, baseline, threshold_pct=5.0) == []


def test_negative_price_is_high_severity(db, record_factory):
    run = db.start_run(new_run_id())
    db.insert_records(run, [record_factory(meter_id="m-neg", retail_price=-0.5)])
    db.finish_run(run)
    findings = validation_rules.run_non_positive(db, run)
    assert findings[0]["severity"] == "HIGH"
    assert "negative price" in findings[0]["details"]
