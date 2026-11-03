import unittest

from agents.page_state import classify_page_state


class PageStateTests(unittest.TestCase):
    def test_blank_tab_is_browser_error(self):
        result = classify_page_state({"url": "about:blank"})
        self.assertEqual(result["state"], "browser_error")

    def test_authentication_page_is_detected(self):
        result = classify_page_state({
            "url": "https://accounts.example.com/signin",
            "title": "Sign in",
            "page_text": "Enter your password",
        })
        self.assertEqual(result["state"], "authentication")

    def test_security_challenge_is_detected(self):
        result = classify_page_state({
            "url": "https://example.com/",
            "title": "Checking your browser",
            "page_text": "Verify you are human",
        })
        self.assertEqual(result["state"], "security_challenge")

    def test_dom_strategy_when_unobstructed_controls_exist(self):
        result = classify_page_state({
            "url": "https://example.com",
            "elements": [{"visible": True, "disabled": False, "center_hit": True}],
        })
        self.assertEqual(result["interaction_strategy"], "dom")

    def test_visual_strategy_for_canvas_pages(self):
        result = classify_page_state({
            "url": "https://example.com/game",
            "canvas_present": True,
            "elements": [],
        })
        self.assertEqual(result["interaction_strategy"], "visual")

    def test_hybrid_strategy_when_no_reliable_dom_target_exists(self):
        result = classify_page_state({
            "url": "https://example.com",
            "elements": [{"visible": True, "disabled": False, "center_hit": False}],
        })
        self.assertEqual(result["interaction_strategy"], "hybrid")


if __name__ == "__main__":
    unittest.main()
