# Azure Price Guard

A self-contained Python framework that **scrapes, validates and monitors Azure
pricing**, stores every snapshot in SQLite, and proves in SQL when a price
duplicates, disappears or moves.

This repository is a **working demo built in under an hour**, not a finished
product. Everything in it runs: the numbers in the report came from the live
public Azure pricing API, and the test suite executes end to end. The section
[What is complete vs. what connects in the project](#what-is-complete-vs-what-connects-in-the-project)
is deliberately blunt about the boundary.

**Live sample report:** https://carlosisaacfaura.github.io/azure-price-guard/

---

## What it does

```
Azure Retail Prices API (public, anonymous)  ─┐
                                              ├─►  SQLite (full snapshot history)
Azure portal via Selenium Page Objects       ─┘            │
                                                           ▼
                                              SQL validation rules
                                     (duplicates · gaps · drift · UI-vs-API)
                                                           │
                                                           ▼
                                    report.html · run.log · summary.csv
```

| Requirement from the brief | Where it lives |
|---|---|
| Selenium + Page Object Model, **data-driven search bar paths** | `price_guard/ui/pages.py`, paths in `config.yaml → ui.search_paths` |
| `requests` calls to the pricing REST endpoints | `price_guard/api_client.py` |
| Side-by-side UI vs API comparison | `ui_observations` table + rule `UI_API_MISMATCH` |
| SQLite storage with history | `price_guard/db.py` |
| SQL rules: duplicates, gaps, sudden changes | `price_guard/validation_rules.py` |
| PyTest + HTML report, run log, CSV for finance | `pytest.ini`, `price_guard/reporting.py`, `reports/` |
| One config file for regions / SKUs / thresholds | `config.yaml` |
| Reporting helper + shared waits/parsers utilities | `price_guard/reporting.py`, `price_guard/utils.py` |
| Proof that an intentional price change is detected | `tests/test_drift_detection.py` + `python -m price_guard --demo` |

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.10+. No Azure account, no API key, no credentials of any kind: the
[Azure Retail Prices API](https://prices.azure.com/api/retail/prices) is public
and anonymous.

## Run it

```bash
# Full acceptance scenario: two live collections, one intentional price change,
# one deliberately removed meter, plus the UI cross-check.
python -m price_guard --demo --fake-ui --inject-gap --fresh

# A plain monitoring run (compares against whatever ran before it):
python -m price_guard

# Change the drift threshold, the regions or the SKUs: edit config.yaml only.
python -m price_guard --config config.yaml --inject-drift-pct 25
```

Output:

```
reports/report.html         run report - findings, UI cross-check, price detail
reports/run.log             full execution log
reports/summary.csv         one row per meter: previous vs current price
reports/findings.csv        one row per finding, for the finance queue
reports/pytest-report.html  PyTest HTML report
data/price_guard.db         SQLite database, every snapshot ever taken
```

## Test it

```bash
python -m pytest                 # 53 tests
python -m pytest -m "not live"   # skip the one test that calls Azure
python -m pytest tests/test_drift_detection.py -v
```

Last run: **53 passed**.

---

## The acceptance demo: proving an intentional price change is caught

`tests/test_drift_detection.py` is the centre of the demo.

1. Store a baseline snapshot.
2. Store a second snapshot with **identical** prices and assert the drift rule
   stays silent — no false positives.
3. Multiply **one** meter by 1.18.
4. Assert the `PRICE_DRIFT` SQL rule returns **exactly** that meter, with the
   correct old price, new price and `+18.00%`, and nothing else.

It also pins the boundaries: a +3% move does not trip a 5% threshold but does
trip a 1% one (config, not code); a −25% move is caught as well as a rise; a
50% swing on a `0.000002` meter is suppressed by the absolute floor.

The same scenario runs against **live Azure data** with
`python -m price_guard --demo`. In the published report, out of 1,514 real
price records collected twice from the live API, the rules produced exactly
two HIGH findings — the injected price change and the removed meter — and
zero false positives.

### About synthetic data

Exactly two figures in the report are not from Azure, and both are labelled
`SYNTHETIC` in the HTML and flagged `is_synthetic = 1` in the database:

* the injected +18% price change, and
* the meter deleted from the second snapshot to demonstrate gap detection.

Every other price is what the Azure Retail Prices API returned at run time.

---

## Validation rules (all SQL)

Rules are SELECT statements in `price_guard/validation_rules.py`, not Python
`if` chains — an auditor can paste any of them into a SQLite console and
reproduce the result by hand.

| Rule | Severity | Fires when |
|---|---|---|
| `PRICE_DRIFT` | HIGH | Run-over-run change exceeds `drift_threshold_pct` **and** the absolute floor |
| `PRICE_GAP` | HIGH | A meter present in the previous run is missing from this one |
| `DUPLICATE_METER` | HIGH | One natural key carries **conflicting** prices in a single run |
| `UI_API_MISMATCH` | HIGH | The portal price reconciles with **no** API meter of that SKU |
| `NON_POSITIVE_PRICE` | HIGH / LOW | Negative price (defect) / zero price (usually a free operation) |
| `MISSING_UNIT` | LOW | A price with no unit of measure cannot be reconciled |

**The natural key is `sku + region + meterId + priceType + unitOfMeasure + tierMinimumUnits`.**

### Three things the live data taught this code

These were found by running against the real API, not by reading docs, and
each has a regression test:

1. **Volume tiers share one `meterId`.** Storage bills the first 50 TB, the
   next 450 TB and the rest under a single meter, distinguished only by
   `tierMinimumUnits`. Leaving it out of the key made every tiered meter a
   false duplicate and turned tier 1 vs tier 3 into a phantom **+400% drift**.
2. **The API returns superseded prices next to the live one.** Rows carrying
   an `effectiveEndDate` in the past come back in the same response. Keeping
   them double-counts every meter and manufactures drift
   (`filters.only_current_prices`).
3. **One meter is legitimately listed under several products** at the same
   price — 80 such repeats in Storage/eastus alone. `DUPLICATE_METER`
   therefore fires only on *conflicting* prices; identical repeats are noise
   that would bury the real findings.

Fixing 1 and 2 took the demo run from 302 findings (mostly phantom) to 98, of
which the only two HIGH findings are the two intentionally injected ones.

---

## Configuration — one file, no code edits

Everything an analyst would want to change is in `config.yaml`: services,
regions, SKU lists, price types to keep, the drift threshold and its absolute
floor, the UI tolerance, locators, search paths, and output paths.

```yaml
targets:
  - name: "vm-eastus-general-purpose"
    service_name: "Virtual Machines"
    arm_region_name: "eastus"
    arm_sku_names: ["Standard_D2s_v3", "Standard_D4s_v3", "Standard_B2ms"]

validation:
  drift_threshold_pct: 5.0
  drift_min_absolute_usd: 0.0001
```

---

## The Page Object Model and the data-driven search paths

The first acceptance criterion was that the search bar paths be data-driven.
They are: a path is an ordered list of steps in YAML, each naming a locator
and an action. Navigating a new part of the portal is a YAML change.

```yaml
ui:
  search_paths:
    - name: "calculator_service_search"
      steps:
        - {action: "wait_visible", locator: "search_input"}
        - {action: "clear",        locator: "search_input"}
        - {action: "type",         locator: "search_input", value: "{service_name}"}
        - {action: "click",        locator: "search_submit"}
        - {action: "wait_visible", locator: "first_result"}
        - {action: "click",        locator: "first_result"}
  locators:
    search_input:  {by: "css", value: "input[data-testid='pricing-search-input']"}
    search_submit: {by: "css", value: "button[data-testid='pricing-search-submit']"}
```

`BasePage.follow_search_path()` executes them; `PricingCalculatorPage` composes
them into a full journey. `tests/test_page_objects.py` asserts on the exact
interaction sequence produced, and one test fails the build if any YAML step
references a locator that is not declared.

The Page Objects **never import Selenium**. They talk to a `BrowserDriver`
interface with two implementations:

* `SeleniumDriver` — real Chrome, real explicit waits. Production.
* `FakeDriver` — a deterministic in-memory DOM keyed by the *same* locator
  strings from `config.yaml`. Used by the tests and by this demo.

Switching is one line: `ui.driver: chrome` in `config.yaml`.

---

## What is complete vs. what connects in the project

**Complete and running here**

- Azure Retail Prices API client: OData filters, pagination, retries, currency,
  normalisation, expired-row handling. Verified against the live endpoint.
- SQLite schema with full snapshot history and a findings ledger.
- All six validation rules, in SQL, with tests.
- Page Object Model, data-driven search paths, locator registry, price parser.
- Reporting: HTML, run log, summary CSV, findings CSV, PyTest HTML report.
- 53 tests, all passing.

**Structured here, connected in the project**

- **The live Selenium half.** `SeleniumDriver` is written and the Page Objects
  drive it unchanged, but the demo runs on `FakeDriver` so anyone can
  reproduce it with no browser, no driver binary and no Azure login. The
  locators in `config.yaml` are placeholders shaped like the real ones — the
  first task on the real engagement is to walk the authenticated portal and
  replace them, which is a config edit plus a `pip install selenium`.
  This is the honest reason the UI section of the sample report is populated
  from the fake driver, and the report says so on its face.
- **Scheduling and alerting.** The framework is a single command; wiring it to
  cron/Task Scheduler/CI and sending the findings CSV to finance is not built.
- **Excel output.** Finance gets CSV today; `openpyxl` formatting was cut for
  time.
- **Multi-currency.** Everything runs in USD; the client sends `currencyCode`,
  so other currencies are a config value plus tests.

---

## Layout

```
config.yaml                     every switch an analyst needs
price_guard/
  config.py                     typed config loader
  api_client.py                 Azure Retail Prices API client
  db.py                         SQLite schema, history, findings ledger
  validation_rules.py           the six rules, as SQL
  reporting.py                  HTML / CSV reporting helper
  utils.py                      logging, waits, retries, parsers
  runner.py                     collect → persist → validate → report
  ui/
    driver.py                   BrowserDriver + SeleniumDriver + FakeDriver
    pages.py                    BasePage, PricingCalculatorPage
tests/                          53 tests
reports/                        generated deliverables (committed as samples)
docs/index.html                 the sample report, published on GitHub Pages
```

## Licence

MIT.
