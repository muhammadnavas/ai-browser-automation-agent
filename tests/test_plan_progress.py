"""Regression tests for execution-plan milestone progress."""

from agents.planner import AIAgentPlanner
from models.schemas import ActionType, BrowserAction


def test_gmail_compose_click_does_not_mark_send_or_report_milestone():
    planner = AIAgentPlanner()
    plan = [
        "Navigate to Gmail inbox",
        "Open new email composition window",
        "Fill recipient, subject, and body",
        "Trigger human approval gate to review email",
        "Send email after approval",
        "Confirm email appears in Sent folder",
        "Generate summary report of email sent",
    ]
    action = BrowserAction(
        action=ActionType.CLICK,
        text="Compose",
        element_id=11,
        reasoning="Open the Compose window to send an email.",
    )

    idx = planner.determine_active_plan_step(
        initial_plan=plan,
        current_action=action,
        trajectory_history=[],
        page_info={"url": "https://mail.google.com/mail/u/0/#inbox"},
        step_number=1,
        max_steps=25,
    )

    assert idx == 1


def test_explicit_send_button_click_marks_send_milestone_not_summary_report():
    planner = AIAgentPlanner()
    plan = [
        "Navigate to Gmail inbox",
        "Open new email composition window",
        "Fill recipient, subject, and body",
        "Trigger human approval gate to review email",
        "Send email after approval",
        "Confirm email appears in Sent folder",
        "Generate summary report of email sent",
    ]
    action = BrowserAction(
        action=ActionType.CLICK,
        text="Send",
        selector="button[aria-label='Send']",
        reasoning="Click the Send button after approval.",
    )

    idx = planner.determine_active_plan_step(
        initial_plan=plan,
        current_action=action,
        trajectory_history=[],
        page_info={"url": "https://mail.google.com/mail/u/0/#inbox?compose=new"},
        step_number=5,
        max_steps=25,
    )

    assert idx == 4
