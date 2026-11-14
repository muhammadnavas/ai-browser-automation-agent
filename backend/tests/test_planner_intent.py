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
