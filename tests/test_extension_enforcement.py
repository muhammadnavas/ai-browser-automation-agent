import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import HTTPException

from models.schemas import TaskRequest, TaskStatus
from api.routes import create_task
from api.task_manager import TaskManager


@pytest.mark.asyncio
async def test_create_task_fails_when_extension_disconnected():
    """Verify that create_task raises HTTPException 400 when extension is disconnected."""
    with patch("api.routes.extension_manager") as mock_ext_mgr:
        mock_ext_mgr.is_connected = False
        req = TaskRequest(goal="Search AI news", execution_layer="extension")
        with pytest.raises(HTTPException) as exc_info:
            await create_task(req)
        assert exc_info.value.status_code == 400
        assert "Chrome Extension is disconnected" in exc_info.value.detail
        assert "connect the extension" in exc_info.value.detail


@pytest.mark.asyncio
async def test_create_task_defaults_to_extension_and_fails_if_disconnected():
    """Verify that omission of execution_layer defaults to extension and fails if disconnected."""
    with patch("api.routes.extension_manager") as mock_ext_mgr:
        mock_ext_mgr.is_connected = False
        req = TaskRequest(goal="Search AI news")  # default execution_layer
        assert req.execution_layer == "extension"
        with pytest.raises(HTTPException) as exc_info:
            await create_task(req)
        assert exc_info.value.status_code == 400
        assert "Chrome Extension is disconnected" in exc_info.value.detail


@pytest.mark.asyncio
async def test_run_task_execution_fails_without_playwright_fallback():
    """Verify that run_task_execution does not fall back to Playwright if extension is disconnected."""
    tm = TaskManager()
    req = TaskRequest(goal="Test goal", execution_layer="extension")
    task = tm.create_task(req)

    with patch("api.task_manager.extension_manager") as mock_ext_mgr, \
         patch("api.task_manager.BrowserController") as mock_playwright:
        mock_ext_mgr.is_connected = False
        await tm.run_task_execution(task.task_id, req)

        # Playwright controller MUST NOT be called / instantiated
        mock_playwright.assert_not_called()
        assert task.status == TaskStatus.FAILED
        assert "Chrome Extension is disconnected" in task.error


@pytest.mark.asyncio
async def test_create_task_succeeds_when_extension_connected():
    """Verify that create_task succeeds when extension is connected."""
    with patch("api.routes.extension_manager") as mock_ext_mgr, \
         patch("api.routes.task_manager.create_task") as mock_create, \
         patch("api.routes.task_manager.run_task_execution", new_callable=AsyncMock):
        mock_ext_mgr.is_connected = True
        mock_task = MagicMock()
        mock_task.task_id = "test-123"
        mock_create.return_value = mock_task

        req = TaskRequest(goal="Search AI news", execution_layer="extension")
        result = await create_task(req)
        assert result.task_id == "test-123"


# ---------------------------------------------------------------------------
# Extension Communication, Injection Readiness, and lock_ui Regression Tests
# ---------------------------------------------------------------------------
import asyncio
import subprocess
from browser.extension_adapter import ExtensionBrowserAdapter, ExtensionManager


def test_chrome_extension_scripts_syntax_validity():
    """Verify that both content.js and background.js have valid JavaScript syntax."""
    res_content = subprocess.run(["node", "-c", "chrome_extension/content.js"], capture_output=True, text=True)
    assert res_content.returncode == 0, f"content.js syntax error: {res_content.stderr}"

    res_bg = subprocess.run(["node", "-c", "chrome_extension/background.js"], capture_output=True, text=True)
    assert res_bg.returncode == 0, f"background.js syntax error: {res_bg.stderr}"


