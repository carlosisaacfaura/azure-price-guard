"""API client tests.

Offline tests cover filter construction, normalisation and pagination against
a stubbed session. The single live test is marked `live` and hits the public
Azure endpoint - deselect it with `-m "not live"` when offline.
"""

from __future__ import annotations

import pytest
import requests

from price_guard.api_client import RetailPricesClient, build_filter, to_record
from price_guard.config import Filters, Target

SAMPLE_ITEM = {
    "currencyCode": "USD",
    "retailPrice": 0.096,
    "unitPrice": 0.096,
    "armRegionName": "eastus",
    "location": "US East",
    "effectiveStartDate": "2026-01-01T00:00:00Z",
    "meterId": "abc-123",
    "meterName": "D2s v3",
    "productName": "Virtual Machines Dsv3 Series",
    "skuName": "D2s v3",
    "serviceName": "Virtual Machines",
    "serviceFamily": "Compute",
    "unitOfMeasure": "1 Hour",
    "type": "Consumption",
    "isPrimaryMeterRegion": True,
    "armSkuName": "Standard_D2s_v3",
}


def test_filter_includes_service_region_and_skus():
    target = Target(
        name="t", service_name="Virtual Machines", arm_region_name="eastus",
        arm_sku_names=["Standard_D2s_v3", "Standard_B2ms"],
    )
    odata = build_filter(target, ["Consumption"])
    assert "serviceName eq 'Virtual Machines'" in odata
    assert "armRegionName eq 'eastus'" in odata
    assert "armSkuName eq 'Standard_D2s_v3' or armSkuName eq 'Standard_B2ms'" in odata
    assert "type eq 'Consumption'" in odata


def test_filter_omits_sku_clause_when_no_skus_configured():
    target = Target(name="t", service_name="Storage", arm_region_name="westeurope")
    assert "armSkuName" not in build_filter(target, [])


def test_filter_escapes_single_quotes():
    target = Target(name="t", service_name="Bob's Service", arm_region_name="eastus")
    assert "Bob''s Service" in build_filter(target, [])


def test_record_normalisation():
    record = to_record(SAMPLE_ITEM)
    assert record.arm_sku_name == "Standard_D2s_v3"
    assert record.retail_price == pytest.approx(0.096)
    assert record.natural_key == (
        "Standard_D2s_v3", "eastus", "abc-123", "Consumption", "1 Hour", 0.0,
    )


def test_record_normalisation_tolerates_missing_fields():
    record = to_record({"serviceName": "Storage"})
    assert record.service_name == "Storage"
    assert record.retail_price == 0.0
    assert record.arm_sku_name == ""


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubSession:
    """Returns two pages, then stops - proves NextPageLink is followed."""

    def __init__(self):
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if len(self.calls) == 1:
            return _StubResponse(
                {"Items": [SAMPLE_ITEM, {**SAMPLE_ITEM, "meterId": "def-456"}],
                 "NextPageLink": "https://prices.azure.com/page2"}
            )
        return _StubResponse(
            {"Items": [{**SAMPLE_ITEM, "meterId": "ghi-789"}], "NextPageLink": None}
        )


def test_pagination_follows_next_page_link(config):
    client = RetailPricesClient(config.api, session=_StubSession())
    target = Target(name="t", service_name="Virtual Machines", arm_region_name="eastus")
    records = list(client.iter_target(target, Filters(["Consumption"], [])))
    assert [r.meter_id for r in records] == ["abc-123", "def-456", "ghi-789"]
    assert len(client.session.calls) == 2


def test_excluded_meters_are_dropped(config):
    class SpotSession(_StubSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return _StubResponse(
                {"Items": [SAMPLE_ITEM, {**SAMPLE_ITEM, "meterName": "D2s v3 Spot"}],
                 "NextPageLink": None}
            )

    client = RetailPricesClient(config.api, session=SpotSession())
    target = Target(name="t", service_name="Virtual Machines", arm_region_name="eastus")
    records = list(client.iter_target(target, Filters(["Consumption"], ["Spot"])))
    assert len(records) == 1
    assert records[0].meter_name == "D2s v3"


def test_wrong_price_type_is_dropped(config):
    class ReservationSession(_StubSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return _StubResponse(
                {"Items": [{**SAMPLE_ITEM, "type": "Reservation"}], "NextPageLink": None}
            )

    client = RetailPricesClient(config.api, session=ReservationSession())
    target = Target(name="t", service_name="Virtual Machines", arm_region_name="eastus")
    assert list(client.iter_target(target, Filters(["Consumption"], []))) == []


@pytest.mark.live
def test_live_public_endpoint_returns_real_prices(config):
    """Hits https://prices.azure.com - public, anonymous, no credentials."""
    client = RetailPricesClient(config.api)
    target = Target(
        name="live", service_name="Virtual Machines", arm_region_name="eastus",
        arm_sku_names=["Standard_D2s_v3"],
    )
    try:
        records = list(client.iter_target(target, Filters(["Consumption"], ["Spot", "Low Priority"])))
    except requests.RequestException as exc:
        pytest.skip(f"Azure Retail Prices API unreachable: {exc}")

    assert records, "the public API returned no Standard_D2s_v3 meters for eastus"
    assert all(r.currency_code == "USD" for r in records)
    assert all(r.arm_region_name == "eastus" for r in records)
    assert any(r.retail_price > 0 for r in records)


def test_expired_prices_are_dropped(config):
    """The API returns superseded rows next to the live one for a meter."""
    class HistorySession(_StubSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return _StubResponse(
                {"Items": [
                    {**SAMPLE_ITEM, "retailPrice": 0.080,
                     "effectiveStartDate": "2025-01-01T00:00:00Z",
                     "effectiveEndDate": "2025-12-31T23:59:00Z"},   # superseded
                    {**SAMPLE_ITEM, "retailPrice": 0.096},          # live
                ], "NextPageLink": None}
            )

    target = Target(name="t", service_name="Virtual Machines", arm_region_name="eastus")
    client = RetailPricesClient(config.api, session=HistorySession())
    records = list(client.iter_target(target, Filters(["Consumption"], [])))
    assert [r.retail_price for r in records] == [pytest.approx(0.096)]

    keep_all = Filters(["Consumption"], [], only_current_prices=False)
    client = RetailPricesClient(config.api, session=HistorySession())
    assert len(list(client.iter_target(target, keep_all))) == 2


def test_pricing_tier_is_part_of_the_natural_key():
    """Volume tiers of one meter share a meterId and must not collide."""
    tier1 = to_record({**SAMPLE_ITEM, "tierMinimumUnits": 0.0, "retailPrice": 0.2})
    tier2 = to_record({**SAMPLE_ITEM, "tierMinimumUnits": 51200.0, "retailPrice": 1.0})
    assert tier1.meter_id == tier2.meter_id
    assert tier1.natural_key != tier2.natural_key
