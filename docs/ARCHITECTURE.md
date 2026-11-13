# System Architecture & Technical Design

This document describes the high-level architecture, module decomposition, execution lifecycle, and concurrency model of the **AI Browser Automation Agent**.

---

## 1. High-Level System Architecture

The system is organized into modular layers that separate API transport, agent planning, browser execution, structured extraction, and reporting:

```mermaid
graph TD
    subgraph Client Layer
        WebUI[Web UI Dashboard<br/>static/index.html & app.js]
        Demos[Terminal Demos<br/>scripts/run_demo_task.py]
        ExternalClient[External REST / WS Clients]
    end

    subgraph API & Routing Layer
        FastAPI[FastAPI Application<br/>main.py]
        Routes[API Router & WS Endpoints<br/>api/routes.py]
    end

    subgraph Orchestration & Planning Layer
        TM[TaskManager<br/>api/task_manager.py]
        Planner[AIAgentPlanner<br/>agents/planner.py]
        Gemini[Google Gemini API<br/>gemini-3.5-flash]
        Heuristics[Heuristic Rule Engine]
    end

    subgraph Browser Automation Layer
        Controller[BrowserController<br/>browser/controller.py]
        Pool[BrowserPool Singleton<br/>browser/pool.py]
        Actions[browser.actions]
        Tabs[browser.tabs - TabManager]
        Dialogs[browser.dialogs]
        Downloader[browser.downloader - DownloadManager]
        Session[browser.session - SessionManager]
        Waiter[browser.waiter]
        Stealth[browser.stealth]
    end

    subgraph Extraction & Reporting Layer
        Extractor[InfoExtractor<br/>browser/extractor.py]
        Discovery[FormDiscovery<br/>browser/form_discovery.py]
        Filler[FormFiller<br/>browser/form_filler.py]
        ExtractionModules[extraction/*<br/>product, news, search, contact, tables, images, metadata]
        Reports[ReportGenerator<br/>reports/generator.py]
    end

    WebUI -->|HTTP & WebSocket| Routes
    Demos -->|Direct Call| TM
    ExternalClient -->|HTTP REST / WS| Routes
    Routes --> FastAPI
    FastAPI --> Routes
    Routes --> TM
    Routes --> Pool

    TM --> Planner
    Planner -->|API Key Present| Gemini
    Planner -->|Fallback| Heuristics

    TM --> Controller
    Controller --> Actions
    Controller --> Tabs
    Controller --> Dialogs
    Controller --> Downloader
    Controller --> Session
    Controller --> Waiter

    Pool --> Stealth
    Pool --> Extractor
    Pool --> Discovery
    Discovery --> Filler

    Extractor --> ExtractionModules
    TM --> Reports
```

---

## 2. Layer-by-Layer Architectural Breakdown

### 2.1. Presentation & API Layer (`main.py`, `api/routes.py`, `static/`)
- **FastAPI Application**: Exposes REST endpoints for task submission, manual approval, direct coordinate clicking, data extraction, form discovery, and report downloads.
- **WebSocket Streaming (`/api/ws/tasks/{task_id}`)**: Delivers real-time task progress, step descriptions, timing, and screenshot URLs to connected clients.
- **Web UI Dashboard**: Single-page frontend providing live task control, interactive approval modals, remote click forwarding, extraction JSON inspector, and form discovery views.

### 2.2. Task Management & Orchestration Layer (`api/task_manager.py`, `models/schemas.py`)
- **Task Lifecycle**: Controls task state transitions (`PENDING` -> `RUNNING` -> `WAITING_APPROVAL` -> `COMPLETED` / `FAILED` / `CANCELLED`).
- **Execution Loop**: Manages the step-by-step loop: DOM snapshotting -> security check -> screenshot capture -> planning -> action execution -> step logging -> WebSocket broadcasting.
- **Human-in-the-Loop Gating**: Detects security gates (CAPTCHAs/payments), pauses execution, captures full-context screenshots, and resumes execution upon user approval or coordinate click.

