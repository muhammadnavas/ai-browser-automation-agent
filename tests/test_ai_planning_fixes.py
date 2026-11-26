"""
tests/test_ai_planning_fixes.py
-------------------------------
Regression tests for:
1. Fix 1: Gemini model configuration and fallback chain.
2. Fix 2: Groq / LLM prompt payload compaction and interactive element filtering.
3. Fix 3: Heuristic planner compound-goal handling vs. pure navigation tasks.
"""

import os
import json
import pytest
from unittest.mock import MagicMock, patch

from backend.agents.planner import AIAgentPlanner
from backend.models.schemas import ActionType, BrowserAction


# ==============================================================================
# Fix 1: Gemini Model Configuration and Fallback Chain
# ==============================================================================

def test_default_gemini_model_configuration():
    """Verify default Gemini model is set to gemini-3.8-flash."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEMINI_MODEL", None)
        planner = AIAgentPlanner(api_key="", groq_api_key="")
        assert planner.model_name == "gemini-3.8-flash"


def test_verified_gemini_fallback_models_list():
    """Verify fallback models contain only active, validated models."""
    assert "gemini-3.5-flash-lite" in AIAgentPlanner._FALLBACK_MODELS
    assert "gemini-2.5-flash-lite" in AIAgentPlanner._FALLBACK_MODELS
    assert "gemini-3-flash-preview" in AIAgentPlanner._FALLBACK_MODELS
    assert "gemini-3.1-flash-lite-preview" in AIAgentPlanner._FALLBACK_MODELS
    # Deprecated preview models must not be in the fallback chain
    assert "gemini-2.5-flash-lite-preview-06-17" not in AIAgentPlanner._FALLBACK_MODELS
    assert "gemini-3.1-flash-lite" not in AIAgentPlanner._FALLBACK_MODELS


def test_gemini_fallback_chain_traversal_on_error():
    """Verify _call_llm walks through the fallback chain on 404/503 errors."""
    planner = AIAgentPlanner(api_key="mock_key", groq_api_key="")
    mock_client = MagicMock()

    # Primary (gemini-3.8-flash) and secondary (gemini-3.5-flash-lite) fail with 503/404, third model succeeds
    call_records = []

    def mock_generate_content(model, contents, config):
        call_records.append(model)
        if model == "gemini-3.8-flash":
            raise RuntimeError("503 Model overloaded")
        elif model == "gemini-3.5-flash-lite":
            raise RuntimeError("404 Model not found")
        else:
            resp = MagicMock()
            resp.text = '{"action": "wait", "reasoning": "Fallback model response"}'
            return resp

    mock_client.models.generate_content.side_effect = mock_generate_content
    planner.client = mock_client

    result = planner._call_llm("Return JSON action", response_mime_type="application/json")
    assert "Fallback model response" in result
    assert "gemini-3.8-flash" in call_records
    assert "gemini-3.5-flash-lite" in call_records
    assert "gemini-2.5-flash-lite" in call_records
    assert planner.last_llm_provider == "gemini"


# ==============================================================================
# Fix 2: Groq / LLM Payload Compaction
# ==============================================================================

def test_compact_interactive_elements_structure():
    """Verify compact_interactive_elements strips geometry and empty fields while preserving essential selectors."""
    verbose_element = {
        "index": 4,
        "tag": "textarea",
        "type": "textarea",
        "name": "q",
        "id": "APjFqb",
        "selector": 'textarea[name="q"]',
        "role": "combobox",
        "aria_label": "Search Google",
        "placeholder": "Type search here",
        "text": "",
        "value": "",
        "href": "",
        "checked": None,
        "disabled": False,
        "readonly": False,
        "screen_x": 482,
        "screen_y": 320,
        "rect": {"x": 482, "y": 320, "width": 584, "height": 48},
        "center_hit": True,
        "alt": "",
        "title": "",
        "accept": "",
    }

    compacted = AIAgentPlanner.compact_interactive_elements([verbose_element], limit=10)
    assert len(compacted) == 1
    c = compacted[0]

    # Essential fields preserved
    assert c["index"] == 4
    assert c["tag"] == "textarea"
    assert c["name"] == "q"
    assert c["id"] == "APjFqb"
    assert c["selector"] == 'textarea[name="q"]'
    assert c["role"] == "combobox"
    assert c["aria_label"] == "Search Google"
    assert c["placeholder"] == "Type search here"

    # Redundant geometry removed
    assert "rect" not in c
    assert "screen_x" not in c
    assert "screen_y" not in c
    assert "center_hit" not in c

    # Empty string attributes removed
    assert "text" not in c
    assert "value" not in c
    assert "href" not in c
    assert "alt" not in c
    assert "title" not in c
    assert "accept" not in c


def test_compact_interactive_elements_payload_reduction():
    """Verify compacting 40 verbose DOM elements reduces payload size by at least 50%."""
    elements = [
        {
            "index": i,
            "tag": "input",
            "type": "text",
            "name": f"input_field_{i}",
            "id": f"field_id_{i}",
            "selector": f"input#field_id_{i}",
            "role": "textbox",
            "aria_label": f"Input Field {i}",
            "placeholder": f"Enter value for {i}",
            "text": "",
            "value": "",
            "href": "",
            "screen_x": 100 + i,
            "screen_y": 200 + i,
            "rect": {"x": 100, "y": 200, "width": 400, "height": 35},
            "center_hit": True,
            "alt": "",
            "title": "",
            "accept": "",
        }
        for i in range(40)
    ]

    original_json = json.dumps(elements, indent=1)
    compacted = AIAgentPlanner.compact_interactive_elements(elements, limit=40)
    compacted_json = json.dumps(compacted, separators=(",", ":"))

    reduction = (len(original_json) - len(compacted_json)) / len(original_json) * 100
    assert reduction >= 50.0, f"Expected at least 50% reduction, got {reduction:.1f}%"
    assert len(compacted) == 40


# ==============================================================================
# Fix 3: Heuristic Planner Compound-Goal Handling
# ==============================================================================

@pytest.mark.parametrize(
    "goal,expected_category,expected_query,has_search",
    [
        (
            "Open Google and search for technology news",
            "search_extraction",
            "technology news",
            True,
        ),
        (
            "Open google.com and search for weather in Delhi",
            "search_extraction",
            "weather in Delhi",
            True,
        ),
        (
            "Go to google.com and find Python tutorials",
            "search_extraction",
            "Python tutorials",
            True,
        ),
        (
            "Search for weather in Delhi on google.com",
            "search_extraction",
            "weather in Delhi",
            True,
        ),
        (
            "Open google.com to search for latest AI news",
            "search_extraction",
            "latest AI news",
            True,
        ),
        (
            "Open google.com",
            "navigation",
            "google.com",
            False,
        ),
        (
            "Go to https://github.com",
            "navigation",
            "https://github.com",
            False,
        ),
    ],
)
def test_task_intent_classification(goal, expected_category, expected_query, has_search):
    """Verify compound search instructions are categorized as search_extraction while pure navigation stays navigation."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    intent = planner._analyze_task_intent(goal)

    assert intent["category_key"] == expected_category
    assert intent["has_search_intent"] is has_search
    if has_search:
        assert intent["clean_query"].lower() == expected_query.lower()


