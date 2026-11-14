"""
tests/test_my_context_form_filling.py
-------------------------------------
Regression tests for:
1. Form field synthesis with My Context retrieved data (Profile, Memories, Documents).
2. Field-level provenance attribution (FieldValueResult.source).
3. Exclusive My Context form-filling: leaving unavailable fields blank.
4. Dropping down options (<select>) and checking checkboxes (<input type="checkbox">).
5. Full practice form walkthrough on /test-form without form submission.
6. User context ownership isolation (User B cannot access User A's data).
7. Password and security field protections during autonomous form filling.
"""

import pytest
from unittest.mock import MagicMock

from backend.agents.planner import (
    AIAgentPlanner,
    FieldValueResult,
    synthesize_generalized_field_value,
)
from backend.models.schemas import ActionType, BrowserAction


# ==============================================================================
# Test Data Fixtures
# ==============================================================================

@pytest.fixture
def user_a_retrieved_context():
    """Mock user context retrieved from PostgreSQL My Context for User A."""
    return {
        "has_context": True,
        "user_id": "user_a_123",
        "profile": {
            "personal_details": {
                "full_name": "Jane Developer",
                "email": "jane.developer@example.com",
                "phone": "+1-555-0199",
                "location": "San Francisco, CA",
            },
            "education": {
                "degree": "B.Tech Computer Science",
                "institution": "University of Technology",
                "graduation_year": "2023",
            },
            "skills_and_experience": {
                "technical_skills": "Python, TypeScript, React, PostgreSQL",
                "years_of_experience": "4",
                "primary_role": "Full Stack Engineer",
            },
            "work_preferences": {
                "authorized_to_work_in_us": True,
            },
        },
        "memories": [
            {
                "id": "mem-1",
                "memory_content": "Authorized to work in US: Yes, citizen without sponsorship requirement.",
                "category": "work_preference",
            }
        ],
        "documents": [
            {
                "id": "doc-1",
                "filename": "Jane_Resume.pdf",
                "snippet": "Jane Developer - Experienced in web automation and distributed backends.",
            }
        ],
        "formatted_prompt": "USER CONTEXT: Jane Developer...",
    }


@pytest.fixture
def test_form_elements():
    """Interactive elements on the /test-form practice registration page."""
    return [
        {
            "index": 1,
            "tag": "input",
            "type": "text",
            "id": "fullName",
            "name": "fullName",
            "placeholder": "Full Name",
            "selector": "#fullName",
            "text": "",
        },
        {
            "index": 2,
            "tag": "input",
            "type": "email",
            "id": "email",
            "name": "email",
            "placeholder": "Email Address",
            "selector": "#email",
            "text": "",
        },
        {
            "index": 3,
            "tag": "input",
            "type": "tel",
            "id": "phone",
            "name": "phone",
            "placeholder": "Phone Number",
            "selector": "#phone",
            "text": "",
        },
        {
            "index": 4,
            "tag": "input",
            "type": "text",
            "id": "location",
            "name": "location",
            "placeholder": "City, State",
            "selector": "#location",
            "text": "",
        },
        {
            "index": 5,
            "tag": "select",
            "type": "select-one",
            "id": "educationDegree",
            "name": "educationDegree",
            "selector": "#educationDegree",
            "options": [
                {"value": "", "text": "Select Degree"},
                {"value": "B.Tech Computer Science", "text": "B.Tech Computer Science"},
                {"value": "M.S. Software Engineering", "text": "M.S. Software Engineering"},
                {"value": "Ph.D", "text": "Ph.D"},
                {"value": "High School", "text": "High School"},
            ],
            "text": "Degree / Education Level",
        },
        {
            "index": 6,
            "tag": "textarea",
            "type": "textarea",
            "id": "skillsExperience",
            "name": "skillsExperience",
            "placeholder": "Technical Skills & Experience",
            "selector": "#skillsExperience",
            "text": "",
        },
        {
            "index": 7,
            "tag": "input",
            "type": "checkbox",
            "id": "workAuth",
            "name": "workAuth",
            "selector": "#workAuth",
            "text": "Authorized to work in US",
        },
        {
            "index": 8,
            "tag": "input",
            "type": "text",
            "id": "emergencyContact",
            "name": "emergencyContact",
            "placeholder": "Emergency Contact Name",
            "selector": "#emergencyContact",
            "text": "Emergency Contact (Unavailable in My Context)",
        },
        {
            "index": 9,
            "tag": "button",
            "type": "submit",
            "id": "submitBtn",
            "name": "submitBtn",
            "selector": "#submitBtn",
            "text": "Register",
        },
    ]


