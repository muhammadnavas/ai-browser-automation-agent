import pytest
from unittest.mock import patch, MagicMock
from models.schemas import TaskResponse, TaskStatus, TaskContext, ActionType
from api.task_manager import TaskManager
from api.routes import get_pool_status


@pytest.mark.asyncio
async def test_pool_status_preserves_full_plan_and_context():
    """Verify that get_pool_status returns full TaskResponse dictionaries so the frontend doesn't drop plan/context data."""
    tm = TaskManager()
    task_id = "test-task-123"
    task = TaskResponse(
        task_id=task_id,
        goal="Test goal",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=110.0,
        initial_plan=["Step 1: Open website", "Step 2: Fill form", "Step 3: Complete"],
        plan=["Step 1: Open website", "Step 2: Fill form", "Step 3: Complete"],
        plan_status="executing",
        current_plan_step=1,
        plan_metadata={"category_label": "Form Automation", "cache_details": {"hit": True}},
        context=TaskContext(
            visited_urls=["https://example.com"],
            action_history=[],
            filled_fields={"#name": {"label": "Name", "value": "Alice"}},
        ),
    )
    tm.tasks[task_id] = task

    with patch("api.routes.task_manager", tm), \
         patch("api.routes.get_browser_pool") as mock_pool, \
         patch("api.routes.extension_manager") as mock_ext_mgr:
        mock_ext_mgr.get_status.return_value = {"connected": True}
        mock_p = MagicMock()
        mock_p.get_metrics.return_value = {"is_started": True}
        mock_pool.return_value = mock_p

        status = await get_pool_status()
        assert status["active_tasks_count"] == 1
        assert len(status["running_tasks"]) == 1
        running = status["running_tasks"][0]
        assert running["task_id"] == task_id
        assert running["initial_plan"] == ["Step 1: Open website", "Step 2: Fill form", "Step 3: Complete"]
        assert running["plan_metadata"]["category_label"] == "Form Automation"
        assert running["context"]["visited_urls"] == ["https://example.com"]
        assert running["context"]["filled_fields"]["#name"]["value"] == "Alice"


def test_task_context_mutation_and_serialization():
    """Verify TaskContext captures visits, actions, fills, and serializes safely."""
    context = TaskContext()
    context.record_visit("https://example.com/login")
    context.record_action(
        step=1,
        action=ActionType.CLICK.value,
        description="Clicked Login button",
        reasoning="To access dashboard",
        url="https://example.com/login",
        success=True,
    )
    context.record_fill("#username", "Username", "alice")
    context.record_fill("#password", "Password", "supersecret123")  # sensitive!

    assert context.visited_urls == ["https://example.com/login"]
    assert len(context.action_history) == 1
    assert context.action_history[0].description == "Clicked Login button"
    # Verify sensitive password was masked
    assert context.filled_fields["#password"]["value"] == "********"
    assert context.filled_fields["#username"]["value"] == "alice"

    # Verify serialization/deserialization retains memory
    dumped = context.model_dump()
    restored = TaskContext.model_validate(dumped)
    assert restored.visited_urls == ["https://example.com/login"]
    assert len(restored.action_history) == 1
    assert restored.filled_fields["#password"]["value"] == "********"
