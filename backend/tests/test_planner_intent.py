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
