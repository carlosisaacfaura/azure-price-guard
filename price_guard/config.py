"""Typed access to config.yaml.

One file drives regions, services, SKUs, thresholds and UI locators. Nothing
in this package reads a hard-coded endpoint, region or threshold.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    api_version: str
    currency: str
    timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: float
    max_pages_per_target: int


@dataclass(frozen=True)
class Target:
    name: str
    service_name: str
    arm_region_name: str
    arm_sku_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Filters:
    price_types: list[str]
    exclude_meter_name_contains: list[str]
    #: Drop rows whose `effectiveEndDate` is in the past. The API returns
    #: superseded prices alongside the live one for the same meter.
    only_current_prices: bool = True


@dataclass(frozen=True)
class ValidationConfig:
    drift_threshold_pct: float
    drift_min_absolute_usd: float
    ui_api_tolerance_pct: float


@dataclass(frozen=True)
class Locator:
    by: str
    value: str


@dataclass(frozen=True)
class SearchStep:
    action: str
    locator: str
    value: str | None = None


@dataclass(frozen=True)
class SearchPath:
    name: str
    description: str
    steps: list[SearchStep]


@dataclass(frozen=True)
class UiConfig:
    enabled: bool
    driver: str
    headless: bool
    base_url: str
    implicit_wait_seconds: int
    explicit_wait_seconds: int
    search_paths: dict[str, SearchPath]
    locators: dict[str, Locator]
    cross_check: list[dict[str, str]]


@dataclass(frozen=True)
class ReportingConfig:
    output_dir: Path
    html_report: str
    log_file: str
    summary_csv: str
    findings_csv: str


@dataclass(frozen=True)
class Config:
    api: ApiConfig
    targets: list[Target]
    filters: Filters
    validation: ValidationConfig
    ui: UiConfig
    database_path: Path
    reporting: ReportingConfig
    root: Path

    @property
    def html_report_path(self) -> Path:
        return self.reporting.output_dir / self.reporting.html_report

    @property
    def log_path(self) -> Path:
        return self.reporting.output_dir / self.reporting.log_file

    @property
    def summary_csv_path(self) -> Path:
        return self.reporting.output_dir / self.reporting.summary_csv

    @property
    def findings_csv_path(self) -> Path:
        return self.reporting.output_dir / self.reporting.findings_csv


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Read config.yaml (or an override path) into typed dataclasses."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle)

    root = config_path.resolve().parent
    api_raw = raw["api"]
    ui_raw = raw.get("ui", {})
    rep_raw = raw.get("reporting", {})

    search_paths = {
        item["name"]: SearchPath(
            name=item["name"],
            description=item.get("description", ""),
            steps=[
                SearchStep(
                    action=step["action"],
                    locator=step["locator"],
                    value=step.get("value"),
                )
                for step in item["steps"]
            ],
        )
        for item in ui_raw.get("search_paths", [])
    }

    return Config(
        root=root,
        api=ApiConfig(
            base_url=api_raw["base_url"],
            api_version=api_raw.get("api_version", "2023-01-01-preview"),
            currency=api_raw.get("currency", "USD"),
            timeout_seconds=int(api_raw.get("timeout_seconds", 30)),
            max_retries=int(api_raw.get("max_retries", 3)),
            retry_backoff_seconds=float(api_raw.get("retry_backoff_seconds", 2)),
            max_pages_per_target=int(api_raw.get("max_pages_per_target", 3)),
        ),
        targets=[
            Target(
                name=item.get("name", f"{item['service_name']}-{item['arm_region_name']}"),
                service_name=item["service_name"],
                arm_region_name=item["arm_region_name"],
                arm_sku_names=list(item.get("arm_sku_names") or []),
            )
            for item in raw.get("targets", [])
        ],
        filters=Filters(
            price_types=list(raw.get("filters", {}).get("price_types") or []),
            exclude_meter_name_contains=list(
                raw.get("filters", {}).get("exclude_meter_name_contains") or []
            ),
            only_current_prices=bool(
                raw.get("filters", {}).get("only_current_prices", True)
            ),
        ),
        validation=ValidationConfig(
            drift_threshold_pct=float(raw["validation"]["drift_threshold_pct"]),
            drift_min_absolute_usd=float(
                raw["validation"].get("drift_min_absolute_usd", 0.0)
            ),
            ui_api_tolerance_pct=float(raw["validation"].get("ui_api_tolerance_pct", 1.0)),
        ),
        ui=UiConfig(
            enabled=bool(ui_raw.get("enabled", False)),
            driver=ui_raw.get("driver", "fake"),
            headless=bool(ui_raw.get("headless", True)),
            base_url=ui_raw.get("base_url", ""),
            implicit_wait_seconds=int(ui_raw.get("implicit_wait_seconds", 5)),
            explicit_wait_seconds=int(ui_raw.get("explicit_wait_seconds", 20)),
            search_paths=search_paths,
            locators={
                key: Locator(by=val["by"], value=val["value"])
                for key, val in (ui_raw.get("locators") or {}).items()
            },
            cross_check=list(ui_raw.get("cross_check") or []),
        ),
        database_path=_resolve(root, raw["storage"]["database_path"]),
        reporting=ReportingConfig(
            output_dir=_resolve(root, rep_raw.get("output_dir", "reports")),
            html_report=rep_raw.get("html_report", "report.html"),
            log_file=rep_raw.get("log_file", "run.log"),
            summary_csv=rep_raw.get("summary_csv", "summary.csv"),
            findings_csv=rep_raw.get("findings_csv", "findings.csv"),
        ),
    )
