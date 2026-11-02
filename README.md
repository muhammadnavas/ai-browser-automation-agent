# AI Browser Automation Agent

An autonomous AI-powered browser agent that can open a browser, navigate websites, fill forms, perform searches, extract page data, and generate CSV reports — with optional Google Gemini LLM intelligence.

---

## What It Does

- Opens a real Chromium browser (visible or headless)
- Navigates to any URL or intelligently routes to the right site based on your goal
- Finds and fills search inputs automatically
- Extracts page content: headings, paragraphs, links, tables
- Generates a structured CSV report of results
- Streams real-time step updates via WebSocket
- Pauses for human approval when encountering CAPTCHAs or payment gates

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r backend/requirements.txt
playwright install chromium
```

### 2. (Optional) Configure AI keys for intelligent planning

Create or update `.env` in `backend/`:
```bash
# Primary AI
GEMINI_API_KEY=your_gemini_key_here
GEMINI_MODEL=gemini-2.5-flash

# Optional local text planner (Ollama; takes priority when enabled)
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT_SECONDS=45

# Secondary text fallback (Groq)
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b

# Optional screenshot-based visual reasoning (xAI Grok Vision)
XAI_API_KEY=your_xai_api_key_here
XAI_VISION_MODEL=grok-2-vision-1212
XAI_VISION_MIN_INTERVAL_SECONDS=3
XAI_VISION_MAX_IMAGE_BYTES=4000000
```

When `XAI_API_KEY` is configured and a Chrome screenshot is available, the planner sends one current screenshot together with DOM, form, and visual-layout context to Grok Vision for its next-action decision. Screenshot SHA-256 deduplication prevents sending an identical image again; requests are spaced by `XAI_VISION_MIN_INTERVAL_SECONDS` (default 3 seconds), and images larger than `XAI_VISION_MAX_IMAGE_BYTES` (default 4 MB) are skipped. Skipped or failed vision calls use the normal text reasoning chain. Screenshots are still captured for task evidence, but duplicate frames are not resent to Grok. Without the xAI key, existing Gemini/Groq and heuristic behavior is unchanged.

### 3. Start the API server

```bash
python main.py
```

Server runs at `http://localhost:8000`

---

## Project Structure

The dashboard lives in `frontend/`; the FastAPI application and Python modules live in `backend/`. `main.py` at the repository root remains as a compatibility launcher.

```
ai-browser-automation-agent/
├── main.py                 # FastAPI server entry point
│
├── agents/
│   └── planner.py          # AI planner (Gemini LLM + heuristic fallback)
│
├── browser/
│   └── controller.py       # Playwright browser controller
│
├── api/
│   ├── routes.py           # REST API endpoints
│   └── task_manager.py     # Task lifecycle and execution loop
│
├── models/
│   └── schemas.py          # Pydantic data models
│
├── extraction/
│   └── search.py           # Search engine extraction parsing rules
│
├── reports/
│   └── generator.py        # CSV report generator
│
├── scripts/
│   ├── run_demo_task.py    # Headed verification script
│   └── demo_form_fill.py   # Form filling live demo
│
├── docs/
│   ├── README.md                 # Documentation Index and Feature Matrix
│   ├── ARCHITECTURE.md           # Architecture, lifecycle, and concurrency
│   ├── BROWSER_ACTIONS.md        # Technical API reference
│   ├── BROWSER_SEARCH_AGENT.md   # Feature documentation and prompts
│   └── TESTING.md                # Testing and verification guide
│
├── screenshots/            # Auto-saved step screenshots
├── logs/                   # Runtime logs
├── requirements.txt
└── .env                    # API keys (not committed)
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/tasks` | Create and start a new browser task (Playwright Browser Pool or Chrome Extension Multi-Tab) |
| `GET` | `/api/tasks` | List all tasks |
| `GET` | `/api/tasks/{task_id}` | Get task status and steps |
| `POST` | `/api/tasks/{task_id}/cancel` | Cancel a running or approval-waiting task immediately |
| `POST` | `/api/tasks/cancel-all` | Batch cancel all active and approval-waiting tasks |
| `GET` | `/api/pool/status` | Get live concurrency telemetry, active tasks, browser pool status, and extension state |
| `POST` | `/api/tasks/{task_id}/approve` | Approve/reject a CAPTCHA or payment gate |
| `GET` | `/api/extension/status` | Get Chrome Extension bridge connection status and active tasks |
| `WS` | `/api/extension/ws` | Chrome Extension Bridge WebSocket connection |
| `GET` | `/api/download/{folder}/{filename}` | Download a report or screenshot |
| `WS` | `/api/ws/tasks/{task_id}` | Real-time task event stream |

