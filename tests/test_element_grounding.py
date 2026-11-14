"""Regression tests for semantic browser-element grounding."""

from agents.planner import AIAgentPlanner, find_matching_element


def test_find_matching_element_uses_accessible_name():
    elements = [
        {
            "index": 1,
            "tag": "button",
            "role": "button",
            "text": "",
            "accessible_name": "Open settings",
            "visible": True,
            "disabled": False,
        },
        {
            "index": 2,
            "tag": "button",
            "role": "button",
            "text": "Help",
            "accessible_name": "Help",
            "visible": True,
            "disabled": False,
        },
    ]

    match = find_matching_element(elements, "Open settings")

    assert match is not None
    assert match["index"] == 1


def test_find_matching_element_rejects_tied_targets():
    elements = [
        {
            "index": 1,
            "tag": "button",
            "role": "button",
            "text": "Open",
            "accessible_name": "Open",
            "visible": True,
            "center_hit": True,
        },
        {
            "index": 2,
            "tag": "button",
            "role": "button",
            "text": "Open",
            "accessible_name": "Open",
            "visible": True,
            "center_hit": True,
        },
    ]

    assert find_matching_element(elements, "Open") is None


def test_compact_snapshot_preserves_grounding_metadata():
    elements = [
        {
            "index": 7,
            "tag": "button",
            "role": "button",
            "text": "Archive",
            "accessible_name": "Archive conversation",
            "in_viewport": True,
            "center_hit": True,
        }
    ]

    compacted = AIAgentPlanner.compact_interactive_elements(elements)

    assert compacted[0]["accessible_name"] == "Archive conversation"
    assert compacted[0]["in_viewport"] is True
    assert compacted[0]["center_hit"] is True
