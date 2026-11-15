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
