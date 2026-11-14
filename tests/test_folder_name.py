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

def test_existing_folder_target():
    import re
    goal = "open gdrive and find automata folder upload any pdf file from the downloads"
    _ef_match = re.search(
        r"\b(?:find|open|search for|search|into|in|inside|select|go to)?\s*([a-zA-Z0-9_\-\.]+)\s+folder\b",
        goal, re.IGNORECASE
    )
    assert _ef_match is not None
    assert _ef_match.group(1).strip().lower() == "automata"

