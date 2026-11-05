"""
api/task_manager.py
-------------------
Manages async background execution of AI Browser Automation tasks.
Handles WebSocket subscriptions for live-streaming, human approval gating,
and final report compilation.

Wires together:
  - ExtensionBrowserAdapter (Chrome Extension browser lifecycle + actions)
  - AIAgentPlanner     (LLM / heuristic decision engine)
  - All ActionType enum values (including new sub-module actions)
  - ReportGenerator    (PDF output)
"""

import asyncio
import json
import hashlib
import os
import re
import time
import uuid
import logging
from typing import Dict, List, Any, Optional, Set, Tuple
from urllib.parse import urlparse

from fastapi import WebSocket

from models.schemas import (
    TaskRequest, TaskResponse, TaskStatus, AgentStep,
    ActionType, HumanApprovalRequest, ApprovalType, DialogAction,
    BrowserAction
)
from browser.extension_adapter import ExtensionBrowserAdapter, extension_manager
from browser.controller import BrowserController
from browser.security import validate_url_security
from agents.planner import AIAgentPlanner, domains_match, extract_target_url
from agents.subagents import SubagentSpec, SubagentBatchResult, subagent_orchestrator
from agents.browser_subagents import register_browser_subagents, close_parent_form_review_tabs

register_browser_subagents()
from services.report_generation_service import ReportGenerator
from services.task_archive import task_archive
from services.cache_service import CacheService, default_cache_service

logger = logging.getLogger("TaskManager")


