"""THE ACCEPTANCE TEST.

The client asked for "proof that the framework detects an intentional price
change". This module is that proof:

  1. A baseline snapshot is stored.
  2. A second snapshot is stored with identical prices - the drift rule must
     stay silent (no false positives).
  3. ONE meter is deliberately re-priced.
  4. The PRICE_DRIFT SQL rule must flag exactly that meter, with the correct
     old price, new price and percentage, and nothing else.

The prices here are synthetic on purpose: a unit test must not depend on Azure
publishing a price change today. The identical scenario is exercised against
live API data by `python -m price_guard --demo`.
"""

from __future__ import annotations

import pytest

from price_guard import validation_rules
from price_guard.utils import new_run_id


@pytest.fixture
def two_runs(db, record_factory):
    """Baseline and current run holding the same three meters."""
    baseline = new_run_id()
    db.start_run(baseline, note="baseline")
    db.insert_records(
        baseline,
        [
            record_factory(meter_id="m-d2s", arm_sku_name="Standard_D2s_v3", retail_price=0.096),
            record_factory(meter_id="m-d4s", arm_sku_name="Standard_D4s_v3", retail_price=0.192),
            record_factory(meter_id="m-b2ms", arm_sku_name="Standard_B2ms", retail_price=0.0832),
        ],
    )
    db.finish_run(baseline)

    current = new_run_id()
    db.start_run(current, note="current")
    db.insert_records(
        current,
        [
            record_factory(meter_id="m-d2s", arm_sku_name="Standard_D2s_v3", retail_price=0.096),
            record_factory(meter_id="m-d4s", arm_sku_name="Standard_D4s_v3", retail_price=0.192),
            record_factory(meter_id="m-b2ms", arm_sku_name="Standard_B2ms", retail_price=0.0832),
        ],
    )
    db.finish_run(current)
    return baseline, current


def test_no_drift_when_prices_are_unchanged(db, two_runs):
    baseline, current = two_runs
    findings = validation_rules.run_drift(db, current, baseline, threshold_pct=5.0)
    assert findings == [], f"drift rule produced false positives: {findings}"


def test_intentional_price_change_is_detected(db, two_runs):
    """Bump one meter by +18% and assert the SQL rule catches exactly it."""
    baseline, current = two_runs

    rows_changed = db.adjust_price(
        run_id=current, arm_sku_name="Standard_D2s_v3", meter_id="m-d2s", factor=1.18
    )
    assert rows_changed == 1

    findings = validation_rules.run_drift(db, current, baseline, threshold_pct=5.0)

    assert len(findings) == 1, f"expected exactly one drift finding, got {findings}"
    finding = findings[0]
    assert finding["rule_id"] == "PRICE_DRIFT"
    assert finding["severity"] == "HIGH"
    assert finding["arm_sku_name"] == "Standard_D2s_v3"
    assert finding["meter_id"] == "m-d2s"
    assert float(finding["previous_value"]) == pytest.approx(0.096)
    assert float(finding["current_value"]) == pytest.approx(0.096 * 1.18)
    assert finding["delta_pct"] == pytest.approx(18.0, abs=1e-6)
    assert "SYNTHETIC" in finding["details"]


def test_change_below_threshold_is_ignored(db, two_runs):
    """A +3% move must not fire a 5% rule - the threshold is honoured."""
    baseline, current = two_runs
    db.adjust_price(current, "Standard_D4s_v3", "m-d4s", factor=1.03)

    findings = validation_rules.run_drift(db, current, baseline, threshold_pct=5.0)
    assert findings == []

    # ...but tightening the threshold in config surfaces it, no code change.
    findings = validation_rules.run_drift(db, current, baseline, threshold_pct=1.0)
    assert len(findings) == 1
    assert findings[0]["arm_sku_name"] == "Standard_D4s_v3"


def test_price_decrease_is_detected_too(db, two_runs):
    baseline, current = two_runs
    db.adjust_price(current, "Standard_B2ms", "m-b2ms", factor=0.75)

    findings = validation_rules.run_drift(db, current, baseline, threshold_pct=5.0)
    assert len(findings) == 1
    assert findings[0]["delta_pct"] == pytest.approx(-25.0)


def test_absolute_floor_suppresses_sub_cent_noise(db, record_factory):
    """A 50% move on a 0.000002 meter is noise, not a finance event."""
    baseline = db.start_run(new_run_id())
    db.insert_records(baseline, [record_factory(meter_id="m-tiny", retail_price=0.000002)])
    db.finish_run(baseline)

    current = db.start_run(new_run_id())
    db.insert_records(current, [record_factory(meter_id="m-tiny", retail_price=0.000003)])
    db.finish_run(current)

    assert validation_rules.run_drift(
        db, current, baseline, threshold_pct=5.0, min_abs=0.0001
    ) == []
    assert len(
        validation_rules.run_drift(db, current, baseline, threshold_pct=5.0, min_abs=0.0)
    ) == 1


def test_drift_finding_is_persisted_and_queryable(db, two_runs):
    """The finding must survive into the findings table for the HTML report."""
    baseline, current = two_runs
    db.adjust_price(current, "Standard_D2s_v3", "m-d2s", factor=1.25)

    findings = validation_rules.run_all(
        db, current, baseline, drift_threshold_pct=5.0, drift_min_absolute=0.0001
    )
    db.insert_findings(findings)

    stored = db.findings_for_run(current)
    drift = [row for row in stored if row["rule_id"] == "PRICE_DRIFT"]
    assert len(drift) == 1
    assert drift[0]["arm_sku_name"] == "Standard_D2s_v3"
    assert drift[0]["delta_pct"] == pytest.approx(25.0)