### Example: Submit a task (Playwright Mode)

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "search for Python tutorials",
    "start_url": "https://www.google.com",
    "execution_layer": "playwright",
    "headless": true,
    "max_steps": 8
  }'
```

### Example: Submit a task (Chrome Extension Mode)

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Open example.com and inspect page",
    "start_url": "https://example.com",
    "execution_layer": "extension",
    "max_steps": 5
  }'
```

---

## Chrome Extension Bridge (Operate inside Active Chrome Session)

The agent includes a Manifest V3 Chrome Extension located in `chrome_extension/` that allows automation to execute directly inside the user's active Chrome browser session.

### Installation & Setup
1. Open Google Chrome and go to `chrome://extensions/`.
2. Enable **Developer mode** in the top right.
3. Click **Load unpacked** and select the `chrome_extension/` folder.
4. The extension automatically connects to `ws://localhost:8000/api/extension/ws`.
5. In the Web UI at `http://localhost:8000`, select **"Chrome Extension (Active Session)"** under Execution Layer to run automation tasks in your personal browser tabs.
6. See [`chrome_extension/README.md`](chrome_extension/README.md) for full privacy and architecture details.


---

## Output

- **CSV Report**: saved to `reports/report_<task_id>.csv` after each task
- **Screenshots**: saved to `screenshots/<task_id>_step_N.png` for each step

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `playwright` | Browser automation |
| `fastapi` + `uvicorn` | REST API server |
| `pydantic` | Data validation and schemas |
| `google-genai` | Google Gemini LLM client |
| `pandas` | CSV report generation |
| `python-dotenv` | `.env` file support |

---

## Documentation

See the following guides in the `docs/` folder:
- [`docs/README.md`](docs/README.md): Documentation Index and Implementation Status Matrix.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): System Architecture, Concurrency, and Event Loop.
- [`docs/BROWSER_SEARCH_AGENT.md`](docs/BROWSER_SEARCH_AGENT.md): Search Agent execution flow and Gemini prompts.
- [`docs/BROWSER_ACTIONS.md`](docs/BROWSER_ACTIONS.md): Reusable browser interactions API reference.
- [`docs/TESTING.md`](docs/TESTING.md): Testing suite and live headed verification demo guide.


### Frontend deployment on Vercel

Vercel configuration is included for both supported project-root settings:

- **Recommended:** Set Vercel's Root Directory to `frontend`. The frontend-specific `vercel.json` builds with `npm ci` and `npm run build`, publishes `dist`, and rewrites client-side routes to `index.html`.
- **Alternative:** Leave the Root Directory at the repository root. The root `vercel.json` installs and builds from `frontend/` and publishes `frontend/dist`.

For a separately hosted FastAPI backend, add these environment variables in **Vercel → Project → Settings → Environment Variables**, then redeploy:

- `VITE_API_BASE_URL`: the deployed FastAPI backend origin only, e.g. `https://your-backend.example.com` (no trailing slash and no `/api` suffix).
- `VITE_WS_BASE_URL`: optional WebSocket origin, e.g. `wss://your-backend.example.com`. If omitted, it is derived from `VITE_API_BASE_URL`.

These `VITE_*` values are embedded at build time. Leaving them blank only works when the frontend and API are served from the same origin. Configure the backend's `CORS_ALLOW_ORIGINS` to include the exact deployed Vercel origin, and ensure the backend supports WebSocket connections. Vercel hosts the static frontend; it does not run this Playwright/FastAPI backend merely because the frontend is deployed.
"# ai-browser-automation-agent" 
