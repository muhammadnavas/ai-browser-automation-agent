import os
import time
import json
import httpx
import pytest
import threading
import concurrent.futures
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from backend.agents.planner import (
    AIAgentPlanner,
    GeminiCircuitBreaker,
    gemini_circuit_breaker,
    FallbackProviderError,
    GeminiProviderError,
    GroqProviderError,
    is_gemini_daily_quota_exhausted,
)
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

@pytest.mark.skipif(
    not os.getenv("RUN_LIVE_LLM_TESTS") or not (os.getenv("GROQ_API_KEY") or "").startswith("gsk_"),
    reason="Set RUN_LIVE_LLM_TESTS=1 with valid GROQ_API_KEY to run live Groq test",
)
@pytest.mark.asyncio
async def test_live_groq_task_decomposition_and_action():
    import os
    groq_key = os.getenv("GROQ_API_KEY")

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
        with pytest.raises(ValueError, match=r"(?:between 2 and 6|2 to 6 milestones)"):
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


# ---------------------------------------------------------------------------
# Comprehensive Fallback & Circuit Breaker Test Suite
# ---------------------------------------------------------------------------

def test_gemini_daily_quota_immediately_activates_groq():
    """Verify daily quota exhaustion immediately breaks Gemini loop and activates Groq."""
    cb = GeminiCircuitBreaker(cooldown_seconds=60.0)
    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test", circuit_breaker=cb)

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError(
        "429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'queries' and limit 'queries per day'"
    )
    planner.client = mock_client

    with patch.object(planner, "_call_groq", return_value='{"action": "click", "element_id": 1}') as mock_groq:
        result = planner._call_llm("Task prompt", response_mime_type="application/json")
        assert result == '{"action": "click", "element_id": 1}'
        assert planner.last_llm_provider == "groq"
        # Crucial check: only 1 Gemini attempt was made instead of looping through all 5 fallback models!
        assert mock_client.models.generate_content.call_count == 1
        # Circuit breaker must be open
        assert cb.is_open() is True
        mock_groq.assert_called_once()


def test_gemini_circuit_breaker_bypasses_subsequent_gemini_calls():
    """Verify subsequent calls bypass Gemini completely while circuit breaker is open."""
    cb = GeminiCircuitBreaker(cooldown_seconds=60.0)
    cb.trip("daily quota limit reached")
    assert cb.is_open() is True

    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test", circuit_breaker=cb)
    mock_client = MagicMock()
    planner.client = mock_client

    with patch.object(planner, "_call_groq", return_value='{"action": "scroll"}') as mock_groq:
        result = planner._call_llm("Subsequent prompt", response_mime_type="application/json")
        assert result == '{"action": "scroll"}'
        assert planner.last_llm_provider == "groq"
        # Gemini was bypassed completely: 0 calls
        assert mock_client.models.generate_content.call_count == 0
        mock_groq.assert_called_once()


def test_circuit_breaker_recovery_after_cooldown():
    """Verify circuit breaker allows probe and recovers to CLOSED after cooldown expires."""
    cb = GeminiCircuitBreaker(cooldown_seconds=0.05)
    cb.trip("daily quota reached")
    assert cb.is_open() is True

    time.sleep(0.06)
    assert cb.is_open() is False

    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test", circuit_breaker=cb)
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = '{"action": "navigate"}'
    mock_client.models.generate_content.return_value = mock_resp
    planner.client = mock_client

    with patch.object(planner, "_call_groq") as mock_groq:
        result = planner._call_llm("Recovered prompt", response_mime_type="application/json")
        assert result == '{"action": "navigate"}'
        assert planner.last_llm_provider == "gemini"
        assert cb.is_open() is False
        assert cb._tripped_at is None
        mock_groq.assert_not_called()


