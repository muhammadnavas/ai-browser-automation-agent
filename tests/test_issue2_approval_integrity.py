"""
tests/test_issue2_approval_integrity.py
---------------------------------------
Regression test suite for Issue 2: Approval Integrity, Cryptographic/TTL Expiration,
Anti-Replay Enforcement, and UI Stability.
"""

import time
import concurrent.futures
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.models.schemas import (
    HumanApprovalRequest,
    ApprovalType,
    TaskRequest,
    TaskStatus,
    BrowserAction,
    ActionType,
    compute_action_fingerprint,
)
from backend.api.task_manager import TaskManager


class TestApprovalIntegrity:
    def test_approval_request_ttl_expiration(self):
        req = HumanApprovalRequest(
            task_id="test-task",
            type=ApprovalType.CUSTOM,
            prompt_message="Test prompt",
            created_at=time.time() - 400.0,
            expires_at=time.time() - 100.0,  # Expired
        )
        assert req.is_expired() is True

        fresh_req = HumanApprovalRequest(
            task_id="test-task",
            type=ApprovalType.CUSTOM,
            prompt_message="Test prompt",
        )
        assert fresh_req.is_expired() is False

    def test_expired_approval_is_rejected_by_task_manager(self):
        tm = TaskManager()
        task_req = TaskRequest(goal="Test goal")
        task = tm.create_task(task_req)
        task.status = TaskStatus.WAITING_APPROVAL

        # Set expired approval request
        task.approval_request = HumanApprovalRequest(
            task_id=task.task_id,
            type=ApprovalType.CUSTOM,
            prompt_message="Expired approval",
            created_at=time.time() - 400.0,
            expires_at=time.time() - 100.0,
        )

        with pytest.raises(ValueError) as exc_info:
            tm.approve_human_step(task.task_id, approved=True)
        assert "expired" in str(exc_info.value).lower()

    def test_anti_replay_enforcement(self):
        tm = TaskManager()
        task_req = TaskRequest(goal="Test goal")
        task = tm.create_task(task_req)
        task.status = TaskStatus.WAITING_APPROVAL

        task.approval_request = HumanApprovalRequest(
            task_id=task.task_id,
            type=ApprovalType.CUSTOM,
            prompt_message="Fresh approval",
        )

        # First approval succeeds
        tm.approve_human_step(task.task_id, approved=True)
        assert task.approval_request.consumed is True
        assert task.status == TaskStatus.RUNNING

        # Second approval on the consumed request must be rejected
        with pytest.raises(ValueError) as exc_info:
            tm.approve_human_step(task.task_id, approved=True)
        assert "already been consumed" in str(exc_info.value).lower()

    def test_approval_fingerprint_cryptographic_binding(self):
        """Verifies that tampering with any parameter alters the cryptographic fingerprint."""
        base_fp = compute_action_fingerprint(
            task_id="task-123",
            action_type="click",
            target_url="https://example.com/checkout",
            selector="#submit-btn",
            text="Submit",
            value="100",
        )
        assert isinstance(base_fp, str) and len(base_fp) == 64

        # Identical parameters yield identical HMAC
        matching_fp = compute_action_fingerprint(
            task_id="task-123",
            action_type="click",
            target_url="https://example.com/checkout",
            selector="#submit-btn",
            text="Submit",
            value="100",
        )
        assert base_fp == matching_fp

        # Changed destination URL
        tampered_url_fp = compute_action_fingerprint(
            task_id="task-123",
            action_type="click",
            target_url="https://evil.com/checkout",
            selector="#submit-btn",
            text="Submit",
            value="100",
        )
        assert base_fp != tampered_url_fp

        # Changed action type
        tampered_action_fp = compute_action_fingerprint(
            task_id="task-123",
            action_type="navigate",
            target_url="https://example.com/checkout",
            selector="#submit-btn",
            text="Submit",
            value="100",
        )
        assert base_fp != tampered_action_fp

        # Changed selector
        tampered_selector_fp = compute_action_fingerprint(
            task_id="task-123",
            action_type="click",
            target_url="https://example.com/checkout",
            selector="#evil-btn",
            text="Submit",
            value="100",
        )
        assert base_fp != tampered_selector_fp

        # Changed task ID
        tampered_task_fp = compute_action_fingerprint(
            task_id="task-999",
            action_type="click",
            target_url="https://example.com/checkout",
            selector="#submit-btn",
            text="Submit",
            value="100",
        )
        assert base_fp != tampered_task_fp

    def test_approval_atomic_concurrency_race(self):
        """Verifies that multiple concurrent approval submissions cannot race-condition into multi-consumption."""
        tm = TaskManager()
        task_req = TaskRequest(goal="Concurrency test")
        task = tm.create_task(task_req)
        task.status = TaskStatus.WAITING_APPROVAL

        task.approval_request = HumanApprovalRequest(
            task_id=task.task_id,
            type=ApprovalType.CUSTOM,
            prompt_message="Concurrent race test",
        )

        successes = 0
        rejections = 0
        exceptions = []

        def try_approve():
            nonlocal successes, rejections
            try:
                tm.approve_human_step(task.task_id, approved=True)
                return True
            except ValueError as e:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(try_approve) for _ in range(25)]
            for f in concurrent.futures.as_completed(futures):
                if f.result() is True:
                    successes += 1
                else:
                    rejections += 1

        # Exactly ONE call must succeed, the remaining 24 must fail with already consumed
        assert successes == 1
        assert rejections == 24
        assert task.approval_request.consumed is True
        assert task.human_approval_count == 1

    @pytest.mark.asyncio
    async def test_execution_blocks_on_fingerprint_mismatch(self):
        """Verifies that _execute_action verifies the fingerprint before resuming and aborts if mismatched."""
        import asyncio
        tm = TaskManager()
        task_req = TaskRequest(goal="Transfer test")
        task = tm.create_task(task_req)

        action = BrowserAction(
            action=ActionType.CLICK,
            selector="#wire-transfer",
            text="wire transfer",
            value="5000",
        )

        controller = MagicMock()
        controller.current_page = None
        controller.current_url = "https://bank.example.com"
        controller.is_running = True
        controller.execute_action = AsyncMock(return_value=("Clicked", None))

        async def tamper_and_approve():
            for _ in range(50):
                if task.approval_request is not None and task.task_id in tm.approval_events:
                    break
                await asyncio.sleep(0.01)
            # Tamper with the fingerprint
            task.approval_request.action_fingerprint = "0000000000000000000000000000000000000000000000000000000000000000"
            tm.approve_human_step(task.task_id, approved=True)

        tamper_task = asyncio.create_task(tamper_and_approve())

        desc, item, success = await tm._execute_action(
            action=action,
            controller=controller,
            task=task,
            task_id=task.task_id,
            planner=MagicMock(),
            trajectory_history=[],
        )
        await tamper_task

        # Execution should be blocked due to signature mismatch
        assert success is False
        assert "fingerprint mismatch" in desc.lower() or "signature mismatch" in desc.lower()
        controller.execute_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_execution_succeeds_with_valid_fingerprint(self):
        """Verifies that _execute_action resumes and executes when fingerprint matches."""
        import asyncio
        tm = TaskManager()
        task_req = TaskRequest(goal="Transfer test")
        task = tm.create_task(task_req)

        action = BrowserAction(
            action=ActionType.CLICK,
            selector="#wire-transfer",
            text="wire transfer",
            value="5000",
        )

        controller = MagicMock()
        controller.current_page = None
        controller.current_url = "https://bank.example.com"
        controller.is_running = True
        controller.execute_action = AsyncMock(return_value=("Clicked", None))

        async def approve_valid():
            for _ in range(50):
                if task.approval_request is not None and task.task_id in tm.approval_events:
                    break
                await asyncio.sleep(0.01)
            tm.approve_human_step(task.task_id, approved=True)

        approve_task = asyncio.create_task(approve_valid())

        desc, item, success = await tm._execute_action(
            action=action,
            controller=controller,
            task=task,
            task_id=task.task_id,
            planner=MagicMock(),
            trajectory_history=[],
        )
        await approve_task

        assert success is True
        controller.execute_action.assert_called_once()
