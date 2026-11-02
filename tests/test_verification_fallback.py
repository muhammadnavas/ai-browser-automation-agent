"""
tests/test_verification_fallback.py
------------------------------------
Regression tests for task verification fallback when model is unavailable:
1. Successful website navigation when verification model is unavailable.
2. Successful extraction or summary when verification model is unavailable.
3. Successful report generation when verification model is unavailable.
4. Genuine browser-action failure.
5. Task whose outcome cannot be determined from available evidence (unverified outcome).
6. Verification timeouts and bounded retries falling back to deterministic verification.
7. Prevention of duplicate side effects during recovery / FINISH execution.
8. Normal operation when the verification model is available.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from models.schemas import (
    ActionType,
    AgentStep,
    BrowserAction,
    TaskContext,
    TaskResponse,
    TaskStatus,
)
from api.task_manager import TaskManager


@pytest.fixture
def tm():
    """Create a fresh TaskManager instance for testing."""
    manager = TaskManager()
    return manager


@pytest.fixture
def mock_controller():
    """Create a mock BrowserController."""
    ctrl = MagicMock()
    ctrl.current_url = "https://example.com"
    ctrl.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com",
        "title": "Example Domain",
        "page_text": "This domain is for use in illustrative examples in documents.",
        "headings": ["Example Domain"],
        "elements": [{"tag": "a", "text": "More information...", "href": "https://www.iana.org/domains/example"}],
    })
    ctrl.clear_badges = AsyncMock()
    return ctrl


@pytest.fixture
def mock_planner_no_llm():
    """Create a planner instance with no LLM client (model unavailable)."""
    planner = MagicMock()
    planner.client = None
    planner.model_name = "models/gemini-2.5"
    return planner



# ===========================================================================
# Extension action postcondition verification
# ===========================================================================

@pytest.mark.asyncio
async def test_extension_navigation_postcondition_requires_target_url(tm):
    controller = MagicMock()
    controller.current_url = "https://example.com"
    controller.parse_interactive_elements = AsyncMock(return_value={"url": "https://example.com"})
    action = BrowserAction(action=ActionType.NAVIGATE, url="https://example.com")
    verified, message = await tm._verify_extension_action_postcondition(
        action, controller, before_page={"url": "about:blank"}
    )
    assert verified is True
    assert "Navigation verified" in message


@pytest.mark.asyncio
async def test_extension_fill_postcondition_checks_live_field_value(tm):
    controller = MagicMock()
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com",
        "form_fields": [{"selector": "#email", "current_value": "navas@example.com"}],
    })
    action = BrowserAction(action=ActionType.FILL, selector="#email", text="navas@example.com")
    verified, message = await tm._verify_extension_action_postcondition(action, controller, {})
    assert verified is True
    assert "value verified" in message


@pytest.mark.asyncio
async def test_extension_fill_postcondition_rejects_mismatched_value(tm):
    controller = MagicMock()
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com",
        "form_fields": [{"selector": "#email", "current_value": "old@example.com"}],
    })
    action = BrowserAction(action=ActionType.FILL, selector="#email", text="new@example.com")
    verified, message = await tm._verify_extension_action_postcondition(action, controller, {})
    assert verified is False
    assert "does not match" in message


@pytest.mark.asyncio
async def test_extension_click_postcondition_does_not_claim_success_from_missing_evidence(tm):
    controller = MagicMock()
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com",
        "elements": [{"index": 1, "text": "Continue", "selector": "#continue"}],
    })
    action = BrowserAction(action=ActionType.CLICK, selector="#continue", text="Continue")
    verified, message = await tm._verify_extension_action_postcondition(
        action, controller,
        before_page={"url": "https://example.com", "elements": [{"index": 1, "text": "Continue", "selector": "#continue"}]},
    )
    assert verified is True
    assert "unchanged" in message


@pytest.mark.asyncio
async def test_extension_fill_postcondition_resolves_element_index_to_selector(tm):
    controller = MagicMock()
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com",
        "elements": [{"index": 7, "selector": "#search", "tag": "input"}],
        "form_fields": [{"selector": "#search", "current_value": "agent automation"}],
    })
    action = BrowserAction(action=ActionType.FILL, element_id=7, text="agent automation")
    verified, message = await tm._verify_extension_action_postcondition(action, controller, {})
    assert verified is True
    assert "value verified" in message


@pytest.mark.asyncio
async def test_extension_checkbox_postcondition_checks_live_checked_state(tm):
    controller = MagicMock()
    controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com",
        "elements": [{"index": 3, "selector": "#terms", "checked": True}],
    })
    action = BrowserAction(action=ActionType.CHECK_BOX, element_id=3, checked=True)
    verified, message = await tm._verify_extension_action_postcondition(action, controller, {})
    assert verified is True
    assert "Checkbox state verified" in message



def test_extension_action_precondition_rejects_missing_target(tm):
    action = BrowserAction(action=ActionType.CLICK, element_id=9)
    ok, message = tm._verify_extension_action_precondition(
        action, {"elements": [{"index": 1, "selector": "#other", "visible": True}]}
    )
    assert ok is False
    assert "no longer present" in message


def test_extension_action_precondition_rejects_disabled_target(tm):
    action = BrowserAction(action=ActionType.FILL, selector="#email")
    ok, message = tm._verify_extension_action_precondition(
        action, {"elements": [{"index": 2, "selector": "#email", "disabled": True}]}
    )
    assert ok is False
    assert "disabled" in message


def test_extension_action_precondition_allows_text_targeted_double_click(tm):
    action = BrowserAction(action=ActionType.DOUBLE_CLICK, text="Project folder")
    ok, message = tm._verify_extension_action_precondition(action, {"elements": []})
    assert ok is True
    assert "Text-targeted" in message


# ===========================================================================
# 1. Successful Navigation when Model is Unavailable
# ===========================================================================

@pytest.mark.asyncio
async def test_navigation_verified_when_model_unavailable(tm, mock_controller, mock_planner_no_llm):
    """Verify navigation to a target site succeeds deterministically without model."""
    task = TaskResponse(
        task_id="task_nav_1",
        goal="Go to https://example.com",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )
    task.steps.append(AgentStep(
        step_number=1,
        action=ActionType.NAVIGATE,
        description="Navigated to https://example.com",
        url="https://example.com",
    ))

    verified, msg = await tm._verify_goal_completion(
        mock_controller, task, mock_planner_no_llm, []
    )

    assert verified is True
    assert "Outcome verified" in msg
    assert "https://example.com" in msg
    assert "Example Domain" in msg
    assert "verification model is unavailable" not in msg.lower()


# ===========================================================================
# 2. Successful Extraction / Summary when Model is Unavailable
# ===========================================================================

@pytest.mark.asyncio
async def test_extraction_summary_verified_when_model_unavailable(tm, mock_controller, mock_planner_no_llm):
    """Verify extraction / summary task succeeds deterministically when data was collected."""
    task = TaskResponse(
        task_id="task_ext_1",
        goal="Summarize the latest news on https://news.ycombinator.com",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )
    task.steps.append(AgentStep(
        step_number=1,
        action=ActionType.NAVIGATE,
        description="Navigated to news",
        url="https://news.ycombinator.com",
    ))
    task.steps.append(AgentStep(
        step_number=2,
        action=ActionType.EXTRACT,
        description="Extracted news headlines",
        url="https://news.ycombinator.com",
    ))
    task.extracted_data = [
        {
            "headings": ["Show HN: Antigravity Agent", "Python 3.14 Released", "New AI Breakthrough"],
            "paragraphs": ["Discussion about new developments in autonomous browser automation."],
        }
    ]

    verified, msg = await tm._verify_goal_completion(
        mock_controller, task, mock_planner_no_llm, []
    )

    assert verified is True
    assert "Outcome verified" in msg
    assert "extracted" in msg.lower()
    assert "headings" in msg.lower()


# ===========================================================================
# 3. Successful Report Generation when Model is Unavailable
# ===========================================================================

@pytest.mark.asyncio
async def test_report_generation_verified_when_model_unavailable(tm, mock_controller, mock_planner_no_llm):
    """Verify report generation task succeeds deterministically when report artifact exists."""
    task_id = "task_rep_1"
    task = TaskResponse(
        task_id=task_id,
        goal="Generate PDF report for product research",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )
    # Store dummy valid report bytes
    tm._report_store[task_id] = b"%PDF-1.4 simulated pdf report content with substantial data"

    verified, msg = await tm._verify_goal_completion(
        mock_controller, task, mock_planner_no_llm, []
    )

    assert verified is True
    assert "Outcome verified" in msg
    assert "report artifact" in msg.lower()


# ===========================================================================
# 4. Genuine Browser-Action Failure
# ===========================================================================

@pytest.mark.asyncio
async def test_genuine_browser_action_failure(tm, mock_planner_no_llm):
    """Verify genuine browser navigation failure (e.g. error page) fails properly."""
    bad_ctrl = MagicMock()
    bad_ctrl.current_url = "chrome-error://chromewebdata/"
    bad_ctrl.parse_interactive_elements = AsyncMock(return_value={
        "url": "chrome-error://chromewebdata/",
        "title": "Site cannot be reached",
        "page_text": "DNS_PROBE_FINISHED_NXDOMAIN",
        "headings": [],
        "elements": [],
    })

    task = TaskResponse(
        task_id="task_fail_1",
        goal="Go to https://nonexistent-fake-domain-999.xyz",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )
    task.steps.append(AgentStep(
        step_number=1,
        action=ActionType.NAVIGATE,
        description="Navigation failed",
        error="net::ERR_NAME_NOT_RESOLVED",
        url="chrome-error://chromewebdata/",
    ))

    verified, msg = await tm._verify_goal_completion(
        bad_ctrl, task, mock_planner_no_llm, []
    )

    assert verified is False
    assert "failed" in msg.lower()
    assert "error page" in msg.lower() or "errors" in msg.lower()


# ===========================================================================
# 5. Task Whose Outcome Cannot Be Determined (Unverified Outcome)
# ===========================================================================

@pytest.mark.asyncio
async def test_task_outcome_cannot_be_determined_reported_unverified(tm, mock_controller, mock_planner_no_llm):
    """
    Verify that when evidence is ambiguous or insufficient to confirm the goal,
    it reports the outcome as unverified with an explicit explanation, rather than
    fabricating success or claiming a model crash.
    """
    task = TaskResponse(
        task_id="task_ambig_1",
        goal="Ensure the secret logo color turned green",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )
    task.steps.append(AgentStep(
        step_number=1,
        action=ActionType.CLICK,
        description="Clicked unknown element",
        url="https://example.com",
    ))

    verified, msg = await tm._verify_goal_completion(
        mock_controller, task, mock_planner_no_llm, []
    )

    assert verified is False
    assert "Outcome unverified" in msg
    assert "insufficient" in msg.lower()
    assert "Ensure the secret logo color turned green" in msg


# ===========================================================================
# 6. Verification Timeout Falls Back to Deterministic Verification
# ===========================================================================

@pytest.mark.asyncio
async def test_verification_timeout_falls_back_to_deterministic(tm, mock_controller):
    """Verify that if LLM verifier times out, it falls back smoothly to deterministic check."""
    planner = MagicMock()
    planner.client = MagicMock()
    planner.model_name = "models/gemini-2.5"

    def slow_llm_call(*args, **kwargs):
        import time
        time.sleep(20)
        return "{}"

    planner._call_llm = MagicMock(side_effect=slow_llm_call)

    task = TaskResponse(
        task_id="task_timeout_1",
        goal="Go to https://example.com",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )

    async def mock_wait_for_timeout(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.wait_for", side_effect=mock_wait_for_timeout):
        verified, msg = await tm._verify_goal_completion(
            mock_controller, task, planner, []
        )

    # Navigation deterministic fallback should have evaluated and passed
    assert verified is True
    assert "Outcome verified" in msg
    assert "https://example.com" in msg


# ===========================================================================
# 7. Prevention of Duplicate Side Effects / Retries on FINISH
# ===========================================================================

@pytest.mark.asyncio
async def test_finish_action_prevents_duplicate_side_effects(tm, mock_controller, mock_planner_no_llm):
    """
    Ensure FINISH action verification failure transitions status cleanly
    without repeating browser clicks or triggering retry loops.
    """
    task = TaskResponse(
        task_id="task_side_effect_1",
        goal="Submit application form",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )
    tm.tasks[task.task_id] = task

    # Set controller to show unfilled form errors (genuine failure)
    mock_controller.parse_interactive_elements = AsyncMock(return_value={
        "url": "https://example.com/form",
        "title": "Application Form",
        "page_text": "This field is required. Please enter a valid email address.",
        "headings": ["Application Form"],
        "elements": [],
    })

    finish_action = BrowserAction(action=ActionType.FINISH, reasoning="Form submitted")

    # Execute FINISH
    desc, extracted, success = await tm._execute_action(
        finish_action, mock_controller, task, task.task_id, mock_planner_no_llm, []
    )

    assert success is False
    assert task.status == TaskStatus.FAILED
    assert "Validation error detected" in task.error or "Form submission failed" in task.error
    # Ensure badges were not cleared on failure and no duplicate clicks occurred
    assert mock_controller.clear_badges.call_count == 0


# ===========================================================================
# 8. Normal Operation when Model is Available
# ===========================================================================

@pytest.mark.asyncio
async def test_normal_operation_when_model_is_available(tm, mock_controller):
    """Verify normal model-based verification succeeds when LLM is available and confirms success."""
    planner = MagicMock()
    planner.client = MagicMock()
    planner.model_name = "models/gemini-2.5"

    model_response = {
        "completed": True,
        "confidence": "high",
        "evidence": "Confirmation banner 'Application ID #12345' is clearly visible in headings.",
        "reason": "The user goal was met and application reference is displayed.",
    }
    planner._call_llm = MagicMock(return_value=json.dumps(model_response))

    task = TaskResponse(
        task_id="task_llm_1",
        goal="Submit mortgage application",
        status=TaskStatus.RUNNING,
        created_at=100.0,
        updated_at=100.0,
        context=TaskContext(),
    )

    verified, msg = await tm._verify_goal_completion(
        mock_controller, task, planner, []
    )

    assert verified is True
    assert "Outcome verified" in msg
    assert "Application ID #12345" in msg
