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


def test_ollama_provider_configuration_and_response():
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True
    planner.ollama_base_url = "http://localhost:11434"
    planner.ollama_model = "qwen2.5:7b"

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "message": {"content": '{"action":"extract"}'}
    }
    mock_response.raise_for_status.return_value = None

    with patch("backend.agents.planner.httpx.post", return_value=mock_response) as mock_post:
        result = planner._call_ollama("Return JSON", "application/json")

    assert result == '{"action":"extract"}'
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:11434/api/chat"
    assert kwargs["json"]["model"] == "qwen2.5:7b"
    assert kwargs["json"]["format"] == "json"
    assert kwargs["json"]["stream"] is False


def test_ollama_is_preferred_when_enabled():
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True

    with patch.object(planner, "_call_ollama", return_value='["Local plan"]') as mock_ollama:
        result = planner._call_llm("Plan this", "application/json")

    assert result == '["Local plan"]'
    assert planner.last_llm_provider == "ollama"
    mock_ollama.assert_called_once()


@pytest.mark.asyncio
async def test_adaptive_replan_uses_live_context_and_returns_remaining_milestones():
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True
    captured = {}

    def fake_llm(prompt, response_mime_type="application/json"):
        captured["prompt"] = prompt
        captured["mime_type"] = response_mime_type
        return '["Inspect the updated page", "Use the alternative search control", "Verify the results"]'

    with patch.object(planner, "_call_llm", side_effect=fake_llm):
        revised = await planner.replan_remaining_plan(
            goal="Find the latest Python release",
            current_plan=["Open search", "Search Python", "Extract release details"],
            completed_history=[{
                "step": 1,
                "action": "navigate",
                "description": "Opened search page",
                "url": "https://example.com",
            }],
            page_info={
                "url": "https://example.com/search",
                "title": "Search",
                "headings": ["Search results"],
                "elements": [{
                    "index": 2,
                    "tag": "input",
                    "aria_label": "Search query",
                    "placeholder": "Search",
                }],
            },
            recent_errors=["Search button selector was not found"],
        )

    assert revised == [
        "Inspect the updated page",
        "Use the alternative search control",
        "Verify the results",
    ]
    assert "Find the latest Python release" in captured["prompt"]
    assert "Search button selector was not found" in captured["prompt"]
    assert "https://example.com/search" in captured["prompt"]
    assert captured["mime_type"] == "application/json"


@pytest.mark.asyncio
async def test_adaptive_replan_rejects_invalid_plan_shape():
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True
    with patch.object(planner, "_call_llm", return_value='["Only one step"]'):
        with pytest.raises(ValueError, match="between 2 and 6"):
            await planner.replan_remaining_plan(
                goal="Test task",
                current_plan=["Step one"],
                completed_history=[],
                page_info={"url": "https://example.com"},
                recent_errors=["Previous action failed"],
            )



@pytest.mark.asyncio
async def test_adaptive_replan_accepts_explicit_completion_verification_milestone():
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True
    with patch.object(
        planner,
        "_call_llm",
        return_value='["Verify the requested outcome and finish"]',
    ):
        revised = await planner.replan_remaining_plan(
            goal="Find the latest Python release",
            current_plan=["Open search", "Search Python"],
            completed_history=[{"step": 1, "action": "extract", "description": "Found release details"}],
            page_info={"url": "https://www.python.org/downloads/"},
            recent_errors=["A final extraction action timed out"],
        )

    assert revised == ["Verify the requested outcome and finish"]


@pytest.mark.asyncio
async def test_adaptive_replan_accepts_milestones_object_response():
    planner = AIAgentPlanner(api_key="", groq_api_key="")
    planner.ollama_enabled = True
    with patch.object(
        planner,
        "_call_llm",
        return_value='{"milestones":["Inspect current page","Verify the result"]}',
    ):
        revised = await planner.replan_remaining_plan(
            goal="Check the result",
            current_plan=["Open page", "Inspect result"],
            completed_history=[],
            page_info={"url": "https://example.com"},
            recent_errors=["Element was not found"],
        )

    assert revised == ["Inspect current page", "Verify the result"]
