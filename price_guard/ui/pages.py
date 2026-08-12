"""Page Object Model for the Azure pricing pages.

`BasePage` owns the plumbing every page shares: locator lookup from config,
explicit waits and, crucially, the execution of a *data-driven search path*.
A search path is a list of steps declared in config.yaml, so navigating a new
part of the portal is a YAML change, not a code change.

`PricingCalculatorPage` is the concrete page used by the price cross-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Locator, SearchPath, SearchStep, UiConfig
from ..utils import get_logger, parse_price, render_template, utc_now_iso
from .driver import BrowserDriver, ElementNotFound

log = get_logger("ui.pages")


class UnknownLocator(KeyError):
    pass


class UnknownAction(ValueError):
    pass


@dataclass(frozen=True)
class UiPriceObservation:
    """What the browser saw, ready to be persisted next to the API price."""

    service_name: str
    arm_region_name: str
    arm_sku_name: str
    raw_price_text: str
    observed_price: float | None
    source_page: str
    search_path: str
    captured_at: str
    currency_code: str = "USD"

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "arm_region_name": self.arm_region_name,
            "arm_sku_name": self.arm_sku_name,
            "raw_price_text": self.raw_price_text,
            "observed_price": self.observed_price,
            "source_page": self.source_page,
            "search_path": self.search_path,
            "captured_at": self.captured_at,
            "currency_code": self.currency_code,
        }


class BasePage:
    """Shared Page Object behaviour. No Selenium import anywhere in this file."""

    #: Overridden by subclasses; used only for provenance in the report.
    page_name = "base"

    def __init__(self, driver: BrowserDriver, ui: UiConfig) -> None:
        self.driver = driver
        self.ui = ui

    # ---------------------------------------------------------- locators
    def locator(self, name: str) -> Locator:
        """Resolve a locator by NAME from config.yaml. Never hard-coded."""
        try:
            return self.ui.locators[name]
        except KeyError as exc:
            raise UnknownLocator(
                f"Locator {name!r} is not declared in config.yaml -> ui.locators"
            ) from exc

    def wait_for(self, name: str, timeout: float | None = None) -> bool:
        return self.driver.wait_visible(
            self.locator(name), timeout or self.ui.explicit_wait_seconds
        )

    def text_of(self, name: str) -> str:
        return self.driver.find_text(self.locator(name))

    # ------------------------------------------------- data-driven paths
    def execute_step(self, step: SearchStep, context: dict[str, str]) -> None:
        """Run one declared step. The action vocabulary lives here, only here."""
        locator = self.locator(step.locator)
        value = render_template(step.value, context) if step.value else ""

        if step.action == "wait_visible":
            if not self.driver.wait_visible(locator, self.ui.explicit_wait_seconds):
                raise ElementNotFound(
                    f"{step.locator} never became visible "
                    f"({self.ui.explicit_wait_seconds}s)"
                )
        elif step.action == "click":
            self.driver.click(locator)
        elif step.action == "type":
            self.driver.type(locator, value)
        elif step.action == "clear":
            self.driver.clear(locator)
        elif step.action == "select":
            self.driver.select(locator, value)
        elif step.action == "read":
            self.driver.find_text(locator)
        else:
            raise UnknownAction(
                f"Unsupported action {step.action!r} in search path step; "
                "supported: wait_visible, click, type, clear, select, read"
            )

    def follow_search_path(self, path_name: str, context: dict[str, str]) -> SearchPath:
        """Execute a whole named search path from config.yaml."""
        try:
            path = self.ui.search_paths[path_name]
        except KeyError as exc:
            raise UnknownLocator(
                f"Search path {path_name!r} is not declared in "
                "config.yaml -> ui.search_paths"
            ) from exc
        log.info("Following search path '%s' with %s step(s)", path.name, len(path.steps))
        for index, step in enumerate(path.steps, start=1):
            log.debug("  step %s/%s: %s %s", index, len(path.steps), step.action, step.locator)
            self.execute_step(step, context)
        return path

    def dismiss_cookie_banner(self) -> None:
        """Best effort; the banner is not always rendered."""
        if "cookie_accept" in self.ui.locators and self.driver.is_visible(
            self.locator("cookie_accept")
        ):
            self.driver.click(self.locator("cookie_accept"))


class PricingCalculatorPage(BasePage):
    """The Azure pricing calculator - where a human would check a price."""

    page_name = "azure-pricing-calculator"

    def open(self) -> "PricingCalculatorPage":
        self.driver.open(self.ui.base_url)
        self.dismiss_cookie_banner()
        return self

    def search_service(self, service_name: str) -> "PricingCalculatorPage":
        """ACCEPTANCE CRITERION #1: search bar driven entirely from config."""
        self.follow_search_path(
            "calculator_service_search", {"service_name": service_name}
        )
        return self

    def select_sku(self, region_display_name: str, sku_display_name: str) -> "PricingCalculatorPage":
        self.follow_search_path(
            "calculator_sku_filter",
            {
                "region_display_name": region_display_name,
                "sku_display_name": sku_display_name,
            },
        )
        return self

    def read_price(self) -> tuple[str, float | None]:
        """Return the raw label and the parsed number (None if unparseable)."""
        raw = self.text_of("price_label")
        return raw, parse_price(raw)

    def capture_price(self, spec: dict[str, str]) -> UiPriceObservation:
        """Full journey for one cross-check entry: search -> filter -> read."""
        self.open()
        self.search_service(spec["service_name"])
        self.select_sku(spec["region_display_name"], spec["sku_display_name"])
        raw, parsed = self.read_price()
        observation = UiPriceObservation(
            service_name=spec["service_name"],
            arm_region_name=spec["arm_region_name"],
            arm_sku_name=spec["arm_sku_name"],
            raw_price_text=raw,
            observed_price=parsed,
            source_page=self.driver.current_url(),
            search_path="calculator_service_search+calculator_sku_filter",
            captured_at=utc_now_iso(),
        )
        log.info(
            "UI observed %s in %s: %r -> %s",
            spec["arm_sku_name"], spec["arm_region_name"], raw, parsed,
        )
        return observation
