"""Regression tests for feedback-form classification and safe review/approval flow."""

from agents.action_safety import approval_was_granted
from agents.planner import AIAgentPlanner, synthesize_generalized_field_value


def test_student_feedback_form_is_not_classified_as_job_application():
    intent = AIAgentPlanner()._analyze_task_intent(
        "Fill this application form with my data https://forms.gle/example"
    )
    # Generic "application form" wording must not imply employment.
    assert intent["category_key"] == "form_filling"


def test_course_feedback_form_gets_feedback_label():
    intent = AIAgentPlanner()._analyze_task_intent(
        "Fill this student feedback form https://forms.gle/example"
    )
    assert intent["category_key"] == "form_filling"
    assert intent["category_label"] == "Feedback & Survey Form"


def test_job_application_still_classifies_as_job_application():
    intent = AIAgentPlanner()._analyze_task_intent(
        "Apply for this software engineering job and upload my resume"
    )
    assert intent["category_key"] in {"file_upload", "job_application"}


def test_feedback_is_never_approval():
    assert not approval_was_granted("not every fields are filled", [])
    assert not approval_was_granted("please review the remaining fields", [])
    assert not approval_was_granted("wait, do not submit", [])


def test_approval_requires_explicit_affirmative_response():
    assert approval_was_granted("Yes, proceed", [])
    assert approval_was_granted("go ahead", [])
    assert not approval_was_granted("I think it is okay but not yet", [])


def test_degree_is_not_used_as_a_course_feedback_comment():
    context = {
        "profile": {
            "education": {"degree": "Computer Science Engineering in AI & ML"}
        }
    }
    result = synthesize_generalized_field_value(
        {"label": "Please share any additional comments, suggestions, or concerns regarding the course.", "type": "text"},
        goal="Fill this feedback form with my data",
        retrieved_context=context,
    )
    assert result.value == ""