def test_groq_exception_propagation_not_masked():
    """Verify Groq failure is chained and not masked by the original Gemini exception."""
    cb = GeminiCircuitBreaker(cooldown_seconds=60.0)
    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test", circuit_breaker=cb)

    gemini_error = RuntimeError("429 RESOURCE_EXHAUSTED: queries per day limit")
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = gemini_error
    planner.client = mock_client

    groq_error = RuntimeError("Groq HTTP 400 Bad Request: Invalid payload")
    with patch.object(planner, "_call_groq", side_effect=groq_error):
        with pytest.raises(FallbackProviderError) as exc_info:
            planner._call_llm("Test prompt")

        raised = exc_info.value
        # Both exceptions must be preserved and accessible
        assert raised.primary_exc == gemini_error
        assert raised.fallback_exc == groq_error
        # Python exception chaining must point to groq_error
        assert raised.__cause__ == groq_error
        assert "Primary Gemini failed with" in str(raised)
        assert "Secondary Groq failed with" in str(raised)


def test_groq_rate_limit_failure_handling():
    """Verify Groq HTTP 429 fails over to subsequent supported Groq models."""
    planner = AIAgentPlanner(api_key="", groq_api_key="gsk_test")

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.text = '{"error": {"message": "Rate limit reached for model openai/gpt-oss-120b"}}'
    resp_429.raise_for_status.side_effect = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=resp_429)

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [{"message": {"content": '{"action": "click", "element_id": 9}'}}]
    }
    resp_200.raise_for_status.return_value = None

    with patch("backend.agents.planner.httpx.post", side_effect=[resp_429, resp_200]) as mock_post:
        result = planner._call_groq("Test rate limit fallback")
        assert result == '{"action": "click", "element_id": 9}'
        # Verified that 2 models were attempted in fallback sequence
        assert mock_post.call_count == 2


def test_groq_prompt_budgeting_compresses_large_dom_prompt():
    """Verify intelligent prompt compression budgets tokens without cutting goal, schema, or interactive controls."""
    planner = AIAgentPlanner(api_key="", groq_api_key="gsk_test")

    huge_page_text = "Detailed paragraph of text. " * 300  # ~8,400 chars
    large_diagnostics = "Error: Uncaught TypeError at bundle.js:42\n" * 20
    large_visual = "Element 1 occluded by Modal dialog overlay rect=(0,0,800,600)\n" * 15

    raw_prompt = f"""You are an expert AI browser automation assistant.
USER GOAL: "Book flight from NYC to SFO"
Step: 2 / 5
URL: https://example.com/flights
Title: Flight Booking
Headings:
<UNTRUSTED_CONTENT source="page_headings">
Flights, Search Results
</UNTRUSTED_CONTENT>

FORM QUESTIONS AND COMPACT REPRESENTATION:
<UNTRUSTED_CONTENT source="page_forms">
[{{"id": "origin", "label": "From"}}]
</UNTRUSTED_CONTENT>

VISUAL LAYOUT AND OCCLUSION CONTEXT:
{large_visual}

BROWSER PAGE DIAGNOSTICS:
{large_diagnostics}

PAGE VISIBLE TEXT (labels, headings, paragraphs — use this to understand form fields):
<UNTRUSTED_CONTENT source="page_text">
{huge_page_text}
</UNTRUSTED_CONTENT>

INTERACTIVE ELEMENTS (index = element_id for click/type_text actions):
<UNTRUSTED_CONTENT source="page_elements">
[{{"index": 1, "tag": "button", "text": "Select Flight"}}]
</UNTRUSTED_CONTENT>

INSTRUCTIONS: Choose the BEST NEXT single action.
- Output ONLY valid JSON.
{{
  "action": "<action_name>",
  "element_id": <integer or null>,
  "reasoning": "<brief explanation>"
}}
"""

    compressed = planner._compress_prompt_for_groq(raw_prompt, max_input_tokens=250)
    assert len(compressed) < len(raw_prompt)
    # Core critical elements are strictly preserved
    assert 'USER GOAL: "Book flight from NYC to SFO"' in compressed
    assert 'FORM QUESTIONS AND COMPACT REPRESENTATION:' in compressed
    assert '<UNTRUSTED_CONTENT source="page_elements">' in compressed
    assert '"Select Flight"' in compressed
    assert 'INSTRUCTIONS: Choose the BEST NEXT single action.' in compressed
    assert '"action": "<action_name>"' in compressed
    # Large non-essential content was pruned
    assert "... [page text truncated to conserve token budget]" in compressed
    assert "... [diagnostics pruned for token budget]" in compressed