### 2.3. Multi-Agent Delegation
- **Master orchestrator**: TaskManager remains the parent authority and asks the configured planner for bounded specialist work during preflight and while the live task is executing.
- **Runtime delegation**: After a successful live DOM snapshot, the master may request up to two read-only specialists for that page. Delegation is deduplicated by URL and visible page structure, capped at six page attempts per task, and skipped on authentication pages.
- **Specialists**: Navigation, research, and interaction workers inspect a dedicated task tab and return bounded evidence. Recovery diagnoses failures; verification independently checks task outcomes; form workers may populate explicitly mapped fields but never submit.
- **Shared task evidence**: Sanitized specialist results are added to task context so the primary planner can use them on its next decision.
- **Mutation authority**: Specialists do not execute browser mutations during automatic runtime delegation. The main TaskManager executor re-inspects the current DOM and remains the only component that chooses and executes the user's browser actions. Existing approval and security gates remain authoritative.
- **Boundaries**: Runtime delegation uses the current validated URL, drops query parameters, preserves SPA fragments, avoids credentials/form values, and continues the parent task if specialist planning or execution fails.

### 2.4. AI Planning & Decision Engine (`agents/planner.py`) (`agents/planner.py`)
- **LLM-Driven Mode**: Sends structured prompts to Google Gemini containing the user goal, current DOM elements, headings, recent trajectory history, and plain-text user data. Enforces strict JSON output schema.
- **Deterministic Heuristic Mode**: Fallback rule engine that requires no external API keys. Handles domain selection, search typing with Enter submission, result extraction, scrolling, and finishing.

### 2.5. Browser Automation Core (`browser/`)
- **`BrowserController`**: Main Playwright browser lifecycle manager supporting Chromium, Firefox, and WebKit in headless or headed/maximized modes.
- **`browser.actions`**: Low-level interaction primitives (clicks, typing, dropdown selection, checkboxes, radios, drag-and-drop, scrolling).
- **`browser.tabs` (`TabManager`)**: Multi-tab lifecycle and popup interception (`wait_for_popup`).
- **`browser.dialogs`**: Native JavaScript dialog handlers (`alert`, `confirm`, `prompt`) and file chooser upload interceptors.
- **`browser.downloader` (`DownloadManager`)**: Playwright download event listener with collision-free unique file path resolution.
- **`browser.session` (`SessionManager`)**: Cookie, localStorage, viewport, geolocation, and custom HTTP header management.
- **`browser.waiter`**: Centralized async wait utilities (`wait_for_selector`, `wait_for_text`, `wait_for_navigation`, `settle_page`).
- **`browser.stealth`**: Anti-bot evasion scripts overriding `navigator.webdriver`, plugins, WebGL vendor, and permissions.
- **`BrowserPool`**: Singleton warm browser instance pool managing isolated contexts and pages for fast ad-hoc extraction and form discovery.

### 2.6. Information Extraction & Form Discovery (`browser/extractor.py`, `extraction/`, `browser/form_discovery.py`)
- **`InfoExtractor`**: Auto-detects content category via multi-signal scoring (URL, metadata, JSON-LD, DOM signals) and delegates to specialized extraction modules (`product`, `news`, `search`, `contact`, `tables`, `images`, `metadata`).
- **`FormDiscovery`**: Automatically inspects forms, identifies field types, infers human-readable labels via an 8-source priority chain, and fuzzy-maps fields to user data.
- **`FormFiller`**: Provides zero-config autofill alongside pre-built workflows for login, registration, job applications, and surveys.

### 2.7. Reporting Layer (`reports/generator.py`)
- **`ReportGenerator`**: Transforms extracted task findings into standardized tabular records and writes CSV reports to `reports/report_<task_id>.csv`.

---

## 3. End-to-End Task Execution Lifecycle

The diagram below illustrates the complete sequence of events when an automation task is submitted:

```mermaid
sequenceDiagram
    autonumber
    actor User as User / Client
    participant API as FastAPI Router
    participant TM as TaskManager
    participant Planner as AIAgentPlanner
    participant BC as BrowserController
    participant Page as Playwright Page
    participant RG as ReportGenerator

    User->>API: POST /api/tasks (goal, start_url, max_steps)
    API->>TM: create_task(req)
    TM-->>API: TaskResponse (status=PENDING)
    API-->>User: Return TaskResponse JSON

    Note over TM,Page: Background Async Task Execution Starts
    TM->>BC: start() -> launch Chromium
    TM->>BC: navigate(start_url)
    BC->>Page: goto(start_url)

    loop Step Loop (step = 1 to max_steps)
        TM->>BC: parse_interactive_elements()
        BC->>Page: evaluate(DOM scan JS)
        Page-->>BC: elements list + page title
        
        TM->>BC: check_captcha_or_payment()
        BC->>Page: evaluate(security check JS)
        Page-->>BC: captcha | payment | None
        
        TM->>BC: capture_screenshot(task_id, step)
        BC->>Page: screenshot()
        
        alt Security Gate Detected
            TM->>User: WS Broadcast (status=WAITING_APPROVAL)
            User->>API: POST /api/tasks/{id}/approve or /click
            API->>TM: approve_human_step() -> event.set()
        end

        TM->>Planner: decide_next_action(goal, elements, history)
        Planner-->>TM: BrowserAction (action, selector, text, reasoning)

        TM->>BC: _execute_action(BrowserAction)
        BC->>Page: click / fill / scroll / extract / etc.
        
        TM->>User: WS Broadcast (AgentStep logged)
        
        opt Action is FINISH
            Note over TM: Loop breaks early
        end
    end

    TM->>RG: generate_report(task_id, extracted_data)
    RG-->>TM: Saved report_task-xxx.csv
    TM->>BC: stop() -> close browser
    TM->>User: WS Broadcast (status=COMPLETED, reports={csv: ...})
```

---

## 4. Concurrency, Event Loop & Scaling

### 4.1. Windows Asyncio Event Loop Policy
On Windows with Python 3.10+, Playwright requires subprocess creation within an active event loop. To avoid `NotImplementedError`, `main.py` explicitly sets the event loop policy before anything else imports or runs:

```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
```

### 4.2. Browser Context Pooling (`BrowserPool`)
For one-off operations such as structured extraction (`POST /api/extract`) and form discovery (`POST /api/forms/discover`), spinning up a full browser process per request causes high latency. 

The `BrowserPool` maintains a single warm Playwright instance in memory and acquires isolated `BrowserContext` and `Page` objects wrapped in an async context manager:

```python
pool = await get_browser_pool()
async with pool.acquire_context() as (context, page):
    await page.goto(url)
    # Context is automatically cleaned up on exit
```

---

## 5. Security & Human-in-the-Loop Architecture

The system avoids breaking on bot verification barriers through automated security checks:

1. **Detection**: `BrowserController.check_captcha_or_payment()` inspects iframes and DOM elements for reCAPTCHA, hCaptcha, Cloudflare Turnstile, and payment/credit card inputs.
2. **Interception**: If detected, `AIAgentPlanner` issues a `REQUEST_APPROVAL` action, transitioning the task status to `WAITING_APPROVAL`.
3. **Remote Interaction**: The user can either complete the challenge directly on a headed browser window or interact through the Web UI via normalized coordinate clicks (`POST /api/tasks/{task_id}/click?x=0.5&y=0.5`), which translates percentages into exact viewport pixel mouse clicks.
4. **Resumption**: Calling `POST /api/tasks/{task_id}/approve` triggers the internal `asyncio.Event` to resume automated execution.


### 2.8. Live-tab specialist observation
Runtime navigation, research, and interaction specialists can inspect the exact task tab through the parent's existing extension adapter. They do not create duplicate tabs when this mode is enabled. A per-task async observation lock serializes their DOM snapshots, and workers receive no mutation API or permission to execute actions. The main TaskManager remains the sole browser-action executor and re-inspects the page before acting. If a parent adapter is unavailable or ownership does not match, the specialist fails closed rather than attaching to another task.