def test_heuristic_compound_search_execution():
    """Verify heuristic planner executes NAVIGATE -> TYPE_TEXT -> EXTRACT for compound goal without premature FINISH."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    compound_goal = "Open Google and search for technology news"

    google_page = {
        "url": "https://www.google.com",
        "title": "Google",
        "elements": [
            {
                "index": 0,
                "tag": "textarea",
                "name": "q",
                "id": "APjFqb",
                "selector": 'textarea[name="q"]',
                "role": "combobox",
                "aria_label": "Search",
                "text": "",
                "value": "",
            }
        ],
    }

    # Step 1: Initial state -> NAVIGATE to Google
    action1 = planner._heuristic_decide_action(
        goal=compound_goal,
        step_number=1,
        page_info={"url": "about:blank", "elements": []},
        extracted_data=[],
        trajectory_history=[],
    )
    assert action1.action == ActionType.NAVIGATE
    assert "google.com" in action1.url

    # Step 2: Google is loaded -> MUST NOT FINISH PREMATURELY. MUST TYPE SEARCH QUERY.
    traj_after_nav = [
        {"action": "navigate", "url": "https://www.google.com", "step": 1}
    ]
    action2 = planner._heuristic_decide_action(
        goal=compound_goal,
        step_number=2,
        page_info=google_page,
        extracted_data=[],
        trajectory_history=traj_after_nav,
    )
    assert action2.action == ActionType.TYPE_TEXT
    assert action2.text == "technology news"
    assert action2.element_id == 0
    assert action2.press_enter is True

    # Step 3: Query typed -> EXTRACT results
    traj_after_type = [
        {"action": "navigate", "url": "https://www.google.com", "step": 1},
        {"action": "type_text", "text": "technology news", "element_id": 0, "step": 2},
    ]
    action3 = planner._heuristic_decide_action(
        goal=compound_goal,
        step_number=3,
        page_info=google_page,
        extracted_data=[],
        trajectory_history=traj_after_type,
    )
    assert action3.action == ActionType.EXTRACT


def test_heuristic_pure_navigation_finishes_correctly():
    """Verify pure navigation tasks navigate and finish once on target domain."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    nav_goal = "Open google.com"

    google_page = {
        "url": "https://www.google.com",
        "title": "Google",
        "elements": [
            {"index": 0, "tag": "textarea", "name": "q", "selector": 'textarea[name="q"]'}
        ],
    }

    # Step 1: Navigates to target
    action1 = planner._heuristic_decide_action(
        goal=nav_goal,
        step_number=1,
        page_info={"url": "about:blank", "elements": []},
        extracted_data=[],
        trajectory_history=[],
    )
    assert action1.action == ActionType.NAVIGATE

    # Step 2: Target is reached -> Finishes successfully
    traj = [{"action": "navigate", "url": "https://google.com", "step": 1}]
    action2 = planner._heuristic_decide_action(
        goal=nav_goal,
        step_number=2,
        page_info=google_page,
        extracted_data=[],
        trajectory_history=traj,
    )
    assert action2.action == ActionType.FINISH
    assert action2.extracted_data.get("status") == "Success"


