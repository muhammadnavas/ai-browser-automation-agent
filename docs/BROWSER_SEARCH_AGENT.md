# Browser Search Agent — Feature Documentation

## Overview

The **Browser Search Agent** is the core implemented feature of this project. It autonomously opens a browser, navigates to a search engine (or any target URL), types a search query, submits it, extracts the resulting page data, and generates a CSV report — all without any human interaction.

The agent supports two planning modes:
- **LLM-Driven** (Google Gemini `gemini-2.5-flash`) — uses an AI model to decide every browser action
- **Heuristic Fallback** — a deterministic rule engine that handles search tasks without an API key

---

## Entry Points

### REST API — via `main.py`

Start the server:
```bash
python main.py
```

Then submit a task via HTTP POST:
```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "latest AI news", "start_url": "https://www.google.com", "headless": true}'
```

Poll the task status:
```bash
curl http://localhost:8000/api/tasks/<task_id>
```

---

## Execution Flow

The full execution lifecycle for a search task:

```
User Input (goal / query)
        |
        v
TaskManager.create_task()
        |  Creates TaskResponse (status=PENDING, task_id=uuid)
        v
TaskManager.run_task_execution()
        |
        |-> BrowserController.start()
        |       Launches Playwright Chromium instance
        |       Sets user-agent to avoid bot detection
        |
        |-> BrowserController.navigate(start_url)
        |       Navigates to Google (or custom URL)
        |       Waits for DOM content loaded
        |
        |-> Main Step Loop (repeats up to max_steps)
                |
                |-> BrowserController.parse_interactive_elements()
                |       Runs JS in browser to collect all visible
                |       buttons, inputs, links, textareas
                |       Returns indexed element map + page title/URL
                |
                |-> BrowserController.check_captcha_or_payment()
                |       Scans page HTML for CAPTCHA/payment markers
                |
                |-> BrowserController.capture_screenshot()
                |       Saves PNG to screenshots/<task_id>_step_N.png
                |
                |-> AIAgentPlanner.decide_next_action()
                |       Priority 1: CAPTCHA/Payment -> REQUEST_APPROVAL
                |       Priority 2: max_steps reached -> FINISH
                |       Priority 3: Gemini LLM (if API key present)
                |       Priority 4: Heuristic Fallback Engine
                |
                |-> Execute Action
                        NAVIGATE  -> controller.navigate(url)
                        TYPE_TEXT -> controller.type_text(query, element_id)
                                    -> Fills input field, then presses Enter
                        CLICK     -> controller.click_element(element_id)
                        SCROLL    -> controller.scroll(direction, amount)
                        EXTRACT   -> controller.extract_page_data()
                                    -> Collects headings, paragraphs, links, tables
                        FINISH    -> controller.clear_badges()
                                    -> Loop breaks

        v
ReportGenerator.generate_report()
        |  Normalizes extracted_data into rows
        |  Writes CSV to reports/report_<task_id>.csv
        v
Task Status -> COMPLETED
```

---

## Heuristic Planner — Step Logic

When no Gemini API key is set, `AIAgentPlanner._heuristic_decide_action()` runs these steps in order:

| Step | Condition | Action |
|------|-----------|--------|
| 1 | URL is blank / chrome-error | `NAVIGATE` to Google (or smart domain match) |
| 2 | Search input found + query not yet typed | `TYPE_TEXT` fills search box, presses Enter |
| 3 | Query typed + data not yet extracted | `EXTRACT` grabs page headings, links, paragraphs |
| 4 | Extracted + few steps done | `SCROLL` 600px down for more results |
| 5 | All objectives met | `FINISH` completes task |

**Smart domain routing (step 1):**

| Keyword in goal | Target URL |
|----------------|-----------|
| `linkedin` | `linkedin.com/jobs` |
| `amazon` / `price` | `amazon.com` |
| `flight` / `travel` | `google.com/travel/flights` |
| `http...` / `www.` in goal | Extracted from goal string |
| (default) | `google.com` |

