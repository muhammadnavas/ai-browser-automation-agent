import unittest

from agents.page_state import classify_page_state


class PageStateTests(unittest.TestCase):
    def test_blank_tab_is_browser_error(self):
        self.assertEqual(classify_page_state({"url": "about:blank"})["state"], "browser_error")

    def test_authentication_page_is_detected_from_path(self):
        result = classify_page_state({
            "url": "https://accounts.example.com/signin",
            "title": "Sign in",
            "page_text": "Enter your password",
        })
        self.assertEqual(result["state"], "authentication")

    def test_authentication_detected_from_explicit_copy(self):
        result = classify_page_state({
            "url": "https://example.com/private",
            "title": "Account",
            "page_text": "Your session has expired. Sign in to continue.",
        })
        self.assertEqual(result["state"], "authentication")

    def test_welcome_back_alone_is_not_authentication(self):
        result = classify_page_state({
            "url": "https://example.com/dashboard",
            "title": "Welcome back",
            "page_text": "Here is your account overview.",
            "elements": [{"visible": True, "center_hit": True}],
        })
        self.assertEqual(result["state"], "normal")

    def test_login_word_in_query_parameter_is_not_authentication(self):
        result = classify_page_state({
            "url": "https://example.com/search?query=login",
            "title": "Search results",
            "page_text": "Articles about login security.",
        })
        self.assertEqual(result["state"], "normal")

    def test_security_challenge_is_detected(self):
        result = classify_page_state({
            "url": "https://example.com/",
            "title": "Checking your browser",
            "page_text": "Verify you are human",
        })
        self.assertEqual(result["state"], "security_challenge")

    def test_navigation_error_requires_error_phrase_or_title(self):
        result = classify_page_state({
            "url": "https://example.com/article/404-guide",
            "title": "How HTTP 404 errors work",
            "page_text": "This article explains the 404 status code.",
        })
        self.assertEqual(result["state"], "normal")

    def test_explicit_not_found_page_is_detected(self):
        result = classify_page_state({
            "url": "https://example.com/missing",
            "title": "Page not found",
        })
        self.assertEqual(result["state"], "navigation_error")

    def test_empty_state_is_detected_from_explicit_message(self):
        result = classify_page_state({
            "url": "https://example.com/search",
            "title": "Search",
            "page_text": "No results found for your query.",
        })
        self.assertEqual(result["state"], "empty_state")

    def test_modal_dialog_is_detected(self):
        result = classify_page_state({
            "url": "https://example.com",
            "visual_context": {"active_dialogs": [{"role": "dialog"}]},
        })
        self.assertEqual(result["state"], "modal_dialog")

    def test_loading_page_without_controls_is_detected(self):
        result = classify_page_state({
            "url": "https://example.com",
            "title": "Just a moment",
        })
        self.assertEqual(result["state"], "loading")

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
