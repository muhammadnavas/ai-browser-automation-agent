import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / 'frontend'
FRONTEND_DIST_DIR = FRONTEND_DIR / 'dist'
for path in [str(BACKEND_DIR), str(PROJECT_ROOT)]:
    if path not in sys.path:
        sys.path.insert(0, path)
os.chdir(PROJECT_ROOT)

import asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load environment variables before importing application modules
load_dotenv()

# ── Windows asyncio fix ──────────────────────────────────────────────
# Playwright needs to spawn browser sub-processes via asyncio.
# On Windows + Python 3.10 the default ProactorEventLoop used by
# uvicorn's --reload watcher cannot create subprocesses inside the
# running loop, raising NotImplementedError.  Setting the policy here
# ensures every loop created (including by uvicorn) uses Proactor
# *before* anything else runs.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager
from api.routes import router as api_router
from services.cache_service import default_cache_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: connect to Redis on startup and close on shutdown."""
    # Preserve screenshots and report assets across restarts; cleanup should be handled by retention policy.

    # Startup: Initialize Redis cache connection (non-fatal)
    await default_cache_service.connect()
    yield
    # Shutdown: Close Redis client and connection pool
    await default_cache_service.close()


app = FastAPI(
    title="AI Browser Automation Agent",
    description="Industry-grade AI Browser Agent for autonomous navigation, form filling, flight/ticket booking, web scraping, and Excel/PDF report generation.",
    version="1.0.0",
    lifespan=lifespan,
)

# Use an explicit CORS allowlist. CORS is not authentication; protect API routes
# with real authentication before exposing this service to untrusted users.
_default_cors_origins = "http://localhost:5173,http://127.0.0.1:5173"
_cors_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOW_ORIGINS", _default_cors_origins).split(",")
    if origin.strip()
]

from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse
from fastapi import Request
from backend.security.auth import (
    session_manager,
    extract_session_id,
    get_client_ip,
    SESSION_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_LIFETIME_SECONDS,
)

class SecurityAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # 1. Exempt CORS preflights, public static files, test forms, docs, and favicon
        if (
            method == "OPTIONS"
            or path == "/favicon.ico"
            or path.startswith("/assets")
            or path == "/test-form"
            or path == "/api/docs"
            or path == "/api/openapi.json"
        ):
            return await call_next(request)

        # 2. Transparent session auto-issue on root dashboard load (GET /)
        if path == "/":
            response = await call_next(request)
            session_id = extract_session_id(request)
            session = session_manager.get_session(session_id)
            if not session:
                client_ip = get_client_ip(request)
                session = session_manager.create_session(client_ip=client_ip)
                response.set_cookie(
                    key=SESSION_COOKIE_NAME,
                    value=session.session_id,
                    httponly=True,
                    samesite="lax",
                    secure=False,
                    max_age=SESSION_LIFETIME_SECONDS,
                )
                response.set_cookie(
                    key=CSRF_COOKIE_NAME,
                    value=session.csrf_token,
                    httponly=False,
                    samesite="lax",
                    secure=False,
                    max_age=SESSION_LIFETIME_SECONDS,
                )
            return response

        # 3. Exempt session handshake and websocket paths
        if (
            path == "/api/auth/session"
            or path.startswith("/api/extension/ws")
            or path.startswith("/ws/extension")
            or path.startswith("/api/ws/tasks")
            or path.startswith("/ws/tasks")
        ):
            return await call_next(request)

        # 4. Protect all API endpoints, screenshots, and reports
        if path.startswith("/api") or path.startswith("/screenshots") or path.startswith("/reports"):
            session_id = extract_session_id(request)
            session = session_manager.get_session(session_id)

            if not session:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required. Active session required."},
                )

            # 5. Verify CSRF on state-changing operations (POST, PUT, DELETE, PATCH)
            if method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api"):
                csrf_token = request.headers.get(CSRF_HEADER_NAME)
                if not session_manager.verify_csrf(session, csrf_token):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF validation failed. Missing or invalid X-CSRF-Token."},
                    )

        return await call_next(request)

# SecurityAuthMiddleware added first
app.add_middleware(SecurityAuthMiddleware)

# CORSMiddleware added after SecurityAuthMiddleware so it wraps around it
# and attaches CORS headers to all responses (including 401 and 403).
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-CSRF-Token", "X-Session-ID", "X-Bootstrap-Token"],
)

# Include API Router
app.include_router(api_router)

from fastapi.responses import FileResponse

# Create screenshots directory if missing
os.makedirs("screenshots", exist_ok=True)

# Mount screenshots for browser preview
app.mount("/screenshots", StaticFiles(directory="screenshots"), name="screenshots")
# Serve the built React/Vite app when present; retain the legacy dashboard as a safe fallback.
if (FRONTEND_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="frontend-assets")

from fastapi import Response

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.get("/")
async def serve_ui():
    """Serve the built React dashboard or explain how to build it."""
    react_index = FRONTEND_DIST_DIR / "index.html"
    if not react_index.is_file() or not (FRONTEND_DIST_DIR / "assets").is_dir():
        return HTMLResponse(
            status_code=503,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            content="""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Browser Agent — Frontend not built</title>
  <style>
    body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1020;color:#e5e7eb;font:16px/1.6 system-ui,sans-serif}
    main{max-width:680px;margin:24px;padding:28px;border:1px solid #334155;border-radius:16px;background:#111827}
    h1{margin-top:0;font-size:1.5rem}p{color:#cbd5e1}code{display:block;overflow-wrap:anywhere;margin:10px 0;padding:12px;border-radius:8px;background:#020617;color:#a5b4fc}
  </style>
</head>
<body><main>
  <h1>Frontend build not found</h1>
  <p>The React dashboard has not been built yet. From the project root, run:</p>
  <code>cd frontend</code>
  <code>npm install</code>
  <code>npm run build</code>
  <p>Then restart or refresh the backend page. For development, run <code>npm run dev</code> inside the frontend directory and open the Vite URL it prints.</p>
</main></body>
</html>""",
        )
    return FileResponse(
        str(react_index),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )

from fastapi.responses import HTMLResponse

@app.get("/test-form", response_class=HTMLResponse)
async def serve_test_form():
    return """
    <html>
    <head>
        <title>Local Testing Form</title>
        <style>
            body { font-family: sans-serif; padding: 40px; background-color: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; min-height: 80vh; }
            .form-container { background: #1e293b; padding: 30px; border-radius: 12px; max-width: 450px; width: 100%; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); border: 1px solid #334155; }
            h2 { margin-top: 0; color: #38bdf8; text-align: center; }
            .field { margin-bottom: 20px; }
            label { display: block; margin-bottom: 8px; font-weight: 600; color: #cbd5e1; }
            input[type="text"] { width: 100%; padding: 10px; border: 1px solid #475569; border-radius: 6px; box-sizing: border-box; background: #0f172a; color: white; font-size: 14px; }
            input[type="text"]:focus { outline: none; border-color: #38bdf8; }
            button { background: #0284c7; color: white; border: none; padding: 12px 15px; border-radius: 6px; cursor: pointer; width: 100%; font-size: 16px; font-weight: bold; transition: background 0.2s; }
            button:hover { background: #0369a1; }
            .success-message { color: #4ade80; font-weight: bold; display: none; margin-top: 20px; text-align: center; font-size: 16px; }
        </style>
        <script>
            function handleSubmit(e) {
                e.preventDefault();
                document.getElementById('success').style.display = 'block';
            }
        </script>
    </head>
    <body>
        <div class="form-container">
            <h2>Local Test Form</h2>
            <form onsubmit="handleSubmit(event)">
                <div class="field">
                    <label for="userName">Full Name</label>
                    <input type="text" id="userName" name="userName" placeholder="Enter Full Name">
                </div>
                <div class="field">
                    <label for="userEmail">Email</label>
                    <input type="text" id="userEmail" name="userEmail" placeholder="Enter Email">
                </div>
                <div class="field">
                    <label for="currentAddress">Current Address</label>
                    <input type="text" id="currentAddress" name="currentAddress" placeholder="Enter Current Address">
                </div>
                <div class="field">
                    <label for="permanentAddress">Permanent Address</label>
                    <input type="text" id="permanentAddress" name="permanentAddress" placeholder="Enter Permanent Address">
                </div>
                <button type="submit" id="submit">Submit</button>
            </form>
            <div id="success" class="success-message">Form Submitted Successfully!</div>
        </div>
    </body>
    </html>
    """

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"Starting AI Browser Automation Agent server at http://localhost:{port}")
    reload = "--reload" in sys.argv or os.getenv("RELOAD", "false").lower() in ("true", "1")
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run("main:app", host=host, port=port, reload=reload)
