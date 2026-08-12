"""Client for the Azure Retail Prices API.

Endpoint: https://prices.azure.com/api/retail/prices
It is public and anonymous - no subscription, no key, no Azure login.
Filtering is OData (`$filter`) on serviceName / armRegionName / armSkuName.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterator

import requests

from .config import ApiConfig, Filters, Target
from .utils import get_logger, retry, utc_now_iso

log = get_logger("api")


@dataclass(frozen=True)
class PriceRecord:
    """One meter, normalised. Mirrors a row of `price_snapshots`."""

    service_name: str
    service_family: str
    arm_region_name: str
    location: str
    arm_sku_name: str
    sku_name: str
    product_name: str
    meter_id: str
    meter_name: str
    unit_of_measure: str
    price_type: str
    retail_price: float
    unit_price: float
    currency_code: str
    effective_start_date: str
    is_primary_meter_region: int
    # Tiered meters (e.g. the first 50 TB of storage vs the next 450 TB) share
    # ONE meterId and differ only by tierMinimumUnits. Leaving it out of the
    # key makes every tiered meter look like a duplicate, and makes tier 1 vs
    # tier 3 look like a 400% price drift. Learned from live data.
    tier_minimum_units: float = 0.0
    effective_end_date: str = ""

    @property
    def natural_key(self) -> tuple[str, str, str, str, str, float]:
        """sku + region + meter + price type + unit + tier."""
        return (
            self.arm_sku_name,
            self.arm_region_name,
            self.meter_id,
            self.price_type,
            self.unit_of_measure,
            self.tier_minimum_units,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _escape(value: str) -> str:
    """OData string literals escape a single quote by doubling it."""
    return value.replace("'", "''")


def build_filter(target: Target, price_types: list[str] | None = None) -> str:
    """Compose the OData `$filter` for one configured target."""
    clauses = [
        f"serviceName eq '{_escape(target.service_name)}'",
        f"armRegionName eq '{_escape(target.arm_region_name)}'",
    ]
    if target.arm_sku_names:
        skus = " or ".join(
            f"armSkuName eq '{_escape(sku)}'" for sku in target.arm_sku_names
        )
        clauses.append(f"({skus})")
    if price_types:
        types = " or ".join(f"type eq '{_escape(t)}'" for t in price_types)
        clauses.append(f"({types})")
    return " and ".join(clauses)


def to_record(item: dict[str, Any]) -> PriceRecord:
    """Normalise one raw API item. Missing fields become empty, never faked."""
    return PriceRecord(
        service_name=item.get("serviceName", ""),
        service_family=item.get("serviceFamily", ""),
        arm_region_name=item.get("armRegionName", ""),
        location=item.get("location", ""),
        arm_sku_name=item.get("armSkuName", "") or item.get("skuName", ""),
        sku_name=item.get("skuName", ""),
        product_name=item.get("productName", ""),
        meter_id=item.get("meterId", ""),
        meter_name=item.get("meterName", ""),
        unit_of_measure=item.get("unitOfMeasure", ""),
        price_type=item.get("type", ""),
        retail_price=float(item.get("retailPrice", 0.0) or 0.0),
        unit_price=float(item.get("unitPrice", 0.0) or 0.0),
        currency_code=item.get("currencyCode", ""),
        effective_start_date=item.get("effectiveStartDate", ""),
        is_primary_meter_region=int(bool(item.get("isPrimaryMeterRegion", False))),
        tier_minimum_units=float(item.get("tierMinimumUnits", 0.0) or 0.0),
        effective_end_date=item.get("effectiveEndDate", "") or "",
    )


def is_expired(record: PriceRecord, now_iso: str | None = None) -> bool:
    """True when the API returned a superseded price.

    The endpoint happily returns historical rows alongside the live one for
    the same meter (they carry an `effectiveEndDate` in the past). Keeping
    them would double-count every meter and manufacture phantom drift.
    """
    if not record.effective_end_date:
        return False
    return record.effective_end_date < (now_iso or utc_now_iso())


class RetailPricesClient:
    """Thin, retrying, paginating wrapper over the public pricing endpoint."""

    def __init__(self, api: ApiConfig, session: requests.Session | None = None) -> None:
        self.api = api
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            response = self.session.get(url, params=params, timeout=self.api.timeout_seconds)
            response.raise_for_status()
            return response.json()

        return retry(
            call,
            attempts=self.api.max_retries,
            backoff_seconds=self.api.retry_backoff_seconds,
            exceptions=(requests.RequestException, ValueError),
            description=f"GET {url}",
        )

    def iter_target(self, target: Target, filters: Filters) -> Iterator[PriceRecord]:
        """Yield every record for one target, following NextPageLink."""
        odata_filter = build_filter(target, filters.price_types)
        params = {
            "api-version": self.api.api_version,
            "currencyCode": self.api.currency,
            "$filter": odata_filter,
        }
        log.info("Querying target '%s': %s", target.name, odata_filter)

        url: str | None = self.api.base_url
        pages = 0
        emitted = 0
        while url and pages < self.api.max_pages_per_target:
            payload = self._get(url, params if pages == 0 else None)
            items = payload.get("Items", [])
            pages += 1
            for item in items:
                record = to_record(item)
                if self._excluded(record, filters):
                    continue
                emitted += 1
                yield record
            url = payload.get("NextPageLink") or None
        log.info(
            "Target '%s': %s records kept across %s page(s)", target.name, emitted, pages
        )

    @staticmethod
    def _excluded(record: PriceRecord, filters: Filters) -> bool:
        for needle in filters.exclude_meter_name_contains:
            if needle.lower() in record.meter_name.lower():
                return True
        if filters.price_types and record.price_type not in filters.price_types:
            return True
        if filters.only_current_prices and is_expired(record):
            return True
        return False

    def fetch_all(self, targets: list[Target], filters: Filters) -> list[PriceRecord]:
        records: list[PriceRecord] = []
        for target in targets:
            records.extend(self.iter_target(target, filters))
        log.info("Fetched %s records in total from the Retail Prices API", len(records))
        return records
