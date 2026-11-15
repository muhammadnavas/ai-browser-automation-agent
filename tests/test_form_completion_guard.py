"""Regression tests for preventing approval of incomplete browser forms."""

from backend.api.task_manager import TaskManager


def test_unanswered_radio_and_checkbox_groups_block_form_review():
    page = {
        "form_groups": [
            {
                "question": "Overall rating",
                "type": "radio_group",
                "options": [{"label": "1", "selected": False}, {"label": "5", "selected": False}],
            },
            {
                "question": "Helpful components",
                "type": "checkbox_group",
                "options": [{"label": "Lectures", "selected": False}],
            },
        ],
        "elements": [],
        "form_fields": [],
    }

    assert TaskManager._unanswered_form_questions(page) == [
        "Overall rating",
        "Helpful components",
    ]


def test_selected_groups_and_answered_dropdown_do_not_block_review():
    page = {
        "form_groups": [
            {
                "question": "Overall rating",
                "type": "radio_group",
                "options": [{"label": "5", "selected": True}],
            }
        ],
        "elements": [
            {"role": "option", "text": "Computer Science", "label": "Course"},
        ],
        "form_fields": [
            {"required": True, "current_value": "Navas", "label": "Name"},
        ],
    }

    assert TaskManager._unanswered_form_questions(page) == []


def test_google_forms_choose_placeholder_is_reported_as_unanswered():
    page = {
        "form_groups": [],
        "elements": [
            {
                "role": "option",
                "text": "Choose",
                "label": "Which course are you providing feedback for?",
            }
        ],
        "form_fields": [],
    }

    assert TaskManager._unanswered_form_questions(page) == [
        "Which course are you providing feedback for?"
    ]



def test_task_instruction_is_not_accepted_as_form_field_value():
    from backend.models.schemas import ActionType, BrowserAction

    goal = "Fill this application form with my data: https://forms.gle/example"
    action = BrowserAction(
        action=ActionType.TYPE_TEXT,
        text=goal,
        selector='input[aria-label="Search"]',
    )

    assert TaskManager._is_task_goal_echo(action, goal)


def test_real_profile_value_is_not_treated_as_task_goal_echo():
    from backend.models.schemas import ActionType, BrowserAction

    action = BrowserAction(
        action=ActionType.FILL,
        text="Muhammad Navas",
        selector='input[aria-label="Full Name"]',
    )

    assert not TaskManager._is_task_goal_echo(
        action,
        "Fill this application form with my data: https://forms.gle/example",
    )


def test_task_goal_url_embedded_in_search_query_is_blocked():
    from backend.models.schemas import ActionType, BrowserAction

    action = BrowserAction(
        action=ActionType.TYPE_TEXT,
        text="Please fill this application form with my data https://forms.gle/example",
        selector='input[aria-label="Search"]',
    )

    assert TaskManager._is_task_goal_echo(
        action,
        "Fill this application form with my data: https://forms.gle/example",
    )
