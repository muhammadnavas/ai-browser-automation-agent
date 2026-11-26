from agents.delegation import build_automatic_subagent_specs


def test_research_goal_automatically_delegates_to_research_worker():
    specs = build_automatic_subagent_specs(
        "Research and summarize the latest report",
        "https://example.com/report",
    )
    assert [spec.role for spec in specs] == ["research"]
    assert specs[0].input["url"] == "https://example.com/report"


def test_simple_navigation_automatically_delegates_to_navigation_worker():
    specs = build_automatic_subagent_specs(
        "Open the dashboard",
        "https://example.com/dashboard",
    )
    assert [spec.role for spec in specs] == ["navigation"]


def test_mutating_task_is_not_automatically_delegated():
    specs = build_automatic_subagent_specs(
        "Delete the selected email",
        "https://mail.google.com",
    )
    assert specs == []


def test_research_is_not_delegated_without_known_url():
    specs = build_automatic_subagent_specs(
        "Research and summarize this topic",
        None,
    )
    assert specs == []


def test_rename_task_is_not_automatically_delegated():
    specs = build_automatic_subagent_specs(
        "open gdrive and rename Assist folder to Test",
        "https://drive.google.com",
    )
    assert specs == []

