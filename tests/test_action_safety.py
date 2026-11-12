from agents.action_safety import approval_was_granted, confirmation_category


def test_delete_confirmation_requires_goal_and_target_match():
    assert confirmation_category("Open Gmail Spam and delete the selected email", "Delete forever") == "delete"


def test_opening_spam_folder_does_not_trigger_delete_confirmation():
    assert confirmation_category("Open the Gmail Spam folder", "Spam") is None


def test_unrelated_delete_button_does_not_trigger_for_read_only_goal():
    assert confirmation_category("Open the latest email", "Delete") is None


def test_send_confirmation_requires_matching_send_target():
    assert confirmation_category("Compose and send an email", "Send") == "send"


def test_approval_is_recognized_from_current_input_or_history():
    assert approval_was_granted("yes, proceed", [])
    assert approval_was_granted(None, [{"action": "request_approval", "description": "User granted approval"}])
    assert not approval_was_granted(None, [{"action": "click", "description": "Clicked Spam"}])
