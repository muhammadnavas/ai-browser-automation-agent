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



def test_orchestration_milestone_graph_tracks_ordered_dependencies():
    graph = TaskManager._build_milestone_graph([
        "Open the site",
        "Inspect the form",
        "Fill required fields",
    ])

    assert [item["id"] for item in graph] == ["m1", "m2", "m3"]
    assert graph[0]["depends_on"] == []
    assert graph[1]["depends_on"] == ["m1"]
    assert graph[2]["depends_on"] == ["m2"]
    assert all(item["status"] == "pending" for item in graph)


def test_orchestration_action_fingerprint_is_stable_and_payload_sensitive():
    from models.schemas import BrowserAction

    first = BrowserAction(action=ActionType.FILL, selector="#name", text="Navas")
    same = BrowserAction(action=ActionType.FILL, selector="#name", text="Navas", reasoning="different explanation")
    changed = BrowserAction(action=ActionType.FILL, selector="#name", text="Muhammad")

    assert TaskManager._action_fingerprint(first) == TaskManager._action_fingerprint(same)
    assert TaskManager._action_fingerprint(first) != TaskManager._action_fingerprint(changed)


def test_orchestration_milestone_graph_does_not_mark_progress_as_complete():
    task = TaskResponse(
        task_id="orchestration-test",
        goal="Complete a form",
        status=TaskStatus.RUNNING,
        created_at=1.0,
        updated_at=1.0,
        initial_plan=["Open form", "Fill fields", "Review and submit"],
        current_plan_step=1,
        plan_metadata={},
    )

    TaskManager._refresh_milestone_graph(task)
    statuses = [item["status"] for item in task.plan_metadata["milestone_graph"]]

    assert statuses == ["in_progress", "active", "pending"]
    assert "completed" not in statuses