@pytest.mark.asyncio
async def test_extension_browser_adapter_start_and_lock_ui_flow():
    """Verify start() sends create_tab and immediately establishes lock_ui."""
    mock_mgr = MagicMock()
    mock_mgr.is_connected = True
    mock_mgr.task_tabs = {}
    mock_mgr.send_command = AsyncMock(side_effect=[
        {"success": True, "data": {"tab_id": 42, "url": "about:blank"}},
        {"success": True, "data": {"locked": True, "restricted": True}},
    ])

    adapter = ExtensionBrowserAdapter(task_id="test-task-1", manager=mock_mgr)
    await adapter.start()

    assert adapter.is_running is True
    assert adapter._current_url == "about:blank"
    assert mock_mgr.send_command.call_count == 2
    create_call, lock_call = mock_mgr.send_command.call_args_list
    assert create_call.kwargs["command_type"] == "create_tab"
    assert lock_call.kwargs["command_type"] == "lock_ui"


@pytest.mark.asyncio
async def test_extension_browser_adapter_lock_ui_failure_raises():
    """Verify lock_ui raises RuntimeError if the extension fails to confirm lock."""
    mock_mgr = MagicMock()
    mock_mgr.send_command = AsyncMock(return_value={
        "success": False,
        "error": "Failed to lock UI: Content script not ready",
    })

    adapter = ExtensionBrowserAdapter(task_id="test-task-2", manager=mock_mgr)
    with pytest.raises(RuntimeError) as exc_info:
        await adapter.lock_ui()
    assert "Could not lock the task browser" in str(exc_info.value)
    assert "Content script not ready" in str(exc_info.value)


@pytest.mark.asyncio
async def test_extension_browser_adapter_lock_ui_timeout_handling():
    """Verify lock_ui propagates TimeoutError when the extension does not respond within timeout."""
    mock_mgr = MagicMock()
    mock_mgr.send_command = AsyncMock(side_effect=TimeoutError("Extension command 'lock_ui' timed out after 8.0s."))

    adapter = ExtensionBrowserAdapter(task_id="test-task-3", manager=mock_mgr)
    with pytest.raises(TimeoutError) as exc_info:
        await adapter.lock_ui()
    assert "Extension command 'lock_ui' timed out after 8.0s." in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_tab_injection_failure_surfaces_exact_error():
    """Verify start() surfaces injection failure error from the extension."""
    mock_mgr = MagicMock()
    mock_mgr.is_connected = True
    mock_mgr.task_tabs = {}
    mock_mgr.send_command = AsyncMock(return_value={
        "success": False,
        "error": "Failed to create task tab: Browser content script did not become ready after injection. Reload the task tab and retry.",
    })

    adapter = ExtensionBrowserAdapter(task_id="test-task-4", manager=mock_mgr)
    with pytest.raises(RuntimeError) as exc_info:
        await adapter.start(initial_url="https://example.com")
    assert "Browser content script did not become ready after injection" in str(exc_info.value)


@pytest.mark.asyncio
async def test_extension_unlock_ui_is_graceful():
    """Verify unlock_ui does not raise exceptions even on extension failure."""
    mock_mgr = MagicMock()
    mock_mgr.send_command = AsyncMock(side_effect=RuntimeError("WebSocket disconnected"))

    adapter = ExtensionBrowserAdapter(task_id="test-task-5", manager=mock_mgr)
    # unlock_ui must catch and log at debug level, never raise
    await adapter.unlock_ui()


@pytest.mark.asyncio
async def test_extension_manager_send_command_roundtrip():
    """Verify ExtensionManager correlates response by id and resolves future."""
    mgr = ExtensionManager()
    mock_ws = AsyncMock()
    task_id = "test-roundtrip"

    async with mgr.lock:
        mgr.active_connections.add(mock_ws)
        mgr.task_connections[task_id] = mock_ws

    async def simulate_extension_reply():
        await asyncio.sleep(0.05)
        # Find the pending request id
        async with mgr.lock:
            msg_id = next(iter(mgr.pending_requests.keys()))
        reply = {
            "id": msg_id,
            "task_id": task_id,
            "type": "response",
            "success": True,
            "data": {"locked": True},
        }
        await mgr.handle_message(mock_ws, reply)

    reply_task = asyncio.create_task(simulate_extension_reply())
    resp = await mgr.send_command(task_id=task_id, command_type="lock_ui", timeout=2.0)
    await reply_task

    assert resp["success"] is True
    assert resp["data"]["locked"] is True
