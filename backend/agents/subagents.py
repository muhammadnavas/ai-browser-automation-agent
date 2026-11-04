"""Task-scoped subagent orchestration primitives.

Workers are executor-agnostic and use the existing planner/browser adapters.
Shared Chrome Extension sessions are protected by per-session exclusive locks.
"""
from __future__ import annotations
import asyncio, logging, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("SubagentOrchestrator")
class SubagentStatus(str, Enum):
    PENDING="pending"; RUNNING="running"; COMPLETED="completed"; FAILED="failed"; CANCELLED="cancelled"; TIMED_OUT="timed_out"
class SubagentSpec(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1, max_length=4000)
    input: Dict[str, Any] = Field(default_factory=dict)
    resource_key: Optional[str] = Field(default=None, max_length=256)
    requires_exclusive_browser: bool = False
    timeout_seconds: float = Field(default=120.0, ge=1.0, le=1800.0)
class SubagentResult(BaseModel):
    subagent_id: str; parent_task_id: str; role: str; goal: str; status: SubagentStatus
    output: Any = None; error: Optional[str] = None; started_at: Optional[float] = None
    completed_at: Optional[float] = None; duration_ms: Optional[float] = None
class SubagentBatchResult(BaseModel):
    parent_task_id: str; status: str; results: List[SubagentResult] = Field(default_factory=list)
    summary: Dict[str, int] = Field(default_factory=dict)
Worker = Callable[["SubagentContext"], Awaitable[Any]]
@dataclass
class SubagentContext:
    parent_task_id: str; subagent_id: str; role: str; goal: str; input: Dict[str, Any]
    memory: Dict[str, Any] = field(default_factory=dict)
    cancellation_event: asyncio.Event = field(default_factory=asyncio.Event)
    def raise_if_cancelled(self) -> None:
        if self.cancellation_event.is_set(): raise asyncio.CancelledError()