class TaskManager:
    """
    Manages async background execution of AI Browser Automation tasks.

    Responsibilities:
      - Task lifecycle (create, run, complete, cancel, fail)
      - WebSocket broadcast for live step updates
      - Human approval gating (CAPTCHA / payment)
      - Executing every ActionType through the Chrome Extension
      - PDF report generation on task completion
    """

    # Terminal task metadata and reports are archived in bounded SQLite storage.

    def __init__(
        self,
        agent_version: str = "1.0.0",
        cache_service: Optional[CacheService] = None,
    ):
        self.tasks: Dict[str, TaskResponse] = {}
        self.task_history: Dict[str, TaskResponse] = {}   # persisted terminal tasks
        self.running_tasks: Dict[str, asyncio.Task] = {}  # live background asyncio tasks
        self.controllers: Dict[str, ExtensionBrowserAdapter] = {}
        self.approval_events: Dict[str, asyncio.Event] = {}
        self.cancel_events: Dict[str, asyncio.Event] = {}
        # Set = execution may proceed; cleared = cooperatively paused.
        self.pause_events: Dict[str, asyncio.Event] = {}
        self.subscribers: Dict[str, Set[WebSocket]] = {}
        self.report_generator = ReportGenerator()
        # Shared orchestration runtime; child contexts remain isolated by child ID.
        self.subagent_orchestrator = subagent_orchestrator
        self.agent_version = agent_version
        self._report_store: Dict[str, bytes] = {}  # short-lived report cache
        # Bounded structured recovery diagnostics, keyed by task id.
        self.recovery_diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        try:
            self.task_history.update({task.task_id: task for task in task_archive.list_tasks()})
        except Exception as exc:
            logger.warning("Could not restore task archive: %s", exc)
        if cache_service is not None:
            self._injected_cache_service = cache_service

    def _is_cancelled(self, task_id: str) -> bool:
        """Returns True immediately if the task was marked CANCELLED or cancel event is set."""
        if task_id in self.tasks and self.tasks[task_id].status == TaskStatus.CANCELLED:
            return True
        ev = self.cancel_events.get(task_id)
        return bool(ev and ev.is_set())

    @staticmethod
    def _action_fingerprint(action: BrowserAction) -> str:
        """Stable fingerprint for detecting an identical action against an unchanged page."""
        payload = action.dict(exclude={"reasoning"}, exclude_none=True)
        # Element indices are snapshot-local. Include them only alongside the DOM fingerprint.
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_milestone_graph(milestones: List[str]) -> List[Dict[str, Any]]:
        """Represent plan milestones as an ordered dependency graph (each depends on its predecessor)."""
        graph = []
        for index, title in enumerate(milestones or []):
            graph.append({
                "id": f"m{index + 1}",
                "index": index,
                "title": str(title),
                "depends_on": [f"m{index}"] if index else [],
                "status": "pending",
            })
        return graph

    @staticmethod
    def _refresh_milestone_graph(task: TaskResponse) -> None:
        """Refresh milestone states; only explicit verification may mark a milestone complete."""
        metadata = task.plan_metadata if isinstance(task.plan_metadata, dict) else {}
        graph = metadata.get("milestone_graph")
        if not isinstance(graph, list) or len(graph) != len(task.initial_plan or []):
            graph = TaskManager._build_milestone_graph(task.initial_plan or [])

        completed_ids = set(metadata.get("completed_milestone_ids") or [])
        active = max(0, min(int(task.current_plan_step or 0), len(graph) - 1)) if graph else -1
        completed_ids.intersection_update(node.get("id") for node in graph)
        for idx, node in enumerate(graph):
            if node.get("id") in completed_ids:
                node["status"] = "completed"
            elif idx == active:
                node["status"] = "active"
            elif idx < active:
                # Earlier steps are historical progress, not verified completion.
                node["status"] = "in_progress"
            else:
                node["status"] = "pending"
        metadata["completed_milestone_ids"] = sorted(
            completed_ids, key=lambda value: int(str(value).lstrip("m") or 0)
        )
        metadata["milestone_graph"] = graph
        task.plan_metadata = metadata

    async def _sleep_or_cancel(self, task_id: str, delay: float) -> bool:
        """
        Sleeps for `delay` seconds, or returns True immediately if cancellation is triggered.
        Returns True if cancelled, False if delay expired normally.
        """
        if self._is_cancelled(task_id):
            return True
        ev = self.cancel_events.get(task_id)
        if ev:
            try:
                await asyncio.wait_for(ev.wait(), timeout=delay)
                return True
            except asyncio.TimeoutError:
                return False
        else:
            await asyncio.sleep(delay)
            return self._is_cancelled(task_id)

    # ------------------------------------------------------------------
    # Redis Task State Caching Helpers
    # ------------------------------------------------------------------

    def _generate_task_state_cache_key(
        self,
        task_id: str,
        dom_fingerprint: Optional[str] = None,
        cache_service: Optional[CacheService] = None,
    ) -> Optional[str]:
        """Generate a deterministic, safe Redis cache key for temporary task execution state."""
        cs = cache_service or getattr(self, "_injected_cache_service", None) or default_cache_service
        if not cs:
            return None
        return cs.generate_key(
            cache_type="task_state",
            agent_version=self.agent_version,
            task_or_goal=task_id,
            url="",
            dom_fingerprint=dom_fingerprint,
        )

    async def _restore_cached_task_state(
        self,
        task_id: str,
        dom_fingerprint: Optional[str] = None,
        cache_service: Optional[CacheService] = None,
    ) -> bool:
        """Restore safe temporary execution state from Redis if a valid entry exists."""
        cs = cache_service or getattr(self, "_injected_cache_service", None) or default_cache_service
        if not cs or task_id not in self.tasks:
            return False

        key = self._generate_task_state_cache_key(task_id, dom_fingerprint, cache_service=cs)
        if not key:
            return False

        try:
            def validate_state_payload(cached_obj: Any) -> bool:
                if not isinstance(cached_obj, dict):
                    return False
                if cached_obj.get("task_id") != task_id:
                    return False
                if str(cached_obj.get("agent_version", "")).strip().lower() != self.agent_version.strip().lower():
                    return False
                # Only interrupted/in-progress checkpoints are resumable. Never
                # resurrect a completed, failed, or cancelled task from stale cache.
                if cached_obj.get("status") not in {
                    TaskStatus.PAUSED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.WAITING_APPROVAL.value,
                }:
                    return False
                if "current_step" not in cached_obj:
                    return False
                try:
                    if int(cached_obj["current_step"]) < 0:
                        return False
                except (TypeError, ValueError):
                    return False
                if not isinstance(cached_obj.get("initial_plan"), list):
                    return False
                return True

            cached_state = await cs.get_validated(key, validator=validate_state_payload)
            if cached_state:
                logger.info(f"task-state cache HIT for task '{task_id}' (key='{key}')")
                task = self.tasks[task_id]

                raw_status = cached_state.get("status")
                if raw_status:
                    try:
                        task.status = TaskStatus(raw_status)
                    except ValueError:
                        pass

                task.current_step = cached_state.get("current_step", task.current_step)
                if "initial_plan" in cached_state:
                    task.initial_plan = cached_state["initial_plan"]
                if "plan_status" in cached_state:
                    task.plan_status = cached_state["plan_status"]
                if "current_plan_step" in cached_state:
                    task.current_plan_step = cached_state["current_plan_step"]
                if "plan_metadata" in cached_state:
                    task.plan_metadata = cached_state["plan_metadata"]

                if "steps" in cached_state and isinstance(cached_state["steps"], list):
                    restored_steps = []
                    for step_dict in cached_state["steps"]:
                        try:
                            restored_steps.append(AgentStep.model_validate(step_dict))
                        except Exception:
                            pass
                    if restored_steps:
                        task.steps = restored_steps

                if "context" in cached_state and isinstance(cached_state["context"], dict):
                    from models.schemas import TaskContext
                    try:
                        task.context = TaskContext.model_validate(cached_state["context"])
                    except Exception:
                        pass

                return True
            else:
                logger.info(f"task-state cache MISS for task '{task_id}' (key='{key}')")
                return False
        except Exception as exc:
            logger.warning(f"task-state cache lookup failed (Redis failure, non-fatal): {exc}")
            return False

    async def _save_cached_task_state(
        self,
        task_id: str,
        dom_fingerprint: Optional[str] = None,
        cache_service: Optional[CacheService] = None,
    ) -> bool:
        """Sanitize and persist safe temporary execution state in Redis."""
        cs = cache_service or getattr(self, "_injected_cache_service", None) or default_cache_service
        if not cs or task_id not in self.tasks:
            return False

        key = self._generate_task_state_cache_key(task_id, dom_fingerprint, cache_service=cs)
        if not key:
            return False

        try:
            task = self.tasks[task_id]
            safe_steps = []
            for step in task.steps:
                if hasattr(step, "model_dump"):
                    safe_steps.append(step.model_dump())
                elif isinstance(step, dict):
                    safe_steps.append(step)

            payload = {
                "task_id": task.task_id,
                "agent_version": self.agent_version,
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "current_step": task.current_step,
                "initial_plan": task.initial_plan,
                "plan_status": task.plan_status,
                "current_plan_step": task.current_plan_step,
                "plan_metadata": task.plan_metadata,
                "steps": safe_steps,
                "context": task.context.model_dump() if hasattr(task, "context") else None,
                "updated_at": task.updated_at,
            }

            sanitized_payload = cs.sanitize_sensitive_data(payload)
            logger.info(f"task-state cache SET for task '{task_id}' (key='{key}')")
            return await cs.set(key, sanitized_payload, ttl=600)
        except Exception as exc:
            logger.warning(f"task-state cache SET failed (Redis failure, non-fatal): {exc}")
            return False

    async def _invalidate_cached_task_state(
        self,
        task_id: str,
        dom_fingerprint: Optional[str] = None,
        cache_service: Optional[CacheService] = None,
    ) -> bool:
        """Delete temporary task execution state from Redis when task reaches a terminal state."""
        cs = cache_service or getattr(self, "_injected_cache_service", None) or default_cache_service
        if not cs:
            return False

        key = self._generate_task_state_cache_key(task_id, dom_fingerprint, cache_service=cs)
        if not key:
            return False

        try:
            logger.info(f"task-state cache INVALIDATED for task '{task_id}' (key='{key}')")
            return await cs.delete(key)
        except Exception as exc:
            logger.warning(f"task-state cache INVALIDATED failed (Redis failure, non-fatal): {exc}")
            return False

    async def extract_page_data_cached(
        self,
        controller: Any,
        task_or_goal: str = "",
        dom_fingerprint: Optional[str] = None,
        extraction_context: str = "page_data",
        cache_service: Optional[CacheService] = None,
    ) -> Dict[str, Any]:
        """Extract page data with Redis caching for intermediate extraction results."""
        from extraction import cached_extraction
        cs = cache_service or getattr(self, "_injected_cache_service", None) or default_cache_service
        url = getattr(controller, "current_url", "") or (
            getattr(controller.current_page, "url", "") if getattr(controller, "current_page", None) else ""
        )
        return await cached_extraction(
            cache_service=cs,
            agent_version=self.agent_version,
            extractor_fn=controller.extract_page_data,
            url=url,
            dom_fingerprint=dom_fingerprint,
            task_or_goal=task_or_goal,
            extraction_context=extraction_context,
        )

    # ------------------------------------------------------------------
    # History (durable and bounded)
    # ------------------------------------------------------------------

    def _record_terminal_task(self, task: TaskResponse) -> None:
        """Persist terminal task metadata/report and keep only a bounded hot cache."""
        try:
            task_archive.save_task(task, self._report_store.get(task.task_id))
        except Exception:
            logger.exception("Failed to archive terminal task %s", task.task_id)
        # Clear sensitive task vault upon task termination
        try:
            from backend.security import clear_task_vault
            clear_task_vault(task.task_id)
        except Exception:
            pass
        self.task_history[task.task_id] = task
        while len(self.task_history) > task_archive.max_tasks:
            oldest_id = min(self.task_history, key=lambda key: self.task_history[key].updated_at)
            self.task_history.pop(oldest_id, None)
        self._report_store.pop(task.task_id, None)

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------

    def create_task(self, req: TaskRequest) -> TaskResponse:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        now = time.time()

        # Check task goal against security policy (blocking Tier 1 secrets)
        from backend.security import check_goal_policy, get_task_vault
        is_allowed, reason = check_goal_policy(req.goal, task_id=task_id)
        if not is_allowed:
            from backend.security import redact_text, get_security_user_message, strip_internal_placeholders
            user_error = get_security_user_message(reason)
            clean_goal = strip_internal_placeholders(redact_text(req.goal), replacement="[protected]")
            task_resp = TaskResponse(
                task_id=task_id,
                goal=clean_goal,
                status=TaskStatus.FAILED,
                error=user_error,
                created_at=now,
                updated_at=now,
                max_steps=req.max_steps,
                steps=[],
                extracted_data=[],
                reports={},
            )
            self._record_terminal_task(task_resp)
            return task_resp

        # Sanitize and vault authorized contact info in the user goal
        vault = get_task_vault(task_id, calling_task_id=task_id)
        sanitized_goal = vault.sanitize_and_vault_text(req.goal)
        req.goal = sanitized_goal

        task_resp = TaskResponse(
            task_id=task_id,
            goal=req.goal,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            max_steps=req.max_steps,
            steps=[],
            extracted_data=[],
            reports={},
        )
        self.tasks[task_id] = task_resp
        return task_resp

    async def run_subagent_batch(self, parent_task_id: str, specs: List[SubagentSpec], *, fail_fast: bool = False) -> SubagentBatchResult:
        """Run registered child workers under an existing parent task."""
        if parent_task_id not in self.tasks and parent_task_id not in self.task_history:
            raise KeyError(f"Parent task {parent_task_id} not found")
        return await self.subagent_orchestrator.run_batch(parent_task_id, specs, fail_fast=fail_fast)

    def register_subagent_worker(self, role: str, worker: Any) -> None:
        """Register a role-specific async worker without replacing the main executor."""
        self.subagent_orchestrator.register_worker(role, worker)

    # ------------------------------------------------------------------
    # WebSocket pub/sub
    # ------------------------------------------------------------------

    async def register_subscriber(self, task_id: str, websocket: WebSocket) -> None:
        if task_id not in self.subscribers:
            self.subscribers[task_id] = set()
        self.subscribers[task_id].add(websocket)

    async def unregister_subscriber(self, task_id: str, websocket: WebSocket) -> None:
        if task_id in self.subscribers:
            self.subscribers[task_id].discard(websocket)

    async def broadcast_task_update(self, task_id: str) -> None:
        if task_id not in self.subscribers or task_id not in self.tasks:
            return

        from backend.security import redact_sensitive_data, sanitize_user_facing_error, strip_internal_placeholders
        task = self.tasks[task_id]
        is_explicit_contact = any(k in (getattr(task, "goal", "") or "").lower() for k in ["contact", "email", "phone", "address", "directory", "lead", "who to call"])
        try:
            task_data = redact_sensitive_data(
                task.model_dump(),
                mask_tier3=not is_explicit_contact,
                preserve_contact_info=is_explicit_contact,
            )
            if task_data.get("error"):
                task_data["error"] = sanitize_user_facing_error(task_data["error"])
            if task_data.get("goal"):
                task_data["goal"] = strip_internal_placeholders(task_data["goal"])
            if task_data.get("steps"):
                for s in task_data["steps"]:
                    if isinstance(s, dict):
                        if s.get("error"):
                            s["error"] = sanitize_user_facing_error(s["error"])
                        if s.get("reasoning"):
                            s["reasoning"] = strip_internal_placeholders(s["reasoning"])
        except Exception as exc:
            logger.error("Failed to sanitize task broadcast, failing closed: %s", exc)
            task_data = {
                "task_id": task_id,
                "goal": "[protected]",
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "error": "This action cannot be completed due to security restrictions.",
            }
        dead: Set[WebSocket] = set()
        for ws in self.subscribers[task_id]:
            try:
                await ws.send_json(task_data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.subscribers[task_id].discard(ws)

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_resume_checkpoint(checkpoint: Any, page_info: Dict[str, Any]) -> Tuple[bool, str]:
        """Fail closed unless the freshly inspected tab matches the saved page URL."""
        if not isinstance(checkpoint, dict):
            return False, "No page checkpoint was saved for this task."
        expected_url = str(checkpoint.get("url") or "").strip()
        actual_url = str(page_info.get("url") or "").strip()
        if not expected_url or not actual_url:
            return False, "The saved or current browser URL is unavailable."
        if actual_url.rstrip("/") != expected_url.rstrip("/"):
            return False, (
                "The live browser page does not match the saved checkpoint. "
                f"Expected {expected_url}, found {actual_url}. No action was executed."
            )
        return True, "Live browser URL matches the saved checkpoint."

    async def run_task_execution(self, task_id: str, req: TaskRequest) -> None:
        """Drive a full browser automation task through its step loop."""
        if task_id not in self.tasks:
            return

        task = self.tasks[task_id]
        restored_checkpoint = await self._restore_cached_task_state(task_id)
        task.status = TaskStatus.RUNNING
        task.updated_at = time.time()
        await self.broadcast_task_update(task_id)

        # Chrome Extension is the only supported browser execution engine.
        # Never launch a managed browser or silently fall back to another engine.
        if not extension_manager.is_connected:
            task.status = TaskStatus.FAILED
            task.plan_status = "failed"
            task.error = "Chrome Extension is disconnected. Connect the extension in Chrome before starting a task."
            task.updated_at = time.time()
            self._record_terminal_task(task)
            await self.broadcast_task_update(task_id)
            await self._save_cached_task_state(task_id)
            return

        use_extension = True
        controller = ExtensionBrowserAdapter(
            task_id=task_id,
            show_badges=req.show_badges,
            screenshots_dir="screenshots",
        )
        self.controllers[task_id] = controller
        planner = AIAgentPlanner()
        trajectory_history: List[Dict[str, Any]] = []
        user_approval_input: Optional[str] = None   # stores last human approval reply

        # Preserve checkpointed context when resuming an existing task. Clearing it
        # here would discard the very history needed for safe continuation.
        if not restored_checkpoint:
            task.context.clear()
        finish_verified = False

        try:
            # ── Phase 0: Generate plan and broadcast it before any browser action ──
            task.plan_status = "restoring" if restored_checkpoint else "planning"
            if not restored_checkpoint or not isinstance(task.plan_metadata, dict):
                task.plan_metadata = planner.get_plan_metadata(
                    goal=req.goal,
                    start_url=req.start_url or "",
                )
            else:
                # Keep checkpointed plan versions and recovery history intact.
                task.plan_metadata.setdefault("goal", req.goal)
                task.plan_metadata.setdefault("start_url", req.start_url or "")
            task.updated_at = time.time()
            await self.broadcast_task_update(task_id)

            plan_start_time = time.time()
            if restored_checkpoint and task.initial_plan:
                # Reuse the persisted milestone plan instead of replacing it with a
                # newly generated plan that would no longer align with saved progress.
                plan = list(task.initial_plan)
                plan_duration_ms = 0.0
                task.plan = list(plan)
                task.plan_status = "restored"
                logger.info(
                    "Task %s restored checkpoint at action %s / plan milestone %s.",
                    task_id, task.current_step, task.current_plan_step,
                )
            else:
                plan = await planner.generate_plan(
                    goal=req.goal,
                    start_url=req.start_url or "",
                )
                plan_duration_ms = round((time.time() - plan_start_time) * 1000, 1)
                task.initial_plan = plan or []
                task.plan = plan or []
                task.plan_status = "planned"
                task.current_plan_step = 0

            # Attach plan cache and reuse details to plan_metadata
            cache_info = getattr(planner, "last_plan_cache_result", None) or {
                "hit": False,
                "tier": "None",
                "source": "llm",
                "reused": False,
                "message": "Generated execution plan",
                "details": f"Generated plan with {len(plan or [])} milestones.",
            }
            cache_info["duration_ms"] = plan_duration_ms
            cache_info["plan"] = plan or []
            if task.plan_metadata is None:
                task.plan_metadata = {}
            task.plan_metadata["cache_details"] = cache_info
            task.plan_metadata["plan"] = plan or []
            task.plan_metadata["initial_plan"] = plan or []
            task.plan_metadata["milestone_graph"] = self._build_milestone_graph(plan or [])
            task.plan_metadata.setdefault("last_successful_action", None)
            task.plan_metadata.setdefault("plan_version", 1)
            task.plan_metadata.setdefault("adaptive_replan_count", 0)
            if not task.plan_metadata.get("plan_versions"):
                task.plan_metadata["plan_versions"] = [{
                    "version": 1,
                    "reason": "initial_plan",
                    "created_at": time.time(),
                    "milestones": list(plan or []),
                }]

            task.updated_at = time.time()
            await self.broadcast_task_update(task_id)
            await self._save_cached_task_state(task_id)

            # Optional master-agent delegation. Existing clients send no
            # subagent_specs and continue through the unchanged single-agent path.
            raw_subagent_specs = getattr(req, "subagent_specs", None) or []
            if raw_subagent_specs:
                child_specs = [SubagentSpec.model_validate(item) for item in raw_subagent_specs]
                child_batch = await self.run_subagent_batch(task_id, child_specs)
                task.plan_metadata["subagent_batch_status"] = child_batch.status
                task.plan_metadata["subagent_summary"] = child_batch.summary
                for child_result in child_batch.results:
                    output = child_result.output
                    if isinstance(output, dict):
                        output = dict(output)
                        if isinstance(output.get("page_text"), str):
                            output["page_text"] = output["page_text"][:6000]
                        if isinstance(output.get("evidence_excerpt"), str):
                            output["evidence_excerpt"] = output["evidence_excerpt"][:1200]
                        if isinstance(output.get("links"), list):
                            output["links"] = output["links"][:20]
                    task.context.extracted_snippets.append({
                        "source": "subagent",
                        "subagent_id": child_result.subagent_id,
                        "role": child_result.role,
                        "status": child_result.status.value,
                        "goal": child_result.goal,
                        "output": output,
                        "error": child_result.error,
                    })
                task.context.extracted_snippets = task.context.extracted_snippets[-20:]
                task.updated_at = time.time()
                await self.broadcast_task_update(task_id)
                await self._save_cached_task_state(task_id)

            # Cancellation may arrive while delegated workers are running, before
            # the main executor has initialized its pause/cancel events.
            if task.status == TaskStatus.CANCELLED:
                task.plan_status = "cancelled"
                task.updated_at = time.time()
                await self.broadcast_task_update(task_id)
                return

            # ── Phase 1: Start browser and navigate ────────────────────────────
            target_goal_url = planner.extract_target_url(req.goal)
            effective_start_url = req.start_url
            resume_checkpoint = (
                task.plan_metadata.get("execution_checkpoint")
                if restored_checkpoint and isinstance(task.plan_metadata, dict)
                else None
            )
            if restored_checkpoint:
                if not isinstance(resume_checkpoint, dict) or not resume_checkpoint.get("url"):
                    raise RuntimeError(
                        "Cannot safely resume: the restored task has no verified page checkpoint. "
                        "Start a new task after checking the browser state."
                    )
                effective_start_url = str(resume_checkpoint["url"])
            if (not effective_start_url or effective_start_url in ("https://www.google.com", "about:blank")) and target_goal_url:
                effective_start_url = target_goal_url

            if effective_start_url and effective_start_url not in ("about:blank",):
                safe, reason, normalized = await asyncio.to_thread(
                    validate_url_security, effective_start_url, True
                )
                if not safe:
                    raise ValueError(f"Initial navigation blocked by security policy: {reason}")
                effective_start_url = normalized

            if use_extension:
                await controller.start(effective_start_url)
            else:
                await controller.start()

                if effective_start_url:
                    await controller.navigate(effective_start_url)

            # current_step is the last fully recorded action; continue at the next
            # step number. The extension opens a fresh task tab, so the planner must
            # inspect the live page before acting rather than replaying old commands.
            step_num = max(1, int(task.current_step or 0) + 1) if restored_checkpoint else 1
            consecutive_failures = 0
            task.plan_status = "executing"
            self.cancel_events[task_id] = asyncio.Event()
            self.pause_events[task_id] = asyncio.Event()
            self.pause_events[task_id].set()
            await self.broadcast_task_update(task_id)
            while (
                step_num <= req.max_steps
                and task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
                and not self._is_cancelled(task_id)
            ):
                # Pause is cooperative: let an in-flight browser action finish,
                # then stop before planning or executing the next action.
                await self.pause_events[task_id].wait()
                if self._is_cancelled(task_id):
                    break
                if task.status == TaskStatus.PAUSED:
                    continue
                if hasattr(controller, "lock_ui"):
                    await controller.lock_ui()
                step_start = time.time()

                # ── Guard: distinguish a closed tab from a lost extension connection ──
                if use_extension and task_id in controller.manager.disconnected_tasks:
                    logger.error(f"Task {task_id} stopping: Chrome extension connection was lost.")
                    task.status = TaskStatus.FAILED
                    task.plan_status = "failed"
                    task.error = (
                        "Chrome Extension disconnected during execution. "
                        "Reconnect the extension and start a new task; the previous "
                        "browser session cannot be safely resumed."
                    )
                    break

                if use_extension and task_id in controller.manager.closed_tasks:
                    logger.warning(f"Task {task_id} stopping: Chrome tab was closed by user.")
                    task.status = TaskStatus.CANCELLED
                    task.plan_status = "cancelled"
                    task.error = "Chrome tab was closed by the user."
                    break

                # ── Guard: exit cleanly if cancellation was requested ──
                if self._is_cancelled(task_id):
                    logger.info(f"Task {task_id} cancellation detected before step {step_num}.")
                    break

                # 1. Snapshot DOM & Perform Form Discovery
                #    Retry once if content script not yet injected (tab just opened)
                page_info = await controller.parse_interactive_elements()
                if page_info.get("inspection_error"):
                    logger.warning(
                        "Task %s: live DOM inspection failed; retrying before making page or auth decisions",
                        task_id,
                    )
                    if await self._sleep_or_cancel(task_id, 1.0):
                        break
                    page_info = await controller.parse_interactive_elements()
                    if page_info.get("inspection_error"):
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = (
                            "Could not inspect the live Chrome task tab. No browser action was attempted. "
                            "Check that the AI Browser Agent extension is reloaded and connected, then retry."
                        )
                        task.updated_at = time.time()
                        await self.broadcast_task_update(task_id)
                        break

                # Deterministic page-state analysis informs the planner before it
                # chooses an action. Page content is treated as evidence, never
                # as instructions.
                from agents.page_state import classify_page_state
                page_info["agent_state"] = classify_page_state(page_info)

                if default_cache_service:
                    from browser.controller import generate_dom_fingerprint
                    task.context.last_dom_fingerprint = generate_dom_fingerprint(page_info, default_cache_service)

                if use_extension and not page_info.get("elements") and not page_info.get("page_text"):
                    logger.info("DOM empty — waiting for content script to initialise, retrying in 2s…")
                    if await self._sleep_or_cancel(task_id, 2.0):
                        break
                    page_info = await controller.parse_interactive_elements()

                if restored_checkpoint and resume_checkpoint is not None:
                    checkpoint_ok, checkpoint_message = self._validate_resume_checkpoint(
                        resume_checkpoint, page_info
                    )
                    if not checkpoint_ok:
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = checkpoint_message
                        task.updated_at = time.time()
                        logger.error("Task %s resume checkpoint rejected: %s", task_id, checkpoint_message)
                        await self.broadcast_task_update(task_id)
                        await self._save_cached_task_state(task_id)
                        break
                    logger.info("Task %s resume checkpoint validated: %s", task_id, checkpoint_message)
                    # Validate once before allowing any new browser action.
                    restored_checkpoint = False
                
                # Form discovery is supplied by the Chrome content-script snapshot.
                task.context.current_form_fields = page_info.get("form_fields", [])

                # Record the current URL as a visited page. Google may redirect
                # Gmail navigation to RotateCookiesPage when its session cookies
                # cannot be rotated. Do not treat that intermediate page as Gmail,
                # extract it as task data, or keep retrying actions against it.
                # Authentication decisions must use only the URL observed
                # by the latest successful extension DOM inspection. The
                # controller's cached current_url can be stale after redirects.
                _current_url = str(
                    page_info.get("browser_tab_url")
                    or page_info.get("url")
                    or ""
                ).strip()
                if _current_url:
                    task.context.record_visit(_current_url)

                # Google can briefly traverse RotateCookiesPage during Gmail
                # navigation and then return to the authenticated Gmail app.
                # Confirm the live URL before treating that transient redirect
                # as a blocking account challenge.
                if "accounts.google.com/RotateCookiesPage" in _current_url:
                    for _challenge_check in range(3):
                        await asyncio.sleep(1)
                        try:
                            live_info = await controller.parse_interactive_elements()
                            if live_info.get("inspection_error"):
                                continue
                            live_url = str(
                                live_info.get("browser_tab_url")
                                or live_info.get("url")
                                or ""
                            ).strip()
                            if live_url and "accounts.google.com/RotateCookiesPage" not in live_url:
                                page_info = live_info
                                _current_url = live_url
                                if _current_url.startswith(("https://mail.google.com/", "http://mail.google.com/")):
                                    logger.info(
                                        "Task %s: Google cookie-rotation redirect was transient; Gmail is live at %s",
                                        task_id, _current_url
                                    )
                                break
                        except Exception as challenge_check_error:
                            logger.debug(
                                "Could not confirm transient Google redirect for %s: %s",
                                task_id, challenge_check_error
                            )

                if "accounts.google.com/RotateCookiesPage" in _current_url:
                    # Google requires the account owner to resolve this session
                    # challenge. Do not attempt to bypass it or fail the whole
                    # task immediately: pause and let the user recover the same
                    # connected Chrome tab, then resume from a fresh DOM snapshot.
                    message = (
                        "Gmail is still on Google's internal cookie-rotation endpoint after repeated live checks. "
                        "This endpoint is not a user-facing recovery form and may appear blank; the agent will not "
                        "open duplicate blank tabs or attempt to bypass Google's session controls. Open Gmail "
                        "manually in the original task tab. Once Gmail is usable, return here and approve to resume. "
                        "No email has been composed or sent yet."
                    )
                    logger.warning("Task %s remains on Google's internal cookie-rotation endpoint: %s", task_id, _current_url)
                    if hasattr(controller, "unlock_ui"):
                        await controller.unlock_ui()
                    # Do not steal browser focus while waiting for manual recovery.
                    task.status = TaskStatus.WAITING_APPROVAL
                    task.plan_status = "waiting_approval"
                    task.error = message
                    task.approval_request = HumanApprovalRequest(
                        task_id=task_id,
                        type=ApprovalType.CUSTOM,
                        prompt_message=message,
                        screenshot_url=None,
                    )
                    event = asyncio.Event()
                    self.approval_events[task_id] = event
                    task.updated_at = time.time()
                    await self.broadcast_task_update(task_id)

                    if not self._is_cancelled(task_id):
                        await event.wait()

                    if self._is_cancelled(task_id) or task.status == TaskStatus.CANCELLED:
                        task.approval_request = None
                        break

                    # Approval is only a signal to check the browser again; it
                    # does not itself clear Google's account challenge. Wait for
                    # the live Chrome tab to leave RotateCookiesPage before
                    # resuming the planner, otherwise the same modal loops.
                    task.approval_request = None
                    task.status = TaskStatus.RUNNING
                    task.plan_status = "recovering_session"
                    task.error = (
                        "Approval received. Checking the original Gmail task tab. "
                        "The separate recovery tab is not used as the automation tab."
                    )
                    task.updated_at = time.time()
                    await self.broadcast_task_update(task_id)

                    # The user completes Google's prompt in the separate tab.
                    # Re-navigate only the original task tab after explicit
                    # approval, then verify that Google serves the Gmail app.
                    try:
                        await controller.navigate("https://mail.google.com/mail/u/0/#inbox")
                    except Exception as navigation_error:
                        logger.warning(
                            "Could not reload original Gmail task tab after recovery for %s: %s",
                            task_id, navigation_error
                        )

                    session_recovered = False
                    recovery_deadline = time.monotonic() + 90
                    while time.monotonic() < recovery_deadline:
                        if self._is_cancelled(task_id) or task.status == TaskStatus.CANCELLED:
                            break
                        try:
                            refreshed_info = await controller.parse_interactive_elements()
                            if refreshed_info.get("inspection_error"):
                                await asyncio.sleep(2)
                                continue
                            refreshed_url = str(
                                refreshed_info.get("browser_tab_url")
                                or refreshed_info.get("url")
                                or ""
                            ).strip()
                            # Only treat recovery as complete when Gmail itself
                            # is open. Any other Google/account page is not sufficient.
                            if (
                                refreshed_url.startswith("https://mail.google.com/")
                                or refreshed_url.startswith("http://mail.google.com/")
                            ):
                                page_info = refreshed_info
                                session_recovered = True
                                break
                        except Exception as refresh_error:
                            logger.debug("Gmail session recovery check failed for %s: %s", task_id, refresh_error)
                        await asyncio.sleep(2)

                    if self._is_cancelled(task_id) or task.status == TaskStatus.CANCELLED:
                        break

                    if not session_recovered:
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = (
                            "Google's session challenge is not yet resolved. Approval cannot bypass it. "
                            "The original Gmail task tab did not load the Gmail app within 90 seconds after "
                            "the recovery step. Complete any remaining Google security prompts in the separate "
                            "recovery tab, then start a fresh task after confirming Gmail works."
                        )
                        task.updated_at = time.time()
                        await self.broadcast_task_update(task_id)
                        break

                    task.error = None
                    task.plan_status = "executing"
                    task.updated_at = time.time()
                    await self.broadcast_task_update(task_id)
                    # Re-inspect the recovered Gmail page at this same step.
                    continue

                if self._is_cancelled(task_id):
                    break

                # 1.5. User-Assisted Authentication Gate (Password / OTP detection)
                from backend.security import is_auth_field, audit_logger
                auth_fields_detected = [
                    f for f in (page_info.get("form_fields", []) or [])
                    if is_auth_field(f)
                ] or [
                    el for el in (page_info.get("elements", []) or [])
                    if is_auth_field(el)
                ]
                current_auth_sig = tuple(sorted(str(f.get("selector") or f.get("index") or f.get("name") or f.get("id")) for f in auth_fields_detected)) if auth_fields_detected else None

                if auth_fields_detected and getattr(task, "_auth_confirmed_state", None) != current_auth_sig:
                    audit_logger.log_auth_event(task_id, action="paused", reason="Password or OTP input field detected on page")
                    if hasattr(controller, "unlock_ui"):
                        await controller.unlock_ui()
                    task.status = TaskStatus.WAITING_APPROVAL
                    task.plan_status = "waiting_approval"
                    from backend.security import get_security_user_message, SecurityEventType
                    auth_msg = get_security_user_message(SecurityEventType.AUTH_REQUIRED)
                    task.error = auth_msg
                    task.approval_request = HumanApprovalRequest(
                        task_id=task_id,
                        type=ApprovalType.CUSTOM,
                        prompt_message=auth_msg,
                        screenshot_url=None,  # Suppress screenshots during auth pause
                    )
                    event = asyncio.Event()
                    self.approval_events[task_id] = event
                    task.updated_at = time.time()
                    await self.broadcast_task_update(task_id)

                    if not self._is_cancelled(task_id):
                        await event.wait()

                    if self._is_cancelled(task_id) or task.status == TaskStatus.CANCELLED or (task.approval_request and task.approval_request.status == "rejected"):
                        task.status = TaskStatus.CANCELLED
                        task.plan_status = "cancelled"
                        task.approval_request = None
                        break

                    # Resuming from user-assisted authentication
                    task._auth_confirmed_state = current_auth_sig
                    task.approval_request = None
                    task.status = TaskStatus.RUNNING
                    task.plan_status = "executing"
                    task.error = None
                    user_approval_input = ""  # Ignore user_input during auth pause

                    audit_logger.log_auth_event(task_id, action="resumed", reason="User confirmed completion of authentication in browser")

                    try:
                        page_info = await controller.parse_interactive_elements()
                    except Exception as refresh_exc:
                        logger.warning("Failed to refresh DOM after auth resume: %s", refresh_exc)

                if self._is_cancelled(task_id):
                    break

                # 2. Security gate check
                captcha_or_payment = await controller.check_captcha_or_payment()

                if self._is_cancelled(task_id):
                    break

                # 3. Screenshot before action
                # If sensitive auth fields are present and extension cannot guarantee reliable pixel masking, skip screenshot capture
                if use_extension and any(is_auth_field(el) for el in (page_info.get("elements", []) or [])):
                    screenshot_path, screenshot_url = None, None
                else:
                    screenshot_path, screenshot_url = await controller.capture_screenshot(
                        task_id, step_num
                    )

                if self._is_cancelled(task_id):
                    break

                # 4. Planner decides next action
                action = await planner.decide_next_action(
                    goal=req.goal,
                    step_number=step_num,
                    max_steps=req.max_steps,
                    page_info=page_info,
                    extracted_data=task.extracted_data,
                    trajectory_history=trajectory_history,
                    captcha_or_payment_detected=captcha_or_payment,
                    user_approval_input=user_approval_input,
                    task_context=task.context,
                    screenshot_path=screenshot_path,
                )

                # Gmail is a dynamic SPA: bind Compose clicks to the fresh DOM
                # snapshot instead of trusting a brittle nth-of-type CSS selector.
                task_intent = planner._analyze_task_intent(req.goal)
                if action.action == ActionType.CLICK and task_intent.get("category_key") == "email_composition":
                    from agents.planner import find_matching_element
                    compose_candidates = [
                        el for el in (page_info.get("elements") or [])
                        if isinstance(el, dict)
                        and not el.get("disabled", False)
                        and el.get("visible", True)
                        and (
                            "compose" in str(el.get("text") or "").lower()
                            or "compose" in str(el.get("aria_label") or "").lower()
                            or "compose" in str(el.get("title") or "").lower()
                        )
                        and (el.get("tag") in ("button", "div", "span") or el.get("role") == "button")
                    ]
                    compose_target = find_matching_element(compose_candidates, "Compose")
                    if compose_target and compose_target.get("index") is not None:
                        action.element_id = int(compose_target["index"])
                        action.selector = None
                        logger.info(
                            "Rebound Gmail Compose click to fresh DOM element index %s (center_hit=%s)",
                            action.element_id, compose_target.get("center_hit")
                        )

                # Guard against planner loops: if two successful navigations have
                # already left us on the same page, do not navigate again. Switch
                # to the deterministic heuristic so it can inspect/fill/extract
                # the current page instead of burning the task step budget.
                current_page_url = (
                    page_info.get("url")
                    or getattr(controller, "current_url", "")
                    or ""
                ).rstrip("/")
                has_navigated_before = any(item.get("action") == ActionType.NAVIGATE.value for item in trajectory_history)
                is_on_valid_page = bool(current_page_url) and "about:blank" not in current_page_url and "chrome-error" not in current_page_url
                target_url_nav = action.url or planner.extract_target_url(req.goal)
                on_target_domain = bool(target_url_nav and domains_match(current_page_url, target_url_nav))

                repeated_navigation = (
                    action.action == ActionType.NAVIGATE
                    and is_on_valid_page
                    and (has_navigated_before or on_target_domain)
                )
                if repeated_navigation:
                    logger.warning(
                        "Task %s: suppressing repeated NAVIGATE to %s (current: %s); "
                        "using heuristic action selection.",
                        task_id,
                        target_url_nav,
                        current_page_url,
                    )
                    action = planner._heuristic_decide_action(
                        goal=req.goal,
                        step_number=step_num,
                        page_info=page_info,
                        extracted_data=task.extracted_data,
                        trajectory_history=trajectory_history,
                    )
                    if action.action == ActionType.NAVIGATE:
                        action = BrowserAction(
                            action=ActionType.WAIT,
                            reasoning="Already arrived at target URL. Inspecting page contents.",
                        )
                # Suppress an identical state-changing action only when the page has not changed
                # since the previous successful execution. If state changed, the action may be valid again.
                if action.action not in (
                    ActionType.WAIT, ActionType.SCROLL, ActionType.SCROLL_TO,
                    ActionType.EXTRACT, ActionType.SCREENSHOT, ActionType.FINISH,
                    ActionType.REQUEST_APPROVAL, ActionType.HOVER,
                ):
                    action_signature = self._action_fingerprint(action)
                    prior = (task.plan_metadata or {}).get("last_successful_action") if isinstance(task.plan_metadata, dict) else None
                    current_fingerprint = task.context.last_dom_fingerprint
                    if (
                        isinstance(prior, dict)
                        and prior.get("signature") == action_signature
                        and bool(current_fingerprint)
                        and prior.get("dom_fingerprint") == current_fingerprint
                        and prior.get("url") == current_page_url
                    ):
                        logger.warning(
                            "Task %s: suppressing repeated %s action against unchanged DOM state.",
                            task_id, action.action.value,
                        )
                        action = planner._heuristic_decide_action(
                            goal=req.goal,
                            step_number=step_num,
                            page_info=page_info,
                            extracted_data=task.extracted_data,
                            trajectory_history=trajectory_history,
                        )
                        if action.action not in (
                            ActionType.WAIT, ActionType.SCROLL, ActionType.SCROLL_TO,
                            ActionType.EXTRACT, ActionType.SCREENSHOT, ActionType.FINISH,
                            ActionType.REQUEST_APPROVAL, ActionType.HOVER,
                        ) and self._action_fingerprint(action) == prior.get("signature"):
                            action = BrowserAction(
                                action=ActionType.WAIT,
                                scroll_amount=500,
                                reasoning="The same action already succeeded on this unchanged page. Re-observing before choosing another action.",
                            )

                user_approval_input = None   # consume after use

                # EXTRACT is only valid when the user explicitly requested data
                # extraction/research. Do not let an LLM or heuristic planner turn
                # navigation, email, or form workflows into unsolicited extraction.
                task_intent = planner._analyze_task_intent(req.goal)
                explicit_extract_task = (
                    task_intent.get("category_key") == "search_extraction"
                    or any(
                        token in (req.goal or "").lower()
                        for token in ("extract", "scrape", "summarize", "research", "compare", "collect data", "gather data")
                    )
                )
                if action.action == ActionType.EXTRACT and not explicit_extract_task:
                    logger.warning(
                        "Suppressing unsolicited EXTRACT action for task %s (goal=%r)",
                        task_id, req.goal[:160],
                    )
                    action = BrowserAction(
                        action=ActionType.WAIT,
                        scroll_amount=700,
                        reasoning="Page inspection is internal. The user did not request data extraction; waiting briefly before continuing the requested workflow.",
                    )

                # Plan progress is updated only after the browser action is
                # successfully executed and verified. Failed attempts must never
                # advance a milestone or show a later step as running.

                # Intercept submit action for human confirmation if not already confirmed
                is_submit_action = False
                agent_filled = task.context.filled_fields

                if agent_filled and not task.context.form_confirmed and action.action == ActionType.CLICK:
                    # Detect native submit controls semantically instead of relying only
                    # on visible words such as "Submit" or "Send".
                    current_page = getattr(controller, "current_page", None)
                    if not use_extension and current_page is not None:
                        from browser.executor import ActionExecutor
                        temp_executor = ActionExecutor(current_page)
                        resolved_loc = await temp_executor.resolve_locator(
                            action, page_info.get("form_fields")
                        )
                        if resolved_loc:
                            try:
                                is_submit_action = await resolved_loc.evaluate("""el => {
                                    const tag = (el.tagName || '').toLowerCase();
                                    const type = (el.getAttribute('type') || '').toLowerCase();
                                    const role = (el.getAttribute('role') || '').toLowerCase();
                                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                                    const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                                    const form = el.form || el.closest('form');
                                    const submitWords = /\b(submit|send|register|confirm|apply|place order|book now|save and continue)\b/i;

                                    if (tag === 'input' && ['submit', 'image'].includes(type)) return true;
                                    if (tag === 'button' && type === 'submit') return true;
                                    // A button without an explicit type defaults to submit
                                    // when associated with a form.
                                    if (tag === 'button' && !type && form) return true;
                                    if (role === 'button' && submitWords.test(text + ' ' + label)) return true;
                                    if (submitWords.test(text + ' ' + label)) return true;
                                    return false;
                                }""")
                            except Exception as exc:
                                logger.debug("Could not inspect clicked control submit semantics: %s", exc)
                    else:
                        # Extension execution has no Playwright page handle. Use the
                        # latest inspected element metadata plus the requested target.
                        target_text = " ".join(str(value or "") for value in (
                            action.text, action.selector, action.reasoning
                        )).lower()
                        target_element = None
                        for element in page_info.get("elements", []) or []:
                            if (
                                (action.element_id and element.get("index") == action.element_id)
                                or (action.selector and element.get("selector") == action.selector)
                            ):
                                target_element = element
                                break
                        metadata = target_element or {}
                        tag = str(metadata.get("tag") or metadata.get("tag_name") or "").lower()
                        control_type = str(metadata.get("type") or metadata.get("input_type") or "").lower()
                        role = str(metadata.get("role") or "").lower()
                        label = " ".join(str(metadata.get(key) or "") for key in (
                            "text", "aria_label", "aria-label", "title", "name"
                        )).lower()
                        submit_words = ("submit", "send", "register", "confirm", "apply", "place order", "book now", "save and continue")
                        is_native_submit = (
                            (tag == "button" and control_type == "submit")
                            or (tag == "button" and not control_type and bool(metadata.get("form")))
                            or (tag == "input" and control_type in ("submit", "image"))
                        )
                        is_labeled_submit = role == "button" and any(word in label for word in submit_words)
                        is_submit_action = is_native_submit or is_labeled_submit or any(
                            word in target_text for word in submit_words
                        )

                if is_submit_action and not task.context.form_confirmed:
                    target_url = getattr(controller, "current_url", "") or (getattr(controller.current_page, "url", "") if getattr(controller, "current_page", None) else "")
                    summary_parts = [f"Target Website: {target_url}"]
                    filled_details = []
                    for k, field_data in agent_filled.items():
                        lbl = field_data.get("label") or k
                        val_str = str(field_data.get("value"))
                        if any(w in str(lbl).lower() for w in ["password", "secret", "pin", "token", "cvv", "card"]):
                            val_str = "********"
                        filled_details.append(f" - {lbl}: {val_str}")
                    summary_parts.append(
                        "Fields Filled:\n" + "\n".join(filled_details)
                        if filled_details else "Fields Filled: None detected."
                    )

                    # Inspect discovered required fields and native browser validity
                    # before asking the user to authorize a potentially consequential submit.
                    incomplete_fields = []
                    # Re-inspect the live page immediately before approval. The planner's
                    # earlier page_info snapshot may be stale after fills, rerenders or SPA updates.
                    validation_page_info = page_info
                    try:
                        fresh_page_info = await controller.parse_interactive_elements()
                        if isinstance(fresh_page_info, dict):
                            validation_page_info = fresh_page_info
                    except Exception as exc:
                        logger.warning(
                            "Live form refresh failed before submit approval; using last DOM snapshot: %s",
                            exc,
                        )

                    def field_value(field, key, default=None):
                        return field.get(key, default) if isinstance(field, dict) else getattr(field, key, default)

                    for field in validation_page_info.get("form_fields", []) or []:
                        if (
                            not field_value(field, "required", False)
                            or field_value(field, "disabled", False)
                            or not field_value(field, "visible", True)
                        ):
                            continue
                        current_value = field_value(field, "current_value")
                        field_type = str(field_value(field, "field_type", "") or "").lower()
                        # Required checkbox/radio controls can have empty values even
                        # when checked; their validity is handled by the browser check below.
                        if field_type in ("checkbox", "radio"):
                            continue
                        if current_value is None or (isinstance(current_value, str) and not current_value.strip()):
                            label = (
                                field_value(field, "label")
                                or field_value(field, "name")
                                or field_value(field, "placeholder")
                                or field_value(field, "selector")
                                or "Required field"
                            )
                            incomplete_fields.append(str(label))

                    native_validation_errors = []
                    current_page = getattr(controller, "current_page", None)
                    if current_page is not None:
                        try:
                            native_validation_errors = await current_page.evaluate("""() => {
                                const invalid = Array.from(document.querySelectorAll('input, select, textarea'))
                                    .filter(el => el.willValidate && !el.disabled && !el.checkValidity());
                                return invalid.map(el => {
                                    const label = el.labels && el.labels.length
                                        ? Array.from(el.labels).map(x => x.innerText).join(' ').trim()
                                        : (el.getAttribute('aria-label') || el.name || el.placeholder || el.type || 'Form field');
                                    return {label, message: el.validationMessage || 'Invalid value'};
                                }).slice(0, 20);
                            }""")
                        except Exception as exc:
                            logger.debug("Could not inspect native form validity before submit: %s", exc)

                    if incomplete_fields:
                        summary_parts.append(
                            "Potentially incomplete required fields:\n - " + "\n - ".join(dict.fromkeys(incomplete_fields))
                        )
                    if native_validation_errors:
                        summary_parts.append(
                            "Browser validation warnings:\n" + "\n".join(
                                f" - {item.get('label', 'Field')}: {item.get('message', 'Invalid value')}"
                                for item in native_validation_errors
                            )
                        )
                    if incomplete_fields or native_validation_errors:
                        summary_parts.append(
                            "Review these warnings and the screenshot carefully. The form may be incomplete or contain invalid values."
                        )
                    summary_msg = "\n\n".join(summary_parts)
                    summary_msg += "\n\nDo you want to authorize the form submission?"

                    screenshot_path, screenshot_url = await controller.capture_screenshot(task_id, step_num)
                    
                    task.status = TaskStatus.WAITING_APPROVAL
                    task.approval_request = HumanApprovalRequest(
                        task_id=task_id,
                        type=ApprovalType.CUSTOM,
                        prompt_message=summary_msg,
                        screenshot_url=screenshot_url,
                    )
                    # Register the waiter before broadcasting. Cancellation can
                    # arrive while the update is being sent; registering afterwards
                    # creates a lost-wakeup race and can leave the task stuck.
                    event = asyncio.Event()
                    self.approval_events[task_id] = event
                    await self.broadcast_task_update(task_id)

                    if not self._is_cancelled(task_id):
                        await event.wait()

                    if self._is_cancelled(task_id) or task.status == TaskStatus.CANCELLED:
                        task.approval_request = None
                        return
                        
                    task._form_confirmed = True
                    task.context.confirm_form()
                    task.status = TaskStatus.RUNNING
                    task.approval_request = None

                # 5. Human approval gate
                if (
                    action.action == ActionType.REQUEST_APPROVAL
                    and not (req.auto_approve_captcha and captcha_or_payment == "captcha")
                    and not (
                        task.context.form_confirmed
                        and captcha_or_payment is None
                    )
                ):
                    if hasattr(controller, "unlock_ui"):
                        await controller.unlock_ui()
                    task.status = TaskStatus.WAITING_APPROVAL
                    captcha_label = (captcha_or_payment or "approval").upper()
                    approval_type = (
                        ApprovalType.CAPTCHA
                        if captcha_or_payment == "captcha"
                        else (
                            ApprovalType.PAYMENT
                            if captcha_or_payment == "payment"
                            else ApprovalType.CUSTOM
                        )
                    )
                    task.approval_request = HumanApprovalRequest(
                        task_id=task_id,
                        type=approval_type,
                        prompt_message=(
                            action.text
                            or f"Human action required: Please solve {captcha_label} on the browser."
                        ),
                        screenshot_url=screenshot_url,
                    )
                    # Register before broadcasting to avoid losing a cancellation
                    # signal that arrives while the websocket update is in flight.
                    event = asyncio.Event()
                    self.approval_events[task_id] = event
                    await self.broadcast_task_update(task_id)

                    if not self._is_cancelled(task_id):
                        await event.wait()

                    # Check if task was cancelled or rejected during approval wait
                    if task.status == TaskStatus.CANCELLED or (task.approval_request and task.approval_request.status == "rejected"):
                        task.status = TaskStatus.CANCELLED
                        task.plan_status = "cancelled"
                        task.approval_request = None
                        logger.info(f"Task {task_id} stopped: cancelled/rejected during approval gate.")
                        break

                    # Capture what the user typed in the approval input box
                    if task.approval_request:
                        user_approval_input = task.approval_request.user_input or ""

                    # A user-approved custom review is the authorization
                    # for this form workflow. Persist it so the planner cannot
                    # repeatedly ask for the same approval on subsequent steps.
                    if captcha_or_payment is None:
                        task.context.confirm_form()
                    if user_approval_input:
                        task.context.record_error(
                            f"Human review feedback: {user_approval_input}"
                        )
                    task.status = TaskStatus.RUNNING
                    task.approval_request = None

                # 6. Execute action
                recovery_log: List[str] = []
                submit_origin_url = (
                    getattr(controller, "current_url", "")
                    or (getattr(getattr(controller, "current_page", None), "url", "") or "")
                ) if is_submit_action else ""
                # Upload steps need a post-action screenshot as well as the
                # pre-action screenshot captured at the top of the loop. This is
                # the evidence that shows the selected file chip/preview (or the
                # still-open picker when the upload did not actually complete).
                is_upload_step = False
                if action.action == ActionType.REQUEST_APPROVAL:
                    exec_description = f"User granted approval. Input: {user_approval_input}"
                    extracted_item = None
                    action_success = True
                else:
                    # If this action involves file upload, attach real file from user's system
                    is_upload_step = (
                        (action.reasoning and any(k in action.reasoning.lower() for k in ["file upload", "upload file", "upload", "attach", "add file", "insert file"]))
                        or (action.text and any(k in action.text.lower() for k in ["file upload", "upload file", "upload", "attach", "add file", "insert file", "browse"]))
                        or action.action in (ActionType.UPLOAD, ActionType.UPLOAD_FILE)
                    )
                    if is_upload_step:
                        try:
                            from browser.os_dialog import get_user_system_file
                            accept_types = None
                            if page_info and page_info.get("elements"):
                                for el in page_info["elements"]:
                                    if action.element_id and el.get("index") == action.element_id:
                                        accept_types = el.get("accept")
                                        break
                            # Include the live target's accessible label and, when
                            # available, the hidden file input's real accept rules.
                            # Google Forms keeps these inputs out of the visible DOM
                            # element list, so reading only the Add file button loses
                            # the MIME/extension restrictions.
                            target_context = ""
                            if page_info and page_info.get("elements"):
                                for target in page_info["elements"]:
                                    if (
                                        (action.element_id and target.get("index") == action.element_id)
                                        or (action.selector and target.get("selector") == action.selector)
                                    ):
                                        target_context = " ".join(str(target.get(key) or "") for key in (
                                            "label", "text", "aria_label", "title", "name"
                                        ))
                                        accept_types = target.get("accept") or accept_types
                                        break
                            target_words = {
                                word for word in re.findall(r"[a-z]{3,}", target_context.lower())
                                if word not in {"upload", "file", "add", "button", "click"}
                            }
                            file_inputs = (page_info or {}).get("file_inputs") or []
                            ranked_inputs = []
                            for file_input in file_inputs:
                                question_text = str(file_input.get("question_text") or "").lower()
                                overlap = sum(1 for word in target_words if word in question_text)
                                if file_input.get("accept"):
                                    ranked_inputs.append((overlap, file_input.get("accept")))
                            if ranked_inputs:
                                ranked_inputs.sort(key=lambda item: item[0], reverse=True)
                                # Prefer a semantically matched input; if the page
                                # exposes only one file input, its accept is authoritative.
                                if ranked_inputs[0][0] > 0 or len(ranked_inputs) == 1:
                                    accept_types = ranked_inputs[0][1]
                            context_text = f"{action.reasoning or ''} {action.text or ''} {target_context} {getattr(task, 'error', '') or ''}"
                            real_file = get_user_system_file(
                                goal=task.goal or "",
                                context_text=context_text,
                                accept_types=accept_types,
                            )
                            if real_file:
                                action.file_name = real_file["name"]
                                action.file_type = real_file["mime_type"]
                                action.file_base64 = real_file["base64"]
                                action.upload_paths = [real_file["path"]]
                                action.file_size = real_file.get("size")
                                action.folder_name = real_file.get("folder_name")
                                action.directory_path = real_file.get("directory")
                                action.neighbor_files = real_file.get("neighbor_files")
                                logger.info(f"Attached real system file to action: {real_file['name']} ({real_file['size']} bytes) from folder '{real_file.get('folder_name')}' ({real_file.get('directory')})")

                                # Require explicit user approval before executing the file upload
                                if not getattr(task, "_upload_confirmed", False):
                                    file_path = real_file["path"]
                                    if hasattr(controller, "unlock_ui"):
                                        await controller.unlock_ui()
                                    upload_msg = (
                                        f"File Upload Approval Required: The agent is requesting to upload the following file:\n"
                                        f"Path: {file_path}\n"
                                        f"Name: {real_file['name']}\n"
                                        f"Do you authorize this upload?"
                                    )
                                    task.status = TaskStatus.WAITING_APPROVAL
                                    task.approval_request = HumanApprovalRequest(
                                        task_id=task_id,
                                        type=ApprovalType.CUSTOM,
                                        prompt_message=upload_msg,
                                        screenshot_url=screenshot_url,
                                    )
                                    event = asyncio.Event()
                                    self.approval_events[task_id] = event
                                    task.updated_at = time.time()
                                    await self.broadcast_task_update(task_id)

                                    if not self._is_cancelled(task_id):
                                        await event.wait()

                                    if self._is_cancelled(task_id) or task.status == TaskStatus.CANCELLED or (task.approval_request and task.approval_request.status == "rejected"):
                                        task.status = TaskStatus.CANCELLED
                                        task.plan_status = "cancelled"
                                        task.approval_request = None
                                        audit_logger.log_upload_event(task_id, file_path, False, "User rejected file upload")
                                        break

                                    task._upload_confirmed = True
                                    task.approval_request = None
                                    task.status = TaskStatus.RUNNING
                                    audit_logger.log_upload_event(task_id, file_path, True, "User authorized file upload")
                            else:
                                audit_logger.log_upload_event(task_id, "unknown", False, "No explicit allowlisted file path specified for upload")
                        except Exception as e:
                            logger.warning(f"Could not attach real user file: {e}")

                    # Physical OS hardware interaction for folder double-clicks and file uploads
                    is_file_upload_click = (
                        action.action == ActionType.CLICK
                        and (
                            (action.reasoning and any(k in action.reasoning.lower() for k in ["file upload", "upload file"]))
                            or (action.text and any(k in action.text.lower() for k in ["file upload", "upload file"]))
                            or bool(action.file_base64)
                        )
                    )
                    screen_x, screen_y = None, None
                    if is_file_upload_click or action.action == ActionType.DOUBLE_CLICK:
                        if page_info and page_info.get("elements"):
                            for el in page_info["elements"]:
                                el_text = (el.get("text") or "").lower()
                                el_aria = (el.get("aria_label") or "").lower()
                                el_sel = el.get("selector") or ""
                                if (
                                    (action.element_id and el.get("index") == action.element_id)
                                    or (action.selector and el_sel == action.selector)
                                    or (action.text and (action.text.lower() in el_text or action.text.lower() in el_aria))
                                    or (is_file_upload_click and "file upload" in f"{el_text} {el_aria}")
                                ):
                                    screen_x = el.get("screen_x")
                                    screen_y = el.get("screen_y")
                                    break

                    if action.action == ActionType.DOUBLE_CLICK and screen_x and screen_y and screen_x > 0 and screen_y > 0:
                        try:
                            import pyautogui
                            pyautogui.FAILSAFE = False
                            pyautogui.moveTo(screen_x, screen_y, duration=0.25)
                            pyautogui.doubleClick(screen_x, screen_y)
                            await asyncio.sleep(0.2)
                            pyautogui.press("enter")
                            logger.info(f"Triggered physical OS double-click + Enter at ({screen_x}, {screen_y}) on '{action.text}'.")
                        except Exception as e:
                            logger.debug(f"Physical double click note: {e}")

                    if is_file_upload_click:
                        logger.info(f"File upload target located — executing upload catalog handler.")

                    if self._is_cancelled(task_id):
                        break

                    # Gmail is a dynamic SPA: the first click in an email-writing
                    # task is a semantic Compose action, never a durable numeric index.
                    # Carry the user intent through to the extension's live-DOM resolver.
                    current_url = str(page_info.get("url") or "")
                    goal_lower = str(req.goal or "").lower()
                    gmail_email_task = (
                        use_extension
                        and urlparse(current_url).hostname == "mail.google.com"
                        and action.action == ActionType.CLICK
                        and any(term in goal_lower for term in (
                            "write an email", "compose an email", "send an email",
                            "write a mail", "send a mail", "compose mail",
                            "write a message", "send a message",
                        ))
                        and not any(
                            h.get("action") == "click"
                            and "compose" in str(h.get("reasoning") or h.get("description") or "").lower()
                            for h in trajectory_history
                        )
                    )
                    if gmail_email_task:
                        action.reasoning = (
                            f"{action.reasoning or 'First interaction in Gmail email workflow.'} "
                            "Gmail email workflow: open the Compose window. Resolve the Compose control "
                            "from the current live DOM; do not use the stored numeric element index."
                        )

                    from browser.recovery import execute_with_retry_and_fallback, RetryPolicy
                    policy = RetryPolicy(max_retries=3, base_delay=1.0, max_delay=6.0, backoff_factor=2.0)

                    async def run_action():
                        if use_extension:
                            precondition_ok, precondition_note = self._verify_extension_action_precondition(
                                action, page_info
                            )
                            if not precondition_ok:
                                # Refresh once and attempt a conservative semantic rebind.
                                # Never guess when multiple controls share the same label.
                                try:
                                    refreshed_page = await controller.parse_interactive_elements()
                                    healed, heal_note = self._heal_extension_action_target(
                                        action, page_info, refreshed_page
                                    )
                                    if healed:
                                        retry_ok, retry_note = self._verify_extension_action_precondition(
                                            action, refreshed_page
                                        )
                                        if retry_ok:
                                            precondition_note = f"{heal_note} {retry_note}"
                                        else:
                                            return f"{heal_note} {retry_note}", None, False
                                    else:
                                        return f"{precondition_note} {heal_note}", None, False
                                except Exception as exc:
                                    return f"{precondition_note} Locator refresh failed: {type(exc).__name__}.", None, False
                        if action.action == ActionType.NAVIGATE:
                            target = action.url or action.text or ""
                            if target:
                                safe, reason, normalized = await asyncio.to_thread(
                                    validate_url_security, target, True
                                )
                                if not safe:
                                    raise ValueError(f"Navigation blocked by security policy: {reason}")
                                action.url = normalized
                        res = await self._execute_action(action, controller, task, task_id, planner, trajectory_history)
                        if isinstance(res, tuple) and len(res) == 3:
                            description, extracted, success = res
                        else:
                            description, extracted = res[0], res[1]
                            success = True

                        # Playwright ActionExecutor already verifies its own actions.
                        # The Chrome-extension path needs a separate live-DOM check.
                        if success and use_extension and action.action not in (
                            ActionType.FINISH, ActionType.REQUEST_APPROVAL, ActionType.EXTRACT,
                            ActionType.SCREENSHOT, ActionType.WAIT, ActionType.SCROLL,
                            ActionType.SCROLL_TO, ActionType.HOVER, ActionType.PRESS,
                            ActionType.PRESS_KEY, ActionType.DOWNLOAD_FILE,
                        ):
                            verified, verification_note = await self._verify_extension_action_postcondition(
                                action, controller, before_page=page_info
                            )
                            description = f"{description} | {verification_note}"
                            if not verified:
                                success = False
                        return description, extracted, success

                    # If the user pauses while the planner is deciding, stop
                    # here before issuing the next browser command. An action that
                    # was already in flight is allowed to finish safely.
                    await self.pause_events[task_id].wait()
                    if self._is_cancelled(task_id):
                        break

                    # FINISH is a verification boundary, not a repeatable browser action.
                    # Retrying it can call the verifier multiple times and hide the original
                    # verification failure behind generic retry output.
                    if action.action == ActionType.FINISH:
                        exec_description, extracted_item, action_success = await run_action()
                        recovery_log = []
                    else:
                        exec_description, extracted_item, action_success, recovery_log = (
                            await execute_with_retry_and_fallback(
                                run_action, action, controller, task=task, task_id=task_id, retry_policy=policy
                            )
                        )

                    # Independently inspect the result of a user-authorized form submit.
                    # Never retry the submit here: the server may have accepted it even
                    # when the browser response is ambiguous.
                    if is_submit_action and action_success:
                        try:
                            post_submit = await controller.parse_interactive_elements()
                            post_url = str(post_submit.get("url") or getattr(controller, "current_url", "") or "")
                            post_text = str(post_submit.get("page_text") or "").lower()
                            post_headings = " ".join(str(x) for x in (post_submit.get("headings") or [])).lower()

                            # Prefer explicit, visible feedback regions after submission.
                            # These cover common framework toasts and accessible status/error
                            # announcements that may not be represented in the page summary.
                            feedback_text = ""
                            post_submit_errors = []
                            current_page = getattr(controller, "current_page", None)
                            if current_page is not None:
                                try:
                                    feedback = await current_page.evaluate("""() => {
                                        const selectors = [
                                            '[role="alert"]', '[role="status"]', '[aria-live]',
                                            '.toast', '.toast-message', '.notification',
                                            '.alert', '.form-error', '.error-message',
                                            '.success-message', '.snackbar'
                                        ].join(',');
                                        const visible = el => {
                                            const style = window.getComputedStyle(el);
                                            const rect = el.getBoundingClientRect();
                                            return style.display !== 'none' && style.visibility !== 'hidden'
                                                && rect.width > 0 && rect.height > 0;
                                        };
                                        const messages = Array.from(document.querySelectorAll(selectors))
                                            .filter(visible)
                                            .map(el => (el.innerText || el.textContent || '').trim())
                                            .filter(Boolean);
                                        const invalid = Array.from(document.querySelectorAll('input,select,textarea'))
                                            .filter(el => el.willValidate && !el.disabled && !el.checkValidity())
                                            .map(el => ({
                                                label: (el.labels && Array.from(el.labels).map(x => x.innerText).join(' ').trim())
                                                    || el.getAttribute('aria-label') || el.name || el.placeholder || 'Form field',
                                                message: el.validationMessage || 'Invalid value'
                                            }));
                                        return {messages: messages.slice(0, 30), invalid: invalid.slice(0, 20)};
                                    }""")
                                    feedback_text = " ".join(feedback.get("messages") or []).lower()
                                    post_submit_errors = feedback.get("invalid") or []
                                except Exception as exc:
                                    logger.debug("Could not inspect post-submit feedback regions: %s", exc)

                            observed = f"{feedback_text} {post_headings} {post_text}"
                            success_markers = (
                                "thank you", "successfully submitted", "form submitted",
                                "submission received", "response has been recorded",
                                "order confirmed", "confirmation number", "message sent",
                                "registration successful", "application submitted",
                                "your request has been sent", "we have received your",
                            )
                            error_markers = (
                                "this field is required", "this is a required question",
                                "this question is required", "please enter a valid",
                                "fix the following errors", "submission failed",
                                "invalid email address", "must be filled out",
                                "please correct the errors", "there was a problem",
                            )
                            success_marker = next((m for m in success_markers if m in observed), None)
                            error_marker = next((m for m in error_markers if m in observed), None)
                            origin_host = (urlparse(submit_origin_url).hostname or "").lower()
                            post_host = (urlparse(post_url).hostname or "").lower()
                            redirected_to_other_host = bool(origin_host and post_host and origin_host != post_host)

                            if post_submit_errors and not success_marker:
                                action_success = False
                                labels = ", ".join(str(item.get("label") or "Field") for item in post_submit_errors[:5])
                                exec_description += (
                                    f" | Submission verification: browser still reports invalid fields ({labels}). "
                                    "The form was not automatically resubmitted."
                                )
                            elif error_marker:
                                action_success = False
                                exec_description += (
                                    f" | Submission verification: validation/error message detected "
                                    f"('{error_marker}'). The form was not automatically resubmitted."
                                )
                            elif success_marker:
                                exec_description += (
                                    f" | Submission verified by visible confirmation "
                                    f"('{success_marker}')."
                                )
                            elif redirected_to_other_host:
                                action_success = False
                                exec_description += (
                                    f" | Submission outcome unverified: redirected from "
                                    f"{origin_host} to {post_host} without a visible confirmation. "
                                    "Inspect the destination; do not resubmit automatically."
                                )
                            else:
                                action_success = False
                                exec_description += (
                                    " | Submission outcome unverified: no recognizable confirmation "
                                    "or explicit validation result appeared. Inspect the page before "
                                    "taking another submission action."
                                )
                        except Exception as exc:
                            action_success = False
                            exec_description += (
                                f" | Submission outcome unverified because the result page "
                                f"could not be inspected ({type(exc).__name__}). Do not resubmit automatically."
                            )

                    if recovery_log:
                        events = self.recovery_diagnostics.setdefault(task_id, [])
                        for recovery_entry in recovery_log:
                            events.append({
                                "timestamp": time.time(),
                                "step": step_num,
                                "action": action.action.value,
                                "message": str(recovery_entry)[:1000],
                                "outcome": "recovered" if action_success else "unresolved",
                            })
                        self.recovery_diagnostics[task_id] = events[-100:]
                        for recovery_entry in recovery_log:
                            logger.info(
                                "Recovery diagnostic task=%s step=%s action=%s outcome=%s detail=%s",
                                task_id, step_num, action.action.value,
                                "recovered" if action_success else "unresolved",
                                str(recovery_entry)[:500],
                            )

                if is_upload_step:
                    try:
                        post_action_path, post_action_url = await controller.capture_screenshot(task_id, step_num)
                        if post_action_path:
                            screenshot_path, screenshot_url = post_action_path, post_action_url
                            exec_description += " | Captured post-upload state for report evidence."
                        else:
                            exec_description += " | Post-upload screenshot unavailable; retained pre-action evidence."
                    except Exception as screenshot_error:
                        logger.warning(
                            "Post-upload screenshot failed for task %s step %s: %s",
                            task_id, step_num, screenshot_error,
                        )
                        exec_description += " | Post-upload screenshot capture failed."

                if action_success:
                    if task.initial_plan:
                        if action.action == ActionType.FINISH:
                            # FINISH is accepted only after the planner's independent
                            # outcome verifier succeeds. This is the sole current
                            # completion boundary; ordinary action success is not
                            # evidence that a high-level milestone is complete.
                            metadata = task.plan_metadata if isinstance(task.plan_metadata, dict) else {}
                            graph = metadata.get("milestone_graph") or self._build_milestone_graph(task.initial_plan)
                            metadata["completed_milestone_ids"] = [node["id"] for node in graph]
                            task.plan_metadata = metadata
                            task.current_plan_step = max(0, len(task.initial_plan) - 1)
                        else:
                            # This index is a UI hint for the active milestone only.
                            # Never derive completed milestones from action keywords
                            # or the number of low-level browser steps.
                            active_plan_idx = planner.determine_active_plan_step(
                                initial_plan=task.initial_plan,
                                current_action=action,
                                trajectory_history=trajectory_history,
                                page_info=page_info,
                                step_number=step_num,
                                max_steps=req.max_steps,
                            )
                            task.current_plan_step = max(0, min(active_plan_idx, len(task.initial_plan) - 1))
                        self._refresh_milestone_graph(task)
                    consecutive_failures = 0
                    if recovery_log:
                        recovery_desc = "; ".join(recovery_log)
                        exec_description += f" [{recovery_desc}]"

                    # Track fields actively filled by the agent
                    if action.action in (
                        ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD,
                        ActionType.SELECT, ActionType.SELECT_OPTION,
                        ActionType.CHECK, ActionType.CHECK_BOX, ActionType.UNCHECK,
                        ActionType.RADIO_BUTTON, ActionType.UPLOAD, ActionType.UPLOAD_FILE
                    ):
                        field_key = action.selector or (f"Element #{action.element_id}" if action.element_id else (action.radio_name or "Input Field"))
                        label = None
                        for f in task.context.current_form_fields:
                            if (action.selector and getattr(f, "selector", None) == action.selector) or (action.element_id and getattr(f, "field_id", None) == str(action.element_id)):
                                label = getattr(f, "label", None) or getattr(f, "name", None) or getattr(f, "placeholder", None)
                                break
                        display_label = label or action.select_label or field_key
                        val = action.value or action.text or (action.upload_paths if action.upload_paths else "checked" if action.checked else "filled")
                        # Persist to structured context (sensitive values are masked inside record_fill)
                        task.context.record_fill(field_key, display_label, val)
                        # Keep backward-compat attribute for submit-gate logic
                        task._agent_filled_fields = {
                            k: v for k, v in task.context.filled_fields.items()
                        }

                    # 7. Record step
                    duration_ms = round((time.time() - step_start) * 1000, 1)
                    agent_step = AgentStep(
                        step_number=step_num,
                        action=action.action,
                        description=exec_description,
                        reasoning=action.reasoning,
                        screenshot_path=screenshot_path,
                        screenshot_url=screenshot_url,
                        url=controller.current_url,
                        extracted_data=extracted_item,
                        duration_ms=duration_ms,
                    )
                    task.steps.append(agent_step)
                    task.current_step = step_num
                    task.updated_at = time.time()
                    if isinstance(task.plan_metadata, dict):
                        checkpoint_url = (
                            getattr(controller, "current_url", "")
                            or page_info.get("url")
                            or ""
                        )
                        if checkpoint_url:
                            task.plan_metadata["execution_checkpoint"] = {
                                "url": checkpoint_url,
                                "step": step_num,
                                "saved_at": task.updated_at,
                            }

                    # Record to structured context memory
                    task.context.record_action(
                        step=step_num,
                        action=action.action.value,
                        description=exec_description,
                        reasoning=action.reasoning,
                        url=controller.current_url,
                        success=True,
                    )
                    if action.action == ActionType.EXTRACT and extracted_item and isinstance(extracted_item, dict):
                        task.context.record_extraction(extracted_item)
                    task.context.reset_failures()
                    if action.action not in (
                        ActionType.WAIT, ActionType.SCROLL, ActionType.SCROLL_TO,
                        ActionType.EXTRACT, ActionType.SCREENSHOT, ActionType.FINISH,
                        ActionType.REQUEST_APPROVAL, ActionType.HOVER,
                    ):
                        task.plan_metadata = task.plan_metadata if isinstance(task.plan_metadata, dict) else {}
                        task.plan_metadata["last_successful_action"] = {
                            "signature": self._action_fingerprint(action),
                            "dom_fingerprint": task.context.last_dom_fingerprint,
                            "url": getattr(controller, "current_url", "") or page_info.get("url") or "",
                            "step": step_num,
                            "verified": True,
                        }

                    trajectory_history.append({
                        "step": step_num,
                        "action": action.action.value,
                        "description": exec_description,
                        "reasoning": action.reasoning,
                        "url": getattr(controller, "current_url", "") or "",
                    })
                    await self.broadcast_task_update(task_id)
                    await self._save_cached_task_state(task_id)

                    if action.action == ActionType.FINISH:
                        finish_verified = True
                        break
                else:
                    consecutive_failures += 1
                    recovery_summary = "; ".join(recovery_log) if recovery_log else f"{policy.max_retries} attempts failed"
                    logger.warning(
                        f"Step {step_num}: Action {action.action.value} execution failed after {policy.max_retries} attempts ({exec_description})"
                    )
                    duration_ms = round((time.time() - step_start) * 1000, 1)
                    agent_step = AgentStep(
                        step_number=step_num,
                        action=action.action,
                        description=exec_description,
                        reasoning=f"{action.reasoning} | Error Recovery Attempts: {recovery_summary}",
                        screenshot_path=screenshot_path,
                        screenshot_url=screenshot_url,
                        url=controller.current_url,
                        error=f"Action execution failed after retries: {exec_description}",
                        duration_ms=duration_ms,
                    )
                    task.steps.append(agent_step)
                    task.current_step = step_num
                    task.updated_at = time.time()

                    # Record failure to structured context memory
                    task.context.record_action(
                        step=step_num,
                        action=action.action.value,
                        description=exec_description,
                        reasoning=action.reasoning,
                        url=controller.current_url,
                        success=False,
                        error=exec_description,
                    )
                    task.context.record_error(exec_description)
                    task.context.increment_failures()

                    trajectory_history.append({
                        "step": step_num,
                        "action": action.action.value,
                        "description": f"FAILED: {exec_description}",
                        "reasoning": f"{action.reasoning}. Error recovery needed: previous attempt failed after {policy.max_retries} retries/fallbacks ({recovery_summary}). Adapt by choosing an alternative element, scrolling to reveal content, or navigating.",
                        "url": getattr(controller, "current_url", "") or "",
                    })
                    await self.broadcast_task_update(task_id)
                    await self._save_cached_task_state(task_id)

                    # A failed FINISH means the requested outcome was not verified.
                    # Do not continue planning or mark this task completed afterward.
                    if any("Recovery paused:" in str(item) for item in recovery_log):
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = (
                            f"Automatic recovery stopped at step {step_num} because "
                            f"{action.action.value} may have caused a side effect. "
                            "The outcome is ambiguous; inspect the browser and task diagnostics "
                            "before manually retrying."
                        )
                        logger.warning("Task %s halted for manual recovery at step %s", task_id, step_num)
                        break

                    if action.action == ActionType.FINISH:
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        if not task.error:
                            task.error = exec_description or "Task outcome could not be verified."
                        break

                    # Replan only after a recoverable failure. Ambiguous side effects
                    # are stopped above and must never be automatically retried.
                    if consecutive_failures < 3:
                        metadata = task.plan_metadata if isinstance(task.plan_metadata, dict) else {}
                        replan_count = int(metadata.get("adaptive_replan_count", 0) or 0)
                        if replan_count < 2:
                            try:
                                live_page_info = await controller.parse_interactive_elements()
                                if not isinstance(live_page_info, dict):
                                    live_page_info = page_info
                            except Exception as refresh_error:
                                logger.warning(
                                    "Task %s could not refresh page before adaptive replanning: %s",
                                    task_id, refresh_error,
                                )
                                live_page_info = page_info

                            try:
                                revised_plan = await planner.replan_remaining_plan(
                                    goal=req.goal,
                                    current_plan=list(task.initial_plan or task.plan or []),
                                    completed_history=trajectory_history,
                                    page_info=live_page_info,
                                    recent_errors=list(getattr(task.context, "recent_errors", []) or []),
                                    task_context=task.context,
                                    progressed_milestones=[
                                        node.get("title", "")
                                        for node in (task.plan_metadata or {}).get("milestone_graph", [])
                                        if node.get("id") in set((task.plan_metadata or {}).get("completed_milestone_ids", []))
                                    ],
                                    active_milestone_index=int(task.current_plan_step or 0),
                                )
                                if revised_plan:
                                    metadata.setdefault("original_plan", list(task.initial_plan or []))
                                    versions = metadata.setdefault("plan_versions", [])
                                    versions.append({
                                        "version": replan_count + 2,
                                        "reason": "recoverable_action_failure",
                                        "failed_step": step_num,
                                        "failed_action": action.action.value,
                                        "created_at": time.time(),
                                        "milestones": list(revised_plan),
                                    })
                                    metadata["adaptive_replan_count"] = replan_count + 1
                                    metadata["plan_version"] = replan_count + 2
                                    metadata["replan_reason"] = "recoverable_action_failure"
                                    metadata["plan"] = list(revised_plan)
                                    task.plan_metadata = metadata
                                    # Preserve only milestones with explicit completion
                                    # evidence. An active/keyword-matched milestone is not
                                    # considered complete merely because an action succeeded.
                                    completed_ids = set(metadata.get("completed_milestone_ids") or [])
                                    old_graph = metadata.get("milestone_graph") or self._build_milestone_graph(task.initial_plan or [])
                                    preserved_prefix = [
                                        node.get("title", "")
                                        for node in old_graph
                                        if node.get("id") in completed_ids and node.get("title")
                                    ]
                                    metadata["remaining_plan"] = list(revised_plan)
                                    combined_plan = preserved_prefix + list(revised_plan)
                                    metadata["milestone_graph"] = self._build_milestone_graph(combined_plan)
                                    metadata["completed_milestone_ids"] = [
                                        f"m{index + 1}" for index in range(len(preserved_prefix))
                                    ]
                                    task.initial_plan = combined_plan
                                    task.plan = list(combined_plan)
                                    task.current_plan_step = min(len(preserved_prefix), max(0, len(combined_plan) - 1))
                                    self._refresh_milestone_graph(task)
                                    metadata["plan"] = list(task.initial_plan)
                                    metadata["initial_plan"] = list(task.initial_plan)
                                    task.current_plan_step = max(0, len(preserved_prefix) - 1)
                                    task.plan_status = "executing"
                                    task.updated_at = time.time()
                                    logger.info(
                                        "Task %s adaptively replanned to version %s after failed step %s.",
                                        task_id, replan_count + 2, step_num,
                                    )
                                    await self.broadcast_task_update(task_id)
                                    await self._save_cached_task_state(task_id)
                            except Exception as replan_error:
                                # Keep the previous plan and continue with the normal
                                # per-step planner; replanning is an enhancement, not
                                # a reason to discard a recoverable task.
                                logger.warning(
                                    "Adaptive replanning failed for task %s; retaining existing plan: %s",
                                    task_id, replan_error,
                                )

                    if consecutive_failures >= 3:
                        logger.error(
                            f"Task {task_id} stopping after {consecutive_failures} consecutive unrecoverable step failures."
                        )
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = f"Agent stopped after {consecutive_failures} consecutive unrecoverable step failures: {exec_description}"
                        break
                    else:
                        logger.info(
                            f"Task {task_id} continuing to next step (consecutive failures: {consecutive_failures}/3) to allow autonomous planner recovery."
                        )

                step_num += 1
                if await self._sleep_or_cancel(task_id, 2.5):
                    logger.info(f"Task {task_id} cancellation detected during inter-step sleep.")
                    break

            if hasattr(controller, "unlock_ui"):
                await controller.unlock_ui()

            # A task is successful only when the planner's FINISH action has
            # independently verified the requested outcome. Reaching max_steps,
            # closing a loop, or otherwise leaving the loop is not success.
            if task.status == TaskStatus.RUNNING and finish_verified:
                task.status = TaskStatus.COMPLETED
                task.plan_status = "done"
                task.current_plan_step = len(task.initial_plan)
                self._refresh_milestone_graph(task)
                if isinstance(task.plan_metadata, dict):
                    for milestone in task.plan_metadata.get("milestone_graph", []):
                        milestone["status"] = "completed"
            elif task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.FAILED
                task.plan_status = "failed"
                task.error = (
                    task.error
                    or f"Task reached its {req.max_steps}-step limit without verifying the requested outcome."
                )
                logger.warning("Task %s exhausted its step budget without verified completion.", task_id)
            elif task.status == TaskStatus.CANCELLED:
                task.plan_status = "cancelled"

        except asyncio.CancelledError:
            logger.warning(f"Task {task_id} received asyncio cancellation.")
            task.status = TaskStatus.CANCELLED
            task.plan_status = "cancelled"
            if not task.error:
                task.error = "Task execution was cancelled."
            if not any("cancelled" in (s.description or "").lower() for s in task.steps):
                task.steps.append(AgentStep(
                    step_number=len(task.steps) + 1,
                    action=ActionType.FINISH,
                    description=f"Task cancelled: {task.error}",
                ))
        except Exception as exc:
            err_msg = str(exc) if str(exc).strip() else repr(exc)

            # Closed-tab errors are user-initiated cancellations, not failures
            tab_closed_phrases = (
                "no chrome tab registered",
                "chrome tab for task",
            )
            extension_disconnected = (
                use_extension
                and task_id in controller.manager.disconnected_tasks
            )
            if extension_disconnected:
                logger.error(f"Task {task_id} failed after Chrome extension disconnect: {err_msg}")
                task.status = TaskStatus.FAILED
                task.plan_status = "failed"
                task.error = (
                    "Chrome Extension disconnected during execution. "
                    "Reconnect the extension and start a new task; the previous "
                    "browser session cannot be safely resumed."
                )
            elif any(p in err_msg.lower() for p in tab_closed_phrases):
                logger.warning(f"Task {task_id} cancelled: {err_msg}")
                task.status = TaskStatus.CANCELLED
                task.plan_status = "cancelled"
                task.error = "Chrome tab was closed by the user."
            else:
                logger.error(f"Task {task_id} execution error: {err_msg}", exc_info=True)
                task.status = TaskStatus.FAILED
                task.plan_status = "failed"
                task.error = err_msg
                task.steps.append(AgentStep(
                    step_number=len(task.steps) + 1,
                    action=ActionType.ERROR,
                    description=f"Task Execution Error: {err_msg}",
                    error=err_msg,
                ))
        finally:
            self.cancel_events.pop(task_id, None)
            self.pause_events.pop(task_id, None)
            self.running_tasks.pop(task_id, None)
            # Always release the browser UI lock, including exceptions and
            # asyncio cancellation paths. Extension adapters make this best-effort.
            try:
                unlock = getattr(controller, "unlock_ui", None)
                if callable(unlock):
                    await asyncio.shield(unlock())
            except Exception as unlock_exc:
                logger.warning("Could not unlock browser UI for task %s: %s", task_id, unlock_exc)
            try:
                await asyncio.shield(controller.stop())
            except Exception:
                pass
            self.controllers.pop(task_id, None)
            task.updated_at = time.time()
            # For terminal tasks, wait until the report URL is attached before broadcasting.
            # The frontend closes the WebSocket on terminal status, so broadcasting here first
            # can make it miss the later update that contains the generated PDF link.
            terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            if task.status not in terminal:
                try:
                    await asyncio.shield(self.broadcast_task_update(task_id))
                except Exception:
                    pass

            # Persist completed / failed / cancelled tasks to disk and invalidate temporary task cache
            if task.status in terminal:
                try:
                    await asyncio.shield(self._invalidate_cached_task_state(task_id))
                except Exception:
                    pass

                # Generate PDF report and keep it in memory
                try:
                    pdf_bytes = self.report_generator.generate_pdf_report(task_id, task)
                    self._report_store[task_id] = pdf_bytes
                    if not task.reports:
                        task.reports = {}
                    task.reports["pdf"] = f"/api/reports/{task_id}/download"
                except Exception as e:
                    logger.error(f"Failed to generate PDF report for task {task_id}: {e}")
                finally:
                    # Keep useful task memory visible in the dashboard and archived history.
                    # Only clear page-specific state that should not survive execution.
                    task.context.clear_transient()

                # Record completed task in in-memory history
                self._record_terminal_task(task)
                
                # Broadcast one last time to push the report URL
                try:
                    await asyncio.shield(self.broadcast_task_update(task_id))
                except Exception:
                    pass

                # Clean up screenshots for this task — they're already embedded in the
                # UI at this point and no longer needed on disk
                screenshots_dir = "screenshots"
                if os.path.isdir(screenshots_dir):
                    prefix = f"{task_id}_step_"
                    for fname in os.listdir(screenshots_dir):
                        if fname.startswith(prefix):
                            try:
                                os.remove(os.path.join(screenshots_dir, fname))
                            except OSError:
                                pass

                # Terminal tasks live in the bounded archive/history, not the active registry.
                self.tasks.pop(task_id, None)
            try:
                from backend.security import clear_task_vault
                clear_task_vault(task_id)
            except Exception:
                pass

    def _heal_extension_action_target(
        self,
        action: BrowserAction,
        previous_page: Optional[Dict[str, Any]],
        current_page: Optional[Dict[str, Any]],
    ) -> tuple[bool, str]:
        """Rebind a stale element index/selector only when one semantic match exists."""
        previous_elements = (previous_page or {}).get("elements") or []
        current_elements = (current_page or {}).get("elements") or []
        old_target = next((
            item for item in previous_elements
            if isinstance(item, dict) and (
                (action.element_id is not None and item.get("index") == action.element_id)
                or (action.selector and item.get("selector") == action.selector)
            )
        ), None)
        if not isinstance(old_target, dict):
            return False, "Locator recovery skipped: the previous target metadata is unavailable."

        identity_fields = ("aria_label", "label", "placeholder", "name", "text")
        def norm(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

        identities = {
            key: norm(old_target.get(key))
            for key in identity_fields
            if norm(old_target.get(key))
        }
        tag = norm(old_target.get("tag"))
        control_type = norm(old_target.get("type"))
        if not identities:
            return False, "Locator recovery skipped: the previous target has no stable semantic label."

        candidates = []
        for item in current_elements:
            if not isinstance(item, dict) or item.get("visible") is False or item.get("disabled") is True:
                continue
            if tag and norm(item.get("tag")) != tag:
                continue
            if control_type and norm(item.get("type")) != control_type:
                continue
            if any(norm(item.get(key)) == value for key, value in identities.items()):
                candidates.append(item)

        if len(candidates) != 1:
            return False, (
                "Locator recovery was ambiguous: "
                f"found {len(candidates)} matching visible controls; no action was issued."
            )

        replacement = candidates[0]
        action.element_id = replacement.get("index")
        action.selector = replacement.get("selector") or None
        return True, (
            "Recovered stale locator using a unique live-DOM semantic match "
            f"(element index {action.element_id})."
        )

    def _verify_extension_action_precondition(
        self,
        action: BrowserAction,
        page_info: Optional[Dict[str, Any]],
    ) -> tuple[bool, str]:
        """Check that a targeted extension control exists and is actionable."""
        target_actions = {
            ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.TYPE_TEXT,
            ActionType.FILL, ActionType.FILL_FIELD, ActionType.SELECT,
            ActionType.SELECT_OPTION, ActionType.CHECK, ActionType.CHECK_BOX,
            ActionType.UNCHECK, ActionType.RADIO_BUTTON, ActionType.UPLOAD,
            ActionType.UPLOAD_FILE,
        }
        if action.action not in target_actions:
            return True, "No element-target precondition required."
        elements = (page_info or {}).get("elements") or []
        if action.element_id is None and not action.selector:
            # Some extension actions intentionally target a semantic group rather
            # than one indexed control (radio groups are addressed by name).
            if action.action == ActionType.RADIO_BUTTON and action.radio_name:
                group = [
                    item for item in elements
                    if isinstance(item, dict)
                    and str(item.get("name") or "") == str(action.radio_name)
                ]
                if group:
                    return True, "Radio group precondition verified."
                return False, "Action precondition failed: radio group is no longer present."
            # Text-only clicks are supported by the extension's fallback resolver.
            if action.action in (ActionType.CLICK, ActionType.DOUBLE_CLICK) and action.text:
                return True, "Text-targeted interaction will use extension resolution."
            return False, "Action precondition failed: no element index or selector was supplied."

        target = next((item for item in elements if isinstance(item, dict) and (
            (action.element_id is not None and item.get("index") == action.element_id)
            or (action.selector and item.get("selector") == action.selector)
        )), None)
        if target is None:
            reasoning = str(action.reasoning or "")
            action_text = str(action.text or "")
            # Rebind dynamic upload controls from live page text instead of
            # rejecting a stale index before the extension can inspect the DOM.
            if action.action == ActionType.CLICK and re.search(
                r"add\s+file|choose\s+file|browse|upload\s+file|attach\s+file",
                f"{action_text} {reasoning}", re.IGNORECASE
            ):
                return True, "Stale upload locator; live semantic resolution will be used."
            if action.action in (ActionType.FILL, ActionType.FILL_FIELD, ActionType.TYPE_TEXT, ActionType.SELECT, ActionType.SELECT_OPTION):
                label_match = re.search(r"""['"“]([^'"”]+)['"”]\s+(?:field|question|input)""", reasoning, re.IGNORECASE)
                if label_match:
                    wanted = label_match.group(1).strip().lower()
                    fields = (page_info or {}).get("form_fields") or []
                    matches = [
                        field for field in fields
                        if wanted in " ".join(str(field.get(key) or "") for key in (
                            "label", "aria_label", "placeholder", "name", "id"
                        )).lower()
                    ]
                    if len(matches) == 1:
                        return True, f"Stale locator; unique field label '{wanted}' will be resolved live."
            return False, "Action precondition failed: target control is no longer present in the latest page snapshot."
        if target.get("visible") is False:
            return False, "Action precondition failed: target control is not visible."
        if target.get("disabled") is True:
            return False, "Action precondition failed: target control is disabled."
        return True, "Target control preconditions verified."

    async def _verify_extension_action_postcondition(
        self,
        action: BrowserAction,
        controller: Any,
        before_page: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """
        Verify observable postconditions for extension-executed actions.
        Returns (verified, explanation). Unsupported/ambiguous actions are
        reported as inconclusive rather than being falsely marked as failures.
        """
        action_type = action.action
        try:
            after = await controller.parse_interactive_elements()
        except Exception as exc:
            return False, f"Postcondition could not be inspected ({type(exc).__name__})."

        before = before_page or {}
        before_url = str(before.get("url") or getattr(controller, "current_url", "") or "")
        after_url = str(after.get("url") or getattr(controller, "current_url", "") or "")

        if action_type == ActionType.NAVIGATE:
            target = str(action.url or action.text or "").strip()
            actual = after_url.strip()
            if target and actual:
                expected_parts = urlparse(target if "://" in target else f"https://{target}")
                actual_parts = urlparse(actual)
                expected_path = (expected_parts.path or "/").rstrip("/") or "/"
                actual_path = (actual_parts.path or "/").rstrip("/") or "/"
                same_destination = (
                    (expected_parts.hostname or "").lower() == (actual_parts.hostname or "").lower()
                    and expected_path == actual_path
                )
                if same_destination:
                    return True, f"Navigation verified at {after_url}."
            return False, f"Navigation postcondition not met (expected {target or 'target URL'}, observed {after_url or 'unknown URL'})."

        target_selector = str(action.selector or "")
        target_id = action.element_id
        def field_matches(field: Any) -> bool:
            get = field.get if isinstance(field, dict) else lambda key, default=None: getattr(field, key, default)
            return bool(
                (target_selector and get("selector") == target_selector)
                or (target_id is not None and str(get("field_id") or get("index") or "") == str(target_id))
            )

        fields = after.get("form_fields") or []
        # Element indexes belong to the interactive-elements snapshot, while
        # FormField.field_id is the HTML id. Resolve index -> selector first.
        target_selector_from_index = None
        if target_id is not None and not target_selector:
            indexed_element = next((
                item for item in (after.get("elements") or [])
                if isinstance(item, dict) and item.get("index") == target_id
            ), None)
            if indexed_element:
                target_selector_from_index = indexed_element.get("selector")
        target_field = next((field for field in fields if field_matches(field)), None)
        if target_field is None and target_selector_from_index:
            target_field = next((
                field for field in fields
                if (field.get("selector") if isinstance(field, dict) else getattr(field, "selector", None))
                == target_selector_from_index
            ), None)
        if action_type in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD):
            # The extension may safely resolve a Google Forms field by its
            # question title even when a prior numeric element index is stale.
            # Verify against that same semantic label, not the old index.
            raw_label = str(
                getattr(action, "field_label", None)
                or getattr(action, "label", None)
                or ""
            ).strip().lower()
            if not raw_label:
                reasoning = str(getattr(action, "reasoning", None) or "")
                quoted = re.search(r"""['"“]([^'"”]+)['"”]\s+(?:field|question|input)""", reasoning, re.I)
                described = re.search(
                    r"""(?:fill|enter|type|populate|update)\s+(?:in\s+)?(?:the\s+)?["'“]?([^"'”]+?)["'”]?\s+(?:field|question|input)\b""",
                    reasoning, re.I
                )
                raw_label = str((quoted.group(1) if quoted else "") or (described.group(1) if described else "")).strip().lower()
            if raw_label:
                normalize_label = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
                semantic_matches = [
                    field for field in fields
                    if normalize_label(
                        field.get("label", "") if isinstance(field, dict) else getattr(field, "label", "")
                    ) == normalize_label(raw_label)
                ]
                if len(semantic_matches) == 1:
                    target_field = semantic_matches[0]
            expected = str(action.value if action.value is not None else action.text or "")
            if target_field is not None:
                # FormField is a Pydantic model, not a dict. Keep access
                # consistent for both raw extension dictionaries and models.
                get = (
                    target_field.get
                    if isinstance(target_field, dict)
                    else lambda key, default=None: getattr(target_field, key, default)
                )
                actual = str(get("current_value", "") or "")
                if actual == expected:
                    return True, "Text input value verified."
                return False, "Text input postcondition failed: the field value does not match the requested value."
            return False, "Text input postcondition inconclusive: the target field was not found after execution."

        if action_type in (ActionType.SELECT, ActionType.SELECT_OPTION):
            expected = str(action.select_label or action.value or action.text or "").strip().lower()
            if target_field is not None:
                get = target_field.get if isinstance(target_field, dict) else lambda key, default=None: getattr(target_field, key, default)
                actual = str(get("current_value", "") or "").strip().lower()
                if actual and (actual == expected or expected in actual):
                    return True, "Selected option verified."
                return False, "Select postcondition failed: the selected value does not match the requested option."
            return False, "Select postcondition inconclusive: the target control was not found."

        if action_type in (ActionType.CHECK, ActionType.CHECK_BOX, ActionType.UNCHECK):
            elements = after.get("elements") or []
            target = next((item for item in elements if isinstance(item, dict) and (
                (target_id is not None and item.get("index") == target_id)
                or (target_selector and item.get("selector") == target_selector)
            )), None)
            if target is not None and "checked" in target:
                expected = action.checked if action.checked is not None else action_type != ActionType.UNCHECK
                if bool(target.get("checked")) == bool(expected):
                    return True, "Checkbox state verified."
                return False, "Checkbox postcondition failed: checked state does not match."
            return False, "Checkbox postcondition inconclusive: checked state was not exposed by the extension."

        if action_type == ActionType.RADIO_BUTTON:
            elements = after.get("elements") or []
            group = [
                item for item in elements
                if isinstance(item, dict)
                and str(item.get("name") or "") == str(action.radio_name or "")
            ]
            if group:
                expected_value = str(action.value or action.text or "").strip()
                selected = next((item for item in group if item.get("checked") is True), None)
                if selected is not None:
                    selected_value = str(selected.get("value") or selected.get("text") or "").strip()
                    if not expected_value or selected_value == expected_value:
                        return True, "Radio selection verified."
                    return False, "Radio postcondition failed: a different option is selected."
            return False, "Radio postcondition inconclusive: selected state was not exposed by the extension."

        if action_type in (ActionType.CLICK, ActionType.DOUBLE_CLICK):
            before_elements = before.get("elements") or []
            after_elements = after.get("elements") or []
            target_label = str(action.text or "").strip().lower()
            before_target = next((item for item in before_elements if isinstance(item, dict) and (
                (target_id is not None and item.get("index") == target_id)
                or (target_selector and item.get("selector") == target_selector)
                or (target_label and target_label in str(item.get("text") or item.get("aria_label") or "").lower())
            )), None)
            after_target = next((item for item in after_elements if isinstance(item, dict) and (
                (target_id is not None and item.get("index") == target_id)
                or (target_selector and item.get("selector") == target_selector)
                or (target_label and target_label in str(item.get("text") or item.get("aria_label") or "").lower())
            )), None)
            changed = after_url != before_url or (
                bool(before_target) and after_target is None
            )
            if changed:
                return True, "Click produced an observable page or target-state change."
            # An accepted click is not proof that its intended effect occurred.
            # In particular, do not let an unchanged Submit button count as success.
            return False, "Click outcome is unverified: the page and target state did not observably change."

        if action_type in (ActionType.UPLOAD, ActionType.UPLOAD_FILE):
            expected_names = []
            if getattr(action, "file_name", None):
                expected_names.append(str(action.file_name).replace("\\", "/").split("/")[-1].lower())
            for path in (getattr(action, "upload_paths", None) or []):
                expected_names.append(str(path).replace("\\", "/").split("/")[-1].lower())
            if target_field is not None:
                get = target_field.get if isinstance(target_field, dict) else lambda key, default=None: getattr(target_field, key, default)
                actual = str(get("current_value", "") or "").replace("\\", "/").split("/")[-1].lower()
                if actual and (not expected_names or any(name in actual for name in expected_names)):
                    return True, "Uploaded filename is visible in the target file input."
                return False, "Upload postcondition failed: the expected filename is not present in the target file input."
            return False, "Upload postcondition inconclusive: the target file input was not found in the latest page snapshot."

        # Actions such as scroll, hover, wait and keyboard shortcuts need
        # action-specific evidence that the extension does not consistently expose.
        return True, f"{action_type.value} accepted; no deterministic extension postcondition is available."

    async def _verify_goal_completion(
        self,
        controller,
        task: TaskResponse,
        planner: AIAgentPlanner,
        trajectory_history: List[Dict[str, Any]],
    ) -> tuple[bool, str]:
        """
        General-purpose outcome verification before accepting FINISH.
        Combines model-based verification when available with deterministic
        evidence-based verification as a reliable fallback when the model
        is unavailable, times out, or fails.
        """
        try:
            page_info = await controller.parse_interactive_elements()
        except Exception as exc:
            logger.warning("Could not inspect final page for outcome verification: %s", exc)
            page_info = {
                "url": getattr(controller, "current_url", "") or "",
                "title": "",
                "page_text": "",
                "headings": [],
                "elements": [],
            }

        # Keep the verifier input bounded; page text and elements can be very large.
        elements = []
        for item in (page_info.get("elements") or [])[:100]:
            if isinstance(item, dict):
                elements.append({
                    key: item.get(key)
                    for key in ("tag", "text", "aria_label", "role", "type", "value", "href", "disabled")
                    if item.get(key) is not None
                })

        extracted_summary = []
        for d in (task.extracted_data or []):
            if isinstance(d, dict):
                summary_dict = {k: v for k, v in d.items() if k in ("title", "url", "source_url", "name", "price", "status")}
                if "headings" in d and isinstance(d["headings"], list):
                    summary_dict["headings_count"] = len(d["headings"])
                if "paragraphs" in d and isinstance(d["paragraphs"], list):
                    summary_dict["paragraphs_count"] = len(d["paragraphs"])
                extracted_summary.append(summary_dict)
            elif isinstance(d, list):
                extracted_summary.append(f"List of {len(d)} items")
            else:
                extracted_summary.append(str(d)[:100])

        evidence = {
            "goal": task.goal,
            "final_page": {
                "url": page_info.get("url") or getattr(controller, "current_url", ""),
                "title": page_info.get("title", ""),
                "visible_text": str(page_info.get("page_text", ""))[:7000],
                "headings": (page_info.get("headings") or [])[:40],
                "interactive_elements": elements,
            },
            "extracted_data": extracted_summary,
            "recent_actions": trajectory_history[-12:],
        }

        model_unavailable_reason = None

        # ------------------------------------------------------------------
        # Primary: Model-based verification (if available and responsive)
        # ------------------------------------------------------------------
        if getattr(planner, "client", None):
            prompt = f"""
You are a strict, independent browser-task outcome verifier. Decide whether the user's original goal was actually achieved, not whether the agent merely attempted its actions.

Return ONLY valid JSON:
{{
  "completed": true or false,
  "confidence": "high" or "medium" or "low",
  "evidence": "short quote or concrete observed state supporting the decision",
  "reason": "brief explanation"
}}

Rules:
- Use only the supplied final browser state, extracted data, and action history as evidence.
- A click, typing, navigation, form fill, agent claim, or planned FINISH step is NOT by itself proof of success.
- Require observable evidence of the requested end state (for example: a success/confirmation message, the requested item appearing in the destination, a changed state, or results matching the goal).
- If the evidence is missing, ambiguous, contradictory, or the page still shows an unfinished form/draft/dialog, set completed=false.
- Do not infer success from the absence of an error.
- For multi-part goals, every important requested outcome must have evidence.
- Be conservative: only completed=true with high confidence and clear evidence.

Task evidence:
{json.dumps(evidence, ensure_ascii=False, default=str)}
"""
            try:
                # Bounded timeout of 12.0s to avoid hanging if model provider is slow
                raw = await asyncio.wait_for(
                    asyncio.to_thread(planner._call_llm, prompt, "application/json"),
                    timeout=12.0,
                )
                if raw.startswith("```"):
                    raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```"))
                verdict = json.loads(raw)
                if isinstance(verdict, dict):
                    completed = verdict.get("completed") is True and str(verdict.get("confidence", "")).lower() == "high"
                    evidence_text = str(verdict.get("evidence") or "").strip()
                    reason = str(verdict.get("reason") or "The requested outcome was not clearly verified.").strip()
                    if completed and evidence_text:
                        return True, f"Outcome verified: {evidence_text[:300]}"
                    elif verdict.get("completed") is False:
                        message = f"Task outcome not verified: {reason}"
                        if evidence_text:
                            message += f" Evidence: {evidence_text[:300]}"
                        return False, message
            except asyncio.TimeoutError:
                logger.warning("Verification model timed out after 12s, engaging deterministic fallback.")
                model_unavailable_reason = "The verification model timed out"
            except Exception as exc:
                logger.warning(
                    "Final task verification model failed (%s): %s, engaging deterministic fallback.",
                    type(exc).__name__,
                    exc,
                )
                model_unavailable_reason = f"The verification model is unavailable ({type(exc).__name__})"
        else:
            model_unavailable_reason = "The verification model is unavailable"

        # ------------------------------------------------------------------
        # Fallback: Deterministic Evidence-Based Verification
        # ------------------------------------------------------------------
        return self._verify_outcome_deterministic(
            task=task,
            controller=controller,
            page_info=page_info,
            trajectory_history=trajectory_history,
            model_reason=model_unavailable_reason,
        )

    def _verify_outcome_deterministic(
        self,
        task: TaskResponse,
        controller: Any,
        page_info: Dict[str, Any],
        trajectory_history: List[Dict[str, Any]],
        model_reason: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Deterministic, task-appropriate outcome verification when the verification
        model is unavailable, times out, or fails.
        """
        goal = (task.goal or "").strip()
        goal_lower = goal.lower()
        curr_url = str(page_info.get("url") or getattr(controller, "current_url", "") or "").strip()
        page_title = str(page_info.get("title") or "").strip()
        visible_text = str(page_info.get("page_text") or "").strip()
        visible_text_lower = visible_text.lower()
        headings = [str(h).strip() for h in (page_info.get("headings") or []) if h]
        headings_lower = [h.lower() for h in headings]

        # 1. Check for genuine browser execution failure
        if curr_url and ("chrome-error://" in curr_url or "dns_probe_finished" in curr_url.lower() or "err_connection_refused" in curr_url.lower()):
            return False, f"Task execution failed: Browser reached error page ({curr_url})."

        for err_kw in ("404 not found", "500 internal server error", "502 bad gateway", "503 service unavailable"):
            if err_kw in page_title.lower():
                return False, f"Task execution failed: Page returned error status '{page_title}' at {curr_url}."

        non_finish_steps = [s for s in task.steps if s.action != ActionType.FINISH]
        if non_finish_steps and all(bool(s.error) for s in non_finish_steps):
            first_err = non_finish_steps[0].error or non_finish_steps[0].description
            return False, f"Task execution failed: All browser actions encountered errors ({first_err})."

        # 2. Check Multi-Website Research
        if task.task_id.startswith("research_") or (("research" in goal_lower) and ("sources" in goal_lower or "compare" in goal_lower)):
            try:
                from services.research_service import research_service
                comp = research_service.get_results(task.task_id)
                if comp and len(comp.records) > 0:
                    n_recs = len(comp.records)
                    n_srcs = comp.summary.get("successful_sources", len(comp.sources))
                    return True, f"Outcome verified: Multi-website research completed with {n_recs} records extracted across {n_srcs} source(s)."
                elif comp and comp.summary.get("successful_sources", 0) == 0 and comp.summary.get("partial_sources", 0) == 0 and len(comp.sources) > 0:
                    return False, "Multi-website research failed: All target sources failed to load or extract."
            except Exception:
                pass
            if isinstance(task.extracted_data, list) and len(task.extracted_data) > 0:
                return True, f"Outcome verified: Multi-website research completed with {len(task.extracted_data)} extracted record(s)."

        # 3. Check Data Extraction / Summary Tasks
        is_extraction_goal = any(kw in goal_lower for kw in (
            "extract", "scrape", "collect", "gather", "summarize", "summary",
            "find headlines", "get prices", "list of", "read article", "headlines", "quotes"
        ))
        has_extract_step = any(s.action == ActionType.EXTRACT for s in task.steps)

        extracted_items = task.extracted_data or []
        if not extracted_items and hasattr(task, "context"):
            extracted_items = getattr(task.context, "extracted_data", []) or getattr(task.context, "extracted_snippets", [])

        substantive_data = False
        detail_parts = []
        if extracted_items:
            total_h = 0
            total_p = 0
            rec_count = 0
            text_len = 0
            for it in (extracted_items if isinstance(extracted_items, list) else [extracted_items]):
                if isinstance(it, dict):
                    rec_count += 1
                    h = it.get("headings", [])
                    p = it.get("paragraphs", [])
                    if isinstance(h, list):
                        total_h += len(h)
                    if isinstance(p, list):
                        total_p += len(p)
                    txt = str(it.get("text") or it.get("content") or it.get("summary") or "")
                    text_len += len(txt)
                    non_meta = [k for k in it if k not in ("headings", "paragraphs", "title", "url", "status") and it[k]]
                    if non_meta:
                        substantive_data = True
                elif isinstance(it, list):
                    rec_count += len(it)
                    if len(it) > 0:
                        substantive_data = True
                elif isinstance(it, str) and len(it.strip()) > 20:
                    text_len += len(it.strip())
                    substantive_data = True

            if total_h > 0 or total_p > 0 or text_len > 40 or substantive_data or rec_count > 0:
                substantive_data = True
                if total_h > 0:
                    detail_parts.append(f"{total_h} headings")
                if total_p > 0:
                    detail_parts.append(f"{total_p} paragraphs")
                if rec_count > 1:
                    detail_parts.append(f"{rec_count} records")
                if text_len > 0:
                    detail_parts.append(f"{text_len} chars text")

        if is_extraction_goal or has_extract_step:
            if substantive_data:
                summary_str = ", ".join(detail_parts) if detail_parts else "data records"
                return True, f"Outcome verified: Successfully extracted {summary_str} from {curr_url}."
            elif is_extraction_goal:
                return False, f"Extraction failed: No content or records were extracted for '{task.goal}'."

        # 4. Check Report Generation Goals
        is_report_goal = any(kw in goal_lower for kw in ("report", "pdf", "generate pdf", "excel", "xlsx", "csv"))
        pdf_bytes = self._report_store.get(task.task_id) or task_archive.get_report(task.task_id)
        has_report_artifact = bool(pdf_bytes and len(pdf_bytes) > 20)
        has_report_link = bool(task.reports and any(k in task.reports for k in ("pdf", "xlsx", "csv")))

        if is_report_goal:
            if has_report_artifact or has_report_link:
                sz = f"{len(pdf_bytes)} bytes" if pdf_bytes else "download link ready"
                return True, f"Outcome verified: Generated valid report artifact ({sz})."
            elif len(goal_lower.split()) <= 6:
                return False, f"Report generation failed: No report artifact was generated for '{task.goal}'."

        # 5. Check Navigation Goals
        target_url = extract_target_url(task.goal)
        if target_url:
            is_domain_match = domains_match(target_url, curr_url)
            has_content = bool(page_title or headings or len(visible_text) > 50)

            parsed_target = urlparse(target_url if "://" in target_url else "https://" + target_url)
            target_path = parsed_target.path.rstrip("/")
            curr_path = urlparse(curr_url).path.rstrip("/")
            path_matches = (not target_path or target_path in curr_path or target_path == "/")

            if is_domain_match and path_matches and has_content and not ("chrome-error://" in curr_url or curr_url == "about:blank"):
                goal_words = set(re.findall(r"\w+", goal_lower))
                interactive_words = {"fill", "submit", "register", "buy", "pay", "order", "purchase", "upload"}
                if not (goal_words & interactive_words):
                    return True, f"Outcome verified: Successfully navigated to {curr_url} (matching {target_url}) with page title '{page_title}'."
            elif not is_domain_match and curr_url and curr_url not in ("about:blank", ""):
                return False, f"Navigation failed: Browser is at {curr_url}, which does not match requested target {target_url}."

        # 6. Check Form Filling and Submission Goals
        is_form_goal = any(kw in goal_lower for kw in ("fill", "submit", "apply", "register", "sign up", "book", "send"))
        if is_form_goal:
            CONFIRMATION_PATTERNS = [
                "thank you", "successfully submitted", "form submitted", "submission received",
                "response has been recorded", "order confirmed", "confirmation number",
                "message sent", "registration successful", "application submitted",
                "your request has been sent", "we have received your"
            ]
            ERROR_PATTERNS = [
                "this field is required", "please enter a valid", "fix the following errors",
                "submission failed", "invalid email address", "must be filled out"
            ]

            confirmed_msg = next((p for p in CONFIRMATION_PATTERNS if p in visible_text_lower or any(p in h for h in headings_lower)), None)
            error_msg = next((p for p in ERROR_PATTERNS if p in visible_text_lower or any(p in h for h in headings_lower)), None)

            if confirmed_msg:
                return True, f"Outcome verified: Observable confirmation message found on page ('{confirmed_msg}')."
            elif error_msg:
                return False, f"Form submission failed: Validation error detected on page ('{error_msg}')."

            filled_fields = getattr(task.context, "filled_fields", {}) if hasattr(task, "context") else {}
            has_submit_click = any(s.action in (ActionType.CLICK, ActionType.SUBMIT) for s in task.steps)
            if filled_fields and has_submit_click:
                return False, f"Outcome unverified: {model_reason or 'The verification model is unavailable'}. Form was filled and submitted, but no explicit confirmation message was observable on {curr_url}."

        # 7. Unverified Outcome / Insufficient Evidence Fallback
        reason_prefix = model_reason or "The verification model is unavailable"
        return False, f"Outcome unverified: {reason_prefix}, and available deterministic evidence is insufficient to confirm whether '{task.goal}' was achieved. Final page state: URL '{curr_url}', title '{page_title}'."

    # ------------------------------------------------------------------
    # Action dispatcher
    # ------------------------------------------------------------------

    async def _execute_action(
        self,
        action,
        controller,
        task: TaskResponse,
        task_id: str,
        planner: AIAgentPlanner,
        trajectory_history: List[Dict[str, Any]],
    ):
        """
        Dispatch a BrowserAction to the appropriate controller / sub-module method.

        Returns:
            (exec_description: str, extracted_item: Any, success: bool)
        """
        if self._is_cancelled(task_id) or getattr(controller, "is_running", True) is False:
            return "Task execution was cancelled by user.", None, False
        if isinstance(controller, ExtensionBrowserAdapter) or (hasattr(controller, "execute_action") and not getattr(controller, "current_page", None)):
            form_fields = getattr(task, "_current_form_fields", None)
            try:
                # Substitute vault placeholders only at actual browser execution time
                from backend.security import get_task_vault
                vault = get_task_vault(task_id, calling_task_id=task_id)
                exec_action = action.model_copy()
                if exec_action.text:
                    exec_action.text = vault.substitute_placeholders(exec_action.text, calling_task_id=task_id)
                if exec_action.value:
                    exec_action.value = vault.substitute_placeholders(exec_action.value, calling_task_id=task_id)
                desc, extracted_item = await controller.execute_action(exec_action, form_fields)
                success = True
            except Exception as exc:
                desc = f"Action {action.action.value.upper()} failed: {exc}"
                extracted_item = None
                success = False

            if success:
                if action.action == ActionType.EXTRACT and extracted_item:
                    from backend.security import check_extraction_safety_threshold
                    is_explicit_contact = any(k in (task.goal or "").lower() for k in ["contact", "email", "phone", "address", "directory", "lead", "who to call"])
                    try:
                        allowed, reason, safe_extracted = check_extraction_safety_threshold(
                            extracted_item,
                            task_id=task_id,
                            is_explicit_contact_request=is_explicit_contact,
                            user_intent=task.goal,
                        )
                    except Exception as exc:
                        allowed = False
                        reason = f"Extraction safety threshold check failed: {type(exc).__name__}"
                        safe_extracted = None
                    if not allowed:
                        from backend.security import get_security_user_message, SecurityEventType
                        user_error = get_security_user_message(SecurityEventType.EXTRACTION_REFUSED)
                        return f"Extraction refused: {user_error}", {"status": "Refused", "error": user_error}, False
                    extracted_item = safe_extracted
                    task.extracted_data.append(extracted_item)
                elif action.action == ActionType.FINISH:
                    verified, verification_message = await self._verify_goal_completion(controller, task, planner, trajectory_history)
                    if not verified:
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = verification_message
                        return verification_message, {"status": "Failed", "error": verification_message}, False
                    await controller.clear_badges()
                    if action.extracted_data:
                        task.extracted_data.append(action.extracted_data)
                        extracted_item = action.extracted_data
                    if action.extracted_data and action.extracted_data.get("status") == "Failed":
                        task.status = TaskStatus.FAILED
                        task.plan_status = "failed"
                        task.error = action.extracted_data.get("error") or "Task execution failed."
                        success = False
            return desc, extracted_item, success

        return (
            f"Unsupported Chrome Extension action: {action.action.value}",
            None,
            False,
        )

    # ------------------------------------------------------------------
    # Human approval
    # ------------------------------------------------------------------

    def approve_human_step(
        self,
        task_id: str,
        approved: bool,
        user_input: Optional[str] = None,
    ) -> None:
        """Signal the execution loop to resume (or cancel) after human review."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            if task.approval_request:
                # Count each approval request once. Repeated network/UI submissions
                # for the same request must not inflate the report metric.
                if approved and task.approval_request.status == "pending":
                    task.human_approval_count = int(getattr(task, "human_approval_count", 0) or 0) + 1
                task.approval_request.status = "approved" if approved else "rejected"
                task.approval_request.user_input = user_input

            if approved:
                task.status = TaskStatus.RUNNING
                task.plan_status = "recovering_session"
                task.error = "Approval received. Waiting for the browser session to recover..."
            else:
                task.status = TaskStatus.CANCELLED
                task.plan_status = "cancelled"
                task.error = "Rejected by human operator."
            task.updated_at = time.time()

        if task_id in self.approval_events:
            self.approval_events[task_id].set()

    async def pause_task(self, task_id: str) -> TaskResponse:
        """Pause after the currently executing browser action safely completes."""
        task = self.tasks.get(task_id)
        if not task:
            if task_id in self.task_history:
                raise ValueError("Task is already finished and cannot be paused.")
            raise KeyError(f"Task {task_id} not found")
        if task.status != TaskStatus.RUNNING:
            raise ValueError(f"Only running tasks can be paused (current status: {task.status.value}).")
        gate = self.pause_events.get(task_id)
        if gate is None:
            raise ValueError("Task is not ready to pause.")
        gate.clear()
        task.status = TaskStatus.PAUSED
        task.plan_status = "paused"
        task.error = "Paused by user. The current browser action will finish before execution stops."
        task.updated_at = time.time()
        await self.broadcast_task_update(task_id)
        await self._save_cached_task_state(task_id)
        return task

    async def resume_task(self, task_id: str) -> TaskResponse:
        """Resume a cooperatively paused task from its next safe execution boundary."""
        task = self.tasks.get(task_id)
        if not task:
            if task_id in self.task_history:
                raise ValueError("Task is already finished and cannot be resumed.")
            raise KeyError(f"Task {task_id} not found")
        if task.status != TaskStatus.PAUSED:
            raise ValueError(f"Only paused tasks can be resumed (current status: {task.status.value}).")
        gate = self.pause_events.get(task_id)
        if gate is None:
            raise ValueError("Task execution is no longer available to resume.")
        task.status = TaskStatus.RUNNING
        task.plan_status = "executing"
        task.error = None
        task.updated_at = time.time()
        gate.set()
        await self.broadcast_task_update(task_id)
        await self._save_cached_task_state(task_id)
        return task

    async def cancel_task(
        self,
        task_id: str,
        reason: str = "Task cancelled by user.",
    ) -> TaskResponse:
        """Cancel a running, pending, or approval-waiting task immediately."""
        task = self.tasks.get(task_id)
        if not task:
            if task_id in self.task_history:
                return self.task_history[task_id]
            raise KeyError(f"Task {task_id} not found")

        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if task.status in terminal:
            return task

        logger.info(f"Explicitly cancelling task {task_id} (current status: {task.status})")
        task.status = TaskStatus.CANCELLED
        task.plan_status = "cancelled"
        task.error = reason
        task.updated_at = time.time()

        # Cancel running children first, then close any form-review tabs they
        # may have retained while finishing their cancellation cleanup.
        async def _cancel_delegated_work() -> None:
            await self.subagent_orchestrator.cancel_parent(task_id)
            await close_parent_form_review_tabs(task_id)

        try:
            asyncio.get_running_loop().create_task(_cancel_delegated_work())
        except RuntimeError:
            pass

        # Signal cancellation event immediately for zero-latency wakeup
        if task_id in self.cancel_events:
            self.cancel_events[task_id].set()

        # If waiting on approval, wake up the event immediately
        if task.approval_request:
            task.approval_request.status = "rejected"
            task.approval_request.user_input = reason
        if task_id in self.approval_events:
            self.approval_events[task_id].set()

        # Mark controller stopped immediately so in-flight checks abort fast
        controller = self.controllers.get(task_id)
        if controller:
            if hasattr(controller, "is_running"):
                controller.is_running = False

        # Cancel the background asyncio task if running
        async_task = self.running_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()

        # Unlock UI on controller if locked, and stop controller with short timeout
        if controller:
            if hasattr(controller, "unlock_ui"):
                try:
                    await asyncio.wait_for(controller.unlock_ui(), timeout=0.5)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(controller.stop(), timeout=1.0)
            except Exception:
                pass

        # Add step record
        if not any("cancelled" in (s.description or "").lower() for s in task.steps):
            task.steps.append(AgentStep(
                step_number=len(task.steps) + 1,
                action=ActionType.FINISH,
                description=f"Task cancelled by user: {reason}",
            ))

        # Broadcast WebSocket update
        await self.broadcast_task_update(task_id)

        # Record in history & invalidate redis cache
        self._record_terminal_task(task)
        try:
            await self._invalidate_cached_task_state(task_id)
        except Exception:
            pass

        return task

    async def delete_task(self, task_id: str) -> Dict[str, Any]:
        """Cancel a task if running and remove it from memory and history."""
        if task_id not in self.tasks and task_id not in self.task_history:
            raise KeyError(f"Task {task_id} not found")

        # Capture the task before cancellation; its finalizer may remove it from the registry.
        background_task = self.running_tasks.get(task_id)
        if task_id in self.tasks:
            task = self.tasks[task_id]
            terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            if task.status not in terminal:
                await self.cancel_task(task_id, reason="Task cancelled via DELETE request.")
            self.tasks.pop(task_id, None)

        # Await the background finalizer before removing state so it cannot re-add
        # a deleted task to history after this method returns.
        if background_task and background_task is not asyncio.current_task() and not background_task.done():
            await asyncio.gather(background_task, return_exceptions=True)

        try:
            from backend.security import clear_task_vault
            clear_task_vault(task_id)
        except Exception:
            pass
        self.tasks.pop(task_id, None)
        self.task_history.pop(task_id, None)
        self.running_tasks.pop(task_id, None)
        self.approval_events.pop(task_id, None)
        self.cancel_events.pop(task_id, None)
        self.controllers.pop(task_id, None)
        self._report_store.pop(task_id, None)
        try:
            task_archive.delete_task(task_id)
        except Exception:
            logger.exception("Failed to delete archived task %s", task_id)

        return {
            "status": "success",
            "message": f"Task {task_id} deleted successfully.",
            "task_id": task_id,
        }


task_manager = TaskManager()
