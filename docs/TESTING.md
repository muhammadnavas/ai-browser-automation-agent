# Testing & Verification Guide

This document describes how to run the automated test suite, execute live verification demos, and verify the capabilities of the **AI Browser Automation Agent** components.

---

## 1. Directory Structure of Tests

All test cases, resources, and configurations are organized under the `tests/` folder:

```
tests/
├── resources/              # HTML and resource files for testing
│   ├── alert_demo.html
│   ├── download_demo.html
│   ├── form_demo.html
│   ├── popup_demo.html
│   └── ...
├── test_agent.py           # Verifies AgentTaskRunner, trajectory tracking, and planning loop
├── test_browser_module.py  # Comprehensive tests for Playwright browser controls, actions, tabs, session, dialogs
├── test_extractor.py       # Verifies InfoExtractor auto-detection logic and all 7 extraction modules
├── test_form_agent.py      # Verifies high-level form filler workflows
├── test_form_discovery.py  # Verifies form field parsing, label inference chain, and suggested data mapping
└── test_pool_stealth.py    # Verifies BrowserPool singleton lifecycle and stealth bot-evasion injections
```

---

## 2. Automated Tests (pytest)

The project uses `pytest` (with `pytest-asyncio` for async loop execution) to verify backend functionality.

### Prerequisites

Make sure the development and testing dependencies are installed:
```bash
pip install -r requirements.txt
playwright install chromium
```

### Running the Test Suite

Run all tests in verbose mode:
```bash
python -m pytest -v
```

Run a specific test file:
```bash
# Run only browser automation module tests
python -m pytest tests/test_browser_module.py -v

# Run only information extraction tests
python -m pytest tests/test_extractor.py -v
```

Run a specific test case inside a file:
```bash
python -m pytest tests/test_browser_module.py -k "test_click" -v
```


### Focused Phase 1 planner and action-verification tests

Run the planner-provider, adaptive-replanning, and action-verification regression tests from the repository root:

```bash
python -m pytest tests/test_groq_fallback.py tests/test_verification_fallback.py tests/test_recovery_policy.py -v
```

The verification tests cover extension action preconditions (target presence, visibility and enabled state), supported postconditions (navigation destination, text values, selected values, checkbox/radio state), and conservative self-healing of stale extension locators. Locator recovery is accepted only for one unique visible, enabled semantic match; ambiguous matches are rejected. Clicks with ambiguous outcomes are deliberately not retried automatically because they may have caused a side effect already.


---

## 3. Live Headed Verification Demos

In addition to headless unit tests, the project provides three interactive, headed demonstration scripts under `scripts/` to verify capabilities visually in real-time.

### 3.1. Full Module Verification Demo (`scripts/run_demo_task.py`)
Launches a headed Chromium browser and exercises almost every capability in a single flow (navigation, mouse gestures, form typing, popup interception, file download, alert/prompt dialogs, session cookies saving, and full-page screenshot capturing) against an internal local HTTP server:

```bash
python scripts/run_demo_task.py
```

### 3.2. Form Autofill Live Demo (`scripts/demo_form_fill.py`)
Loads a local form, triggers the Form Discovery Engine to detect all fields, suggest data mappings to a mock user profile, and automatically populates the form without manual CSS selectors:

```bash
python scripts/demo_form_fill.py
```

### 3.3. Data Extraction Live Demo (`scripts/demo_extraction.py`)
Opens a headed browser, navigates to real/mock web pages, runs the auto-detection classifier to identify categories (product pages, news articles, search results, contact forms, tables, galleries), parses the data structure, and outputs a formatted JSON payload:

```bash
python scripts/demo_extraction.py
```

---

## 4. Test Resource Pages

The `tests/resources/` directory contains static HTML mockups used to run tests locally with zero network dependency:
* `form_demo.html`: Form widgets (text, dropdown, radios, checkboxes, file uploads).
* `popup_demo.html`: Links and scripts triggering popup tabs/windows.
* `alert_demo.html`: Standard JS triggers (`alert()`, `confirm()`, `prompt()`).
* `download_demo.html`: Anchor links triggering native file downloads.


Recovery policy tests also verify that locator/timeout/navigation failures remain eligible for bounded retries, while extension disconnects, approval/permission failures, and unknown errors stop without repeatedly issuing the action.

Checkpoint restoration preserves the saved plan, action counters, and structured task context. The extension starts a fresh task tab on continuation, so the agent inspects the current live page and must not replay previously recorded actions. This is checkpoint-aware continuation, not a guarantee that an interrupted browser tab or authenticated session can be reconstructed after a process crash.


Checkpoint recovery regression tests verify that saved progress, plan milestones, and task context are restored, while terminal checkpoints (completed, failed, cancelled) are rejected. Restored plan-version metadata is preserved during startup. Continuation uses a fresh extension task tab and must inspect live page state; it does not recreate a prior authenticated tab or guarantee safe recovery from an unknown in-flight side effect.

Live resume validation stores the last confirmed action's page URL in the execution checkpoint. On restoration, the agent opens that URL in a new extension-managed tab, inspects the page, and compares the actual URL before allowing another action. Missing or mismatched checkpoints fail closed. This verifies URL continuity only; it does not prove that every server-side effect or page-local state was preserved.