# ==============================================================================
# Unit Tests: synthesize_generalized_field_value
# ==============================================================================

def test_field_value_result_tuple_compatibility():
    """Verify FieldValueResult behaves identically to a 2-tuple while providing provenance."""
    res = FieldValueResult("Jane Doe", "Full Name", "My Context Profile (personal_details.full_name)")
    val, field = res
    assert val == "Jane Doe"
    assert field == "Full Name"
    assert res.val == "Jane Doe"
    assert res.field_name == "Full Name"
    assert res.source == "My Context Profile (personal_details.full_name)"


def test_synthesize_fields_from_my_context(user_a_retrieved_context, test_form_elements):
    """Verify each practice form field is correctly retrieved from My Context."""
    el_by_id = {el["id"]: el for el in test_form_elements}

    # 1. Full Name
    res = synthesize_generalized_field_value(el_by_id["fullName"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == "Jane Developer"
    assert "personal_details.full_name" in res.source

    # 2. Email
    res = synthesize_generalized_field_value(el_by_id["email"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == "jane.developer@example.com"
    assert "personal_details.email" in res.source

    # 3. Phone
    res = synthesize_generalized_field_value(el_by_id["phone"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == "+1-555-0199"
    assert "personal_details.phone" in res.source

    # 4. Location
    res = synthesize_generalized_field_value(el_by_id["location"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == "San Francisco, CA"
    assert "personal_details.location" in res.source

    # 5. Education Degree
    res = synthesize_generalized_field_value(el_by_id["educationDegree"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == "B.Tech Computer Science"
    assert "education.degree" in res.source

    # 6. Technical Skills
    res = synthesize_generalized_field_value(el_by_id["skillsExperience"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert "Python, TypeScript, React, PostgreSQL" in res.val
    assert "skills_and_experience.technical_skills" in res.source

    # 7. Work Authorization Checkbox
    res = synthesize_generalized_field_value(el_by_id["workAuth"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == "true"
    assert "work authorization" in res.source.lower()

    # 8. Emergency Contact (Unavailable in My Context)
    res = synthesize_generalized_field_value(el_by_id["emergencyContact"], retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == ""
    assert "Unavailable in My Context" in res.source


def test_unavailable_field_blank_when_exclusive(user_a_retrieved_context, test_form_elements):
    """Verify unavailable field is left blank when exclusive My Context mode is enabled."""
    el_by_id = {el["id"]: el for el in test_form_elements}
    res = synthesize_generalized_field_value(
        el_by_id["emergencyContact"],
        goal="Fill the practice registration form exclusively from My Context. Do not invent details.",
        retrieved_context=user_a_retrieved_context,
    )
    assert res.val == ""
    assert res.source == "Unavailable in My Context (left blank)"


def test_password_field_protected(user_a_retrieved_context):
    """Verify password inputs are never synthesized or automated even if context is present."""
    pw_el = {"tag": "input", "type": "password", "id": "accountPass", "name": "password", "placeholder": "Password"}
    res = synthesize_generalized_field_value(pw_el, retrieved_context=user_a_retrieved_context, exclusive_my_context=True)
    assert res.val == ""
    assert "Security" in res.source or "Password" in res.source


# ==============================================================================
# Form-Filling Flow & Verification Tests
# ==============================================================================

def test_heuristic_planner_form_filling_flow(user_a_retrieved_context, test_form_elements):
    """
    Simulate full execution walkthrough on /test-form:
    - Fills text, email, phone, location
    - Selects degree option
    - Fills skills textarea
    - Checks work authorization checkbox
    - Leaves emergency contact blank
    - Finishes without submitting form
    """
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    page_info = {
        "url": "http://127.0.0.1:8000/test-form",
        "title": "Practice Registration Form",
        "elements": test_form_elements,
    }
    goal = "Open http://127.0.0.1:8000/test-form and fill the registration form retrieving matching information exclusively from My Context. Leave unavailable fields blank. Do not submit."
    trajectory = []

    # Step 1: Already on form URL, planner should pick first field (fullName)
    action1 = planner._heuristic_decide_action(
        goal=goal,
        step_number=1,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action1.action == ActionType.FILL
    assert action1.selector == "#fullName"
    assert action1.value == "Jane Developer"
    assert "personal_details.full_name" in action1.reasoning
    trajectory.append({"action": "fill", "element_id": 1, "selector": "#fullName", "reasoning": action1.reasoning})

    # Step 2: email
    action2 = planner._heuristic_decide_action(
        goal=goal,
        step_number=2,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action2.action == ActionType.FILL
    assert action2.selector == "#email"
    assert action2.value == "jane.developer@example.com"
    trajectory.append({"action": "fill", "element_id": 2, "selector": "#email", "reasoning": action2.reasoning})

    # Step 3: phone
    action3 = planner._heuristic_decide_action(
        goal=goal,
        step_number=3,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action3.action == ActionType.FILL
    assert action3.selector == "#phone"
    assert action3.value == "+1-555-0199"
    trajectory.append({"action": "fill", "element_id": 3, "selector": "#phone", "reasoning": action3.reasoning})

    # Step 4: location
    action4 = planner._heuristic_decide_action(
        goal=goal,
        step_number=4,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action4.action == ActionType.FILL
    assert action4.selector == "#location"
    assert action4.value == "San Francisco, CA"
    trajectory.append({"action": "fill", "element_id": 4, "selector": "#location", "reasoning": action4.reasoning})

    # Step 5: educationDegree (<select>)
    action5 = planner._heuristic_decide_action(
        goal=goal,
        step_number=5,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action5.action == ActionType.SELECT_OPTION
    assert action5.selector == "#educationDegree"
    assert action5.select_label == "B.Tech Computer Science"
    trajectory.append({"action": "select_option", "element_id": 5, "selector": "#educationDegree", "reasoning": action5.reasoning})

    # Step 6: skillsExperience (<textarea>)
    action6 = planner._heuristic_decide_action(
        goal=goal,
        step_number=6,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action6.action == ActionType.FILL
    assert action6.selector == "#skillsExperience"
    assert "Python, TypeScript, React, PostgreSQL" in action6.value
    trajectory.append({"action": "fill", "element_id": 6, "selector": "#skillsExperience", "reasoning": action6.reasoning})

    # Step 7: workAuth (<input type="checkbox">)
    action7 = planner._heuristic_decide_action(
        goal=goal,
        step_number=7,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action7.action == ActionType.CHECK_BOX
    assert action7.selector == "#workAuth"
    assert action7.checked is True
    trajectory.append({"action": "check_box", "element_id": 7, "selector": "#workAuth", "reasoning": action7.reasoning})

    # Step 8: emergencyContact is unavailable in My Context and must be left blank.
    # All fillable fields are now filled, and the goal specifies 'Do not submit'.
    # The planner must emit ActionType.FINISH with a report of all filled and blank fields.
    action8 = planner._heuristic_decide_action(
        goal=goal,
        step_number=8,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action8.action == ActionType.FINISH
    assert "Form submission intentionally omitted" in action8.reasoning or "skipped" in action8.reasoning.lower()

    # Verify extracted data contains complete source provenance and summary
    summary = action8.extracted_data.get("form_filling_summary", {})
    filled = summary.get("filled_fields", [])
    blank = summary.get("blank_fields", [])

    assert len(filled) == 7
    filled_names = [f["field"] for f in filled]
    assert "Full Name" in filled_names
    assert "Email Address" in filled_names
    assert "Phone Number" in filled_names
    assert "City, State" in filled_names or "location" in str(filled).lower()
    assert any("Computer Science" in f["value"] for f in filled)

    # Emergency contact is correctly identified as blank
    assert len(blank) == 1
    assert "Emergency Contact" in blank[0]["field"]
    assert "Unavailable in My Context" in blank[0]["reason"]
    assert "do not submit" in summary.get("submission_status", "").lower()


def test_form_submission_prevented_when_instructed(user_a_retrieved_context, test_form_elements):
    """Verify that submit button is never clicked when user goal specifies not to submit."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    page_info = {
        "url": "http://127.0.0.1:8000/test-form",
        "title": "Practice Registration Form",
        "elements": test_form_elements,
    }
    goal = "Fill form from My Context without submitting."
    # Simulate that all 7 fillable fields are already filled
    trajectory = [
        {"action": "fill", "element_id": 1, "selector": "#fullName"},
        {"action": "fill", "element_id": 2, "selector": "#email"},
        {"action": "fill", "element_id": 3, "selector": "#phone"},
        {"action": "fill", "element_id": 4, "selector": "#location"},
        {"action": "select_option", "element_id": 5, "selector": "#educationDegree"},
        {"action": "fill", "element_id": 6, "selector": "#skillsExperience"},
        {"action": "check_box", "element_id": 7, "selector": "#workAuth"},
    ]

    action = planner._heuristic_decide_action(
        goal=goal,
        step_number=8,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory,
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    # Must finish, NEVER click the submit button or request approval to submit
    assert action.action == ActionType.FINISH
    assert action.action != ActionType.CLICK
    assert action.action != ActionType.REQUEST_APPROVAL


# ==============================================================================
# Security & Ownership Isolation Tests
# ==============================================================================

def test_user_ownership_context_isolation(user_a_retrieved_context, test_form_elements):
    """
    Verify cross-user isolation:
    User B running an exclusive My Context task with an empty or separate context
    CANNOT access or fill User A's profile information.
    """
    user_b_retrieved_context = {
        "has_context": True,
        "user_id": "user_b_999",
        "profile": {
            "personal_details": {
                "full_name": "Bob Builder",
                "email": "bob@example.com",
            }
        },
        "memories": [],
        "documents": [],
    }

    el_by_id = {el["id"]: el for el in test_form_elements}

    # User B gets Bob Builder, NOT Jane Developer
    res_b = synthesize_generalized_field_value(
        el_by_id["fullName"],
        retrieved_context=user_b_retrieved_context,
        exclusive_my_context=True,
    )
    assert res_b.val == "Bob Builder"
    assert res_b.val != "Jane Developer"

    # User B has no phone stored -> must be left blank, NOT leaked from User A
    res_b_phone = synthesize_generalized_field_value(
        el_by_id["phone"],
        retrieved_context=user_b_retrieved_context,
        exclusive_my_context=True,
    )
    assert res_b_phone.val == ""
    assert res_b_phone.val != "+1-555-0199"
    assert "Unavailable in My Context" in res_b_phone.source



def test_unknown_form_values_are_never_fabricated():
    """Unknown fields must not receive plausible-looking synthetic personal data."""
    field = {
        "tag": "input",
        "type": "text",
        "id": "department",
        "name": "department",
        "placeholder": "Enter department",
        "text": "Department",
    }
    result = synthesize_generalized_field_value(
        field,
        goal="Fill this form with my data",
        retrieved_context={},
    )
    assert result.value == ""
    assert "Needs user input" in result.source


def test_heuristic_skips_field_already_containing_requested_value(user_a_retrieved_context, test_form_elements):
    """A rerendered form must not cause the planner to refill the same populated field."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    elements = [dict(item) for item in test_form_elements]
    elements[0]["value"] = "Jane Developer"
    page_info = {
        "url": "https://docs.google.com/forms/d/e/example/viewform",
        "title": "Feedback Form",
        "elements": elements,
    }
    action = planner._heuristic_decide_action(
        goal="Fill this form with my data from My Context. Do not submit.",
        step_number=2,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[],
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action.action == ActionType.FILL
    assert action.selector == "#email"
    assert action.value == "jane.developer@example.com"


def test_heuristic_skips_checkbox_already_in_desired_state(user_a_retrieved_context, test_form_elements):
    """A checkbox already matching the desired state is not toggled again."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    elements = [dict(item) for item in test_form_elements]
    elements[0]["value"] = "Jane Developer"
    elements[1]["value"] = "jane.developer@example.com"
    elements[2]["value"] = "+1-555-0199"
    elements[3]["value"] = "San Francisco, CA"
    elements[4]["value"] = "B.Tech Computer Science"
    elements[5]["value"] = "Python, TypeScript, React, PostgreSQL"
    elements[6]["checked"] = True
    page_info = {
        "url": "http://127.0.0.1:8000/test-form",
        "title": "Practice Registration Form",
        "elements": elements,
    }
    action = planner._heuristic_decide_action(
        goal="Fill the registration form from My Context. Do not submit.",
        step_number=8,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[],
        task_context={"retrieved_context": user_a_retrieved_context},
    )
    assert action.action == ActionType.FINISH
