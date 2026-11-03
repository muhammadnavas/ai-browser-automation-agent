# AI Browser Automation Agent

An AI-powered browser automation platform that turns natural-language goals into browser actions. It can navigate websites, interact with page elements, work with forms, extract information, and produce task artifacts through either a managed Playwright browser or a Chrome extension connected to an existing browser session.

The project combines LLM-based planning, live page context, browser automation, optional visual reasoning, adaptive replanning, and human-in-the-loop safeguards.

## Highlights

- **Natural-language automation** — describe the outcome you want instead of scripting every browser step.
- **Two execution modes:** Playwright for an isolated managed browser, or a Manifest V3 Chrome extension for supported actions in an active Chrome session.
- **Hybrid AI reasoning:** Ollama for optional local text planning, Gemini and Groq as configured text fallbacks, xAI Grok Vision for a separate screenshot-based reasoning path, and heuristic planning for supported flows.
- **Context-aware planning** based on the goal, current page, interactive elements, and recent action history.
- **Adaptive replanning** after eligible recoverable failures, using refreshed page context.
- **Action verification** with extension-mode precondition checks and supported postcondition checks.
- **Human-in-the-loop safeguards** for sensitive or ambiguous checkpoints.
- **Live task updates** streamed to the frontend over WebSockets.
- **Task artifacts**, including screenshots and generated reports.

## Architecture

| Layer | Responsibility |
|---|---|
| frontend/ | React + Vite interface. The root route (/) presents agent information; /agent contains the automation workspace. |
| backend/ | FastAPI service, task orchestration, AI planner, browser controllers, extraction, and reporting. |
| chrome_extension/ | Manifest V3 extension that connects an existing Chrome session to the backend. |

~~~text
User goal
   |
   v
React frontend  <---- REST + WebSocket ---->  FastAPI backend
                                               |
                                               v
                                         Task manager
                                               |
                                  +------------+------------+
                                  |                         |
                                  v                         v
                              AI planner               Page context
                         Ollama / Gemini / Groq       DOM / controls
                                  |                         |
                                  +------------+------------+
                                               |
                                               v
                                        Browser executor
                                      /                 \
                               Playwright          Chrome Extension
                                      \                 /
                                               |
                                               v
                                  Verification / replanning
                                               |
                                               v
                                  Results, screenshots, reports
~~~

## Tech Stack

- **Frontend:** React, Vite
- **Backend:** Python, FastAPI, Pydantic, WebSockets
- **Browser automation:** Playwright, Chromium
- **Browser integration:** Chrome Extension Manifest V3
- **AI providers:** Ollama, Google Gemini, Groq, xAI Grok Vision (optional)
- **Supporting services:** Redis (when configured), local task persistence
- **Testing:** pytest, pytest-asyncio

## Getting Started

### 1. Clone the repository

~~~bash
git clone https://github.com/muhammadnavas/ai-browser-automation-agent.git
cd ai-browser-automation-agent
~~~

### 2. Configure the backend

Create a .env file in backend/ and add only the providers you intend to use:

~~~env
# Local text planning (optional; preferred when enabled)
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT_SECONDS=45

# Cloud text-planning fallbacks (optional)
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b

# Screenshot-based visual reasoning (optional)
XAI_API_KEY=
XAI_VISION_MODEL=grok-2-vision-1212
XAI_VISION_MIN_INTERVAL_SECONDS=3
XAI_VISION_MAX_IMAGE_BYTES=4000000
~~~

When Ollama is enabled, it is tried first for text planning and next-action reasoning. If it is unavailable or fails, configured cloud text providers can be used as fallbacks. Grok Vision is a separate visual-reasoning provider; it is not the text-planning fallback.

To use the local Ollama model:

~~~bash
ollama pull qwen2.5:7b
ollama serve
~~~

Keep Ollama running while the backend is active. If the backend runs in Docker, localhost refers to the container; configure OLLAMA_BASE_URL to reach the host or remote Ollama service. Model size and speed depend on available RAM/VRAM; a smaller quantized model may be more practical on machines with limited GPU memory.

### 3. Install backend dependencies

~~~bash
pip install -r backend/requirements.txt
playwright install chromium
~~~

### 4. Start the backend

~~~bash
cd backend
python main.py
~~~

The API is expected at http://localhost:8000 unless configured otherwise.

### 5. Start the frontend

In a second terminal:

~~~bash
cd frontend
npm install
npm run dev
~~~

Open the local URL printed by Vite. Use / for the project landing page and /agent for the automation workspace.

## Chrome Extension Setup

The extension lets the agent interact with tabs in an existing Chrome session.

1. Start the backend.
2. Open chrome://extensions/ in Chrome.
3. Enable **Developer mode**.
4. Select **Load unpacked** and choose the repository's chrome_extension/ directory.
5. Confirm that the extension connects to the backend WebSocket bridge.
6. Open /agent and select the Chrome Extension execution mode.

