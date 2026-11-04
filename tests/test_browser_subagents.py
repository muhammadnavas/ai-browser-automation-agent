import pytest
from types import SimpleNamespace
from agents.subagents import SubagentContext
from agents import browser_subagents as workers
from models.schemas import ActionType

class FakeAdapter:
    def __init__(self, **kwargs): self.task_id=kwargs["task_id"]; self.is_running=False; self.actions=[]
    async def start(self, url): self.url=url; self.is_running=True
    async def parse_interactive_elements(self):
        return {"url":self.url,"title":"Test page","headings":["Heading"],"page_text":"Welcome to the test page","elements":[{"text":"Docs","href":"https://example.com/docs"}],"form_fields":[SimpleNamespace(field_type=SimpleNamespace(value="text"),selector="#name",name="name",field_id="name",label="Name",aria_label=None,visible=True,disabled=False,readonly=False)]}
    async def execute_action(self, action, task_form_fields=None): self.actions.append(action.action); return ("action completed",None)
    async def stop(self): self.is_running=False

def context(role="navigation", payload=None):
    return SubagentContext(parent_task_id="parent",subagent_id="child-"+role,role=role,goal="test",input=payload or {"url":"https://example.com"})

@pytest.fixture
def fake_browser(monkeypatch):
    monkeypatch.setattr(workers,"extension_manager",SimpleNamespace(is_connected=True))
    monkeypatch.setattr(workers,"validate_url_security",lambda url,resolve_dns=True:(True,"",url))
    monkeypatch.setattr(workers,"ExtensionBrowserAdapter",FakeAdapter)

@pytest.mark.asyncio
async def test_navigation_worker_returns_live_observation(fake_browser):
    result=await workers.navigation_worker(context())
    assert result["url"]=="https://example.com" and result["title"]=="Test page"

@pytest.mark.asyncio
async def test_research_worker_returns_bounded_evidence(fake_browser):
    result=await workers.research_worker(context("research"))
    assert result["page_text"]=="Welcome to the test page"
    assert result["links"][0]["url"]=="https://example.com/docs"

@pytest.mark.asyncio
async def test_form_worker_fills_only_explicit_mapping_and_never_submits(fake_browser,monkeypatch):
    instances=[]
    class TrackingAdapter(FakeAdapter):
        def __init__(self,**kwargs): super().__init__(**kwargs); instances.append(self)
    monkeypatch.setattr(workers,"ExtensionBrowserAdapter",TrackingAdapter)
    result=await workers.form_worker(context("form",{"url":"https://example.com","values":{"name":"Navas"}}))
    assert result["filled_count"]==1 and result["submitted"] is False
    assert ActionType.FILL_FIELD in instances[0].actions
    assert ActionType.FINISH not in instances[0].actions

@pytest.mark.asyncio
async def test_verification_worker_requires_expected_postconditions(fake_browser):
    result=await workers.verification_worker(context("verification",{"url":"https://example.com","expected_text":"missing text"}))
    assert result["checks"]["expected_text_found"] is False
    assert result["verified"] is False

def test_worker_registration_exposes_all_four_roles():
    workers.register_browser_subagents()
    assert {"navigation","research","form","verification"}.issubset(workers.subagent_orchestrator._workers)