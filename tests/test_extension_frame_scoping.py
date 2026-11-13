import asyncio
import subprocess
import pytest
from unittest.mock import AsyncMock

from browser.extension_adapter import ExtensionBrowserAdapter


def test_child_frame_ignores_tab_level_inspect_dom():
    """Verify that in content.js, child frames (window !== window.top) never invoke

    sendResponse on inspect_dom, preventing empty child snapshots from winning the response race.
    """
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    let listener = null;
    let childWindow = {
      top: {}, // distinct object -> child iframe
      location: { href: 'https://accounts.google.com/gsi/iframe' },
      addEventListener: () => {},
      document: { querySelectorAll: () => [], getElementById: () => null }
    };
    childWindow.self = childWindow;

    let sendResponseCalled = false;
    let mockChrome = {
      runtime: { onMessage: { addListener: (fn) => { listener = fn; } } }
    };

    const ctx = vm.createContext({
      window: childWindow,
      location: childWindow.location,
      document: childWindow.document,
      chrome: mockChrome,
      console,
      Node: { ELEMENT_NODE: 1 },
    });
    vm.runInContext(contentSrc, ctx);

    listener({ type: 'inspect_dom', show_badges: false }, {}, () => {
      sendResponseCalled = true;
    });

    setTimeout(() => {
      if (sendResponseCalled) {
        process.exit(1);
      }
      process.exit(0);
    }, 50);
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Child frame called sendResponse on inspect_dom: {res.stderr}"


