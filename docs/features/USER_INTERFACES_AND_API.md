# User Interfaces, REST API & CLI

This document describes the interactive Web Dashboard, REST API endpoints, real-time WebSocket communication, and CLI utilities.

---

## 1. Overview & Purpose

- **Interactive Web UI Dashboard**: A single-page dashboard for launching tasks, monitoring live execution steps, solving approval gates, inspecting extracted data, and downloading CSV reports.
- **REST API & WebSockets**: Programmatic integration surface for creating automation tasks, polling states, receiving live event streams, and performing ad-hoc data extractions.
- **Verification Demos**: Standalone terminal-run scripts for quick end-to-end local capability verification.

> [!IMPORTANT]
> **Chrome Extension Status: Not Implemented**
> The project does **not** include a Chrome Extension (`manifest.json`, background service worker, or extension popup). The agent is designed to run as a backend service and Web Dashboard.

---

## 2. Main Components & Files

| Component | File Location | Responsibility |
|---|---|---|
| **Web Dashboard** | `static/index.html`<br>`static/app.js`<br>`static/styles.css` | Client-side simple single-view frontend with live execution plan, WebSocket log stream, screenshot viewer, and coordinate click listener. |
| **Server & Static Mounts** | `main.py` | FastAPI server setup, CORS configuration, static folder mounting (`/reports`, `/screenshots`, `/static`), and UI route (`/`). |
| **API Endpoints** | `api/routes.py` | REST API and WebSocket handlers. |
| **Feature Verification Demo** | `scripts/run_demo_task.py` | End-to-end headed demonstration exercising all browser submodules against a local HTTP server. |

---

## 3. Web UI Dashboard

Access the Web Dashboard by running `python main.py` and opening `http://localhost:8000` in a browser.

- **Multi-Task Concurrency Bar & Switcher**: Live concurrency telemetry chip, browser pool capacity meter, extension status, horizontal task tabs track (`#task-tabs-track`) for switching between running/completed tasks in real time without losing state or logs, individual task cancellation, and a batch `Stop All Tasks` control.
- **Task Inputs**: Natural-language Goal, optional Start URL, Execution Layer selector (Playwright Browser Pool vs Chrome Extension Multi-Tab), Max Steps allocation, and Visible browser toggle.
- **AI Execution Plan**: Displays the high-level execution plan generated before browser automation begins for the active tab.
- **Live Stream**: Displays step descriptions, planner reasoning, action types, and step timing in real time.
- **Live Browser Preview**: Displays the latest screenshot captured at each execution step.
- **Human Approval Modal**: When a security gate is hit, opens a prompt modal showing the challenge. Clicking directly on the screenshot sends exact mouse coordinates to the live browser page.

---

## 4. REST API Reference

All API routes are prefixed under `/api`.

| Method | Endpoint | Description | Request Body / Query |
|---|---|---|---|
| `POST` | `/api/tasks` | Create and launch an automation task (supports parallel execution) | `TaskRequest` (JSON) |
| `GET` | `/api/tasks` | List all created automation tasks | *None* |
| `GET` | `/api/tasks/{task_id}` | Retrieve status and steps for a specific task | `task_id` (Path) |
| `POST` | `/api/tasks/{task_id}/approve` | Approve or reject a human-in-the-loop gate | `approved` (bool), `user_input` (string) |
| `POST` | `/api/tasks/{task_id}/cancel` | Cancel a running, pending, or approval-waiting task | *None* |
| `POST` | `/api/tasks/cancel-all` | Batch cancel all active and approval-waiting tasks simultaneously | *None* |
| `DELETE` | `/api/tasks/{task_id}` | Cancel and remove a task from the live registry | *None* |
| `GET` | `/api/pool/status` | Get live concurrency telemetry, active tasks, browser pool status, and extension state | *None* |
| `POST` | `/api/tasks/{task_id}/click` | Perform normalized mouse click (`0.0` to `1.0`) on active page | `x` (float), `y` (float) |
| `POST` | `/api/extract` | Extract structured data using a warm browser context | `ExtractionRequest` (JSON) |
| `POST` | `/api/forms/discover` | Discover all form fields on a target URL | `FormDiscoveryRequest` (JSON) |
| `GET` | `/api/download/{folder}/{filename}` | Download a generated CSV report or screenshot PNG | `folder` (`reports` or `screenshots`), `filename` |
| `WS` | `/api/ws/tasks/{task_id}` | WebSocket stream for real-time task updates | *None* |

---


## 5. Terminal Demos

### 5.1. Feature Verification Demo (`scripts/run_demo_task.py`)
Runs an end-to-end visual demonstration that tests all browser actions (navigation, form filling, alerts, prompts, popups, downloads, scrolling, screenshots, and session saving) against an internal local server:

```bash
python scripts/run_demo_task.py
```

---

## 6. Basic Integration Examples

### Submitting a Task via cURL:
```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Search for top artificial intelligence tools",
    "start_url": "https://www.google.com",
    "headless": true,
    "max_steps": 6
  }'
```

### Submitting a Remote Coordinate Click During Approval:
```bash
curl -X POST "http://localhost:8000/api/tasks/task-abc12345/click?x=0.52&y=0.48"
```
