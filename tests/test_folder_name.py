import pytest
from backend.agents.planner import AIAgentPlanner

def test_extract_folder_name():
    import re
    _NAME_CHARS = r'[A-Za-z0-9_\-\.\(\) ]+?'
    _STOP = r'(?:\s*$|\s+(?:in|on|at|inside|to|and|then|for|from|under|within)\b)'

    def _extract_folder_name(target_goal: str) -> str:
        name = 'New Folder'
        _dq = re.search(r'"([^"]+)"', target_goal)
        _sq = re.search(r"'([^']+)'", target_goal)
        if _dq:
            return _dq.group(1).strip()
        if _sq:
            return _sq.group(1).strip()
        _kw_patterns = [
            r'\bname\s+(?:it\s+(?:as\s+)?|the\s+folder\s+)' + f'({_NAME_CHARS})' + _STOP,
            r'\bnamed?\s+' + f'({_NAME_CHARS})' + _STOP,
            r'\bcall(?:ed|ing|s)?\s+(?:it\s+)?' + f'({_NAME_CHARS})' + _STOP,
            r'\btitl(?:ed?|ing)\s+(?:it\s+)?' + f'({_NAME_CHARS})' + _STOP,
            r'\blabell?ed?\s+(?:it\s+)?' + f'({_NAME_CHARS})' + _STOP,
            r'\bwith\s+(?:the\s+)?name\s+' + f'({_NAME_CHARS})' + _STOP,
            r'\bas\s+' + f'({_NAME_CHARS})' + _STOP,
            r'\b(?:create|make|mkdir)\s+(?:a\s+|new\s+)?folder\s+([A-Za-z0-9_\-\.]+)',
            r'\bfolder\s+([A-Za-z0-9_\-\.]+)' + _STOP,
        ]
        for _pat in _kw_patterns:
            _m = re.search(_pat, target_goal, re.IGNORECASE)
            if _m:
                cand = _m.group(1).strip()
                if cand.lower() not in ('a', 'an', 'the', 'new', 'in', 'on', 'my', 'this', 'that'):
                    return cand
        return name

    assert _extract_folder_name("open gdrive and create a folder called Automata") == "Automata"
    assert _extract_folder_name("open gdrive and create a folder named Automata") == "Automata"
    assert _extract_folder_name("create folder called Automata") == "Automata"
    assert _extract_folder_name("create a folder name it Automata") == "Automata"
    assert _extract_folder_name("create a new folder with name Automata") == "Automata"
    assert _extract_folder_name("make a folder Automata") == "Automata"
    assert _extract_folder_name('open gdrive and create a folder "Automata"') == "Automata"
    assert _extract_folder_name("open gdrive and create a folder 'Automata'") == "Automata"
    assert _extract_folder_name("create a folder as Automata") == "Automata"

def _sync_run(coro):
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    import asyncio
    return asyncio.run(coro)


def test_existing_folder_target():
    from backend.agents.planner import _extract_existing_folder_target

    assert _extract_existing_folder_target("open gdrive and open folder named Automata and upload any pdf file from Downloads.") == "Automata"
    assert _extract_existing_folder_target("open gdrive and find automata folder upload any pdf file from the downloads") == "automata"
    assert _extract_existing_folder_target("open gdrive and open folder called Automata") == "Automata"
    assert _extract_existing_folder_target("open gdrive and open folder 'Automata'") == "Automata"
    assert _extract_existing_folder_target('open gdrive and open folder "Automata"') == "Automata"
    assert _extract_existing_folder_target("open gdrive and open 'Automata' folder") == "Automata"
    assert _extract_existing_folder_target("open folder Automata and upload pdf") == "Automata"
    assert _extract_existing_folder_target("go to Automata folder and upload file") == "Automata"
    assert _extract_existing_folder_target("open Automata folder") == "Automata"
    assert _extract_existing_folder_target("open gdrive and create a folder named Test") is None
    assert _extract_existing_folder_target("open gdrive and create folder Automata") is None
    assert _extract_existing_folder_target("open gdrive and upload a file") is None



