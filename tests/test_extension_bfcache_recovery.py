"""
tests/test_extension_bfcache_recovery.py
----------------------------------------
Tests for BFCache navigation recovery and error handling:
1. Extension: Detects BFCache channel closure during action, awaits navigation, and verifies URL change.
2. Extension: Refuses to falsely report success when message channel closes without navigation.
3. Backend: Classifies BFCache port closure as navigation_or_page_state.
4. Backend: Recovery engine avoids duplicate clicks on ambiguous or verified navigation.
5. Backend: ExtensionBrowserAdapter validates URL security on post-action navigation.
"""

import asyncio
import pytest
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from backend.browser.recovery import classify_browser_error, execute_with_retry_and_fallback, RetryPolicy
from backend.browser.extension_adapter import ExtensionBrowserAdapter
from backend.models.schemas import BrowserAction, ActionType


# ==============================================================================
# 1. Extension Tests: Node.js VM Execution of background.js
# ==============================================================================

def test_extension_handles_bfcache_navigation_success():
    """Verify background.js detects BFCache closure during click, waits for navigation,

    verifies URL change, and returns success with the new destination URL.
    """
    node_script = """
    const fs = require('fs');
    let src = fs.readFileSync('chrome_extension/background.js', 'utf8');
    const vm = require('vm');

    let wsMessages = [];
    class MockWebSocket {
      constructor() { this.readyState = 1; }
      send(str) { wsMessages.push(JSON.parse(str)); }
    }
    MockWebSocket.OPEN = 1;

    let currentTabUrl = 'https://www.google.com';
    const mockChrome = {
      tabs: {
        get: async (id) => ({ id, url: currentTabUrl, status: 'complete' }),
        sendMessage: async (tabId, msg, opts) => {
          if (msg.type === 'ping') return { pong: true, is_top_frame: true };
          if (msg.type === 'execute_action') {
            // Action executes and triggers navigation
            currentTabUrl = 'https://www.google.com/search?q=technology';
            throw new Error('The page keeping the extension port is moved into back/forward cache, so the message channel is closed.');
          }
          return { success: true };
        },
        onUpdated: { addListener: () => {}, removeListener: () => {} },
        onRemoved: { addListener: () => {}, removeListener: () => {} },
      },
      runtime: {
        onMessage: { addListener: () => {}, removeListener: () => {} },
      },
      alarms: {
        onAlarm: { addListener: () => {}, removeListener: () => {} },
        create: () => {},
      },
      scripting: {
        executeScript: async () => {},
      },
      storage: {
        local: {
          get: async () => ({
            taskMappings: { 'task-1': 123 },
            extensionToken: 'test-token',
          }),
          set: async () => {},
        },
      },
    };

    const ctx = vm.createContext({
      chrome: mockChrome,
      WebSocket: MockWebSocket,
      console: console,
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
    });

    vm.runInContext(src, ctx);

    (async () => {
      await new Promise(r => setTimeout(r, 50));
      await ctx.handleBackendMessage({
        id: 'cmd-1',
        task_id: 'task-1',
        type: 'execute_action',
        action: { action: 'click', selector: 'input[name="btnK"]' },
      });

      if (wsMessages.length !== 1) {
        console.error('Expected 1 WS response, got:', wsMessages.length);
        process.exit(1);
      }
      const resp = wsMessages[0];
      if (!resp.success) {
        console.error('Expected success: true, got:', resp);
        process.exit(2);
      }
      if (!resp.data.navigated || !resp.data.url.includes('/search')) {
        console.error('Expected navigated: true and new search URL, got:', resp.data);
        process.exit(3);
      }
      process.exit(0);
    })().catch(err => {
      console.error('Execution error:', err);
      process.exit(4);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Node test failed: {res.stderr}\n{res.stdout}"


def test_extension_refuses_to_falsely_report_success_without_navigation():
    """Verify background.js does NOT report success if message channel closed

    without any verified URL change.
    """
    node_script = """
    const fs = require('fs');
    let src = fs.readFileSync('chrome_extension/background.js', 'utf8');
    const vm = require('vm');

    let wsMessages = [];
    class MockWebSocket {
      constructor() { this.readyState = 1; }
      send(str) { wsMessages.push(JSON.parse(str)); }
    }
    MockWebSocket.OPEN = 1;

    let currentTabUrl = 'https://example.com/page';
    const mockChrome = {
      tabs: {
        get: async (id) => ({ id, url: currentTabUrl, status: 'complete' }),
        sendMessage: async (tabId, msg, opts) => {
          if (msg.type === 'ping') return { pong: true, is_top_frame: true };
          if (msg.type === 'execute_action') {
            throw new Error('The page keeping the extension port is moved into back/forward cache, so the message channel is closed.');
          }
          return { success: true };
        },
        onUpdated: { addListener: () => {}, removeListener: () => {} },
        onRemoved: { addListener: () => {}, removeListener: () => {} },
      },
      runtime: {
        onMessage: { addListener: () => {}, removeListener: () => {} },
      },
      alarms: {
        onAlarm: { addListener: () => {}, removeListener: () => {} },
        create: () => {},
      },
      scripting: { executeScript: async () => {} },
      storage: {
        local: {
          get: async () => ({
            taskMappings: { 'task-1': 123 },
            extensionToken: 'test-token',
          }),
          set: async () => {},
        },
      },
    };

    const ctx = vm.createContext({
      chrome: mockChrome,
      WebSocket: MockWebSocket,
      console: console,
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
    });

    vm.runInContext(src, ctx);

    (async () => {
      await new Promise(r => setTimeout(r, 50));
      await ctx.handleBackendMessage({
        id: 'cmd-2',
        task_id: 'task-1',
        type: 'execute_action',
        action: { action: 'click', selector: '#button' },
      });

      if (wsMessages.length !== 1) {
        console.error('Expected 1 WS response, got:', wsMessages.length);
        process.exit(1);
      }
      const resp = wsMessages[0];
      if (resp.success !== false) {
        console.error('Expected success: false for unverified navigation, got:', resp);
        process.exit(2);
      }
      if (!resp.error.includes('navigation could not be verified')) {
        console.error('Expected error message about unverified navigation, got:', resp.error);
        process.exit(3);
      }
      process.exit(0);
    })().catch(err => {
      console.error('Execution error:', err);
      process.exit(4);
    });
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Node test failed: {res.stderr}\n{res.stdout}"


