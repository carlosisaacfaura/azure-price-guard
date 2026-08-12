"""Page Object Model tests.

These prove the first acceptance criterion: the search bar paths are DATA.
The assertions are on the exact interaction sequence the Page Object produced
from config.yaml, so a wrong or missing step fails the build.
"""

from __future__ import annotations

import pytest

from price_guard.config import Locator, SearchStep
from price_guard.ui.driver import ElementNotFound, FakeDriver, FakeElement, build_driver
from price_guard.ui.pages import PricingCalculatorPage, UnknownAction, UnknownLocator
from price_guard.utils import parse_price


@pytest.fixture
def dom():
    return {
        "input[data-testid='pricing-search-input']": FakeElement(),
        "button[data-testid='pricing-search-submit']": FakeElement(),
        "[data-testid='search-results'] li:first-child a": FakeElement(text="Virtual Machines"),
        "select[data-testid='region-selector']": FakeElement(options=["East US", "West Europe"]),
        "select[data-testid='instance-selector']": FakeElement(options=["D2s v3", "B2ms"]),
        "[data-testid='estimate-total'] .price": FakeElement(text="$0.0960/hour"),
        "onetrust-accept-btn-handler": FakeElement(visible=False),
    }


@pytest.fixture
def page(config, dom):
    return PricingCalculatorPage(FakeDriver(dom), config.ui)


def test_config_declares_the_search_paths(config):
    assert "calculator_service_search" in config.ui.search_paths
    assert "calculator_sku_filter" in config.ui.search_paths
    path = config.ui.search_paths["calculator_service_search"]
    assert [step.action for step in path.steps] == [
        "wait_visible", "clear", "type", "click", "wait_visible", "click",
    ]


def test_every_search_step_references_a_declared_locator(config):
    """A typo in a YAML path is caught here, not at 2am against production."""
    for path in config.ui.search_paths.values():
        for step in path.steps:
            assert step.locator in config.ui.locators, (
                f"path '{path.name}' references undeclared locator '{step.locator}'"
            )


def test_search_path_executes_declared_steps_in_order(page):
    page.search_service("Virtual Machines")
    actions = [(a, v) for a, _, v in page.driver.interactions]
    assert actions == [
        ("wait_visible", ""),
        ("clear", ""),
        ("type", "Virtual Machines"),   # placeholder {service_name} substituted
        ("click", ""),
        ("wait_visible", ""),
        ("click", ""),
    ]


def test_locators_used_come_from_config(page, config):
    page.search_service("Storage")
    used = [selector for _, selector, _ in page.driver.interactions]
    assert config.ui.locators["search_input"].value in used
    assert config.ui.locators["first_result"].value in used


def test_adding_a_step_to_config_changes_behaviour_without_code(page, config):
    """The whole point of data-driven paths: YAML edit == behaviour change."""
    before = len(page.driver.interactions)
    page.follow_search_path("calculator_service_search", {"service_name": "SQL Database"})
    baseline_steps = len(page.driver.interactions) - before

    extra = config.ui.search_paths["calculator_service_search"]
    object.__setattr__(extra, "steps", [*extra.steps, SearchStep("read", "price_label")])
    page.follow_search_path("calculator_service_search", {"service_name": "SQL Database"})
    assert len(page.driver.interactions) - before - baseline_steps == baseline_steps + 1
    object.__setattr__(extra, "steps", extra.steps[:-1])   # restore for other tests


def test_full_capture_journey_returns_a_parsed_observation(page):
    observation = page.capture_price(
        {
            "service_name": "Virtual Machines",
            "arm_region_name": "eastus",
            "arm_sku_name": "Standard_D2s_v3",
            "region_display_name": "East US",
            "sku_display_name": "D2s v3",
        }
    )
    assert observation.raw_price_text == "$0.0960/hour"
    assert observation.observed_price == pytest.approx(0.096)
    assert observation.arm_sku_name == "Standard_D2s_v3"
    assert "calculator_service_search" in observation.search_path


def test_unknown_locator_fails_loudly(page):
    with pytest.raises(UnknownLocator):
        page.locator("does_not_exist")
    with pytest.raises(UnknownLocator):
        page.follow_search_path("no_such_path", {})


def test_unknown_action_fails_loudly(page):
    with pytest.raises(UnknownAction):
        page.execute_step(SearchStep(action="teleport", locator="search_input"), {})


def test_missing_element_raises_rather_than_returning_a_default(config):
    driver = FakeDriver({})
    page = PricingCalculatorPage(driver, config.ui)
    with pytest.raises(ElementNotFound):
        driver.click(Locator(by="css", value="input[data-testid='pricing-search-input']"))
    with pytest.raises(ElementNotFound):
        page.execute_step(SearchStep("wait_visible", "search_input"), {})


def test_invalid_select_option_is_rejected(page):
    with pytest.raises(ElementNotFound):
        page.select_sku("Mars Central", "D2s v3")


def test_driver_factory_honours_config(config, dom):
    assert isinstance(build_driver(config.ui, dom), FakeDriver)
    with pytest.raises(ValueError):
        build_driver(config.ui.__class__(**{**config.ui.__dict__, "driver": "netscape"}))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$0.0960/hour", 0.096),
        ("0,096 EUR", 0.096),
        ("$1,234.56 per month", 1234.56),
        ("Free", None),
        ("", None),
        (None, None),
        (0.25, 0.25),
    ],
)
def test_price_parser_handles_portal_formats(raw, expected):
    assert parse_price(raw) == (pytest.approx(expected) if expected is not None else None)