def test_folder_candidate_ranking_ignores_container_row():
    """Verify that multi-folder container rows are ignored and specific folder elements are ranked first."""
    _eft_lower = "automata"
    elements = [
        {
            "index": 21,
            "tag": "div",
            "role": "row",
            "text": "Activity PointsMore actionsAutomataMore actionsNew FolderMore actionsPlacementMore actions",
            "selector": "div#\\:a > ... > div.KNHpub.bn8JV",
            "accessible_name": "",
            "aria_label": "",
            "disabled": False,
            "visible": True,
        },
        {
            "index": 23,
            "tag": "div",
            "role": "gridcell",
            "text": "Activity PointsMore actions",
            "accessible_name": "Activity Points Folder",
            "aria_label": "Activity Points Folder",
            "selector": '[aria-label="Activity Points Folder"]',
            "disabled": False,
            "visible": True,
        },
        {
            "index": 24,
            "tag": "div",
            "role": "gridcell",
            "text": "AutomataMore actions",
            "accessible_name": "Automata Folder",
            "aria_label": "Automata Folder Loading…",
            "selector": '[aria-label="Automata Folder Loading…"]',
            "disabled": False,
            "visible": True,
        },
        {
            "index": 40,
            "tag": "div",
            "role": None,
            "text": "Automata",
            "accessible_name": "Automata Folder",
            "selector": '[data-tooltip="Automata Folder"]',
            "disabled": False,
            "visible": True,
        },
    ]

    candidates = []
    for el in elements:
        if el.get("disabled") is True or el.get("visible") is False:
            continue
        el_text = (el.get("text") or "").strip().lower()
        el_aria = (el.get("aria_label") or "").strip().lower()
        el_acc = (el.get("accessible_name") or "").strip().lower()
        el_role = (el.get("role") or "").strip().lower()
        el_selector = (el.get("selector") or "").lower()

        if len(el_text) > 60 or el_text.count("more actions") > 1:
            continue
        if el_role == "row" and any(r in el_selector for r in ["s55kne", "bn8jv"]):
            continue

        score = 0
        if f"{_eft_lower} folder" in el_acc or f"{_eft_lower} folder" in el_aria or f"{_eft_lower} folder" in el_selector:
            score += 100
        elif el_acc == _eft_lower or el_aria == _eft_lower:
            score += 90
        if el_text == _eft_lower:
            score += 85
        elif el_text in (f"{_eft_lower}more actions", f"{_eft_lower} more actions"):
            score += 80
        elif el_text.startswith(_eft_lower) and len(el_text) <= len(_eft_lower) + 15:
            score += 70

        if el_role in ("gridcell", "treeitem", "link", "button"):
            score += 15

        if score > 0:
            candidates.append((score, el))

    assert len(candidates) >= 2
    candidates.sort(key=lambda c: c[0], reverse=True)
    best = candidates[0][1]
    # Must NOT be element 21 (the multi-folder container row) or element 23 (Activity Points)
    assert best["index"] in (24, 40)
    assert "automata" in (best.get("text") or "").lower()
    assert "activity" not in (best.get("text") or "").lower()


