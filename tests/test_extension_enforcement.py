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
