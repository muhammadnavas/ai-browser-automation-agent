import os
import logging
import ipaddress
import socket
from urllib.parse import urlsplit
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from models.schemas import (
    TaskRequest, TaskResponse,
    ExtractionRequest, ExtractionResult,
    FormDiscoveryRequest, FormDiscoveryResult,
    TaskStatus, ExtensionStatusResponse,
    ResearchRequest, ResearchTaskResponse, ComparisonResult,
)
import asyncio
from api.task_manager import task_manager
from services.task_archive import task_archive
from services.research_service import research_service
research_service.task_manager = task_manager
from browser.extractor import InfoExtractor
from browser.form_discovery import FormDiscovery
from browser.pool import get_browser_pool
from browser.waiter import settle_page
from browser.extension_adapter import extension_manager
from browser.security import install_navigation_guard

router = APIRouter(prefix="/api")
logger = logging.getLogger("api.routes")
MAX_ACTIVE_TASKS = max(1, int(os.getenv("MAX_ACTIVE_TASKS", "8")))


def _validate_public_http_url(raw_url: str) -> str:
    """Reject malformed URLs and obvious private-network targets before navigation."""
    try:
        parsed = urlsplit((raw_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            ]
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError
        return parsed.geturl()
    except (ValueError, OSError, socket.gaierror):
        raise HTTPException(status_code=400, detail="URL must be a valid public HTTP or HTTPS address.")


@router.post("/tasks", response_model=TaskResponse)
async def create_task(req: TaskRequest):
    """Creates and starts an AI Browser Automation task."""
    active_statuses = {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED}
    active_count = sum(task.status in active_statuses for task in task_manager.tasks.values())
    if active_count >= MAX_ACTIVE_TASKS:
        raise HTTPException(status_code=429, detail=f"Task capacity reached ({MAX_ACTIVE_TASKS} active tasks). Try again later.")

    # All automation runs in the user's connected Chrome session.
    if not extension_manager.is_connected:
        raise HTTPException(
            status_code=400,
            detail="Chrome Extension is disconnected. Please connect the extension in your Chrome browser before starting a task.",
        )

    task = task_manager.create_task(req)
    bg_task = asyncio.create_task(task_manager.run_task_execution(task.task_id, req))
    task_manager.running_tasks[task.task_id] = bg_task
    return task

@router.get("/tasks/history", response_model=List[TaskResponse])
async def list_task_history():
    """Returns bounded terminal task history restored from durable storage."""
    try:
        history_by_id = {task.task_id: task for task in task_archive.list_tasks()}
    except Exception as exc:
        logger.exception("Could not read archived task history")
        raise HTTPException(status_code=503, detail="Task history is temporarily unavailable.") from exc
    history_by_id.update(task_manager.task_history)
    history = list(history_by_id.values())
    # Return newest first; malformed timestamps must not break the whole history view.
    history.sort(key=lambda task: float(task.updated_at or 0), reverse=True)
    return history

@router.get("/tasks", response_model=List[TaskResponse])
async def list_tasks():
    """Lists all currently in-flight (live) automation tasks."""
    return list(task_manager.tasks.values())

@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Retrieves specific task status, steps, and report links."""
    task = task_manager.tasks.get(task_id) or task_manager.task_history.get(task_id) or task_archive.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@router.get("/tasks/{task_id}/replay", tags=["tasks"])
async def get_task_replay(task_id: str):
    """Return a chronological, read-only replay timeline with captured screenshots."""
    task = task_manager.tasks.get(task_id) or task_manager.task_history.get(task_id) or task_archive.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    timeline = []
    for step in (task.steps or [])[-100:]:
        action = step.action.value if hasattr(step.action, "value") else str(step.action)
        timeline.append({
            "step_number": step.step_number,
            "timestamp": getattr(step, "timestamp", None),
            "action": action,
            "description": (step.description or "")[:1000],
            "reasoning": (step.reasoning or "")[:1000],
            "url": step.url,
            "success": bool(getattr(step, "success", not bool(step.error))),
            "error": (step.error or "")[:1000],
            "duration_ms": step.duration_ms,
            "screenshot_url": step.screenshot_url,
        })

    timeline.sort(key=lambda item: (item["timestamp"] or 0, item["step_number"]))
    context = getattr(task, "context", None)
    return {
        "task_id": task_id,
        "goal": task.goal,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "timeline": timeline,
        "action_history": [
            record.model_dump() if hasattr(record, "model_dump") else dict(record)
            for record in (getattr(context, "action_history", []) or [])[-100:]
        ],
        "screenshots_available": sum(1 for item in timeline if item["screenshot_url"]),
        "replay_mode": "read_only",
    }


@router.get("/tasks/{task_id}/recovery", tags=["tasks"])
async def get_task_recovery(task_id: str):
    """Return the latest self-healing attempts and whether the task needs intervention."""
    task = task_manager.tasks.get(task_id) or task_manager.task_history.get(task_id) or task_archive.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    events = task_manager.recovery_diagnostics.get(task_id, [])
    unresolved = [event for event in events if event.get("outcome") == "unresolved"]
    return {
        "task_id": task_id,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "needs_intervention": bool(unresolved) or task.status == TaskStatus.FAILED,
        "latest_error": task.error,
        "attempt_count": len(events),
        "events": events[-25:],
    }


@router.get("/tasks/{task_id}/diagnostics", tags=["tasks"])
async def get_task_diagnostics(task_id: str):
    """Provide bounded task diagnostics, including step errors and recovery attempts."""
    task = task_manager.tasks.get(task_id) or task_manager.task_history.get(task_id) or task_archive.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    steps = []
    for step in (task.steps or [])[-50:]:
        steps.append({
            "step_number": step.step_number,
            "action": step.action.value if hasattr(step.action, "value") else str(step.action),
            "description": (step.description or "")[:1000],
            "reasoning": (step.reasoning or "")[:1000],
            "url": step.url,
            "error": (step.error or "")[:1000],
            "duration_ms": step.duration_ms,
        })
    context = getattr(task, "context", None)
    return {
        "task_id": task_id,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "error": task.error,
        "recent_errors": list(getattr(context, "recent_errors", []) or [])[-5:],
        "steps": steps,
        "recovery": task_manager.recovery_diagnostics.get(task_id, [])[-25:],
    }


@router.post("/tasks/{task_id}/approve")
async def approve_human_step(task_id: str, approved: bool = True, user_input: Optional[str] = None):
    """Approve or reject a Human-In-The-Loop action (CAPTCHA/Payment)."""
    if task_id not in task_manager.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = task_manager.tasks[task_id]
    if task.status != TaskStatus.WAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Task is not waiting for approval (current status: {task.status.value if hasattr(task.status, 'value') else task.status}).",
        )

    task_manager.approve_human_step(task_id, approved, user_input)
    await task_manager.broadcast_task_update(task_id)
    return {
        "status": "accepted",
        "task_id": task_id,
        "approved": approved,
        "task_status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "message": (
            "Approval received. Checking the live Chrome tab for session recovery."
            if approved else "Task cancellation accepted."
        ),
    }

class TaskCancelRequest(BaseModel):
    reason: Optional[str] = "Task cancelled by user."


@router.post("/tasks/{task_id}/pause", response_model=TaskResponse)
async def pause_task(task_id: str):
    """Pause a running task at the next safe action boundary."""
    try:
        return await task_manager.pause_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
async def resume_task(task_id: str):
    """Resume a cooperatively paused task."""
    try:
        return await task_manager.resume_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    payload: Optional[TaskCancelRequest] = None,
    reason: Optional[str] = None,
):
    """Cancels a running, pending, or approval-waiting task mid-execution."""
    cancel_reason = (payload.reason if payload and payload.reason else reason) or "Task cancelled by user."
    try:
        if task_id in research_service.research_tasks:
            research_service.cancel_research_task(task_id)
        return await task_manager.cancel_task(task_id, reason=cancel_reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to cancel task: {exc}")

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """Cancels and removes a task from the active task registry."""
    try:
        result = await task_manager.delete_task(task_id)
        # Research metadata and cached exports live in a separate service registry;
        # remove those too so deleted research tasks cannot reappear from memory.
        research_service.research_tasks.pop(task_id, None)
        research_service.comparison_store.pop(task_id, None)
        research_service.export_store.pop(task_id, None)
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except Exception:
        logger.exception("Failed to delete task %s", task_id)
        raise HTTPException(status_code=500, detail="Failed to delete task.")


@router.get("/pool/status", tags=["pool"])
async def get_pool_status():
    """Returns concurrency metrics: active running tasks, total tasks, and browser pool state."""
    pool_metrics = None
    try:
        pool = await get_browser_pool()
        pool_metrics = pool.get_metrics()
    except Exception as exc:
        pool_metrics = {"is_started": False, "error": str(exc)}

    running_list = []
    for tid, t in task_manager.tasks.items():
        if t.status in (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.PENDING):
            running_list.append(t.model_dump())

    return {
        "active_tasks_count": len(running_list),
        "total_memory_tasks": len(task_manager.tasks),
        "running_tasks": running_list,
        "browser_pool": pool_metrics,
        "extension_status": extension_manager.get_status(),
    }


@router.post("/tasks/cancel-all", tags=["tasks"])
async def cancel_all_tasks():
    """Cancels all currently running and waiting tasks."""
    cancelled_ids = []
    for tid, t in list(task_manager.tasks.items()):
        if t.status in (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.PENDING):
            await task_manager.cancel_task(tid, reason="Cancelled via cancel-all.")
            cancelled_ids.append(tid)
    return {"status": "success", "cancelled_count": len(cancelled_ids), "cancelled_tasks": cancelled_ids}


@router.post("/tasks/{task_id}/click")
async def click_human_step(task_id: str, x: float, y: float):
    """Click on the active browser page at normalized coordinates (0.0 to 1.0) while waiting for approval."""
    if task_id not in task_manager.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = task_manager.tasks[task_id]
    if task.status != TaskStatus.WAITING_APPROVAL:
        raise HTTPException(status_code=400, detail="Task is not waiting for human approval")
        
    controller = task_manager.controllers.get(task_id)
    if not controller or not controller.current_page:
        raise HTTPException(status_code=400, detail="Browser controller or page not available")
        
    try:
        page = controller.current_page
        viewport = page.viewport_size
        if not viewport:
            viewport = {"width": 1280, "height": 800}
            
        real_x = int(x * viewport["width"])
        real_y = int(y * viewport["height"])
        
        # Perform click
        await page.mouse.click(real_x, real_y)
        
        # Settle page
        await asyncio.sleep(1.5)
        
        # Take a new screenshot
        step_num = task.current_step or 1
        screenshot_path, screenshot_url = await controller.capture_screenshot(task_id, step_num)
        
        # Update approval request screenshot
        if task.approval_request:
            task.approval_request.screenshot_url = screenshot_url
            
        # Broadcast the update
        await task_manager.broadcast_task_update(task_id)
        
        return {"status": "success", "screenshot_url": screenshot_url}
        
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Click failed: {exc}")


@router.get("/reports/{task_id}/download")
async def download_report(task_id: str):
    """Streams a report only after its task reaches a terminal state."""
    pdf_bytes = task_manager._report_store.get(task_id) or task_archive.get_report(task_id) or research_service.get_export_report(task_id, "pdf")
    task = task_manager.tasks.get(task_id) or task_manager.task_history.get(task_id) or task_archive.get_task(task_id)
    terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    if task and task.status not in terminal_statuses:
        raise HTTPException(status_code=409, detail="Report is available only after the task finishes.")
    if not pdf_bytes and task and task.status in terminal_statuses:
        try:
            pdf_bytes = task_manager.report_generator.generate_pdf_report(task_id, task)
            task_archive.save_task(task, pdf_bytes)
        except Exception as e:
            logger.exception("Failed to generate report for task %s", task_id)
            raise HTTPException(status_code=500, detail="Failed to generate report.")
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="Report not found or task not yet complete")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report_{task_id}.pdf"'},
    )


@router.get("/download/screenshots/{filename}")
async def download_screenshot(filename: str):
    """Downloads a screenshot by filename."""
    if filename != os.path.basename(filename) or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = os.path.abspath(os.path.join("screenshots", filename))
    base_dir = os.path.abspath("screenshots")
    if os.path.commonpath([base_dir, filepath]) != base_dir:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(filepath)

@router.websocket("/ws/tasks/{task_id}")
async def task_websocket(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for real-time task log streaming and screenshot updates."""
    await websocket.accept()
    await task_manager.register_subscriber(task_id, websocket)
    
    # Send current state immediately if available
    if task_id in task_manager.tasks:
        await websocket.send_json(task_manager.tasks[task_id].model_dump())

    try:
        while True:
            # Keep socket alive and listen for client messages
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unexpected error in task WebSocket for %s", task_id)
    finally:
        await task_manager.unregister_subscriber(task_id, websocket)


# ---------------------------------------------------------------------------
# Chrome Extension Bridge Endpoints
# ---------------------------------------------------------------------------

@router.get("/extension/status", response_model=ExtensionStatusResponse, tags=["extension"])
async def get_extension_status():
    """Returns the current connection status and active tasks for the Chrome Extension."""
    return extension_manager.get_status()


@router.websocket("/extension/ws")
@router.websocket("/ws/extension")
async def extension_websocket(websocket: WebSocket):
    """WebSocket endpoint for Chrome Extension Bridge bidirectional communication."""
    await websocket.accept()
    await extension_manager.register(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            await extension_manager.handle_message(websocket, data)
    except WebSocketDisconnect:
        await extension_manager.unregister(websocket)
    except Exception:
        logger.exception("Chrome extension WebSocket failed")
        await extension_manager.unregister(websocket)



# ---------------------------------------------------------------------------
# Information Extraction Endpoint
# ---------------------------------------------------------------------------

@router.post("/extract", response_model=ExtractionResult, tags=["extraction"])
async def extract_page(req: ExtractionRequest):
    """
    Extract structured information from any public web page using a warm browser pool context.
    """
    safe_url = _validate_public_http_url(req.url)
    try:
        pool = await get_browser_pool()
        async with pool.acquire_context() as (context, page):
            # Guard every navigation, including redirects and subframe navigation,
            # rather than validating only the initial URL supplied by the caller.
            await install_navigation_guard(page)
            await page.goto(safe_url, timeout=req.timeout, wait_until="domcontentloaded")
            await settle_page(page, timeout=5000)

            extractor = InfoExtractor(wait_for_idle=False)
            result = await extractor.extract(
                page     = page,
                category = req.category,
            )
            return result

    except Exception as exc:
        raise HTTPException(
            status_code = 502,
            detail      = f"Extraction failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Form Discovery Endpoint
# ---------------------------------------------------------------------------

@router.post("/forms/discover", response_model=FormDiscoveryResult, tags=["forms"])
async def discover_form_fields(req: FormDiscoveryRequest):
    """
    Discover all form fields on a public web page using a warm browser pool context.
    """
    safe_url = _validate_public_http_url(req.url)
    try:
        pool = await get_browser_pool()
        async with pool.acquire_context() as (context, page):
            # Guard redirects as well as the initially validated form URL.
            await install_navigation_guard(page)
            await page.goto(safe_url, timeout=req.timeout, wait_until="domcontentloaded")
            await settle_page(page, timeout=5000)

            discovery = FormDiscovery()
            result = await discovery.discover(
                page           = page,
                scope_selector = req.scope_selector,
            )
            return result

    except Exception as exc:
        raise HTTPException(
            status_code = 502,
            detail      = f"Form discovery failed: {exc}",
        )



# ---------------------------------------------------------------------------
# Multi-Website Research & Comparison Endpoints
# ---------------------------------------------------------------------------

@router.post("/research", response_model=ResearchTaskResponse, tags=["research"])
async def submit_research_task(req: ResearchRequest):
    """
    Submit a multi-website background research and comparison task.
    Accepts research goal, multiple sources, comparison criteria, and output schema.
    """
    try:
        return await research_service.submit_research_task(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to submit research task: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to submit research task: {e}")


@router.get("/research/{task_id}", response_model=ResearchTaskResponse, tags=["research"])
async def get_research_status(task_id: str):
    """Retrieve status, per-source progress, and summary for a research task."""
    task = research_service.get_research_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Research task not found")
    return task


@router.get("/research/{task_id}/results", response_model=ComparisonResult, tags=["research"])
async def get_research_results(task_id: str):
    """Retrieve structured comparison results across websites."""
    results = research_service.get_results(task_id)
    if not results:
        task = research_service.get_research_task(task_id)
        if task and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            raise HTTPException(status_code=409, detail="Research is still in progress. Please wait for completion.")
        raise HTTPException(status_code=404, detail="Research results not found")
    return results


@router.post("/research/{task_id}/cancel", response_model=ResearchTaskResponse, tags=["research"])
async def cancel_research_task(task_id: str):
    """Cancel a running multi-website research task."""
    task = research_service.cancel_research_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Research task not found or already finished")
    # Push the cancellation immediately; do not make the UI wait for the worker's
    # next browser/network operation before its status changes.
    if task_manager and task_id in task_manager.tasks:
        await task_manager.broadcast_task_update(task_id)
    return task


@router.get("/research/{task_id}/export", tags=["research"])
async def export_research_report(task_id: str, format: str = "pdf"):
    """
    Download a structured research report in CSV, XLSX, or PDF format.
    Query param format must be 'csv', 'xlsx', or 'pdf' (default: 'pdf').
    """
    fmt = format.lower()
    if fmt not in ("csv", "xlsx", "pdf"):
        raise HTTPException(status_code=400, detail="Unsupported format. Choose 'csv', 'xlsx', or 'pdf'.")

    report_bytes = research_service.get_export_report(task_id, fmt)
    if not report_bytes:
        task = research_service.get_research_task(task_id)
        if task and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            raise HTTPException(status_code=409, detail="Report is being generated. Please wait until task completes.")
        raise HTTPException(status_code=404, detail=f"Export for format '{fmt}' not found.")

    media_types = {
        "csv": "text/csv; charset=utf-8",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pdf": "application/pdf",
    }
    return Response(
        content=report_bytes,
        media_type=media_types[fmt],
        headers={"Content-Disposition": f'attachment; filename="research_{task_id}.{fmt}"'},
    )
