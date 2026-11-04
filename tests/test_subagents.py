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
