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
        ]
    }))
    specs = asyncio.run(plan_subagent_specs(planner, "Research this report", "https://example.com"))
    assert [item.role for item in specs] == ["research"]
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



def test_preflight_does_not_run_verification_before_browser_execution():
    planner = FakePlanner(json.dumps({
        "subtasks": [
            {"role": "verification", "goal": "Verify a report is present", "input": {"url": "https://example.com", "expected_text": "Report"}}
        ]
    }))
    specs = asyncio.run(plan_subagent_specs(planner, "Research this report", "https://example.com"))
    assert specs == []


def test_followup_agent_can_retry_failed_role_but_not_repeat_completed_role():
    from agents.delegation import plan_followup_subagent_specs

    planner = FakePlanner(json.dumps({
        "subtasks": [
            {"role": "research", "goal": "Retry reading the report after the first worker timed out", "input": {"url": "https://example.com"}, "timeout_seconds": 60},
            {"role": "navigation", "goal": "Repeat completed navigation", "input": {"url": "https://example.com"}}
        ]
    }))
    prior = [
        {"role": "research", "status": "timed_out", "goal": "Read report", "output": {}, "error": "timeout"},
        {"role": "navigation", "status": "completed", "goal": "Inspect page", "output": {"title": "Report"}},
    ]
    specs = asyncio.run(plan_followup_subagent_specs(planner, "Research this report", "https://example.com", prior))
    assert [item.role for item in specs] == ["research"]
    assert specs[0].input["url"] == "https://example.com"