class SubagentOrchestrator:
    """Bounded worker execution, task-scoped contexts and result aggregation."""
    def __init__(self, max_concurrency: int = 4):
        if max_concurrency < 1: raise ValueError("max_concurrency must be at least 1")
        self._semaphore=asyncio.Semaphore(max_concurrency); self._workers: Dict[str,Worker]={}
        self._batches: Dict[str,SubagentBatchResult]={}; self._contexts: Dict[str,SubagentContext]={}
        self._resource_locks: Dict[str,asyncio.Lock]={}; self._parent_tasks: Dict[str,List[asyncio.Task]]={}
        self._max_batches = 200
    def register_worker(self, role: str, worker: Worker) -> None:
        role=(role or "").strip().lower()
        if not role or not callable(worker): raise ValueError("A non-empty role and async worker are required")
        self._workers[role]=worker
    def unregister_worker(self, role: str) -> None: self._workers.pop((role or "").strip().lower(),None)
    def get_batch(self, parent_task_id: str) -> Optional[SubagentBatchResult]: return self._batches.get(parent_task_id)
    async def run_batch(self, parent_task_id: str, specs: Iterable[SubagentSpec], *, fail_fast: bool=False) -> SubagentBatchResult:
        if not parent_task_id: raise ValueError("parent_task_id is required")
        if any(not t.done() for t in self._parent_tasks.get(parent_task_id,[])): raise RuntimeError("parent already has a running subagent batch")
        specs=list(specs); results: List[Optional[SubagentResult]]=[None]*len(specs); stop_event=asyncio.Event()
        async def execute(index: int, spec: SubagentSpec) -> None:
            role=spec.role.strip().lower(); child_id="subagent-"+uuid.uuid4().hex[:12]
            result=SubagentResult(subagent_id=child_id,parent_task_id=parent_task_id,role=role,goal=spec.goal,status=SubagentStatus.PENDING)
            worker=self._workers.get(role)
            if worker is None:
                result.status=SubagentStatus.FAILED; result.error=f"No worker registered for role {role!r}."; result.completed_at=time.time(); results[index]=result; return
            if spec.requires_exclusive_browser and not spec.resource_key:
                result.status=SubagentStatus.FAILED; result.error="resource_key is required for exclusive browser work."; result.completed_at=time.time(); results[index]=result; return
            ctx=SubagentContext(parent_task_id,child_id,role,spec.goal,dict(spec.input)); self._contexts[child_id]=ctx
            started=time.time(); result.status=SubagentStatus.RUNNING; result.started_at=started
            try:
                async with self._semaphore:
                    ctx.raise_if_cancelled()
                    if stop_event.is_set(): raise asyncio.CancelledError()
                    if spec.requires_exclusive_browser:
                        lock=self._resource_locks.setdefault(spec.resource_key,asyncio.Lock())
                        async with lock: ctx.raise_if_cancelled(); result.output=await asyncio.wait_for(worker(ctx),timeout=spec.timeout_seconds)
                    else: result.output=await asyncio.wait_for(worker(ctx),timeout=spec.timeout_seconds)
                result.status=SubagentStatus.COMPLETED
            except asyncio.TimeoutError:
                result.status=SubagentStatus.TIMED_OUT; result.error=f"Worker exceeded {spec.timeout_seconds:g}s timeout."
                if fail_fast:
                    stop_event.set()
                    for child_context in list(self._contexts.values()):
                        if child_context.parent_task_id == parent_task_id:
                            child_context.cancellation_event.set()
                    current = asyncio.current_task()
                    for sibling in self._parent_tasks.get(parent_task_id, []):
                        if sibling is not current and not sibling.done():
                            sibling.cancel()
            except asyncio.CancelledError:
                result.status=SubagentStatus.CANCELLED; result.error="Subagent cancelled."
            except Exception as exc:
                result.status=SubagentStatus.FAILED; result.error=f"{type(exc).__name__}: {exc}"[:1000]; logger.exception("Subagent %s failed",child_id)
                if fail_fast:
                    stop_event.set()
                    for child_context in list(self._contexts.values()):
                        if child_context.parent_task_id == parent_task_id:
                            child_context.cancellation_event.set()
                    current = asyncio.current_task()
                    for sibling in self._parent_tasks.get(parent_task_id, []):
                        if sibling is not current and not sibling.done():
                            sibling.cancel()
            finally:
                result.completed_at=time.time(); result.duration_ms=round((result.completed_at-started)*1000,2)
                self._contexts.pop(child_id,None); results[index]=result
        if not specs:
            batch=SubagentBatchResult(parent_task_id=parent_task_id,status="completed")
            self._batches[parent_task_id]=batch
            while len(self._batches) > self._max_batches:
                self._batches.pop(next(iter(self._batches)))
            return batch
        tasks=[asyncio.create_task(execute(i,s),name=f"subagent:{parent_task_id}:{i}") for i,s in enumerate(specs)]
        self._parent_tasks[parent_task_id]=tasks
        try: await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            stop_event.set()
            for ctx in list(self._contexts.values()):
                if ctx.parent_task_id==parent_task_id: ctx.cancellation_event.set()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True); raise
        normalized=[r for r in results if r is not None]
        summary={s.value:sum(r.status==s for r in normalized) for s in SubagentStatus}
        failures=summary["failed"]+summary["timed_out"]
        status="partial" if failures and summary["completed"] else ("failed" if failures else ("cancelled" if summary["cancelled"] else "completed"))
        batch=SubagentBatchResult(parent_task_id=parent_task_id,status=status,results=normalized,summary=summary)
        self._batches[parent_task_id]=batch
        while len(self._batches) > self._max_batches:
            self._batches.pop(next(iter(self._batches)))
        # Keep result snapshots, but release completed asyncio task handles.
        self._parent_tasks.pop(parent_task_id, None)
        return batch
    async def cancel_parent(self,parent_task_id: str) -> int:
        tasks=self._parent_tasks.get(parent_task_id,[]); count=0
        for ctx in list(self._contexts.values()):
            if ctx.parent_task_id==parent_task_id: ctx.cancellation_event.set(); count+=1
        for task in tasks:
            if not task.done(): task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        return count
    def clear_parent(self,parent_task_id: str) -> None:
        self._batches.pop(parent_task_id,None); self._parent_tasks.pop(parent_task_id,None)
        for child_id,ctx in list(self._contexts.items()):
            if ctx.parent_task_id==parent_task_id: self._contexts.pop(child_id,None)
subagent_orchestrator=SubagentOrchestrator()