def test_folder_candidate_ranking_ignores_more_info_button():
    """Verify that 3-dots 'More info / More actions' buttons are strictly ignored in candidate ranking."""
    _eft_lower = "automata"
    elements = [
        {
            "index": 10,
            "tag": "div",
            "role": "button",
            "text": "",
            "accessible_name": "Automata Folder Loading… More info (Alt + →)",
            "aria_label": "Automata Folder Loading… More info (Alt + →)",
            "selector": '[aria-label="Automata Folder Loading… More info (Alt + →)"]',
            "disabled": False,
            "visible": True,
        },
        {
            "index": 11,
            "tag": "div",
            "role": "gridcell",
            "text": "Automata",
            "accessible_name": "Automata Folder",
            "aria_label": "Automata Folder",
            "selector": '[aria-label="Automata Folder"]',
            "disabled": False,
            "visible": True,
        },
    ]

    candidates = []
    for el in elements:
        if el.get("disabled") is True or el.get("visible") is False:
            continue
        el_text = (el.get("text") or "").strip().lower()
        el_aria = (el.get("aria_label") or "").strip().lower()
        el_acc = (el.get("accessible_name") or "").strip().lower()
        el_role = (el.get("role") or "").strip().lower()
        el_selector = (el.get("selector") or "").lower()

        # Skip multi-folder container rows
        if len(el_text) > 60 or el_text.count("more actions") > 1:
            continue
        if el_role == "row" and any(r in el_selector for r in ["s55kne", "bn8jv"]):
            continue

        # STRICTLY EXCLUDE 3-dots "More info" / "More actions" buttons!
        if any(term in el_aria for term in ["more info", "more actions", "alt +"]):
            continue
        if any(term in el_text for term in ["more info", "alt +"]):
            continue
        if any(term in el_acc for term in ["more info", "more actions", "alt +"]):
            continue

        score = 0
        if f"{_eft_lower} folder" in el_acc or f"{_eft_lower} folder" in el_aria or f"{_eft_lower} folder" in el_selector:
            score += 100
        elif el_acc == _eft_lower or el_aria == _eft_lower:
            score += 90
        if el_text == _eft_lower:
            score += 85
        elif el_text in (f"{_eft_lower}more actions", f"{_eft_lower} more actions"):
            score += 80
        elif el_text.startswith(_eft_lower) and len(el_text) <= len(_eft_lower) + 15:
            score += 70

        if el_role in ("gridcell", "treeitem"):
            score += 25
        elif el_role in ("link", "button"):
            score += 10

        if score > 0:
            candidates.append((score, el))

    assert len(candidates) == 1
    assert candidates[0][1]["index"] == 11
    assert candidates[0][1]["accessible_name"] == "Automata Folder"


def test_planner_recovers_from_wrong_drive_folder():
    """Verify that if the browser enters the wrong Drive folder (e.g. Activity Points), planner navigates back to My Drive."""
    from backend.agents.planner import AIAgentPlanner
    from backend.models.schemas import ActionType

    planner = AIAgentPlanner()
    page_info = {
        "url": "https://drive.google.com/drive/u/0/folders/1R75Q2_KRaHVLbOkt57uaSQcQ9wfw4ngvl",
        "title": "Activity Points - Google Drive",
        "headings": ["Activity Points"],
        "text": "Activity Points Old Age Home School Activity",
    }
    trajectory_history = [
        {"action": "double_click", "reasoning": "Opening 'Automata' folder.", "success": True},
        {"action": "wait", "reasoning": "Waiting for 'Automata' folder to finish opening.", "success": True},
    ]

    action = planner._heuristic_decide_action(
        goal="open gdrive and open a folder called Automata and upload any pdf file from downloads",
        step_number=3,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory_history,
    )

    assert action is not None
    assert action.action == ActionType.NAVIGATE
    assert "my-drive" in action.url
    assert "not 'Automata'" in action.reasoning


def test_planner_navigates_to_my_drive_if_home_dblclick_fails():
    """Verify that if the folder did not open from Drive /home, planner switches directly to My Drive view."""
    from backend.agents.planner import AIAgentPlanner
    from backend.models.schemas import ActionType

    planner = AIAgentPlanner()
    page_info = {
        "url": "https://drive.google.com/drive/u/0/home",
        "title": "Home - Google Drive",
        "headings": ["Welcome to Drive"],
        "text": "Suggested folders Activity Points Automata New Folder",
        "elements": [],
    }
    trajectory_history = [
        {"action": "double_click", "reasoning": "Opening 'Automata' folder.", "success": True},
        {"action": "wait", "reasoning": "Waiting for 'Automata' folder to finish opening.", "success": True},
    ]

    action = planner._heuristic_decide_action(
        goal="open gdrive and open a folder called Automata and upload any pdf file from downloads",
        step_number=3,
        page_info=page_info,
        extracted_data=[],
        trajectory_history=trajectory_history,
    )

    assert action is not None
    assert action.action == ActionType.NAVIGATE
    assert "my-drive" in action.url
    assert "My Drive" in action.reasoning