def test_groq_structured_json_response_parsing():
    """Verify Groq responses with code blocks or clean JSON are parsed cleanly."""
    planner = AIAgentPlanner(api_key="", groq_api_key="gsk_test")

    resp_fenced = MagicMock()
    resp_fenced.status_code = 200
    resp_fenced.json.return_value = {
        "choices": [{"message": {"content": "```json\n{\"action\": \"click\", \"element_id\": 7}\n```"}}]
    }
    resp_fenced.raise_for_status.return_value = None

    with patch("backend.agents.planner.httpx.post", return_value=resp_fenced):
        result = planner._call_groq("Return click")
        assert result == '{"action": "click", "element_id": 7}'
        parsed = json.loads(result)
        assert parsed["action"] == "click"
        assert parsed["element_id"] == 7


def test_gemini_temporary_503_retries_next_gemini_model():
    """Verify transient 503 error tries the next Gemini model and does NOT trip the daily circuit breaker."""
    cb = GeminiCircuitBreaker(cooldown_seconds=60.0)
    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test", circuit_breaker=cb)

    mock_client = MagicMock()
    mock_success_resp = MagicMock()
    mock_success_resp.text = '{"action": "wait"}'

    # First model 503, second model succeeds
    mock_client.models.generate_content.side_effect = [
        RuntimeError("503 The model is overloaded. Please try again later."),
        mock_success_resp,
    ]
    planner.client = mock_client

    with patch.object(planner, "_call_groq") as mock_groq:
        result = planner._call_llm("Prompt")
        assert result == '{"action": "wait"}'
        assert planner.last_llm_provider == "gemini"
        # Verified next Gemini model was attempted
        assert mock_client.models.generate_content.call_count == 2
        # Circuit breaker must NOT be open
        assert cb.is_open() is False
        mock_groq.assert_not_called()


def test_both_providers_failing_raises_chained_error():
    """Verify that when both providers fail, FallbackProviderError chains the failures clearly."""
    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test")

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED: queries per day quota")
    planner.client = mock_client

    groq_err = GroqProviderError("All fallback AI models failed. Last error: HTTP 500 Internal Server Error")
    with patch.object(planner, "_call_groq", side_effect=groq_err):
        with pytest.raises(FallbackProviderError) as exc_info:
            planner._call_llm("Plan something")

        err = exc_info.value
        assert "All AI providers failed." in str(err)
        assert "Primary Gemini failed with" in str(err)
        assert "Secondary Groq failed with" in str(err)
        assert err.__cause__ == groq_err


@pytest.mark.asyncio
async def test_existing_planner_decide_next_action_with_groq_fallback():
    """Verify decide_next_action completes end-to-end with Groq fallback when Gemini is quota-exhausted."""
    cb = GeminiCircuitBreaker(cooldown_seconds=60.0)
    planner = AIAgentPlanner(api_key="dummy_gemini", groq_api_key="gsk_test", circuit_breaker=cb)

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError(
        "429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'queries' and limit 'queries per day'"
    )
    planner.client = mock_client

    groq_response = json.dumps({
        "action": "click",
        "element_id": 3,
        "selector": "button#submit",
        "reasoning": "Clicking submit button via Groq fallback reasoning",
    })

    page_info = {
        "url": "https://example.com/checkout",
        "title": "Checkout",
        "elements": [
            {"index": 3, "tag": "button", "text": "Submit Order", "selector": "button#submit"}
        ],
        "page_text": "Please click submit order below.",
    }

    with patch.object(planner, "_call_groq", return_value=groq_response):
        action = await planner.decide_next_action(
            goal="Submit the order on checkout page",
            step_number=1,
            max_steps=5,
            page_info=page_info,
            extracted_data=[],
            trajectory_history=[],
            use_cache=False,
        )

        assert action is not None
        assert action.action == ActionType.CLICK
        assert action.element_id == 3
        assert planner.last_llm_provider == "groq"
        assert cb.is_open() is True


