import unittest

from agents.planner import AIAgentPlanner, normalize_action_target


class PlannerIntentTests(unittest.TestCase):
    def setUp(self):
        self.planner = AIAgentPlanner.__new__(AIAgentPlanner)

    def test_gmail_compose_and_send_is_email_workflow(self):
        intent = self.planner._analyze_task_intent(
            "open gmail and write a sample gmail and send it to myself"
        )
        self.assertEqual(intent["category_key"], "email_composition")
        self.assertTrue(intent["requires_human_approval"])

    def test_plain_gmail_navigation_is_not_email_composition(self):
        intent = self.planner._analyze_task_intent("open gmail")
        self.assertEqual(intent["category_key"], "navigation")

    def test_explicit_extraction_is_search_extraction(self):
        intent = self.planner._analyze_task_intent("extract the latest headlines from a news website")
        self.assertEqual(intent["category_key"], "search_extraction")

    def test_numeric_selector_is_normalized_to_element_id(self):
        self.assertEqual(
            normalize_action_target({"selector": "10"}),
            (10, None),
        )

    def test_real_css_selector_is_preserved(self):
        self.assertEqual(
            normalize_action_target({"selector": 'input[name="to"]'}),
            (None, 'input[name="to"]'),
        )

    def test_explicit_element_id_takes_precedence(self):
        self.assertEqual(
            normalize_action_target({"element_id": 7, "selector": "10"}),
            (7, "10"),
        )


if __name__ == "__main__":
    unittest.main()


class ActionRegistryTests(unittest.TestCase):
    def test_catalog_contains_supported_browser_actions(self):
        from agents.action_registry import get_action_catalog_prompt
        catalog = get_action_catalog_prompt()
        self.assertIn("- click:", catalog)
        self.assertIn("- navigate:", catalog)
        self.assertIn("- finish:", catalog)

    def test_validates_required_navigation_arguments(self):
        from agents.action_registry import validate_action_payload
        self.assertFalse(validate_action_payload("navigate", {"action": "navigate"})[0])
        self.assertTrue(validate_action_payload("navigate", {"action": "navigate", "url": "https://example.com"})[0])

    def test_rejects_unknown_and_internal_actions(self):
        from agents.action_registry import validate_action_payload
        self.assertFalse(validate_action_payload("run_shell", {})[0])
        self.assertFalse(validate_action_payload("plan_cache", {})[0])

    def test_rejects_target_action_without_target(self):
        from agents.action_registry import validate_action_payload
        self.assertFalse(validate_action_payload("click", {"action": "click"})[0])
        self.assertTrue(validate_action_payload("click", {"action": "click", "element_id": 3})[0])

    def test_validates_drag_drop_pair(self):
        from agents.action_registry import validate_action_payload
        self.assertFalse(validate_action_payload("drag_drop", {"action": "drag_drop", "source_selector": "#a"})[0])
        self.assertTrue(validate_action_payload("drag_drop", {"action": "drag_drop", "source_selector": "#a", "target_selector": "#b"})[0])


class PlanFormatTests(unittest.TestCase):
    def test_accepts_canonical_json_array(self):
        from agents.plan_format import normalize_plan_response
        plan = normalize_plan_response(
            '["Open Gmail", "Inspect the inbox", "Open the requested message", "Verify the message"]'
        )
        self.assertEqual(len(plan), 4)
        self.assertEqual(plan[0], "Open Gmail")

    def test_accepts_documented_object_envelope_and_milestone_objects(self):
        from agents.plan_format import normalize_plan_response
        plan = normalize_plan_response(
            '{"plan":[{"title":"Open Drive"},{"description":"Inspect folders"},{"milestone":"Open target folder"},{"step":"Verify folder"}]}'
        )
        self.assertEqual(plan, ["Open Drive", "Inspect folders", "Open target folder", "Verify folder"])

    def test_strips_numbering_and_deduplicates(self):
        from agents.plan_format import normalize_plan_response
        plan = normalize_plan_response(
            '["1. Open Gmail", "Inspect inbox", "Open message", "Open Gmail", "Verify result"]'
        )
        self.assertEqual(plan, ["Open Gmail", "Inspect inbox", "Open message", "Verify result"])

    def test_rejects_invalid_json_and_too_few_milestones(self):
        from agents.plan_format import normalize_plan_response
        with self.assertRaises(ValueError):
            normalize_plan_response("not json")
        with self.assertRaises(ValueError):
            normalize_plan_response('["Open Gmail", "Verify result"]')

    def test_allows_only_explicit_completion_marker_during_replan(self):
        from agents.plan_format import normalize_plan_response
        self.assertEqual(
            normalize_plan_response(
                '["Verify the requested outcome and finish"]',
                allow_completion_marker=True,
            ),
            ["Verify the requested outcome and finish"],
        )
        with self.assertRaises(ValueError):
            normalize_plan_response('["Do something"]', allow_completion_marker=True)

    def test_caps_overlong_milestone_text(self):
        from agents.plan_format import normalize_plan_response, MAX_WORDS_PER_MILESTONE
        plan = normalize_plan_response(
            '["one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen", "Inspect the page", "Complete the task", "Verify the result"]'
        )
        self.assertEqual(len(plan[0].split()), MAX_WORDS_PER_MILESTONE)

    def test_adaptive_plan_can_contain_only_remaining_milestones(self):
        from agents.plan_format import normalize_plan_response
        self.assertEqual(
            normalize_plan_response(
                '["Open the target folder", "Verify the requested folder"]',
                min_milestones=1,
            ),
            ["Open the target folder", "Verify the requested folder"],
        )


class MilestoneVerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = AIAgentPlanner.__new__(AIAgentPlanner)
        self.planner.ollama_enabled = True
        self.planner.client = None
        self.planner.groq_api_key = ""

    async def test_verifier_accepts_completion_only_with_evidence(self):
        from models.schemas import BrowserAction, ActionType
        self.planner._call_llm = lambda prompt, mime: (
            '{"complete":true,"evidence":"The target folder title is visible."}'
        )
        result = await self.planner.verify_milestone_completion(
            goal="Open the Reports folder",
            milestone={"id": "m2", "title": "Open the Reports folder"},
            action=BrowserAction(action=ActionType.CLICK, milestone_id="m2"),
            page_info={"url": "https://drive.google.com", "title": "Reports", "page_text": "Reports"},
            trajectory_history=[],
        )
        self.assertTrue(result["complete"])
        self.assertIn("Reports", result["evidence"])

    async def test_verifier_rejects_completion_without_evidence(self):
        from models.schemas import BrowserAction, ActionType
        self.planner._call_llm = lambda prompt, mime: '{"complete":true,"evidence":""}'
        result = await self.planner.verify_milestone_completion(
            goal="Delete the selected email",
            milestone={"id": "m3", "title": "Verify the email was deleted"},
            action=BrowserAction(action=ActionType.CLICK, milestone_id="m3"),
            page_info={"url": "https://mail.google.com", "title": "Inbox", "page_text": "Inbox"},
            trajectory_history=[],
        )
        self.assertFalse(result["complete"])

    async def test_verifier_fails_closed_on_invalid_response(self):
        from models.schemas import BrowserAction, ActionType
        self.planner._call_llm = lambda prompt, mime: "not json"
        result = await self.planner.verify_milestone_completion(
            goal="Open Gmail",
            milestone={"id": "m1", "title": "Open Gmail"},
            action=BrowserAction(action=ActionType.NAVIGATE, milestone_id="m1"),
            page_info={"url": "about:blank"},
            trajectory_history=[],
        )
        self.assertFalse(result["complete"])


class LivePagePlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_url_starts_with_short_discovery_plan(self):
        planner = AIAgentPlanner(api_key="", groq_api_key="")
        planner.plan_cache = None
        planner.cache_service = None
        plan = await planner.generate_plan(
            goal="https://forms.gle/example fill this form with my data",
            use_cache=False,
        )
        self.assertEqual(len(plan), 3)
        self.assertIn("Inspect the live page", plan[1])
        self.assertTrue(planner.last_plan_requires_inspection)

    async def test_observed_form_plan_uses_only_inspected_structure(self):
        planner = AIAgentPlanner.__new__(AIAgentPlanner)
        planner.ollama_enabled = False
        planner.client = None
        planner.groq_api_key = ""
        plan = await planner.generate_observed_plan(
            "Fill this form with my data",
            {
                "title": "Course feedback",
                "form_fields": [
                    {"label": "Full name", "type": "text", "required": False},
                    {"label": "Course", "type": "select", "required": True},
                ],
            },
        )
        self.assertTrue(any("2 observed form fields" in item for item in plan))
        self.assertTrue(any("verified user data" in item for item in plan))
        self.assertFalse(any("rating" in item.lower() for item in plan))
