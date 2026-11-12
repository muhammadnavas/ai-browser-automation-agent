import asyncio
import json

from agents.browser_subagents import recovery_worker, verification_worker
from agents.subagents import SubagentContext


class FakePlanner:
    def __init__(self, response):
        self.response = response

    def _call_llm(self, prompt, response_mime_type):
        return self.response


class FakeController:
    async def parse_interactive_elements(self):
        return {
            "url": "https://example.com/result",
            "title": "Result",
            "headings": ["Result"],
            "page_text": "The requested record was not found.",
            "elements": [],
        }


def make_context(role, goal, inputs=None, memory=None):
    return SubagentContext(
        parent_task_id="parent-test",
        subagent_id="child-test",
        role=role,
        goal=goal,
        input=inputs or {},
        memory=memory or {},
    )


def test_recovery_worker_returns_milestones_without_browser_actions():
    planner = FakePlanner(json.dumps({
        "diagnosis": "The target control was not present.",
        "strategy": "Re-inspect the current page and use a visible alternative.",
        "revised_steps": ["Inspect the current page", "Locate the correct section"],
        "confidence": 0.85,
        "requires_human_review": False,
    }))
    ctx = make_context(
        "recovery",
        "Recover failed action",
        {"goal": "Find the requested record", "error": "Target not found"},
        {"planner": planner},
    )
    result = asyncio.run(recovery_worker(ctx))
    assert result["revised_steps"] == ["Inspect the current page", "Locate the correct section"]
    assert result["confidence"] == 0.85
    assert result["requires_human_review"] is False


def test_verification_worker_uses_parent_tab_and_reports_confidence():
    planner = FakePlanner(json.dumps({
        "verified": False,
        "confidence": 0.95,
        "reason": "The page explicitly says the record was not found.",
        "evidence": "The requested record was not found.",
    }))
    controller = FakeController()
    ctx = make_context(
        "verification",
        "Verify requested record",
        {"goal": "Find the requested record", "use_parent_tab": True},
        {"planner": planner, "controller": controller},
    )
    result = asyncio.run(verification_worker(ctx))
    assert result["verified"] is False
    assert result["confidence"] == 0.95
    assert "not found" in result["evidence"]
