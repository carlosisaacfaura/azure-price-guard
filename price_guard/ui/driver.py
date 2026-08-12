"""Browser abstraction.

The Page Objects never import Selenium. They talk to `BrowserDriver`, which
has two implementations:

* `SeleniumDriver` - the production one. Real Chrome, real waits. It imports
  Selenium lazily so the package installs and the test suite runs on a machine
  with no browser and no driver binary.
* `FakeDriver`     - an in-memory DOM used by the test suite and by the public
  demo. It records every interaction, so the tests assert on the exact
  sequence of steps a data-driven search path produced.

Swapping one for the other is a single line in config.yaml (`ui.driver`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Locator, UiConfig
from ..utils import get_logger, wait_until

log = get_logger("ui.driver")


class ElementNotFound(RuntimeError):
    pass


class BrowserDriver(Protocol):
    """The only surface the Page Objects are allowed to use."""

    def open(self, url: str) -> None: ...
    def find_text(self, locator: Locator) -> str: ...
    def click(self, locator: Locator) -> None: ...
    def type(self, locator: Locator, text: str) -> None: ...
    def clear(self, locator: Locator) -> None: ...
    def select(self, locator: Locator, visible_text: str) -> None: ...
    def is_visible(self, locator: Locator) -> bool: ...
    def wait_visible(self, locator: Locator, timeout: float) -> bool: ...
    def current_url(self) -> str: ...
    def quit(self) -> None: ...


# --------------------------------------------------------------------------- fake
@dataclass
class FakeElement:
    text: str = ""
    visible: bool = True
    options: list[str] = field(default_factory=list)
    value: str = ""


class FakeDriver:
    """Deterministic in-memory browser. No network, no binary, no flakiness.

    `dom` maps a locator's `value` (the CSS selector / id string) to a
    FakeElement, so the very same locators declared in config.yaml drive it.
    """

    def __init__(self, dom: dict[str, FakeElement] | None = None) -> None:
        self.dom: dict[str, FakeElement] = dom or {}
        self.interactions: list[tuple[str, str, str]] = []
        self._url = "about:blank"
        self.quit_called = False

    # -- helpers ----------------------------------------------------------
    def _element(self, locator: Locator) -> FakeElement:
        element = self.dom.get(locator.value)
        if element is None:
            raise ElementNotFound(f"No element for {locator.by}={locator.value!r}")
        return element

    def _record(self, action: str, locator: Locator, value: str = "") -> None:
        self.interactions.append((action, locator.value, value))

    # -- BrowserDriver ----------------------------------------------------
    def open(self, url: str) -> None:
        self._url = url
        self.interactions.append(("open", url, ""))

    def find_text(self, locator: Locator) -> str:
        self._record("read", locator)
        return self._element(locator).text

    def click(self, locator: Locator) -> None:
        self._record("click", locator)
        self._element(locator)

    def type(self, locator: Locator, text: str) -> None:
        self._record("type", locator, text)
        self._element(locator).value += text

    def clear(self, locator: Locator) -> None:
        self._record("clear", locator)
        self._element(locator).value = ""

    def select(self, locator: Locator, visible_text: str) -> None:
        self._record("select", locator, visible_text)
        element = self._element(locator)
        if element.options and visible_text not in element.options:
            raise ElementNotFound(
                f"Option {visible_text!r} not in {locator.value}: {element.options}"
            )
        element.value = visible_text

    def is_visible(self, locator: Locator) -> bool:
        element = self.dom.get(locator.value)
        return bool(element and element.visible)

    def wait_visible(self, locator: Locator, timeout: float) -> bool:
        self._record("wait_visible", locator)
        return self.is_visible(locator)

    def current_url(self) -> str:
        return self._url

    def quit(self) -> None:
        self.quit_called = True


# ----------------------------------------------------------------------- real
class SeleniumDriver:
    """Production driver. Selenium is imported inside __init__ on purpose."""

    def __init__(self, ui: UiConfig) -> None:
        from selenium import webdriver  # noqa: PLC0415 - lazy, optional dependency
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if ui.headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,900")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")

        self.ui = ui
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(ui.implicit_wait_seconds)
        log.info("Selenium Chrome session started (headless=%s)", ui.headless)

    def _by(self, locator: Locator) -> tuple[Any, str]:
        from selenium.webdriver.common.by import By  # noqa: PLC0415

        mapping = {
            "css": By.CSS_SELECTOR,
            "xpath": By.XPATH,
            "id": By.ID,
            "name": By.NAME,
            "class": By.CLASS_NAME,
            "link_text": By.LINK_TEXT,
            "partial_link_text": By.PARTIAL_LINK_TEXT,
            "tag": By.TAG_NAME,
        }
        if locator.by not in mapping:
            raise ValueError(f"Unsupported locator strategy: {locator.by}")
        return mapping[locator.by], locator.value

    def _find(self, locator: Locator) -> Any:
        from selenium.common.exceptions import NoSuchElementException  # noqa: PLC0415

        try:
            return self.driver.find_element(*self._by(locator))
        except NoSuchElementException as exc:
            raise ElementNotFound(f"{locator.by}={locator.value}") from exc

    def open(self, url: str) -> None:
        self.driver.get(url)

    def find_text(self, locator: Locator) -> str:
        return self._find(locator).text

    def click(self, locator: Locator) -> None:
        self._find(locator).click()

    def type(self, locator: Locator, text: str) -> None:
        self._find(locator).send_keys(text)

    def clear(self, locator: Locator) -> None:
        self._find(locator).clear()

    def select(self, locator: Locator, visible_text: str) -> None:
        from selenium.webdriver.support.ui import Select  # noqa: PLC0415

        Select(self._find(locator)).select_by_visible_text(visible_text)

    def is_visible(self, locator: Locator) -> bool:
        try:
            return self._find(locator).is_displayed()
        except ElementNotFound:
            return False

    def wait_visible(self, locator: Locator, timeout: float) -> bool:
        return wait_until(
            lambda: self.is_visible(locator),
            timeout_seconds=timeout,
            description=f"{locator.by}={locator.value}",
        )

    def current_url(self) -> str:
        return self.driver.current_url

    def quit(self) -> None:
        self.driver.quit()


def build_driver(ui: UiConfig, fake_dom: dict[str, FakeElement] | None = None) -> BrowserDriver:
    """Factory driven by `ui.driver` in config.yaml."""
    if ui.driver == "chrome":
        return SeleniumDriver(ui)
    if ui.driver == "fake":
        return FakeDriver(fake_dom)
    raise ValueError(f"Unknown ui.driver: {ui.driver!r} (expected 'chrome' or 'fake')")
