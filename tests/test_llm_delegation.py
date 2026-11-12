import asyncio
import json

from agents.delegation import plan_subagent_specs


class FakePlanner:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def _call_llm(self, prompt, response_mime_type):
        self.calls.append((prompt, response_mime_type))
        return self.response


def test_master_llm_can_select_multiple_read_only_specialists():
    planner = FakePlanner(json.dumps({
        "subtasks": [
            {"role": "research", "goal": "Read the visible report", "input": {"url": "https://example.com"}, "timeout_seconds": 80},
            {"role": "verification", "goal": "Check whether the report title is visible", "input": {"url": "https://example.com", "expected_text": "Annual Report"}, "timeout_seconds": 30},
        ]
    }))
    specs = asyncio.run(plan_subagent_specs(planner, "Research this report", "https://example.com"))
    assert [item.role for item in specs] == ["research", "verification"]
    assert all(item.input["url"] == "https://example.com" for item in specs)
    assert len(planner.calls) == 1


def test_master_llm_cannot_delegate_mutating_worker_or_invent_url():
    planner = FakePlanner(json.dumps({
        "subtasks": [
            {"role": "form", "goal": "Fill and submit the form", "input": {"url": "https://example.com"}},
            {"role": "research", "goal": "Research another site", "input": {"url": "https://evil.example"}},
        ]
    }))
    specs = asyncio.run(plan_subagent_specs(planner, "Research this report", "https://example.com"))
    assert specs == []


def test_mutating_parent_goal_skips_llm_delegation():
    planner = FakePlanner('{"subtasks": []}')
    specs = asyncio.run(plan_subagent_specs(planner, "Delete the selected email", "https://mail.google.com"))
    assert specs == []
    assert planner.calls == []


def test_malformed_llm_response_uses_safe_read_only_fallback():
    planner = FakePlanner("not json")
    specs = asyncio.run(plan_subagent_specs(planner, "Research this page", "https://example.com"))
    assert [item.role for item in specs] == ["research"]
