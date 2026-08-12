from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from price_guard.api_client import PriceRecord  # noqa: E402
from price_guard.config import load_config  # noqa: E402
from price_guard.db import PriceDatabase  # noqa: E402


@pytest.fixture(scope="session")
def config():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def db(tmp_path):
    database = PriceDatabase(tmp_path / "test.db")
    yield database
    database.close()


def make_record(
    arm_sku_name: str = "Standard_D2s_v3",
    arm_region_name: str = "eastus",
    meter_id: str = "meter-001",
    retail_price: float = 0.096,
    price_type: str = "Consumption",
    unit_of_measure: str = "1 Hour",
    meter_name: str = "D2s v3",
) -> PriceRecord:
    """Build a synthetic PriceRecord for unit tests (never used in reports)."""
    return PriceRecord(
        service_name="Virtual Machines",
        service_family="Compute",
        arm_region_name=arm_region_name,
        location="US East",
        arm_sku_name=arm_sku_name,
        sku_name=meter_name,
        product_name="Virtual Machines Dsv3 Series",
        meter_id=meter_id,
        meter_name=meter_name,
        unit_of_measure=unit_of_measure,
        price_type=price_type,
        retail_price=retail_price,
        unit_price=retail_price,
        currency_code="USD",
        effective_start_date="2026-01-01T00:00:00Z",
        is_primary_meter_region=1,
    )


@pytest.fixture
def record_factory():
    return make_record
