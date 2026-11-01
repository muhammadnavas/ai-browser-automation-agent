import os
import pytest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from backend.agents.planner import AIAgentPlanner
from backend.models.schemas import ActionType

def test_groq_planner_init():
    planner = AIAgentPlanner(api_key="", groq_api_key="gsk_dummy_test_key")
    assert planner.client is None
    assert planner.groq_api_key == "gsk_dummy_test_key"
    assert planner.groq_model == "openai/gpt-oss-120b"

def test_groq_fallback_when_gemini_fails():
    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_dummy")
    
    # Mock Gemini client to raise 503 error
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("503 Service Unavailable")
    planner.client = mock_client

    with patch.object(planner, "_call_groq", return_value='["Step 1", "Step 2"]') as mock_groq:
        result = planner._call_llm("test prompt", response_mime_type="application/json")
        assert result == '["Step 1", "Step 2"]'
        mock_groq.assert_called_once_with("test prompt", response_mime_type="application/json")

@pytest.mark.asyncio
async def test_live_groq_task_decomposition_and_action():
    import os
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key or not groq_key.startswith("gsk_"):
        pytest.skip("GROQ_API_KEY not configured for live test")

    planner = AIAgentPlanner(api_key="", groq_api_key=groq_key)
    plan = await planner.generate_plan(
        goal="Search for python tutorials on Google",
        start_url="https://www.google.com",
        use_cache=False
    )
    assert isinstance(plan, list)
    assert len(plan) >= 2

    page_info = {
        "url": "https://www.google.com",
        "title": "Google",
        "headings": ["Google"],
        "page_text": "Search Google or type a URL",
        "elements": [
            {"index": 0, "tag": "textarea", "text": "", "aria_label": "Search", "selector": "textarea[name='q']"}
        ]
    }
    action = await planner.decide_next_action(
        goal="Search for python tutorials on Google",
        step_number=1,
        max_steps=5,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=[],
        use_cache=False
    )
    assert action is not None
    assert action.action in (ActionType.TYPE_TEXT, ActionType.FILL, ActionType.CLICK, ActionType.NAVIGATE)
