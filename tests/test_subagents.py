import asyncio
import pytest
from agents.subagents import SubagentOrchestrator, SubagentSpec, SubagentStatus

@pytest.mark.asyncio
async def test_workers_have_independent_context_and_results_keep_input_order():
    manager=SubagentOrchestrator(max_concurrency=2); seen=[]
    async def worker(ctx):
        ctx.memory["private"]=ctx.subagent_id; await asyncio.sleep(0.01); seen.append(ctx.memory["private"]); return ctx.goal
    manager.register_worker("research",worker)
    batch=await manager.run_batch("p1",[SubagentSpec(role="research",goal="A"),SubagentSpec(role="research",goal="B")])
    assert batch.status=="completed" and [r.output for r in batch.results]==["A","B"]
    assert len(set(seen))==2 and len({r.subagent_id for r in batch.results})==2

@pytest.mark.asyncio
async def test_same_extension_session_is_serialized():
    manager=SubagentOrchestrator(max_concurrency=4); active=0; maximum=0
    async def worker(ctx):
        nonlocal active,maximum
        active+=1; maximum=max(maximum,active); await asyncio.sleep(0.01); active-=1; return "ok"
    manager.register_worker("browser",worker)
    specs=[SubagentSpec(role="browser",goal=str(i),resource_key="chrome-1",requires_exclusive_browser=True) for i in range(3)]
    batch=await manager.run_batch("p2",specs)
    assert batch.status=="completed" and maximum==1

@pytest.mark.asyncio
async def test_missing_worker_and_missing_resource_key_fail_closed():
    manager=SubagentOrchestrator()
    batch=await manager.run_batch("p3",[SubagentSpec(role="missing",goal="x"),SubagentSpec(role="browser",goal="y",requires_exclusive_browser=True)])
    assert all(r.status==SubagentStatus.FAILED for r in batch.results)

@pytest.mark.asyncio
async def test_worker_timeout_is_reported():
    manager=SubagentOrchestrator()
    async def slow(ctx): await asyncio.sleep(2)
    manager.register_worker("slow",slow)
    batch=await manager.run_batch("p4",[SubagentSpec(role="slow",goal="wait",timeout_seconds=1)])
    assert batch.results[0].status==SubagentStatus.TIMED_OUT

@pytest.mark.asyncio
async def test_fail_fast_cancels_running_siblings():
    manager=SubagentOrchestrator(max_concurrency=2)
    started=asyncio.Event()
    async def broken(ctx):
        await asyncio.sleep(0.01)
        raise RuntimeError("worker failure")
    async def long_running(ctx):
        started.set()
        await asyncio.sleep(30)
    manager.register_worker("broken",broken)
    manager.register_worker("long",long_running)
    batch=await manager.run_batch("p5",[
        SubagentSpec(role="broken",goal="fail"),
        SubagentSpec(role="long",goal="wait"),
    ],fail_fast=True)
    assert batch.status=="failed"
    assert batch.summary["failed"]==1
    assert batch.summary["cancelled"]==1


def test_task_request_keeps_subagent_delegation_opt_in():
    from models.schemas import TaskRequest
    request = TaskRequest(goal="Open a website")
    assert request.subagent_specs == []
    delegated = TaskRequest(
        goal="Research a website",
        subagent_specs=[{
            "role": "research",
            "goal": "Collect public page information",
            "input": {"url": "https://example.com"},
        }],
    )
    assert delegated.subagent_specs[0]["role"] == "research"


@pytest.mark.asyncio
async def test_parent_cancellation_stops_running_children():
    manager=SubagentOrchestrator()
    started=asyncio.Event()
    async def worker(ctx):
        started.set()
        await asyncio.sleep(30)
    manager.register_worker("worker",worker)
    batch_task=asyncio.create_task(manager.run_batch(
        "p6",[SubagentSpec(role="worker",goal="long-running")]
    ))
    await asyncio.wait_for(started.wait(),timeout=1)
    cancelled=await manager.cancel_parent("p6")
    batch=await asyncio.wait_for(batch_task,timeout=1)
    assert cancelled==1
    assert batch.results[0].status==SubagentStatus.CANCELLED

@pytest.mark.asyncio
async def test_direct_batch_cancellation_releases_parent_task_handles():
    manager=SubagentOrchestrator()
    started=asyncio.Event()

    async def worker(ctx):
        started.set()
        await asyncio.sleep(30)

    manager.register_worker("worker",worker)
    batch_task=asyncio.create_task(manager.run_batch(
        "p7",[SubagentSpec(role="worker",goal="cancel batch runner")])
    )
    await asyncio.wait_for(started.wait(),timeout=1)
    batch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch_task

    assert "p7" not in manager._parent_tasks
    # A retry for the same parent must not be blocked by stale task handles.
    retry=await manager.run_batch("p7",[])
    assert retry.status=="completed"