# ==============================================================================
# 2. Backend Error Classification Tests
# ==============================================================================

def test_classify_bfcache_error_as_navigation_or_page_state():
    """Verify BFCache and port-closure errors are classified as navigation_or_page_state."""
    err1 = "Action execution error: The page keeping the extension port is moved into back/forward cache, so the message channel is closed."
    err2 = "A listener indicated an asynchronous response by returning true, but the message channel closed before a response was received"
    err3 = "Error: extension port is moved to cache"

    assert classify_browser_error(err1) == "navigation_or_page_state"
    assert classify_browser_error(err2) == "navigation_or_page_state"
    assert classify_browser_error(err3) == "navigation_or_page_state"


# ==============================================================================
# 3. Backend Recovery and Non-Duplication Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_recovery_avoids_duplicate_click_on_unverified_port_closure():
    """Verify that when a CLICK fails with an unverified port closure,

    execute_with_retry_and_fallback halts immediately and does not retry the click.
    """
    print(">>> ENTERING TEST", flush=True)
    click_count = 0

    async def mock_primary_click():
        nonlocal click_count
        click_count += 1
        print(f">>> MOCK CLICK CALLED: {click_count}", flush=True)
        return (
            "Action execution error: Message channel closed during [click], but navigation could not be verified",
            None,
            False,
        )

    action = BrowserAction(action=ActionType.CLICK, selector="#submit-btn")
    mock_controller = MagicMock()
    mock_controller.current_page = None
    mock_controller.is_running = True

    print(">>> CALLING EXECUTE_WITH_RETRY", flush=True)
    desc, extracted, success, recovery_log = await execute_with_retry_and_fallback(
        execute_primary_fn=mock_primary_click,
        action=action,
        controller=mock_controller,
        retry_policy=RetryPolicy(max_retries=2, base_delay=0, jitter=False),
    )
    print(">>> FINISHED EXECUTE_WITH_RETRY", flush=True)

    assert success is False
    assert click_count == 1, f"Expected exactly 1 click attempt, got {click_count} (duplicate execution occurred!)"
    assert any("may have side effects" in entry for entry in recovery_log)


# ==============================================================================
# 4. Backend URL Security Validation on Navigation
# ==============================================================================

@pytest.mark.asyncio
async def test_extension_adapter_rejects_unauthorized_post_action_navigation():
    """Verify ExtensionBrowserAdapter validates the post-action destination URL

    and rejects navigation to unauthorized/private metadata addresses.
    """
    mock_mgr = MagicMock()
    adapter = ExtensionBrowserAdapter(
        task_id="test-task",
        manager=mock_mgr,
    )
    adapter.is_running = True

    # Mock manager to return success with a forbidden internal cloud metadata IP
    mock_mgr.send_command = AsyncMock(return_value={
        "success": True,
        "data": {
            "description": "Clicked link and navigated to http://169.254.169.254/latest/meta-data/",
            "url": "http://169.254.169.254/latest/meta-data/",
            "navigated": True,
        }
    })

    action = BrowserAction(action=ActionType.CLICK, selector="a.cloud-link")
    with pytest.raises(RuntimeError) as exc_info:
        await adapter.execute_action(action)

    assert "Unauthorized navigation detected" in str(exc_info.value)
    assert adapter.is_running is False


@pytest.mark.asyncio
async def test_extension_adapter_accepts_valid_post_action_navigation():
    """Verify ExtensionBrowserAdapter accepts authorized post-action navigation

    and updates its current URL.
    """
    mock_mgr = MagicMock()
    adapter = ExtensionBrowserAdapter(
        task_id="test-task",
        manager=mock_mgr,
    )
    adapter.is_running = True

    mock_mgr.send_command = AsyncMock(return_value={
        "success": True,
        "data": {
            "description": "Clicked link and navigated to https://en.wikipedia.org/wiki/Python",
            "url": "https://en.wikipedia.org/wiki/Python",
            "navigated": True,
        }
    })

    action = BrowserAction(action=ActionType.CLICK, selector="a.wiki-link")
    desc, extracted = await adapter.execute_action(action)

    assert "navigated to https://en.wikipedia.org/wiki/Python" in desc
    assert adapter.current_url == "https://en.wikipedia.org/wiki/Python"
    assert adapter.is_running is True

