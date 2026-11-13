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


def test_master_llm_can_select_allowed_read_only_specialists():
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



def test_master_can_delegate_read_only_interaction_specialist():
    planner = FakePlanner(json.dumps({
        "subtasks": [
            {
                "role": "interaction",
                "goal": "Identify the Gmail search control and inbox navigation links",
                "input": {"url": "https://mail.google.com"},
                "timeout_seconds": 50,
            }
        ]
    }))
    specs = asyncio.run(plan_subagent_specs(
        planner, "Open Gmail and locate the search control", "https://mail.google.com"
    ))
    assert [item.role for item in specs] == ["interaction"]
    assert specs[0].input == {"url": "https://mail.google.com"}
    assert "interaction" in planner.calls[0][0]


def test_interaction_specialist_cannot_be_given_mutating_goal():
    planner = FakePlanner(json.dumps({
        "subtasks": [
            {
                "role": "interaction",
                "goal": "Click delete and remove the selected email",
                "input": {"url": "https://mail.google.com"},
            }
        ]
    }))
    specs = asyncio.run(plan_subagent_specs(
        planner, "Inspect the inbox", "https://mail.google.com"
    ))
    assert specs == []



def test_runtime_master_can_delegate_read_only_specialist_on_live_page():
    from agents.delegation import plan_runtime_subagent_specs

    planner = FakePlanner(json.dumps({
        "subtasks": [{
            "role": "interaction",
            "goal": "Identify the Spam folder navigation control",
            "input": {"url": "https://mail.google.com/mail/u/0/#inbox"},
            "timeout_seconds": 45,
        }]
    }))
    page = {
        "url": "https://mail.google.com/mail/u/0/#inbox",
        "title": "Gmail",
        "headings": ["Inbox"],
        "elements": [
            {"tag": "a", "text": "Spam", "aria_label": "Spam", "href": "https://mail.google.com/mail/u/0/#spam"},
            {"tag": "input", "type": "password", "value": "must-not-be-forwarded"},
        ],
    }
    specs = asyncio.run(plan_runtime_subagent_specs(
        planner,
        "Open Gmail spam and delete an unwanted message",
        "https://mail.google.com/mail/u/0/#inbox",
        page,
    ))
    assert [item.role for item in specs] == ["interaction"]
    assert specs[0].input["url"] == "https://mail.google.com/mail/u/0/#inbox"
    assert "must-not-be-forwarded" not in planner.calls[0][0]
    assert "delete an unwanted message" in planner.calls[0][0]


def test_runtime_delegation_rejects_unknown_url_role_and_mutating_subtask():
    from agents.delegation import plan_runtime_subagent_specs

    planner = FakePlanner(json.dumps({
        "subtasks": [
            {"role": "interaction", "goal": "Click delete", "input": {"url": "https://mail.google.com"}},
            {"role": "research", "goal": "Read another site", "input": {"url": "https://evil.example"}},
        ]
    }))
    specs = asyncio.run(plan_runtime_subagent_specs(
        planner,
        "Delete a message in Gmail",
        "https://mail.google.com",
        {"title": "Gmail", "headings": [], "elements": []},
    ))
    assert specs == []