See [chrome_extension/README.md](chrome_extension/README.md) for extension-specific setup and implementation details.

## How Planning Works

1. The task manager receives the user's goal and execution settings.
2. The browser opens the requested starting page or navigates to a relevant destination.
3. The backend gathers live page context, including URL, title, headings, and available interactive elements.
4. The planner selects the next action using the configured text provider or heuristic fallback. When enabled and useful, visual reasoning can also inspect the current screenshot.
5. The selected action is sent to the chosen browser execution layer.
6. Supported actions are checked against current page state. If an eligible recoverable failure occurs, the planner may replan the remaining milestones using refreshed context.
7. The task manager records progress and streams updates to the frontend.

### Screenshot efficiency

The visual-reasoning path avoids unnecessary image requests. It uses screenshot hashing to avoid resending an identical frame, enforces a configurable minimum interval, and skips images above the configured size limit. A failed vision request does not mark the screenshot as successfully processed, allowing a later retry. Text reasoning and heuristic behavior remain available when visual reasoning is skipped or unavailable.

## API Overview

The backend exposes REST and WebSocket interfaces for task execution and monitoring. Common endpoints include:

| Method | Endpoint | Purpose |
|---|---|---|
| POST | /api/tasks | Create and start a browser task |
| GET | /api/tasks | List tasks |
| GET | /api/tasks/{task_id} | Get task status and execution history |
| POST | /api/tasks/{task_id}/cancel | Cancel a task |
| POST | /api/tasks/cancel-all | Cancel active tasks |
| POST | /api/tasks/{task_id}/approve | Submit a human approval decision |
| GET | /api/pool/status | Inspect browser-pool and runtime status |
| GET | /api/extension/status | Inspect extension bridge status |
| WS | /api/ws/tasks/{task_id} | Receive task progress events |
| WS | /api/extension/ws | Chrome extension bridge |

Check the backend route definitions for the current request and response schemas.

### Create a Playwright task

~~~bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Search for Python tutorials and summarize useful results",
    "start_url": "https://www.google.com",
    "execution_layer": "playwright",
    "headless": true,
    "max_steps": 8
  }'
~~~

### Create a Chrome Extension task

~~~bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Open example.com and inspect the page",
    "start_url": "https://example.com",
    "execution_layer": "extension",
    "max_steps": 5
  }'
~~~

The exact accepted fields are defined by the backend Pydantic request schema.

## Safety and Limitations

- The agent is designed to pause for human input at sensitive checkpoints such as CAPTCHA or payment-related flows; it should not be used to bypass access controls or security challenges.
- Review actions carefully before allowing submissions, purchases, account changes, or other consequential operations.
- Browser pages are untrusted input. Page text should be treated as content, not as instructions that override the user's goal or the agent's safety rules.
- Verification is strongest for state that can be read back reliably, such as navigation destinations and form values. Some actions, especially clicks with ambiguous outcomes, cannot always be verified deterministically. The agent avoids blindly retrying such actions because they may already have caused a side effect.
- Provider availability, model quality, browser compatibility, and website behavior can affect task success.
- The project is under active development; not every website or workflow is guaranteed to work without adjustment.

## Testing

Install backend dependencies, then run the focused Phase 1 regression tests from the repository root:

~~~bash
pip install -r backend/requirements.txt
python -m pytest tests/test_groq_fallback.py tests/test_verification_fallback.py -v
~~~

Run the full test suite:

~~~bash
python -m pytest -v
~~~

The focused tests cover planner provider behavior, adaptive replanning, and supported browser-extension action preconditions and postconditions.

**Test status:** these commands are provided for local verification. Run them in your environment before treating the current implementation as validated; no passing test result is implied by this README.

See [docs/TESTING.md](docs/TESTING.md) for additional test and demo instructions.

## Deployment

The frontend can be deployed separately as a static Vite application. The FastAPI backend must be hosted on a service that supports persistent processes and WebSockets; browser automation also requires a compatible browser runtime and Playwright dependencies.

For a separate frontend/backend deployment, configure the frontend build environment:

- VITE_API_BASE_URL: backend origin, without a trailing slash or /api suffix.
- VITE_WS_BASE_URL: optional WebSocket origin. If omitted, the frontend may derive it from the API origin.

Configure the backend CORS allowlist to include the deployed frontend origin. Ensure the backend host supports WebSockets and has the required browser dependencies. Deploying the frontend to Vercel alone does **not** deploy or run the FastAPI/Playwright backend.

## Documentation

- [System architecture](docs/ARCHITECTURE.md)
- [Browser actions reference](docs/BROWSER_ACTIONS.md)
- [Browser search agent](docs/BROWSER_SEARCH_AGENT.md)
- [Testing and verification](docs/TESTING.md)
- [Chrome extension guide](chrome_extension/README.md)

## Contributing

Issues and improvements are welcome. When changing planner or browser-execution behavior, add or update regression tests for the affected flow. Keep user approval requirements and safe handling of ambiguous side effects intact.

---