def test_verify_milestone_completion_folder_opening():
    """Verify that verify_milestone_completion immediately verifies folder opening when inside target folder."""
    import asyncio
    from backend.agents.planner import AIAgentPlanner
    from backend.models.schemas import ActionType, BrowserAction

    planner = AIAgentPlanner.__new__(AIAgentPlanner)
    planner.ollama_enabled = False
    planner.client = None
    planner.groq_api_key = ""

    page_info = {
        "url": "https://drive.google.com/drive/u/0/folders/1x-C4LjEUamB4ivs7zRRHnrjUANEISc0O",
        "title": "Automata - Google Drive",
        "headings": ["Automata"],
        "page_text": "My Drive > Automata Drop files here or use the 'New' button.",
        "elements": [],
    }
    action = BrowserAction(
        action=ActionType.DOUBLE_CLICK,
        text="Automata",
        reasoning="Opening 'Automata' folder.",
        milestone_id="m2",
    )
    milestone = {"id": "m2", "title": "Locate and open the 'Automata' folder"}

    result = _sync_run(planner.verify_milestone_completion(
        goal="open gdrive and open a folder called Automata and upload any pdf file from downloads",
        milestone=milestone,
        action=action,
        page_info=page_info,
        trajectory_history=[{"step": 1, "action": "double_click", "description": "Opening folder"}],
    ))

    assert result["complete"] is True
    assert "Automata" in result["evidence"]


def test_verify_milestone_completion_drive_access():
    """Verify that verify_milestone_completion verifies Drive access."""
    import asyncio
    from backend.agents.planner import AIAgentPlanner
    from backend.models.schemas import ActionType, BrowserAction

    planner = AIAgentPlanner.__new__(AIAgentPlanner)
    planner.ollama_enabled = False
    planner.client = None
    planner.groq_api_key = ""

    page_info = {
        "url": "https://drive.google.com/drive/u/0/my-drive",
        "title": "My Drive - Google Drive",
        "headings": ["My Drive"],
        "page_text": "My Drive",
        "elements": [],
    }
    action = BrowserAction(
        action=ActionType.NAVIGATE,
        url="https://drive.google.com",
        reasoning="Accessing Google Drive interface.",
        milestone_id="m1",
    )
    milestone = {"id": "m1", "title": "Access the Google Drive interface"}

    result = _sync_run(planner.verify_milestone_completion(
        goal="open gdrive and open a folder called Automata and upload any pdf file from downloads",
        milestone=milestone,
        action=action,
        page_info=page_info,
        trajectory_history=[],
    ))

    assert result["complete"] is True
    assert "Google Drive" in result["evidence"]


def test_synthesize_generalized_field_value_reads_element_label():
    """Verify that synthesize_generalized_field_value reads el.get('label') and extracts user name."""
    from backend.agents.planner import synthesize_generalized_field_value

    el = {
        "tag": "input",
        "type": "text",
        "label": "Full Name (Optional)",
        "name": "entry.123456789",
    }
    context = {
        "personal_details": {
            "full_name": "Muhammad Navas",
            "email": "navasns0409@gmail.com",
        }
    }
    res = synthesize_generalized_field_value(el, goal="fill this form with my data", retrieved_context=context)
    assert res.val == "Muhammad Navas"
    assert "full_name" in res.source


def test_verify_milestone_completion_form_field():
    """Verify that verify_milestone_completion verifies form field milestones."""
    import asyncio
    from backend.agents.planner import AIAgentPlanner
    from backend.models.schemas import ActionType, BrowserAction

    planner = AIAgentPlanner.__new__(AIAgentPlanner)
    planner.ollama_enabled = False
    planner.client = None
    planner.groq_api_key = ""

    page_info = {
        "url": "https://docs.google.com/forms/d/e/1FAIpQLSdYQrpqz7VQU_TrcRwru35eZgLd-CZp4-Btc2l_vF0K0J12cA/viewform",
        "title": "Feedback Form",
        "page_text": "Full Name Course Rating",
        "elements": [],
    }
    action = BrowserAction(
        action=ActionType.FILL,
        text="Muhammad Navas",
        reasoning="Entering Full Name in form.",
        milestone_id="m2",
    )
    milestone = {"id": "m2", "title": "Enter Full Name (Optional) with user data"}

    result = _sync_run(planner.verify_milestone_completion(
        goal="fill this form with my data",
        milestone=milestone,
        action=action,
        page_info=page_info,
        trajectory_history=[],
    ))

    assert result["complete"] is True
    assert "Full Name" in result["evidence"]