def test_top_frame_handles_inspect_dom_and_returns_actual_dom():
    """Verify that in content.js, top frame (window === window.top) responds to inspect_dom

    with is_top_frame: true and actual page DOM nodes.
    """
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    let listener = null;
    let topWindow = {};
    topWindow.top = topWindow; // identical object -> top frame
    topWindow.self = topWindow;
    topWindow.location = { href: 'https://www.google.com/' };
    topWindow.innerWidth = 1280;
    topWindow.innerHeight = 800;
    topWindow.devicePixelRatio = 1;
    topWindow.scrollX = 0;
    topWindow.scrollY = 0;
    topWindow.addEventListener = () => {};
    topWindow.getComputedStyle = () => ({
      visibility: 'visible',
      display: 'block',
      opacity: '1',
      position: 'static'
    });

    let searchBtn = {
      tagName: 'BUTTON',
      nodeType: 1,
      innerText: 'Google Search',
      getAttribute: (a) => (a === 'type' ? 'submit' : null),
      getBoundingClientRect: () => ({ left: 100, top: 100, width: 120, height: 40 }),
      dataset: {},
      closest: () => null,
    };

    topWindow.document = {
      title: 'Google',
      querySelectorAll: (sel) => (sel.includes('button') ? [searchBtn] : []),
      getElementById: () => null,
      body: { appendChild: () => {} },
      forms: [],
      elementFromPoint: () => searchBtn,
    };

    let mockChrome = {
      runtime: { onMessage: { addListener: (fn) => { listener = fn; } } }
    };

    const ctx = vm.createContext({
      window: topWindow,
      location: topWindow.location,
      document: topWindow.document,
      chrome: mockChrome,
      console,
      performance: { now: () => 0, getEntriesByType: () => [] },
      Node: { ELEMENT_NODE: 1 },
    });
    vm.runInContext(contentSrc, ctx);

    listener({ type: 'inspect_dom', show_badges: false }, {}, (resp) => {
      if (!resp || !resp.success || !resp.data) process.exit(2);
      if (resp.data.is_top_frame !== true) process.exit(3);
      if (resp.data.title !== 'Google') process.exit(4);
      if (!resp.data.elements || resp.data.elements.length !== 1) process.exit(5);
      process.exit(0);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Top frame inspect_dom failed with code {res.returncode}: {res.stderr}"


def test_ping_distinguishes_top_frame_from_child_frame():
    """Verify that ping responds with is_top_frame: false in child iframes and

    is_top_frame: true in the top frame.
    """
    node_script = """
    const fs = require('fs');
    const contentSrc = fs.readFileSync('chrome_extension/content.js', 'utf8');
    const vm = require('vm');

    // 1. Child
    let childListener = null;
    let childWindow = {
      top: {},
      location: { href: 'https://accounts.google.com/gsi/iframe' },
      addEventListener: () => {},
      document: { querySelectorAll: () => [], getElementById: () => null }
    };
    childWindow.self = childWindow;
    let childMock = { runtime: { onMessage: { addListener: (fn) => { childListener = fn; } } } };
    const childCtx = vm.createContext({
      window: childWindow, location: childWindow.location, document: childWindow.document,
      chrome: childMock, console, Node: { ELEMENT_NODE: 1 }
    });
    vm.runInContext(contentSrc, childCtx);

    let childPing = null;
    childListener({ type: 'ping' }, {}, (r) => { childPing = r; });

    // 2. Top
    let topListener = null;
    let topWindow = {};
    topWindow.top = topWindow;
    topWindow.self = topWindow;
    topWindow.location = { href: 'https://www.google.com/' };
    topWindow.addEventListener = () => {};
    topWindow.document = { querySelectorAll: () => [], getElementById: () => null };
    let topMock = { runtime: { onMessage: { addListener: (fn) => { topListener = fn; } } } };
    const topCtx = vm.createContext({
      window: topWindow, location: topWindow.location, document: topWindow.document,
      chrome: topMock, console, Node: { ELEMENT_NODE: 1 }
    });
    vm.runInContext(contentSrc, topCtx);

    let topPing = null;
    topListener({ type: 'ping' }, {}, (r) => { topPing = r; });

    setTimeout(() => {
      if (childPing?.is_top_frame !== false) process.exit(1);
      if (topPing?.is_top_frame !== true) process.exit(2);
      process.exit(0);
    }, 50);
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Ping frame distinction failed with code {res.returncode}: {res.stderr}"


@pytest.mark.asyncio
async def test_backend_extension_adapter_processes_top_frame_dom(tmp_path):
    """Verify that ExtensionBrowserAdapter successfully parses a top-frame snapshot

    containing Google elements, title, and diagnostic attributes.
    """
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(return_value={
        "success": True,
        "data": {
            "url": "https://www.google.com/",
            "browser_tab_url": "https://www.google.com/",
            "frame_url": "https://www.google.com/",
            "is_top_frame": True,
            "title": "Google",
            "elements": [
                {"index": 1, "text": "Google Search", "tag": "button", "type": "submit", "selector": "button[name='btnK']"},
                {"index": 2, "text": "I'm Feeling Lucky", "tag": "button", "type": "submit", "selector": "button[name='btnI']"},
            ],
            "page_text": "About Store Gmail Images Sign in Google Search",
            "headings": ["Google"],
            "form_fields": [
                {"field_type": "text", "name": "q", "selector": "input[name='q']", "label": "Search"}
            ],
        },
    })

    adapter = ExtensionBrowserAdapter(
        task_id="task-top-frame-test", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result.get("inspection_error") is not True
    assert result["title"] == "Google"
    assert len(result["elements"]) == 2
    assert result["elements"][0]["text"] == "Google Search"
    assert result["url"] == "https://www.google.com/"
    assert manager.send_command.await_count == 1


@pytest.mark.asyncio
async def test_child_frame_empty_snapshot_rejected_and_retried_until_top_frame_responds(tmp_path, monkeypatch):
    """Verify that if an empty child frame snapshot is received on attempt 1,

    the adapter rejects it and recovers on attempt 2 when the top frame snapshot arrives.
    """
    manager = type("Manager", (), {})()
    manager.send_command = AsyncMock(side_effect=[
        # Attempt 1: empty child frame snapshot
        {"success": True, "data": {
            "url": "https://www.google.com/",
            "browser_tab_url": "https://www.google.com/",
            "frame_url": "https://accounts.google.com/gsi/iframe",
            "is_top_frame": False,
            "title": "",
            "elements": [],
            "page_text": "",
            "headings": [],
            "form_fields": [],
        }},
        # Attempt 2: top frame snapshot arrives
        {"success": True, "data": {
            "url": "https://www.google.com/",
            "browser_tab_url": "https://www.google.com/",
            "frame_url": "https://www.google.com/",
            "is_top_frame": True,
            "title": "Google",
            "elements": [{"index": 1, "text": "Google Search", "tag": "button", "selector": "button"}],
            "page_text": "Google",
            "headings": ["Google"],
            "form_fields": [],
        }},
    ])
    monkeypatch.setattr("browser.extension_adapter.asyncio.sleep", AsyncMock())

    adapter = ExtensionBrowserAdapter(
        task_id="task-recovery-test", manager=manager, screenshots_dir=str(tmp_path)
    )
    result = await adapter.parse_interactive_elements()

    assert result.get("inspection_error") is not True
    assert result["title"] == "Google"
    assert len(result["elements"]) == 1
    assert manager.send_command.await_count == 2

