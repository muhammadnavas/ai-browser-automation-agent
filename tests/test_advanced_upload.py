import os
import pytest
from browser.os_dialog import (
    infer_requested_category,
    extract_target_folder,
    extract_target_file_name,
    is_sensitive_file,
    get_user_system_file,
    generate_clean_mock_file,
)


def test_infer_requested_category_photograph():
    cat = infer_requested_category(goal="Fill form and upload recent photograph")
    assert cat == "image"


def test_infer_requested_category_resume():
    cat = infer_requested_category(goal="Apply for software job and upload my resume")
    assert cat == "resume"


def test_infer_requested_category_spreadsheet():
    cat = infer_requested_category(goal="Upload sales metrics excel sheet to portal")
    assert cat == "spreadsheet"


def test_is_sensitive_file_blocks_secrets():
    assert is_sensitive_file("client_secret_12345.json") is True
    assert is_sensitive_file("credentials.json") is True
    assert is_sensitive_file(".env") is True
    assert is_sensitive_file("id_rsa") is True
    assert is_sensitive_file("token.txt") is True
    assert is_sensitive_file("photo.jpg") is False
    assert is_sensitive_file("report.pdf") is False


def test_extract_target_folder():
    assert extract_target_folder("travel into folder Pictures and upload photo") == "pictures"
    assert extract_target_folder("go into folder Downloads and find file") == "downloads"
    assert extract_target_folder("upload resume from my Documents folder") == "documents"


def test_extract_target_file_name():
    assert extract_target_file_name("upload profile_pic.jpg to the form") == "profile_pic.jpg"
    assert extract_target_file_name("attach sales_data.csv") == "sales_data.csv"
    assert extract_target_file_name("upload client_secret.json") is None  # Blocked sensitive


def test_get_user_system_file_never_picks_json_for_photograph():
    file_info = get_user_system_file(
        goal="Fill registration form",
        context_text="The form was not submitted because a required file upload field ('Upload Recent Photograph') failed validation",
    )
    assert file_info is not None
    assert file_info["mime_type"].startswith("image/")
    assert any(file_info["name"].lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"])


def test_generate_clean_mock_file():
    img_path = generate_clean_mock_file("image")
    assert os.path.isfile(img_path)
    assert img_path.lower().endswith(".jpg")

    pdf_path = generate_clean_mock_file("resume")
    assert os.path.isfile(pdf_path)
    assert pdf_path.lower().endswith(".pdf")


def test_domains_match_shortener_aliases():
    from agents.planner import domains_match, AIAgentPlanner
    assert domains_match("https://docs.google.com/forms/d/e/123/viewform", "https://forms.gle/xyz") is True
    assert domains_match("https://forms.gle/xyz", "https://docs.google.com/forms") is True
    assert domains_match("https://youtu.be/123", "https://youtube.com/watch?v=123") is True
    assert domains_match("https://x.com/user", "https://twitter.com/user") is True
    assert domains_match("https://google.com", "https://amazon.com") is False

    # Also test method access via class and instance
    planner = AIAgentPlanner()
    assert planner.domains_match("https://forms.gle/xyz", "https://docs.google.com/forms") is True
    assert AIAgentPlanner.domains_match("https://x.com/user", "https://twitter.com/user") is True


def test_heuristic_prevents_navigation_loop():
    from agents.planner import AIAgentPlanner
    from models.schemas import ActionType
    planner = AIAgentPlanner()

    goal = "Fill the form at https://forms.gle/GzEc34f1i7XjEY8B8"
    page_info = {
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSe2cW_7y6UavXJclS_R8PwQOeNh3hdh70ML1HPo2MTUF58Nng/viewform",
        "elements": [
            {"index": 1, "tag": "input", "type": "text", "aria_label": "Full Name", "selector": "#name"},
            {"index": 2, "tag": "input", "type": "text", "aria_label": "Branch", "selector": "#branch"},
            {"index": 3, "tag": "span", "text": "Submit", "selector": "#submit"},
        ],
        "form_questions": [{"question": "Full Name"}, {"question": "Branch"}],
    }

    # If already navigated in history, planner MUST NOT navigate again
    trajectory_history = [
        {"action": "navigate", "url": "https://forms.gle/GzEc34f1i7XjEY8B8"}
    ]

    action = planner._heuristic_decide_action(
        goal=goal,
        step_number=2,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory_history,
    )

    # Action MUST be FILL (for Full Name), NOT NAVIGATE!
    assert action.action == ActionType.FILL
    assert action.element_id == 1
    assert action.text is not None and len(action.text) > 0


@pytest.mark.asyncio
async def test_guardrail_prevents_premature_submission_with_unfilled_file_upload():
    from agents.planner import AIAgentPlanner
    from models.schemas import ActionType
    planner = AIAgentPlanner()

    goal = "Submit the completed Google Form"
    page_info = {
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSe2cW_7y6UavXJclS_R8PwQOeNh3hdh70ML1HPo2MTUF58Nng/viewform",
        "elements": [
            {"index": 1, "tag": "div", "text": "Add file", "aria_label": "Upload Recent Photograph", "selector": "#upload-btn"},
            {"index": 2, "tag": "span", "text": "Submit", "selector": "#submit"},
        ],
        "form_questions": ["Name", "Branch", "Upload Recent Photograph *"],
    }

    # History where all text fields were filled, but file upload has NOT occurred
    trajectory_history = [
        {"action": "fill", "element_id": 10, "description": "Filled Name"},
        {"action": "fill", "element_id": 11, "description": "Filled Branch"},
    ]

    action = await planner.decide_next_action(
        goal=goal,
        step_number=6,
        max_steps=10,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory_history,
        use_cache=False,
    )

    # The guardrail MUST intercept and choose to click the 'Add file' element rather than submitting!
    assert action.action == ActionType.CLICK
    assert action.element_id == 1
    assert "Add File" in action.text or "add file" in action.reasoning.lower()