---

## LLM Planner — Gemini Integration

When `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) is set in the environment, the planner calls:

```python
google.genai.Client.models.generate_content(
    model="gemini-2.5-flash",
    contents=<structured prompt>,
    config={"response_mime_type": "application/json"}
)
```

The prompt includes:
- User goal
- Current step number
- Current URL and page title
- Up to 30 interactive DOM elements (indexed)
- Last 4 trajectory steps

The model responds with a strict JSON action object:
```json
{
  "action": "navigate | click | type_text | scroll | extract | finish",
  "element_id": 5,
  "text": "latest AI news",
  "reasoning": "Typing the search query into the search input."
}
```

---

## Key Files

| File | Role |
|------|------|
| `main.py` | FastAPI server entry point |
| `agents/planner.py` | Decision engine (LLM + heuristic) |
| `browser/controller.py` | Playwright browser wrapper |
| `api/task_manager.py` | Task lifecycle and execution loop |
| `api/routes.py` | REST API endpoints |
| `models/schemas.py` | Pydantic data models |
| `reports/generator.py` | CSV report writer |

---

## Data Models

### `TaskRequest`
```python
goal: str                       # Natural language goal or search query
start_url: Optional[str]        # Default: https://www.google.com
headless: bool = False          # Visible or hidden browser
show_badges: bool = False       # Show element number overlays
max_steps: int = 15             # Maximum agent loop iterations
auto_approve_captcha: bool      # Auto-skip human approval gates
```

### `BrowserAction` (returned by planner)
```python
action: ActionType              # navigate | click | type_text | scroll | extract | finish
element_id: Optional[int]       # Index from parse_interactive_elements()
text: Optional[str]             # Text to type or URL to navigate
reasoning: Optional[str]        # Explanation of why this action was chosen
extracted_data: Optional[dict]  # Data to attach when action=extract/finish
```

### `AgentStep` (recorded per loop iteration)
```python
step_number: int
action: ActionType
description: str                # Human readable execution description
reasoning: str                  # Why the planner chose this action
screenshot_path: str            # Local path to step screenshot
screenshot_url: str             # Relative URL for API access
url: str                        # Current page URL at this step
extracted_data: Any             # What was extracted (if any)
```

---

## Report Output Format

After task completion, a CSV is written to `reports/report_<task_id>.csv`.

**Schema when page content was extracted:**

| Column | Description |
|--------|-------------|
| `Page Title` | Browser tab title at extraction time |
| `Heading / Result` | h1/h2/h3 text or link anchor text |
| `Snippet / Content` | Paragraph text (>20 chars) |
| `URL` | Hyperlink href |
| `Source URL` | Full page URL at time of extraction |

**Schema when only task summary was recorded (no extract step):**

| Column | Description |
|--------|-------------|
| `Task Goal` | The original search goal |
| `Status` | `Success` or `Failed` |
| `Total Steps` | Number of steps executed |
| `Source URL` | Final page URL |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Optional | Enables Google Gemini LLM-driven planning |
| `GOOGLE_API_KEY` | Optional | Alternative key name for Gemini |
| `PORT` | Optional | Server port for `main.py` (default: `8000`) |

Without an API key, the agent runs entirely on the heuristic engine — fully functional for standard web search tasks.

---

## WebSocket — Real-Time Task Streaming

The API exposes a WebSocket endpoint for real-time task updates:

```
ws://localhost:8000/api/ws/tasks/<task_id>
```

Clients receive a JSON-serialized `TaskResponse` object after every step, including new screenshots, step descriptions, and status changes.

---

## Human-in-the-Loop

If the browser encounters a **CAPTCHA** or **payment gate**, the task pauses:
- Status changes to `waiting_approval`
- `approval_request` is populated with a prompt and screenshot URL
- Execution resumes only after calling `POST /api/tasks/<task_id>/approve`
- If rejected, task status changes to `cancelled`