def test_drive_rename_not_misclassified_or_finished_prematurely():
    """Verify compound Drive rename goal is neither pure navigation nor prematurely finished/hijacked by upload heuristic."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    goal = "open gdrive and rename Assist folder to Test"

    intent = planner._analyze_task_intent(goal)
    assert intent["category_key"] != "navigation"

    drive_home_page = {
        "url": "https://drive.google.com/drive/u/0/home",
        "title": "Home - Google Drive",
        "elements": [
            {"index": 1, "tag": "input", "type": "text", "name": "q", "aria_label": "Get answers from Drive", "selector": 'input[name="q"]'},
            {"index": 12, "tag": "button", "text": "New", "aria_label": "New", "selector": "button.new"},
            {"index": 23, "tag": "div", "role": "gridcell", "text": "AssistMore actions", "aria_label": "Assist Folder", "selector": '[aria-label="Assist Folder"]'},
        ],
    }

    action = planner._heuristic_decide_action(
        goal=goal,
        step_number=1,
        page_info=drive_home_page,
        extracted_data=[],
        trajectory_history=[],
    )
    assert action.action != ActionType.DOUBLE_CLICK
    assert action.action != ActionType.TYPE_TEXT
    assert not (action.action == ActionType.CLICK and (action.text or "").lower() == "new")
    assert action.extracted_data.get("status") != "Success"


@pytest.mark.asyncio
async def test_drive_rename_uses_llm_instead_of_upload_heuristic_bypass():
    """Verify decide_next_action delegates Drive rename tasks to LLM instead of bypassing to upload/create heuristic."""
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True
    planner.plan_cache = None
    planner.cache_service = None
    called = {"llm": False}

    async def fake_llm_decide(*args, **kwargs):
        called["llm"] = True
        return BrowserAction(
            action=ActionType.RIGHT_CLICK,
            element_id=23,
            text="Assist",
            reasoning="Open context menu on Assist folder to rename it.",
        )

    drive_home_page = {
        "url": "https://drive.google.com/drive/u/0/home",
        "title": "Home - Google Drive",
        "elements": [
            {"index": 23, "tag": "div", "role": "gridcell", "text": "Assist", "aria_label": "Assist Folder"},
        ],
    }

    with patch.object(planner, "_llm_decide_action", side_effect=fake_llm_decide):
        action = await planner.decide_next_action(
            goal="open gdrive and rename Assist folder to Test",
            step_number=1,
            max_steps=10,
            page_info=drive_home_page,
            extracted_data=[],
            trajectory_history=[],
            use_cache=False,
        )

    assert called["llm"] is True
    assert action.action == ActionType.RIGHT_CLICK


