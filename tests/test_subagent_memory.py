import asyncio

from agents.subagents import SubagentContext, SubagentOrchestrator, SubagentSpec


def test_shared_parent_memory_is_available_to_specialist():
    orchestrator = SubagentOrchestrator(max_concurrency=1)
    expected = object()

    async def worker(ctx: SubagentContext):
        return {"same_object": ctx.memory.get("planner") is expected}

    orchestrator.register_worker("memory-test", worker)
    batch = asyncio.run(orchestrator.run_batch(
        "parent-1",
        [SubagentSpec(role="memory-test", goal="Check shared context")],
        shared_memory={"planner": expected},
    ))
    assert batch.status == "completed"
    assert batch.results[0].output == {"same_object": True}



def test_parent_history_accumulates_across_followup_batches():
    orchestrator = SubagentOrchestrator(max_concurrency=1)

    async def worker(ctx: SubagentContext):
        return ctx.goal

    orchestrator.register_worker("history-test", worker)

    async def run_batches():
        await orchestrator.run_batch(
            "parent-history",
            [SubagentSpec(role="history-test", goal="Initial research")],
        )
        await orchestrator.run_batch(
            "parent-history",
            [SubagentSpec(role="history-test", goal="Follow-up research")],
        )
        return orchestrator.get_batch("parent-history")

    history = asyncio.run(run_batches())
    assert [result.output for result in history.results] == [
        "Initial research",
        "Follow-up research",
    ]
    assert history.summary["completed"] == 2