def test_half_open_circuit_breaker_permits_exactly_one_concurrent_probe():
    """Verify that multiple simultaneous requests after cooldown expiry result in exactly one Gemini probe,
    while all other concurrent requests route immediately to Groq."""
    cb = GeminiCircuitBreaker(cooldown_seconds=0.05)
    cb.trip("daily quota limit reached")
    assert cb.is_open() is True

    # Cooldown expires into HALF-OPEN
    time.sleep(0.06)

    planner = AIAgentPlanner(api_key="gemini_key", groq_api_key="gsk_key", circuit_breaker=cb)

    probe_started = threading.Event()
    probe_can_finish = threading.Event()
    gemini_call_count = 0
    groq_call_count = 0
    count_lock = threading.Lock()

    def mock_gemini(model, contents, config):
        nonlocal gemini_call_count
        with count_lock:
            gemini_call_count += 1
        # Signal that the single probe is currently active
        probe_started.set()
        # Hold execution until concurrent threads attempt to acquire
        probe_can_finish.wait(timeout=2.0)
        resp = MagicMock()
        resp.text = '{"action": "probe_success"}'
        return resp

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = mock_gemini
    planner.client = mock_client

    def mock_groq(prompt, response_mime_type="application/json"):
        nonlocal groq_call_count
        with count_lock:
            groq_call_count += 1
        return '{"action": "groq_fallback"}'

    planner._call_groq = mock_groq

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        # Submit first thread which should claim the probe slot
        f1 = executor.submit(planner._call_llm, "prompt 1")
        assert probe_started.wait(timeout=1.0) is True

        # Submit 4 concurrent threads while probe is still in flight
        other_futures = [executor.submit(planner._call_llm, f"prompt {i}") for i in range(2, 6)]

        # Wait until all 4 concurrent requests have reached Groq fallback while probe is active
        deadline = time.time() + 2.0
        while groq_call_count < 4 and time.time() < deadline:
            time.sleep(0.01)

        # Allow the probe thread to complete
        probe_can_finish.set()

        res1 = f1.result(timeout=2.0)
        other_results = [f.result(timeout=2.0) for f in other_futures]

    # Exactly ONE probe call reached Gemini
    assert gemini_call_count == 1
    assert res1 == '{"action": "probe_success"}'

    # All 4 concurrent calls while probe was active routed to Groq
    assert groq_call_count == 4
    for r in other_results:
        assert r == '{"action": "groq_fallback"}'

    # Probe succeeded: circuit breaker is now closed and not probing
    assert cb.is_open() is False
    assert cb._tripped_at is None
    assert cb.is_probing is False


def test_half_open_probe_failure_reopens_circuit_and_resets_probe_flag():
    """Verify that if the single probe to Gemini fails, the circuit breaker reopens for a new cooldown."""
    cb = GeminiCircuitBreaker(cooldown_seconds=0.05)
    cb.trip("daily quota limit reached")
    time.sleep(0.06)

    planner = AIAgentPlanner(api_key="gemini_key", groq_api_key="gsk_key", circuit_breaker=cb)

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("503 Service Unavailable on Probe")
    planner.client = mock_client

    with patch.object(planner, "_call_groq", return_value='{"action": "groq_fallback"}') as mock_groq:
        result = planner._call_llm("probe prompt")
        assert result == '{"action": "groq_fallback"}'
        # Probe flag reset and circuit reopened with new cooldown
        assert cb.is_probing is False
        assert cb.is_open() is True
        assert cb.remaining_cooldown > 0
        mock_groq.assert_called_once()


