"""
Compatibility launcher for AI Browser Automation Agent.
Invokes backend/main.py with proper sys.path and environment configuration.
"""
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if __name__ == "__main__":
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    reload = "--reload" in sys.argv or os.getenv("RELOAD", "false").lower() in ("true", "1")
    print(f"Starting AI Browser Automation Agent server at http://localhost:{port} (reload={reload})")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload, app_dir=str(BACKEND_DIR), loop="asyncio")

