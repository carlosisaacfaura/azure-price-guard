"""Persistence, history and the reporting deliverables."""

from __future__ import annotations

import csv

import pytest

from price_guard import validation_rules
from price_guard.reporting import RunSummary, write_all
from price_guard.utils import new_run_id, pct_change


def test_history_is_kept_run_after_run(db, record_factory):
    for price in (0.096, 0.099, 0.105):
        run = db.start_run(new_run_id())
        db.insert_records(run, [record_factory(retail_price=price)])
        db.finish_run(run)

    runs = db.list_runs()
    assert len(runs) == 3
    assert all(row["finished_at"] for row in runs)
    assert all(row["record_count"] == 1 for row in runs)

    history = db.query(
        "SELECT retail_price FROM price_snapshots ORDER BY id"
    )
    assert [row["retail_price"] for row in history] == [0.096, 0.099, 0.105]


def test_previous_run_id_walks_back_one_step(db, record_factory):
    first = db.start_run(new_run_id())
    db.finish_run(first)
    second = db.start_run(new_run_id())
    db.finish_run(second)
    assert db.previous_run_id(second) == first
    assert db.previous_run_id(first) is None


def test_snapshots_are_never_overwritten(db, record_factory):
    run_a = db.start_run(new_run_id())
    db.insert_records(run_a, [record_factory(retail_price=0.096)])
    run_b = db.start_run(new_run_id())
    db.insert_records(run_b, [record_factory(retail_price=0.150)])
    assert db.snapshot_count(run_a) == 1
    assert db.query(
        "SELECT retail_price FROM price_snapshots WHERE run_id = ?", (run_a,)
    )[0]["retail_price"] == pytest.approx(0.096)


def test_injected_rows_are_flagged_synthetic(db, record_factory):
    run = db.start_run(new_run_id())
    db.insert_records(run, [record_factory(meter_id="m-1")])
    assert db.query("SELECT is_synthetic FROM price_snapshots")[0]["is_synthetic"] == 0
    db.adjust_price(run, "Standard_D2s_v3", "m-1", 1.2)
    assert db.query("SELECT is_synthetic FROM price_snapshots")[0]["is_synthetic"] == 1


def test_pct_change_helper():
    assert pct_change(100, 118) == pytest.approx(18.0)
    assert pct_change(100, 75) == pytest.approx(-25.0)
    assert pct_change(0, 5) is None


def test_reports_are_written_and_contain_the_finding(db, record_factory, tmp_path):
    baseline = db.start_run(new_run_id())
    db.insert_records(baseline, [record_factory(meter_id="m-1", retail_price=0.096)])
    db.finish_run(baseline)

    current = db.start_run(new_run_id())
    db.insert_records(current, [record_factory(meter_id="m-1", retail_price=0.096)])
    db.adjust_price(current, "Standard_D2s_v3", "m-1", 1.18)
    db.finish_run(current)

    findings = validation_rules.run_all(db, current, baseline, drift_threshold_pct=5.0)
    db.insert_findings(findings)

    run_row = db.query("SELECT * FROM runs WHERE run_id = ?", (current,))[0]
    summary = RunSummary(
        run_id=current, previous_run_id=baseline,
        started_at=run_row["started_at"], finished_at=run_row["finished_at"],
        records=db.snapshot_count(current), targets=["unit-test"],
        findings=findings, ui_observations=0, drift_threshold_pct=5.0,
        notes=["unit test run"],
    )
    paths = write_all(
        db, summary, findings,
        tmp_path / "report.html", tmp_path / "summary.csv", tmp_path / "findings.csv",
    )

    html = paths["html"].read_text(encoding="utf-8")
    assert "PRICE_DRIFT" in html
    assert "Standard_D2s_v3" in html
    assert "+18.00%" in html
    assert "<title>" in html

    with open(paths["summary_csv"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["arm_sku_name"] == "Standard_D2s_v3"
    assert float(rows[0]["previous_price"]) == pytest.approx(0.096)
    assert float(rows[0]["delta_pct"]) == pytest.approx(18.0)
    assert rows[0]["is_synthetic"] == "1"

    with open(paths["findings_csv"], encoding="utf-8") as handle:
        finding_rows = list(csv.DictReader(handle))
    assert any(r["rule_id"] == "PRICE_DRIFT" for r in finding_rows)


def test_html_escapes_untrusted_text(db, record_factory, tmp_path):
    """A meter name with markup must not break (or inject into) the report."""
    run = db.start_run(new_run_id())
    db.insert_records(run, [record_factory(meter_name="<script>alert(1)</script>")])
    db.finish_run(run)
    run_row = db.query("SELECT * FROM runs WHERE run_id = ?", (run,))[0]
    summary = RunSummary(
        run_id=run, previous_run_id=None, started_at=run_row["started_at"],
        finished_at=run_row["finished_at"] or "", records=1, targets=["t"],
        findings=[], ui_observations=0, drift_threshold_pct=5.0, notes=[],
    )
    paths = write_all(
        db, summary, [], tmp_path / "r.html", tmp_path / "s.csv", tmp_path / "f.csv"
    )
    html = paths["html"].read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
